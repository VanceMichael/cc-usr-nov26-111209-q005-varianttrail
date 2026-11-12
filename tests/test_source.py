import base64
import json
import threading
import unittest
import urllib.parse
import urllib.request

from varianttrail.source_server import FixtureServer
from varianttrail.transport import fetch


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def get(self, path):
        return fetch(self.base + path)

    def phase(self, value):
        self.assertEqual(self.get('/control/phase?value=' + value)[0], 200)

    def token(self, body):
        from varianttrail.parser import inspect_document
        fields = inspect_document(body)['forms'][0]['fields']
        return next(field['value'] for field in fields if field['name'] == 'token')

    def choose(self, body, field, value):
        return self.get('/select?' + urllib.parse.urlencode({'token': self.token(body), 'field': field, 'value': value}))

    def test_token_replay_and_dependency_reset(self):
        page = self.get('/product')[2]
        status, _, selected = self.choose(page, 'material', 'cotton')
        self.assertEqual(status, 200)
        self.assertEqual(self.choose(page, 'material', 'linen')[0], 409)
        selected = self.choose(selected, 'color', 'white')[2]
        selected = self.choose(selected, 'pack', 'large')[2]
        self.assertIn(b'data-stock="soldout"', selected)
        reset = self.choose(selected, 'material', 'linen')[2]
        self.assertNotIn(b'id="sku"', reset)
        self.assertEqual(self.choose(reset, 'color', 'black')[0], 422)

    def test_revision_and_write_boundary(self):
        page = self.get('/product')[2]
        self.phase('updated')
        self.assertEqual(self.choose(page, 'material', 'cotton')[0], 409)
        self.assertEqual(self.get('/purchase')[0], 403)
