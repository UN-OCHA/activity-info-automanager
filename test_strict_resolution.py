from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from activity_manager.schema_change import apply_form_schema_change
from activity_manager.schema_identity import prepare_schema_for_write
from test_add_field import schema
from test_default_comparison import with_default


class StrictResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresolved_enumerated_defaults_never_put(self):
        for default in ('Unknown', ['yesId', 'Unknown'], 123, 'Duplicated'):
            with self.subTest(default=default):
                loaded = with_default('yesId', 'enumerated')
                target = deepcopy(loaded)
                target['elements'][-1]['defaultValue'] = default
                target['elements'][-1]['typeParameters']['values'].extend([
                    {'id': 'a', 'label': 'Duplicated'}, {'id': 'b', 'label': 'Duplicated'}])
                service = SimpleNamespace(get_form_schema=AsyncMock(return_value=loaded), put_form_schema=AsyncMock())
                result = await apply_form_schema_change(service, 'form1', loaded, target, AsyncMock())
                self.assertEqual(result.status, 'read_error', result.messages)
                self.assertIn('Enumerated default', result.messages[0])
                service.put_form_schema.assert_not_awaited()

    async def test_missing_reference_label_field_or_any_unresolved_item_never_puts(self):
        for label_field, default in [(None, 'Alice'), ('name', ['people:existing', 'Unknown'])]:
            with self.subTest(default=default):
                kind = 'multiselectreference' if isinstance(default, list) else 'reference'
                loaded = with_default('people:old', kind)
                target = with_default(default, kind)
                service = SimpleNamespace(get_form_schema=AsyncMock(return_value=loaded),
                    get_form_tree=AsyncMock(return_value={'people': {'recordLabelFieldId': label_field}}),
                    query_rows=AsyncMock(return_value=[]), put_form_schema=AsyncMock())
                result = await apply_form_schema_change(service, 'form1', loaded, target, AsyncMock())
                self.assertEqual(result.status, 'read_error', result.messages)
                service.put_form_schema.assert_not_awaited()

    async def test_unresolved_formula_still_reaches_put(self):
        loaded = schema()
        target = deepcopy(loaded)
        target['elements'][0]['validationCondition'] = 'UNKNOWN == "1"'
        service = SimpleNamespace(get_form_schema=AsyncMock(side_effect=[loaded, target]), put_form_schema=AsyncMock())
        result = await apply_form_schema_change(service, 'form1', loaded, target, AsyncMock())
        self.assertEqual(result.status, 'success', result.messages)
        service.put_form_schema.assert_awaited_once()
        self.assertEqual(service.put_form_schema.await_args.args[1]['elements'][0]['validationCondition'],
                         'UNKNOWN == "1"')

    async def test_unmatched_id_shaped_form_name_is_not_silently_written(self):
        loaded = schema()
        target = with_default(None)
        target['elements'][-1]['typeParameters']['range'][0]['formId'] = 'missingformname'
        service = SimpleNamespace(get_form_schema=AsyncMock(side_effect=[loaded, ValueError('Not found')]),
            get_form_tree=AsyncMock(return_value={}), get_form_names=AsyncMock(return_value={}),
            put_form_schema=AsyncMock())
        result = await apply_form_schema_change(service, 'form1', loaded, target, AsyncMock())
        self.assertEqual(result.status, 'read_error', result.messages)
        service.put_form_schema.assert_not_awaited()

    async def test_null_and_empty_defaults_can_clear_values(self):
        for kind in ('enumerated', 'reference', 'multiselectreference'):
            for value in (None, []):
                with self.subTest(kind=kind, value=value):
                    source = with_default(value, kind)
                    service = SimpleNamespace()
                    result = await prepare_schema_for_write(service, source, {})
                    self.assertEqual(result['elements'][-1]['defaultValue'], value)
