import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client
from nicegui.elements.markdown import prepare_content
import json

from activity_manager.command_list import CommandListManager
from activity_manager.commands import AddFieldCommand
from activity_manager.schema_resolution import resolve_command_schema
from activity_manager.command_execution import ExecutionResult
from activity_manager.schema_display import underline_substitutions
from activity_manager.schema_change import apply_form_schema_change
from activity_manager.schema_identity import prepare_schema_for_put
from test.test_add_field import schema


class PreviewResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_source_form_id_warns_highlights_and_allows_later_commands(self):
        source = schema()
        source['elements'].append({'id': 'ref', 'code': 'REF', 'label': 'Reference', 'type': 'reference',
            'defaultValue': 'deletedform:record', 'typeParameters': {'range': [{'formId': 'deletedform'}]}})
        service = self.service()
        service.get_form_names.side_effect = ValueError('Directory unavailable')
        service.get_form_schema = AsyncMock(side_effect=ValueError('HTTP 404'))
        service.get_form_tree.side_effect = ValueError('HTTP 404: form deleted')
        async def resolve(data):
            return await resolve_command_schema(service, data, {}, {}, {}, {}, source_schema=source)
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(resolve_schema=resolve, on_execute=AsyncMock())
            manager.set_schema(source)
            manager.add_command(AddFieldCommand('one', 'One', validation_condition='REF.UNKNOWN == 1'))
            manager.add_command(AddFieldCommand('two', 'Two'))
            await manager._evaluation_task
            self.assertEqual([item.status for item in manager.executions], ['warning', 'warning'])
            self.assertTrue(manager.execute_button.enabled)
            self.assertEqual(manager.target_schema()['elements'][-1]['code'], 'two')
            first = manager.executions[0]
            path = ('elements', 1, 'typeParameters', 'range', 0, 'formId')
            self.assertIn(path, [issue.path for issue in first.issues])
            self.assertIn('keeping the ID', first.message)
            self.assertIn('UNKNOWN', first.message)
            self.assertEqual(first.schema['elements'][1]['defaultValue'], 'deletedform:record')
            for data in (first.schema, first.names_schema):
                self.assertEqual(data['elements'][1]['typeParameters']['range'][0]['formId'], 'deletedform')
                html = prepare_content('```json\n' + json.dumps(data, indent=2) + '\n```', extras='fenced-code-blocks')
                self.assertIn('<mark>', underline_substitutions(html, data, [path]))
        service.get_form_schema.assert_not_awaited()
        service.get_form_names.assert_not_awaited()

    async def test_source_ids_are_preserved_for_all_form_reference_locations(self):
        for kind in ('reference', 'multiselectreference', 'subform', 'reversereference'):
            with self.subTest(kind=kind):
                source = schema()
                params = {'range': [{'formId': 'old'}]} if kind in ('reference', 'multiselectreference') else {'formId': 'old'}
                source['elements'].append({'id': 'ref', 'type': kind, 'typeParameters': params})
                service = self.service()
                service.get_form_tree.side_effect = ValueError('Not accessible')
                result = await resolve_command_schema(service, source, {}, {}, {}, {}, source_schema=source)
                self.assertEqual(result.status, 'warning')
                self.assertEqual(result.schema, source)
                self.assertIsNotNone(result.names_schema)

    async def test_new_id_shaped_name_is_still_mandatory(self):
        source = schema()
        service = self.service()
        service.get_form_names.return_value = {}
        service.get_form_schema = AsyncMock(side_effect=ValueError('HTTP 404'))
        preview = AddFieldCommand('ref', 'Reference', field_type='reference',
                                  referenced_form='missingformname').execute(source).schema
        result = await resolve_command_schema(service, preview, {}, {}, {}, {}, source_schema=source)
        self.assertEqual(result.status, 'error')
        self.assertIn('missingformname', result.message)
        service.get_form_schema.assert_awaited_once_with('missingformname')

    async def test_resolved_name_does_not_require_readable_related_schema(self):
        service = self.service()
        service.get_form_tree.side_effect = ValueError('Permission denied')
        source = schema()
        preview = AddFieldCommand('ref', 'Reference', field_type='reference',
                                  referenced_form='People').execute(source).schema
        result = await resolve_command_schema(service, preview, {}, {}, {}, {}, source_schema=source)
        self.assertEqual(result.status, 'warning')
        self.assertEqual(result.schema['elements'][-1]['typeParameters']['range'][0]['formId'], 'peopleid')
        service.get_form_tree.assert_awaited_once_with('peopleid')

    async def test_missing_context_still_blocks_required_record_label_conversion(self):
        source = schema()
        source['elements'].append({'id': 'ref', 'code': 'REF', 'type': 'reference',
            'typeParameters': {'range': [{'formId': 'deletedform'}]}})
        preview = deepcopy(source)
        preview['elements'][-1]['defaultValue'] = 'Alice'
        service = self.service()
        service.get_form_tree.side_effect = ValueError('Deleted')
        result = await resolve_command_schema(service, preview, {}, {}, {}, {}, source_schema=source)
        self.assertEqual(result.status, 'error')
        self.assertEqual([item.severity for item in result.diagnostics], ['warning', 'error'])
        self.assertEqual(result.schema['elements'][-1]['defaultValue'], 'Alice')

    async def test_record_id_to_label_api_failure_does_not_block(self):
        source = schema()
        source['elements'].append({'id': 'ref', 'code': 'REF', 'type': 'reference',
            'defaultValue': 'peopleid:record', 'typeParameters': {'range': [{'formId': 'peopleid'}]}})
        related = {'peopleid': {'id': 'peopleid', 'recordLabelFieldId': 'name', 'elements': []}}
        service = self.service()
        service.query_rows.side_effect = ValueError('HTTP 403')
        result = await resolve_command_schema(service, source, related, {}, {}, {}, source_schema=source)
        self.assertEqual(result.status, 'applied')
        self.assertEqual(result.schema['elements'][-1]['defaultValue'], 'peopleid:record')
        self.assertIn('record', result.names_schema['elements'][-1]['defaultValue'])

    def service(self):
        return SimpleNamespace(get_form_names=AsyncMock(return_value={'peopleid': 'People'}),
            get_database_names=AsyncMock(return_value={'db2': 'Remote'}),
            get_form_tree=AsyncMock(return_value={'peopleid': {
                'id': 'peopleid', 'databaseId': 'db2', 'label': 'People', 'recordLabelFieldId': 'nameid',
                'elements': [{'id': 'nameid', 'code': 'NAME', 'type': 'FREE_TEXT'}]}}),
            query_rows=AsyncMock(return_value=[{'_id': 'person'}]))

    async def test_per_command_resolution_saves_ids_names_and_pending_formulas(self):
        service = self.service()
        related, aliases, cache = {}, {}, {}

        async def resolve(data):
            return await resolve_command_schema(service, data, related, {'db2': 'Remote'}, aliases, cache)

        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(resolve_schema=resolve, on_execute=AsyncMock())
            manager.set_schema(schema())
            with patch('activity_manager.schema_identity.secrets.token_hex') as allocate:
                manager.add_command(AddFieldCommand('ref', 'Reference', field_type='reference',
                    referenced_form='Remote/People', default_value='Alice'))
                self.assertEqual(manager.executions[0].status, 'resolving')
                self.assertIsNone(manager.target_schema())
                self.assertFalse(manager.execute_button.enabled)
                await manager._evaluation_task
                manager.add_command(AddFieldCommand('check', 'Check', validation_condition='ref.NAME == "1"'))
                await manager._evaluation_task
                allocate.assert_not_called()
            self.assertTrue(all(item.status == 'applied' for item in manager.executions))
            first = manager.executions[0]
            self.assertEqual(first.schema['elements'][-1]['defaultValue'], 'peopleid:person')
            self.assertEqual(first.names_schema['elements'][-1]['defaultValue'], 'Remote/People:Alice')
            self.assertTrue(first.names_schema['elements'][-1]['defaultValue'].spans)
            self.assertEqual(manager.target_schema()['elements'][-1]['validationCondition'], '[pending-field:ref].nameid == "1"')
            self.assertTrue(manager.execute_button.enabled)
            # The query snapshot is reused even when evaluating the prefix again.
            id_queries = [call for call in service.query_rows.await_args_list if call.args[0]['columns'][0]['id'] == '_id']
            self.assertEqual(len(id_queries), 1)

    async def test_all_unresolved_values_are_retained_marked_and_block_chain(self):
        service = self.service()
        source = schema()
        source['elements'].extend([
            {'id': 'enum', 'code': 'CHOICE', 'type': 'enumerated', 'defaultValue': ['Missing A', 'Missing B'],
             'typeParameters': {'values': [{'id': 'yes', 'label': 'Yes'}]}},
            {'id': 'ref', 'code': 'REF', 'type': 'reference', 'defaultValue': ['Absent A', 'Absent B'],
             'typeParameters': {'range': [{'formId': 'peopleid'}]}},
        ])
        related = {'peopleid': {'id': 'peopleid', 'recordLabelFieldId': 'nameid', 'elements': []}}
        service.query_rows.return_value = []

        async def resolve(data):
            return await resolve_command_schema(service, data, related, {}, {}, {})

        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(resolve_schema=resolve)
            manager.set_schema(source)
            manager.add_command(AddFieldCommand('one', 'One'))
            manager.add_command(AddFieldCommand('two', 'Two'))
            await manager._evaluation_task
            first = manager.executions[0]
            self.assertEqual(first.status, 'error')
            self.assertEqual(len(first.issues), 4)
            self.assertEqual(manager.executions[1].status, 'not_executed')
            self.assertIsNone(manager.target_schema())
            for name in ('Missing A', 'Missing B', 'Absent A', 'Absent B'):
                self.assertIn(name, first.message)
            for data in (first.schema, first.names_schema):
                highlighted = prepare_content('```json\n' + json.dumps(data, indent=2) + '\n```', extras='fenced-code-blocks')
                html = underline_substitutions(highlighted, data, [issue.path for issue in first.issues])
                self.assertIn('<mark>', html)
                self.assertIn('Missing A', html)

    async def test_unknown_option_in_command_becomes_reviewable_error(self):
        command = AddFieldCommand('choice', 'Choice', field_type='enumerated',
            option_list={'cardinality': 'single', 'presentation': 'dropdown',
                         'values': [{'code': 'yes', 'label': 'Yes'}]}, default_value='Unknown')
        result = await resolve_command_schema(self.service(), command.execute(schema()).schema, {}, {}, {}, {})
        self.assertEqual(result.schema['elements'][-1]['defaultValue'], 'Unknown')
        self.assertEqual(result.names_schema['elements'][-1]['defaultValue'], 'Unknown')
        self.assertEqual(len(result.issues), 1)

    async def test_pending_translation_reference_is_materialized_with_its_target(self):
        source = AddFieldCommand('english', 'English').execute(schema()).schema
        source = AddFieldCommand('translated', 'Translated', translation=True,
                                 translation_field='english', translation_language=['fr']).execute(source).schema
        resolution = await resolve_command_schema(self.service(), source, {}, {}, {}, {})
        self.assertFalse(resolution.issues)
        self.assertEqual(resolution.schema['elements'][-1]['typeParameters']['translationConfig']['fieldId'],
                         'pending-field:english')
        written = prepare_schema_for_put(resolution.schema)
        self.assertEqual(written['elements'][-1]['typeParameters']['translationConfig']['fieldId'],
                         written['elements'][-2]['id'])

    async def test_late_response_cannot_overwrite_new_evaluation(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def resolve(data):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
            return ExecutionResult(data, names_schema=data)

        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(resolve_schema=resolve)
            manager.set_schema(schema())
            manager.add_command(AddFieldCommand('oldCommand', 'Old'))
            await started.wait()
            old_task = manager._evaluation_task
            manager.delete_command(0)
            manager.add_command(AddFieldCommand('newCommand', 'New'))
            await manager._evaluation_task
            release.set()
            await old_task
            self.assertEqual(manager.target_schema()['elements'][-1]['code'], 'newCommand')

    async def test_resolved_execution_uses_snapshot_without_name_or_record_queries(self):
        service = self.service()
        loaded = schema()
        related, aliases = {}, {}
        preview = AddFieldCommand('ref', 'Reference', field_type='reference', referenced_form='Remote/People',
                                  default_value='Alice').execute(loaded).schema
        preview = AddFieldCommand('check', 'Check', validation_condition='ref.NAME != ""').execute(preview).schema
        resolution = await resolve_command_schema(service, preview, related, {'db2': 'Remote'}, aliases, {})
        self.assertFalse(resolution.issues)
        written = None

        async def read(form_id):
            return loaded if written is None else written

        async def write(form_id, target):
            nonlocal written
            written = deepcopy(target)

        service.get_form_schema = AsyncMock(side_effect=read)
        service.put_resolved_form_schema = AsyncMock(side_effect=write)
        service.put_form_schema = AsyncMock(side_effect=AssertionError('Must use resolved write'))
        for method in (service.query_rows, service.get_form_tree, service.get_form_names, service.get_database_names):
            method.reset_mock()
            method.side_effect = AssertionError('Execution must not repeat resolution queries')
        result = await apply_form_schema_change(service, 'form1', loaded, resolution.schema, AsyncMock(),
            resolved=True, related_schemas=related, reference_defaults=aliases)
        self.assertEqual(result.status, 'success', result.messages)
        ref = written['elements'][-2]
        self.assertEqual(ref['defaultValue'], 'peopleid:person')
        self.assertEqual(written['elements'][-1]['validationCondition'], ref['id'] + '.nameid != ""')
        service.put_resolved_form_schema.assert_awaited_once()
