"""Creator -> command text -> preview resolver -> generated API body -> readback.

All API reads and writes are mocked; no live service is contacted.
"""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client
from activity_info_api_client.models.add_update_form_response import AddUpdateFormResponse

from activity_manager.api import ActivityInfoService, DEFAULT_BASE_URL, update_form_schema
from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import parse_command_text
from activity_manager.commands import MODIFY_FIELD, AddFieldCommand
from activity_manager.schema_change import apply_form_schema_change
from activity_manager.schema_resolution import resolve_command_schema


def source_schema():
    return {'id': 'form1', 'label': 'Survey', 'databaseId': 'db', 'schemaVersion': 1, 'elements': [
        {'id': 'choiceid', 'code': 'choice', 'label': 'Choice', 'type': 'enumerated', 'required': False,
         'description': 'Remove me', 'defaultValue': ['removeid'],
         'typeParameters': {'cardinality': 'multiple', 'presentation': 'dropdown', 'values': [
             {'id': 'removeid', 'label': 'Remove'}, {'id': 'keepid', 'label': 'Keep', 'extra': 'preserve'}]}},
        {'id': 'referenceid', 'code': 'person', 'label': 'Person', 'type': 'reference', 'required': False,
         'description': 'Remove lookup description', 'typeParameters': {
             'range': [{'formId': 'people'}], 'lookupConfigs': [
                 {'id': 'oldlookup', 'lookupLabel': 'Remove', 'formula': 'nameid'},
                 {'id': 'keeplookup', 'lookupLabel': 'Keep', 'formula': 'nameid', 'extra': 'preserve'}]}},
        {'id': 'quantityid', 'code': 'population', 'label': 'Population', 'type': 'quantity', 'required': True,
         'requiredCondition': 'TRUE', 'relevanceCondition': 'TRUE', 'defaultValue': 3,
         'typeParameters': {'units': 'people', 'extra': 'preserve'}},
    ]}


class ModifyFieldEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_then_modify_lookup_ids_stay_in_original_namespace_through_write(self):
        source = source_schema()
        add = AddFieldCommand('REF1', 'New reference', field_type='reference', field_variant='reference',
                              referenced_form='people', lookup_fields=[
                                  {'label': 'First', 'formula': 'name'}, {'label': 'Second', 'formula': 'code'}])
        creator = self.creator('REF1', 'reference', 'reference')
        rows = next(editor for editor in creator.editors if editor.definition.key == 'lookup_fields')
        rows.activation.set_value(True)
        for label, formula in (('First', 'code'), ('Third', 'name')):
            rows._change_rows(None, 0)
            rows.rows[-1][0].control.set_value(label)
            rows.rows[-1][1].control.set_value(formula)
        creator._parameter_changed(None)
        manager, related = await self.preview(source, [add, self.finish(creator)])
        self.assertEqual([run.status for run in manager.executions], ['applied', 'applied'])
        initial = manager.executions[0].schema['elements'][-1]['typeParameters']['lookupConfigs']
        self.assertEqual([row['id'] for row in initial], ['pending-lookup:REF1:0', 'pending-lookup:REF1:1'])
        preview = manager.target_schema()
        rows = preview['elements'][-1]['typeParameters']['lookupConfigs']
        self.assertEqual([row['id'] for row in rows], ['pending-lookup:REF1:0', 'pending-lookup:REF1:2'])
        self.assertEqual([row['formula'] for row in rows], ['codeid', 'nameid'])
        payload = await self.write_and_verify(source, preview, related)
        actual = payload['elements'][-1]['typeParameters']['lookupConfigs']
        self.assertEqual([row['lookupLabel'] for row in actual], ['First', 'Third'])
        self.assertEqual(len({row['id'] for row in actual}), 2)
        self.assertTrue(all(not row['id'].startswith('pending-') for row in actual))

    def creator(self, target, kind, variant=None):
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((MODIFY_FIELD,))
        creator.controls['field_code'].set_value(target)
        creator.controls['field_type'].set_value(kind)
        creator._parameter_changed(None)
        if variant:
            creator.controls['field_variant'].set_value(variant)
        creator._parameter_changed(None)
        return creator

    def edit(self, creator, key, value=None):
        creator.activations[key].set_value(True)
        if value is not None:
            creator.controls[key].set_value(value)
        creator._parameter_changed(None)

    def finish(self, creator):
        self.assertFalse(creator.add_button.props.get('disable', False), creator.error.text)
        commands = []
        creator.on_add = commands.append
        creator._add_command()
        self.assertEqual(len(commands), 1)
        return commands[0]

    async def preview(self, source, commands):
        remote = {'id': 'people', 'label': 'People', 'databaseId': 'db', 'schemaVersion': 1,
                  'elements': [{'id': 'nameid', 'code': 'name', 'label': 'Name', 'type': 'FREE_TEXT'},
                               {'id': 'codeid', 'code': 'code', 'label': 'Code', 'type': 'FREE_TEXT'}]}
        related = {'people': remote}
        service = SimpleNamespace(get_form_names=AsyncMock(return_value={'people': 'People'}))
        async def resolve(candidate):
            return await resolve_command_schema(service, candidate, related, {}, {}, {})
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(resolve_schema=resolve, on_execute=lambda: None)
        manager.set_schema(source)
        # Exercise the actual Text Apply path with creator-generated serialization.
        manager.advanced_draft = '\n'.join(command.to_syntax() for command in commands)
        self.assertEqual(parse_command_text(manager.advanced_draft), commands)
        manager._apply_text()
        await manager._evaluation_task
        return manager, related

    async def write_and_verify(self, source, preview, related, expected_status='success'):
        original = deepcopy(source)
        preview_before = deepcopy(preview)
        written = None
        payloads = []

        async def request(endpoint, **kwargs):
            nonlocal written
            self.assertIs(endpoint, update_form_schema)
            self.assertEqual(kwargs['form_id'], 'form1')
            # Serialize the generated client's actual FormSchema request body.
            written = kwargs['body'].to_dict()
            payloads.append(deepcopy(written))
            return AddUpdateFormResponse(database=object(), forms=[])

        async def read(form_id):
            self.assertEqual(form_id, 'form1')
            return deepcopy(source if written is None else written)

        service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
        recovery = AsyncMock()
        try:
            with patch.object(service, '_request', AsyncMock(side_effect=request)) as update, \
                    patch.object(service, 'get_form_schema', AsyncMock(side_effect=read)) as get_schema:
                outcome = await apply_form_schema_change(service, 'form1', source, preview, recovery,
                                                         resolved=True, related_schemas=related)
                self.assertEqual(outcome.status, expected_status, outcome.messages)
                if expected_status == 'success':
                    update.assert_awaited_once()
                    self.assertEqual(get_schema.await_count, 2)
                    self.assertEqual(outcome.actual_schema, written)
                else:
                    update.assert_not_awaited()
                recovery.assert_not_awaited()
        finally:
            await service.aclose()
        self.assertEqual(source, original)
        self.assertEqual(preview, preview_before)
        return payloads[0] if payloads else None

    async def test_replace_options_and_lookups_clear_properties_and_verify_actual_payload(self):
        source = source_schema()
        before = deepcopy(source)
        options = self.creator('choice', 'enumerated', 'multiple')
        self.edit(options, 'description')
        self.edit(options, 'default_value')
        group = next(editor for editor in options.editors if editor.definition.key == 'option_list')
        group.activation.set_value(True)
        rows = next(child for child in group.children if child.definition.key == 'values')
        rows.activation.set_value(True)
        for label in ('Keep', 'New option'):
            rows._change_rows(None, 0)
            rows.rows[-1][0].control.set_value(label)
        options._parameter_changed(None)

        lookups = self.creator('person', 'reference', 'reference')
        rows = next(editor for editor in lookups.editors if editor.definition.key == 'lookup_fields')
        rows.activation.set_value(True)
        for label, formula in (('Keep', 'code'), ('New lookup', 'name')):
            rows._change_rows(None, 0)
            rows.rows[-1][0].control.set_value(label)
            rows.rows[-1][1].control.set_value(formula)
        lookups._parameter_changed(None)

        clear = self.creator('population', 'quantity')
        for key in ('units', 'default_value', 'relevance_condition', 'required_condition', 'required'):
            self.edit(clear, key)

        commands = [self.finish(creator) for creator in (options, lookups, clear)]
        manager, related = await self.preview(source, commands)
        self.assertEqual([run.status for run in manager.executions], ['applied'] * 3)
        self.assertTrue(manager.execute_button.enabled)
        preview = manager.target_schema()
        option_rows = preview['elements'][0]['typeParameters']['values']
        lookup_rows = preview['elements'][1]['typeParameters']['lookupConfigs']
        self.assertEqual(option_rows[0], {'id': 'keepid', 'label': 'Keep', 'extra': 'preserve'})
        self.assertEqual(lookup_rows[0], {'id': 'keeplookup', 'lookupLabel': 'Keep', 'formula': 'codeid', 'extra': 'preserve'})
        self.assertTrue(option_rows[1]['id'].startswith('pending-option:'))
        self.assertTrue(lookup_rows[1]['id'].startswith('pending-lookup:'))

        payload = await self.write_and_verify(source, preview, related)
        self.assertEqual([field['id'] for field in payload['elements']], ['choiceid', 'referenceid', 'quantityid'])
        actual_options = payload['elements'][0]['typeParameters']['values']
        actual_lookups = payload['elements'][1]['typeParameters']['lookupConfigs']
        self.assertEqual([row['label'] for row in actual_options], ['Keep', 'New option'])
        self.assertEqual([row['lookupLabel'] for row in actual_lookups], ['Keep', 'New lookup'])
        self.assertEqual(actual_options[0]['id'], 'keepid')
        self.assertEqual(actual_lookups[0]['id'], 'keeplookup')
        for row in (actual_options[1], actual_lookups[1]):
            self.assertRegex(row['id'], r'^c[a-f0-9]{24}$')
        for key in ('description', 'defaultValue'):
            self.assertNotIn(key, payload['elements'][0])
        quantity = payload['elements'][2]
        for key in ('defaultValue', 'relevanceCondition', 'requiredCondition'):
            self.assertNotIn(key, quantity)
        self.assertIs(quantity['required'], False)
        self.assertEqual(quantity['typeParameters'], {'extra': 'preserve'})
        self.assertEqual(source, before)

        # Applying the same standalone commands again preserves every allocated ID.
        repeated, related = await self.preview(payload, commands)
        self.assertEqual([run.status for run in repeated.executions], ['applied'] * 3)
        self.assertEqual(repeated.target_schema(), payload)
        await self.write_and_verify(payload, repeated.target_schema(), related, expected_status='unchanged')

    async def test_empty_lookup_collection_removes_property_in_generated_payload(self):
        source = source_schema()
        creator = self.creator('person', 'reference', 'reference')
        rows = next(editor for editor in creator.editors if editor.definition.key == 'lookup_fields')
        rows.activation.set_value(True)
        self.edit(creator, 'description')
        command = self.finish(creator)
        self.assertEqual(command.changes, {'description': None, 'lookup_fields': None})
        manager, related = await self.preview(source, [command])
        payload = await self.write_and_verify(source, manager.target_schema(), related)
        field = payload['elements'][1]
        self.assertEqual(field['id'], 'referenceid')
        self.assertNotIn('description', field)
        self.assertEqual(field['typeParameters'], {'range': [{'formId': 'people'}]})

    async def test_invalid_clearing_and_duplicate_options_block_write_target(self):
        for case in ('empty_options', 'duplicate_options', 'required_label'):
            with self.subTest(case=case):
                source = source_schema()
                creator = self.creator('choice', 'enumerated', 'multiple')
                if case == 'required_label':
                    self.edit(creator, 'label')
                else:
                    group = next(editor for editor in creator.editors if editor.definition.key == 'option_list')
                    group.activation.set_value(True)
                    rows = next(child for child in group.children if child.definition.key == 'values')
                    rows.activation.set_value(True)
                    if case == 'duplicate_options':
                        for _ in range(2):
                            rows._change_rows(None, 0)
                            rows.rows[-1][0].control.set_value('Keep')
                    creator._parameter_changed(None)
                manager, _ = await self.preview(source, [self.finish(creator)])
                self.assertEqual(manager.executions[0].status, 'error')
                self.assertIsNone(manager.target_schema())
                self.assertFalse(manager.execute_button.enabled)
                self.assertEqual(manager.executions[0].schema, source)
