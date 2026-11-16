import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from nicegui import ui
from nicegui.client import Client

from activity_manager.api import DEFAULT_BASE_URL, ActivityInfoService
from activity_manager.listbox import DatabaseListbox
from activity_manager.page import connection_page


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_reuses_and_closes_one_http_client(self):
        original = httpx.AsyncClient
        clients = []

        def handler(request):
            return httpx.Response(200, json=[])

        def client(**kwargs):
            instance = original(transport=httpx.MockTransport(handler), **kwargs)
            clients.append(instance)
            return instance

        with patch('activity_info_api_client.client.httpx.AsyncClient', side_effect=client) as factory:
            service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
            await service.get_databases()
            await service.get_databases()
            factory.assert_called_once()
            self.assertFalse(clients[0].is_closed)
            await service.aclose()
            self.assertTrue(clients[0].is_closed)

    async def fetch(self, status, payload, database_id=None):
        original = httpx.AsyncClient

        def handler(request):
            self.assertEqual(str(request.url), DEFAULT_BASE_URL + '/databases' + (f'/{database_id}' if database_id else ''))
            self.assertEqual(request.headers['authorization'], 'Bearer test-token')
            return httpx.Response(status, json=payload)

        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), **kwargs)

        with patch('activity_info_api_client.client.httpx.AsyncClient', side_effect=client):
            service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
            try:
                if database_id:
                    return await service.get_forms(database_id)
                return await service.get_databases()
            finally:
                await service.aclose()

    async def test_forms_include_nested_forms_and_subforms_only(self):
        tree = dict(databaseId='db', userId='user', version='1', label='Database',
                    ownerRef=None, suspended=False, role={'id': 'owner'}, roles=[],
                    storage='', publishedTemplate=False, securityCategories=[],
                    billingAccountId=1, billingPlan='', resources=[
                        {'id': 'folder', 'type': 'FOLDER', 'label': 'Folder'},
                        {'id': 'b', 'type': 'SUB_FORM', 'label': 'Visits', 'parentId': 'a'},
                        {'id': 'a', 'type': 'FORM', 'label': 'Survey', 'parentId': 'folder'},
                        {'id': 'c', 'type': 'FORM', 'label': 'alpha', 'parentId': 'folder'},
                        {'id': 'report', 'type': 'REPORT', 'label': 'Report'},
                    ])
        self.assertEqual(await self.fetch(200, tree, 'db'),
                         {'c': 'alpha (c)', 'a': 'Survey (a)', 'b': 'Visits (b)'})
        tree['resources'] = []
        self.assertEqual(await self.fetch(200, tree, 'db'), {})

    async def test_form_schema_is_returned_as_json_ready_data(self):
        payload = {
            'id': 'form-1', 'label': 'Survey', 'schemaVersion': 3,
            'databaseId': 'db', 'elements': [],
        }
        original = httpx.AsyncClient

        def handler(request):
            self.assertEqual(str(request.url), DEFAULT_BASE_URL + '/form/form-1/schema')
            return httpx.Response(200, json=payload)

        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), **kwargs)

        with patch('activity_info_api_client.client.httpx.AsyncClient', side_effect=client):
            service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
            self.assertEqual(await service.get_form_schema('form-1'), payload)
            await service.aclose()

    async def test_databases_and_empty_result(self):
        result = await self.fetch(200, [
            {'databaseId': 'z', 'label': 'Zulu'},
            {'databaseId': 'b', 'label': 'Same name'},
            {'databaseId': 'a', 'label': 'Same name'},
            {'databaseId': 'c', 'label': 'alpha'},
        ])
        self.assertEqual(result, {
            'c': 'alpha (c)',
            'a': 'Same name (a)',
            'b': 'Same name (b)',
            'z': 'Zulu (z)',
        })
        self.assertEqual(await self.fetch(200, []), {})

    async def test_errors(self):
        with self.assertRaisesRegex(ValueError, 'HTTP 403'):
            await self.fetch(403, {})
        with self.assertRaisesRegex(ValueError, 'invalid API response'):
            await self.fetch(200, [{'unexpected': 'payload'}])
        with self.assertRaisesRegex(ValueError, 'API token'):
            ActivityInfoService(DEFAULT_BASE_URL, '')


