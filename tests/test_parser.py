import unittest
from pathlib import Path
from varianttrail.parser import inspect_document

class ParserTests(unittest.TestCase):
    def test_select_and_disabled(self):
        result = inspect_document(Path('varianttrail/fixtures/page.html').read_bytes())
        self.assertEqual(result['forms'][0]['action'], '/select')
        options = result['forms'][0]['selects'][0]['options']
        self.assertEqual(options[0]['label'], '棉')
        self.assertTrue(options[1]['disabled'])

    def test_hidden_and_entities(self):
        result = inspect_document(b'<form><input type="hidden" name="token" value="a&amp;b"></form>')
        self.assertEqual(result['forms'][0]['fields'][0]['value'], 'a&b')

    def test_empty_form(self):
        self.assertEqual(inspect_document(b'<form></form>')['forms'][0]['selects'], [])

    def test_invalid_encoding(self):
        with self.assertRaises(UnicodeDecodeError):
            inspect_document(b'\xff')
