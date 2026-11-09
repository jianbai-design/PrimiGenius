"""Verify real online search handlers without application startup or Docker."""
import ast
import functools
import gzip
import logging
import io
import json
import pathlib
import re
import time
import types
import unittest
import urllib.parse
import urllib.request
from unittest import mock

from flask import Flask, jsonify, request


class RPackageSearchTests(unittest.TestCase):
    def setUp(self):
        source = pathlib.Path(__file__).parents[1] / 'app.py'
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        names = {'_r_online_package_search', 'r_search_packages',
                 '_r_repository_package_index', '_r_repository_package_search',
                 '_normalize_cran_repo_url', '_resolve_bioc_mirror_from_cran'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.assertEqual(len(names), len(nodes))
        module = types.ModuleType('isolated_search')
        module.__dict__.update(globals())
        module.app = Flask('isolated_search')
        module.R_RUNTIME = {'repos': 'auto'}
        module.BIOCONDUCTOR_VERSION = '3.23'
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<real search handlers>', 'exec'), module.__dict__)
        self.runtime, self.client = module, module.app.test_client()

    def test_name_matching_sort_deduplication_and_case_insensitive_cache(self):
        body = {'results': [{'Package': 'caper'}, {'Package': 'ape', 'Title': 'Phylogenetics'},
                            {'Package': 'ape'}, {'Package': 'other', 'Title': 'ape'}, {'Package': '../bad'}]}
        with mock.patch.object(urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(body).encode())) as fetch:
            data = self.client.get('/r/search-packages?q=APE').get_json()
            self.assertEqual(data, self.client.get('/r/search-packages?q=ape').get_json())
        fetch.assert_called_once()
        self.assertEqual(['ape', 'caper'], [p['package'] for p in data['packages']])
        self.assertEqual(4, fetch.call_args.kwargs['timeout'])
        self.assertIn('limit=30', fetch.call_args.args[0].full_url)
        self.assertEqual(32, self.runtime._r_online_package_search.cache_info().maxsize)

    def test_bounded_results_and_metadata(self):
        body = {'results': [{'Package': 'ape' + str(i), 'Title': 'x' * 500} for i in range(100)]}
        with mock.patch.object(urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(body).encode())):
            data = self.client.get('/r/search-packages?q=ape').get_json()
        self.assertEqual(30, len(data['packages']))
        self.assertTrue(all(len(p['title']) <= 240 for p in data['packages']))

    def test_empty_result_is_distinct_from_service_failure(self):
        with mock.patch.object(urllib.request, 'urlopen', return_value=io.BytesIO(b'{"results": []}')):
            data = self.client.get('/r/search-packages?q=absent').get_json()
            self.assertEqual({'status': 'success', 'packages': []}, data)
        with mock.patch.object(urllib.request, 'urlopen', side_effect=TimeoutError('offline')) as fetch:
            response = self.client.get('/r/search-packages?q=ape')
            self.assertEqual(503, response.status_code)
            self.assertEqual('error', response.get_json()['status'])
            self.assertEqual(4, fetch.call_count)

    def test_repository_fallback_and_index_reuse(self):
        archive = gzip.compress(b'Package: ape\nVersion: 5.8\n\nPackage: caper\nVersion: 1\n')
        def fetch(req, **kwargs):
            if 'r-universe.dev' in req.full_url:
                raise TimeoutError('primary service unavailable')
            return io.BytesIO(archive)
        with mock.patch.object(urllib.request, 'urlopen', side_effect=fetch) as remote:
            first = self.client.get('/r/search-packages?q=ape').get_json()
            second = self.client.get('/r/search-packages?q=cap').get_json()
        self.assertEqual(['ape', 'caper'], [p['package'] for p in first['packages']])
        self.assertEqual('caper', second['packages'][0]['package'])
        self.assertEqual(3, remote.call_count)  # Two searches, one cached index.

    def test_input_validation_never_contacts_remote_service(self):
        with mock.patch.object(urllib.request, 'urlopen') as fetch:
            for query in ('a', 'ape%3Bsystem', 'a' * 101):
                self.assertEqual(400, self.client.get('/r/search-packages?q=' + query).status_code)
            fetch.assert_not_called()

    def test_response_size_and_shape_rejected(self):
        for raw in (b'x' * (1024 * 1024 + 1), b'{"results": "invalid"}'):
            with mock.patch.object(urllib.request, 'urlopen', return_value=io.BytesIO(raw)):
                self.assertEqual(503, self.client.get('/r/search-packages?q=ape').status_code)


if __name__ == '__main__':
    unittest.main()