class UITests(unittest.IsolatedAsyncioTestCase):
    def test_page_and_single_select_listbox(self):
        with Client(ui.page('/')), ui.column() as page:
            connection_page()
        selectors = [element for element in page.descendants() if isinstance(element, DatabaseListbox)]
        self.assertEqual(len(selectors), 2)
        self.assertTrue(selectors[1].props['disabled'])
        selector = selectors[0]
        self.assertNotIn('multiple', selector.props)
        selector.set_options({'a': 'Alpha', 'b': 'Beta'})
        self.assertFalse(selector.props['disabled'])
        self.assertEqual([option.text for option in selector.default_slot.children],
                         ['Select a database', 'Alpha', 'Beta'])
        selector.value = 'a'
        selector.set_options({})
        self.assertIsNone(selector.value)
        self.assertTrue(selector.props['disabled'])

    async def test_selection_loads_forms_and_ignores_stale_requests(self):
        service = AsyncMock(spec=ActivityInfoService)
        service.get_databases.return_value = {'old': 'Old (old)', 'new': 'New (new)'}
        pending = asyncio.get_running_loop().create_future()

        async def fetch_forms(database_id):
            return await pending if database_id == 'old' else {'new-form': 'New (new-form)'}

        service.get_forms.side_effect = fetch_forms
        with patch('activity_manager.page.ActivityInfoService', return_value=service):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
            token = next(e for e in page.descendants() if isinstance(e, ui.input) and e.label == 'API token')
            token.value = 'test-token'
            await asyncio.sleep(0.01)
            button = next(e for e in page.descendants() if isinstance(e, ui.button) and e.text == 'Connect')
            connect = next(iter(button._event_listeners.values())).handler.__closure__[0].cell_contents
            await connect()
            databases, forms = [e for e in page.descendants() if isinstance(e, DatabaseListbox)]
            change = next(iter(databases._event_listeners.values())).handler
            old = asyncio.create_task(change(SimpleNamespace(args='old')))
            await asyncio.sleep(0)
            self.assertTrue(forms.props['disabled'])
            await change(SimpleNamespace(args='new'))
            self.assertIsNone(forms.value)
            self.assertEqual(forms.default_slot.children[1].text, 'New (new-form)')
            pending.set_result({'old-form': 'Old (old-form)'})
            await old
            self.assertEqual(forms.default_slot.children[1].text, 'New (new-form)')
            token.set_value('changed')
            await token._change_handlers[0]()
            self.assertIsNone(forms.value)
            self.assertTrue(forms.props['disabled'])
            service.aclose.assert_awaited_once()

    async def test_form_selection_loads_and_clears_pretty_schema(self):
        service = AsyncMock(spec=ActivityInfoService)
        service.get_databases.return_value = {'db': 'Database (db)'}
        service.get_forms.return_value = {}
        payload = {'id': 'form-1', 'elements': [{'id': 'field-1'}]}
        service.get_form_schema.return_value = payload
        with patch('activity_manager.page.ActivityInfoService', return_value=service):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
            token = next(e for e in page.descendants() if isinstance(e, ui.input) and e.label == 'API token')
            token.value = 'test-token'
            await asyncio.sleep(0.01)
            button = next(e for e in page.descendants() if isinstance(e, ui.button) and e.text == 'Connect')
            connect = next(iter(button._event_listeners.values())).handler.__closure__[0].cell_contents
            await connect()
            databases, forms = [e for e in page.descendants() if isinstance(e, DatabaseListbox)]
            schema = next(e for e in page.descendants() if isinstance(e, ui.code))
            form_change = next(iter(forms._event_listeners.values())).handler
            database_change = next(iter(databases._event_listeners.values())).handler
            await form_change(SimpleNamespace(args='form-1'))
            self.assertTrue(schema.visible)
            self.assertEqual(schema.content, '{\n  "id": "form-1",\n  "elements": [\n    {\n      "id": "field-1"\n    }\n  ]\n}')
            await database_change(SimpleNamespace(args='db-2'))
            self.assertFalse(schema.visible)
            self.assertEqual(schema.content, '')


if __name__ == '__main__':
    unittest.main()
