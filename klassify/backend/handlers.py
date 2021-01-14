import collections
# from itertools import imap
import json
import math
from tornado.gen import Task, coroutine
from tornado.web import RequestHandler, HTTPError
from tornado.websocket import WebSocketHandler
from tornadoredis import Client
from .tokenizer import tokenize

Infinity = float('inf')


class BaseHandler(RequestHandler):
    def write_error(self, status_code, **kwargs):
        self.response({
            "error": self._reason
        })

    def get_json(self, *params):
        try:
            data = json.loads(self.request.body)
        except ValueError:
            raise HTTPError(400, reason='You should provide valid json body.')

        if params:
            return [data[param] for param in params]

        return data

    def response(self, data, status_code=None):
        if self.application.debug:
            self.set_header('Content-Type', 'application/json')
            self.set_header("Access-Control-Allow-Origin", "*")
            self.set_header("Access-Control-Allow-Credentials", "true")
            self.set_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

        if status_code:
            self.set_status(status_code)

        self.finish(json.dumps(data))

    def build_key(self, *keys):
        prefix = self.application.options.prefix

        return '{prefix}:{key}'.format(
            prefix=prefix,
            key='_'.join(keys)
        )


class TrainerHandler(BaseHandler):

    @coroutine
    def post(self):
        store = self.application.redis

        text, label = self.get_json('text', 'label')

        yield Task(
            store.sadd,
            self.build_key('labels'),
            label
        )

        counter = collections.Counter(tokenize(text))

        for key, value in counter.items():
            yield Task(
                store.hincrby,
                self.build_key('label', label),
                key,
                value
            )

        self.response({
            'success': True
        }, 201)

        store.publish('stats', 'train')


class ClassifierHandler(BaseHandler):
    tolerance = 0.0001

    @coroutine
    def post(self):
        store = self.application.redis

        (text, ) = self.get_json('text')

        words = set(tokenize(text))

        scores = collections.defaultdict(lambda: 0.)

        labels = yield Task(
            store.smembers,
            self.build_key('labels')
        )

        best = None
        best_score = -Infinity

        for label in labels:
            counts = yield Task(
                store.hvals,
                self.build_key('label', label)
            )

            total = sum(map(int, counts))

            if not total:
                continue

            for word in words:

                score = yield Task(
                    store.hget,
                    self.build_key('label', label),
                    word
                )

                scores[label] += math.log(
                    float(score or self.tolerance) / total
                )

            if scores[label] > best_score:
                best_score = scores[label]
                best = label

        self.response({
            'label': best,
            'scores': scores
        })

        Task(
            store.hincrby,
            self.build_key('stats'),
            'classification',
            1
        )

        store.publish('stats', 'classify')


class StatsHandler(BaseHandler):

    @coroutine
    def get(self):
        store = self.application.redis

        labels = yield Task(
            store.smembers,
            self.build_key('labels')
        )

        classification = yield Task(
            store.hget,
            self.build_key('stats'),
            'classification'
        )

        words = set()

        for label in labels:

            keys = yield Task(
                store.hkeys,
                self.build_key('label', label)
            )

            words |= set(keys)

        self.response({
            'labels': len(labels),
            'classifications': int(classification or 0),
            'words': len(words)
        })


class WordsHandler(BaseHandler):

    @coroutine
    def get(self, label):
        store = self.application.redis

        words = yield Task(
            store.hgetall,
            self.build_key('label', label)
        )

        self.response({
            'words': words
        })


class LabelsHandler(BaseHandler):

    @coroutine
    def get(self):
        store = self.application.redis

        labels = yield Task(
            store.smembers,
            self.build_key('labels')
        )

        self.response({
            'labels': list(labels)
        })


class InterfaceHandler(BaseHandler):
    def get(self):
        self.render("../interface/index.html",
                    options=self.application.options)


class StatsWSHandler(WebSocketHandler):
    def __init__(self, *args, **kwargs):
        super(StatsWSHandler, self).__init__(*args, **kwargs)

        self.client = Client(
            self.application.options.redis_host,
            self.application.options.redis_port,
            selected_db=self.application.options.redis_db
        )

        self.listen()

    @coroutine
    def listen(self):
        client = self.client

        client.connect()

        yield Task(
            client.subscribe,
            'stats'
        )

        self.client.listen(self.on_message)

    def on_message(self, msg):
        if msg.kind == 'message':
            self.write_message(str(msg.body))

        if msg.kind == 'disconnect':
            self.close()

    def on_close(self):
        client = self.client
        if client.subscribed:
            client.unsubscribe('stats')
            client.disconnect()

    def check_origin(self, origin):
        if self.application.debug:
            return True

        return super(StatsWSHandler, self).check_origin(origin)
