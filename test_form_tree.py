import unittest
import json
from unittest.mock import patch

import httpx

from activity_manager.api import ActivityInfoService, DEFAULT_BASE_URL
from activity_manager.schema_names import substitute_record_id


class FormTreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_labels_use_generated_endpoint_and_cache(self):
        original = httpx.AsyncClient
        requests = []

        def handle(request):
            requests.append(request)
            self.assertEqual(request.url.raw_path, b'/resources/query/rows')
            self.assertEqual(request.method, 'POST')
            self.assertEqual(request.headers['authorization'], 'Bearer test-token')
            self.assertEqual(json.loads(request.content), {
                'rowSources': [{'rootFormId': 'form/id'}],
                'columns': [{'id': 'calculated_name', 'formula': 'calculated_name'}],
                'filter': '_id == "record:id"',
            })
            return httpx.Response(200, json=[{'calculated_name': 'Alice'}])

        def client(**kwargs):
            return original(transport=httpx.MockTransport(handle), **kwargs)

        with patch('activity_info_api_client.client.httpx.AsyncClient', side_effect=client):
            service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
            try:
                cache = {}
                forms = {'form/id': {'recordLabelFieldId': 'calculated_name', 'elements': [
                    {'id': 'calculated_name', 'type': 'calculated'},
                ]}}
                for _ in range(2):
                    self.assertEqual(await substitute_record_id(
                        service, 'form/id', 'record:id', forms, cache), 'Alice')
                self.assertEqual(len(requests), 1)
            finally:
                await service.aclose()

    async def test_record_errors_keep_original_id(self):
        original = httpx.AsyncClient
        for status in (401, 403, 404, 410, 500):
            with self.subTest(status=status):
                def client(**kwargs):
                    return original(transport=httpx.MockTransport(
                        lambda request: httpx.Response(status, json={'code': str(status), 'message': 'error'})), **kwargs)
                with patch('activity_info_api_client.client.httpx.AsyncClient', side_effect=client):
                    service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
                    try:
                        self.assertEqual(await substitute_record_id(
                            service, 'f', 'r', {'f': {'recordLabelFieldId': 'name'}}), 'r')
                    finally:
                        await service.aclose()

    async def fetch(self, payload, status=200):
        original = httpx.AsyncClient
        requests = []

        def handle(request):
            requests.append(request)
            self.assertEqual(str(request.url), DEFAULT_BASE_URL + '/form/root/tree')
            self.assertEqual(request.headers['authorization'], 'Bearer test-token')
            return httpx.Response(status, json=payload)

        def client(**kwargs):
            return original(transport=httpx.MockTransport(handle), **kwargs)

        with patch('activity_info_api_client.client.httpx.AsyncClient', side_effect=client):
            service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
            try:
                return await service.get_form_tree('root')
            finally:
                self.assertEqual(len(requests), 1)
                await service.aclose()

    async def test_tree_skips_inaccessible_and_deleted_forms_without_schemas(self):
        root = {'id': 'root', 'label': 'Survey', 'databaseId': 'db', 'schemaVersion': 1, 'elements': []}
        related = {'id': 'related', 'label': 'People', 'databaseId': 'db', 'schemaVersion': 1, 'elements': []}
        permissions = {'view': True, 'createRecord': False, 'editRecord': False, 'deleteRecord': False}
        result = await self.fetch({'root': 'root', 'forms': {
            'root': {'id': 'root', 'schema': root, 'schemaVersion': 1, 'permissions': permissions},
            'related': {'id': 'related', 'schema': related, 'permissions': permissions},
            'unavailable': {'id': 'unavailable', 'visible': False, 'permissions': permissions},
            'deleted': {'id': 'deleted', 'deleted': True, 'permissions': permissions},
        }})
        self.assertEqual(result, {'root': root, 'related': related})

    async def test_invalid_tree_or_missing_root(self):
        for payload in ([], {}, {'forms': []}, {'forms': {'root': {'id': 'root'}}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                await self.fetch(payload)

    async def test_authentication_and_http_errors(self):
        for status, message in ((401, 'Authentication failed'), (403, 'HTTP 403'), (500, 'HTTP 500')):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, message):
                await self.fetch({'code': str(status), 'message': 'error'}, status)
