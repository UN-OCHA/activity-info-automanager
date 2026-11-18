from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from activity_manager.commands import AddFieldCommand
from activity_manager.schema_names import resolve_form_names
from activity_manager.schema_identity import prepare_schema_for_write
from activity_manager.schema_change import apply_form_schema_change
from test.test_add_field import schema


class FormNameTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        async def names(database_id):
            return {'localid': 'beneficiaries', 'childid': 'Visits'} if database_id == 'db1' else {'remoteid': 'People'}

        async def tree(form_id):
            return {form_id: {'id': form_id, 'recordLabelFieldId': 'labelid', 'elements': [
                {'id': 'labelid', 'code': 'NAME', 'type': 'FREE_TEXT'},
            ]}}

        return SimpleNamespace(get_form_names=AsyncMock(side_effect=names),
            get_database_names=AsyncMock(return_value={'db1': 'Local', 'db2': 'Remote'}),
            get_form_tree=AsyncMock(side_effect=tree), query_rows=AsyncMock(return_value=[{'_id': 'record'}]))

    async def test_local_remote_subform_and_known_ids(self):
        source = schema()
        for code, target, kind in [('ref', 'beneficiaries', 'reference'), ('multi', 'Remote/People', 'multiselectreference'),
                                   ('child', 'Visits', 'subform')]:
            source = AddFieldCommand(code, code, field_type=kind, referenced_form=target).execute(source).schema
        before = deepcopy(source)
        service = self.service()
        result = await resolve_form_names(service, source, {})
        self.assertEqual(result['elements'][1]['typeParameters']['range'][0]['formId'], 'localid')
        self.assertEqual(result['elements'][2]['typeParameters']['range'][0]['formId'], 'remoteid')
        self.assertEqual(result['elements'][3]['typeParameters']['formId'], 'childid')
        self.assertEqual(service.get_form_names.await_count, 2)
        service.get_database_names.assert_awaited_once()
        self.assertEqual(source, before)

    async def test_ambiguity_and_missing_names_fail_before_materialization(self):
        for database_names, form_names, target in [
            ({'one': 'Remote', 'two': 'Remote'}, {}, 'Remote/People'),
            ({'one': 'Remote'}, {'first': 'People', 'second': 'People'}, 'Remote/People'),
            ({'one': 'Remote'}, {}, 'Remote/Missing'),
        ]:
            service = SimpleNamespace(get_database_names=AsyncMock(return_value=database_names),
                                      get_form_names=AsyncMock(return_value=form_names))
            preview = AddFieldCommand('ref', 'Reference', field_type='reference', referenced_form=target).execute(schema()).schema
            with self.subTest(target=target), patch('activity_manager.schema_identity.secrets.token_hex') as allocate:
                with self.assertRaises(ValueError):
                    await prepare_schema_for_write(service, preview)
                allocate.assert_not_called()

    async def test_remote_name_resolves_before_formula_and_default(self):
        service = self.service()
        preview = AddFieldCommand('ref', 'Reference', field_type='reference', referenced_form='Remote/People',
            default_value='Alice', lookup_fields=[{'label': 'Name', 'formula': 'NAME'}]).execute(schema()).schema
        target = await prepare_schema_for_write(service, preview, {})
        field = target['elements'][-1]
        self.assertEqual(field['defaultValue'], 'remoteid:record')
        self.assertEqual(field['typeParameters']['lookupConfigs'][0]['formula'], 'labelid')
        self.assertEqual(field['typeParameters']['range'], [{'formId': 'remoteid'}])
        self.assertEqual(preview['elements'][-1]['typeParameters']['range'], [{'formId': 'Remote/People'}])
        direct = await prepare_schema_for_write(self.service(), preview)
        self.assertEqual(direct['elements'][-1]['defaultValue'], 'remoteid:record')
        self.assertEqual(direct['elements'][-1]['typeParameters']['lookupConfigs'][0]['formula'], 'labelid')

    async def test_name_only_change_skips_write(self):
        service = self.service()
        loaded = AddFieldCommand('ref', 'Reference', field_type='reference', referenced_form='remoteid').execute(schema()).schema
        loaded['elements'][-1]['id'] = 'existingref'
        target = deepcopy(loaded)
        target['elements'][-1]['typeParameters']['range'][0]['formId'] = 'Remote/People'
        service.get_form_schema = AsyncMock(return_value=loaded)
        service.put_form_schema = AsyncMock()
        outcome = await apply_form_schema_change(service, 'form1', loaded, target, AsyncMock())
        self.assertEqual(outcome.status, 'unchanged', outcome.messages)
        service.put_form_schema.assert_not_awaited()

    async def test_known_and_reserved_ids_are_preserved_without_directory_reads(self):
        service = self.service()
        source = AddFieldCommand('ref', 'Reference', field_type='reference', referenced_form='known').execute(schema()).schema
        source = AddFieldCommand('author', 'Author', field_type='reference', metadata='ADDED_BY').execute(source).schema
        result = await resolve_form_names(service, source, {'known': {}})
        self.assertEqual(result, source)
        service.get_form_names.assert_not_awaited()
        service.get_database_names.assert_not_awaited()
