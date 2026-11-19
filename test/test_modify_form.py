import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import parse_command_text, CommandTextError
from activity_manager.commands.modify_form import MODIFY_FORM, ModifyFormCommand
from activity_manager.multi_form import prepare_operations
from activity_manager.schema_change import apply_schema_sequence
from activity_manager.schema_resolution import CachedResolutionService
from test import test_multi_form as fixtures


def form(identity, label, database='db', code='NAME'):
    return fixtures.form(identity, label, database, elements=[
        {'id': f'{identity}-name', 'code': code, 'label': 'Name', 'type': 'text'}])


class ModifyFormTests(unittest.TestCase):
    def test_parser_roundtrip_optional_edits_and_case_insensitivity(self):
        for text in ('ModifyForm(form="A")', 'ModifyForm(form="A", name="New")',
                     'ModifyForm(form="A", recordLabelFieldId="")',
                     'modifyform(FORMPATTERN="(?P<region>.*)", NAME="${form.region}", RECORDLABELFIELDID="NAME")'):
            with self.subTest(text=text):
                commands = parse_command_text(text)
                self.assertEqual(parse_command_text(commands[0].to_syntax()), commands)

    def test_invalid_parameters_and_selectors(self):
        for args in ('', 'form="A", formPattern=".*"', 'formPattern="["',
                     'form="A", name=" "', 'form="A", name=None',
                     'form="A", recordLabelFieldId=None', 'form="A", recordLabelFieldId=1',
                     'form="A", unknown="X"', 'formPattern="(?P<name>.*)"'):
            with self.subTest(args=args), self.assertRaises(CommandTextError):
                parse_command_text(f'ModifyForm({args})')

    def test_atomic_edit_preserves_unrelated_properties_and_ids(self):
        original = form('a', 'A')
        original['other'] = {'unchanged': True}
        before = deepcopy(original)
        result = ModifyFormCommand(form='a', new_name='Renamed', record_label_field_id='NAME').execute(original)
        self.assertEqual(result.status, 'applied')
        self.assertEqual(result.schema, {**original, 'label': 'Renamed', 'recordLabelFieldId': 'a-name'})
        self.assertEqual(original, before)
        failed = ModifyFormCommand(form='a', new_name='Bad rename', record_label_field_id='missing').execute(original)
        self.assertEqual(failed.status, 'error')
        self.assertEqual(failed.schema, before)

    def test_record_label_id_priority_ambiguity_and_clear(self):
        original = form('a', 'A')
        original['elements'].append({'id': 'other', 'code': 'a-name'})
        result = ModifyFormCommand(form='a', record_label_field_id='a-name').execute(original)
        self.assertEqual(result.schema['recordLabelFieldId'], 'a-name')
        original['elements'].append({'id': 'third', 'code': 'NAME'})
        self.assertEqual(ModifyFormCommand(form='a', record_label_field_id='NAME').execute(original).status, 'error')
        original['recordLabelFieldId'] = 'a-name'
        self.assertNotIn('recordLabelFieldId', ModifyFormCommand(form='a', record_label_field_id='').execute(original).schema)
        self.assertEqual(ModifyFormCommand(form='a').execute(original).schema, original)

    def test_creator_optional_inclusion_and_multiple_targeting(self):
        added = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((MODIFY_FORM,), on_add=added.append)
            creator.controls['form'].set_value('A')
            self.assertFalse(creator.activations['new_name'].value)
            self.assertFalse(creator.activations['record_label_field_id'].value)
            creator.controls['new_name'].set_value('Ignored')
            creator._add_command()
            self.assertEqual(added[-1], ModifyFormCommand(form='A'))
            creator.activations['new_name'].set_value(True)
            creator.controls['new_name'].set_value('New')
            creator.activations['record_label_field_id'].set_value(True)
            creator.controls['record_label_field_id'].set_value('NAME')
            creator._add_command()
            self.assertEqual(added[-1], ModifyFormCommand(form='A', new_name='New', record_label_field_id='NAME'))
            creator.set_suggestions('form_field_codes', ['NAME'])
            creator.target_mode.set_value('Multiple')
            creator.controls['form'].set_value('A|B')
            self.assertEqual(creator.controls['record_label_field_id'].options, [])
            self.assertEqual(creator.controls['form'].options, [])
            creator._add_command()
            self.assertEqual(added[-1].form_pattern, 'A|B')
            creator.controls['new_name'].set_value(' ')
            self.assertFalse(creator.add_button.enabled)


class ModifyFormPreviewTests(unittest.IsolatedAsyncioTestCase):
    evaluate = fixtures.PreviewTests.evaluate

    async def test_single_edits_switch_context_and_local_rename_resolves(self):
        manager, _ = await self.evaluate([form('a', 'A'), form('b', 'B')], '''
ModifyForm(form="B", name="Renamed", recordLabelFieldId="NAME")
AddField(fieldCode="new", label="New")
WithForm(form="A")
WithForm(form="Renamed")
''')
        self.assertEqual([e.status for e in manager.executions], ['applied'] * 4,
                         [e.message for e in manager.executions])
        self.assertEqual(manager.executions[0].schema['id'], 'b')
        self.assertEqual(manager.executions[0].schema['recordLabelFieldId'], 'b-name')
        self.assertEqual(manager.executions[1].schema['elements'][-1]['code'], 'new')
        self.assertEqual(manager.executions[-1].schema['label'], 'Renamed')
        self.assertEqual(manager.executions[0].opschema[0]['label'], 'A')
        self.assertEqual(len(manager.executions[0].schema['elements']), 1)

    async def test_multiple_renames_keep_frozen_captures_and_active_scope(self):
        manager, _ = await self.evaluate([form('root', 'Root'), form('a', 'Site A'), form('b', 'Site B')], '''
ModifyForm(formPattern="Site (?P<region>.)", name="New ${form.region}", recordLabelFieldId="NAME")
AddField(fieldCode="new", label="${form.name}/${form.1}/${form.index}")
''')
        self.assertEqual([e.status for e in manager.executions], ['applied', 'applied'],
                         [e.message for e in manager.executions])
        self.assertEqual([e.application_label for e in manager.executions[0].iterations], ['Site A', 'Site B'])
        self.assertEqual([s['label'] for s in manager.target_opschema()], ['Root', 'New A', 'New B'])
        self.assertEqual([s['recordLabelFieldId'] for s in manager.target_opschema()[1:]], ['a-name', 'b-name'])
        self.assertEqual([s['elements'][-1]['label'] for s in manager.target_opschema()[1:]], ['Site A/A/1', 'Site B/B/2'])
        self.assertEqual(manager.workspace.active_schema['id'], 'b')

    async def test_multiple_error_is_atomic_and_stops_remaining_targets(self):
        manager, _ = await self.evaluate([form('root', 'Root'), form('a', 'A', code='OTHER'), form('b', 'B')], '''
ModifyForm(formPattern="A|B", name="Renamed", recordLabelFieldId="NAME")
AddField(fieldCode="later", label="Later")
''')
        self.assertEqual([e.status for e in manager.executions[0].iterations], ['error', 'not_executed'])
        # Stage 2: failed form selection/edit publishes neither candidate nor context.
        self.assertEqual(manager.executions[0].schema['id'], 'root')
        self.assertEqual(manager.executions[0].schema['label'], 'Root')
        self.assertEqual(manager.executions[1].status, 'not_executed')
        self.assertEqual([s['id'] for s in manager.workspace.opschema], ['root'])

    async def test_new_form_command_replaces_scope_and_discards_local_variables(self):
        manager, _ = await self.evaluate([form('a', 'A'), form('b', 'B')], '''
WithForm(formPattern="A|B")
SetVariable(name="local", method="literal", value="${form.name}")
ModifyForm(form="A", name="Only A")
AddField(fieldCode="bad", label="${vars.local}")
''')
        self.assertEqual([e.status for e in manager.executions], ['applied', 'applied', 'applied', 'error'])
        self.assertEqual(manager.executions[2].schema['id'], 'a')
        self.assertEqual(manager.workspace.latest['b']['label'], 'B')

    async def test_qualified_selection_switches_database_and_multiple_stays_local(self):
        manager, _ = await self.evaluate([form('a', 'A'), form('b', 'B', 'remote'), form('c', 'C', 'remote')], '''
ModifyForm(form="Remote/B", name="New B")
ModifyForm(formPattern=".*", recordLabelFieldId="NAME")
''')
        self.assertEqual([e.status for e in manager.executions], ['applied', 'applied'],
                         [e.message for e in manager.executions])
        self.assertEqual([e.schema['id'] for e in manager.executions[1].iterations], ['c', 'b'])
        self.assertNotIn('recordLabelFieldId', manager.workspace.latest['a'])

    async def test_zero_matches_skip_following_commands_until_next_switch(self):
        manager, _ = await self.evaluate([form('a', 'A')], '''
ModifyForm(formPattern="Missing", name="Unused")
AddField(fieldCode="skip", label="Skip")
WithForm(form="A")
''')
        self.assertEqual([e.status for e in manager.executions], ['warning', 'warning', 'applied'])
        self.assertEqual(manager.target_schema()['elements'][0]['code'], 'NAME')
        self.assertEqual(len(manager.target_schema()['elements']), 1)

    async def test_pending_record_label_materializes_and_writes_correct_ids(self):
        manager, service = await self.evaluate([form('a', 'A')], '''
AddField(fieldCode="new", label="New")
ModifyForm(form="A", recordLabelFieldId="new")
''')
        self.assertEqual([e.status for e in manager.executions], ['applied', 'applied'])
        preview = manager.target_opschema()[0]
        self.assertTrue(preview['recordLabelFieldId'].startswith('pending-field:'))
        from activity_manager.confirmed_plan import materialize_plan, AttemptBinding
        from test.form_command_runtime import execute
        plan = materialize_plan(manager.candidate_plan, AttemptBinding('test', {}, 'session'))
        target = plan.actions[-1].request.schema
        self.assertEqual(target['recordLabelFieldId'], target['elements'][-1]['id'])
        self.assertFalse(target['recordLabelFieldId'].startswith('pending-field:'))
        outcome = await execute(manager, service)
        self.assertEqual(outcome.status, 'success', outcome.messages)
        self.assertEqual(service.schemas['a']['recordLabelFieldId'], service.schemas['a']['elements'][-1]['id'])

    async def test_clear_record_label_preserves_name_and_fields(self):
        original = form('a', 'A')
        original['recordLabelFieldId'] = 'a-name'
        manager, _ = await self.evaluate([original], 'ModifyForm(form="A", recordLabelFieldId="")')
        self.assertEqual(manager.executions[0].status, 'applied')
        self.assertNotIn('recordLabelFieldId', manager.target_schema())
        self.assertEqual(manager.target_schema()['label'], 'A')
        self.assertEqual(manager.target_schema()['elements'], original['elements'])


class ModifyFormSuggestionTests(unittest.IsolatedAsyncioTestCase):
    async def make_page(self):
        managers, creators = [], []
        def manager(**kwargs):
            value = CommandListManager(**kwargs)
            managers.append(value)
            return value
        def creator(*args, **kwargs):
            value = CommandCreatorRenderer(*args, **kwargs)
            creators.append(value)
            return value
        with patch('activity_manager.page.CommandListManager', side_effect=manager), \
                patch('activity_manager.page.CommandCreatorRenderer', side_effect=creator):
            with Client(ui.page('/')), ui.column():
                fixtures.connection_page()
        manager, creator = managers[0], creators[0]
        service = fixtures.MemoryService([form('a', 'A', code='FIRST'), form('b', 'B', code='SECOND')])
        manager.form_service = CachedResolutionService(service, manager.resolution_cache)
        manager.set_schema(service.schemas['a'])
        creator.command_select.set_value('modify_form')
        await asyncio.sleep(0)
        return manager, creator, service

    @staticmethod
    def set_target(creator, target):
        creator._updating = True
        try:
            creator.controls['form'].set_value(target)
        finally:
            creator._updating = False

    async def test_dropdown_tracks_selected_form_and_uses_latest_local_fields(self):
        manager, creator, service = await self.make_page()
        self.set_target(creator, 'A')
        await creator.on_parameter_change(creator)
        self.assertEqual(creator.controls['record_label_field_id'].options, ['FIRST'])
        self.set_target(creator, 'B')
        await creator.on_parameter_change(creator)
        self.assertEqual(creator.controls['record_label_field_id'].options, ['SECOND'])
        self.assertEqual(service.reads, ['b'])
        self.assertEqual([s['id'] for s in manager.workspace.opschema], ['a'])
        await creator.on_parameter_change(creator)
        self.assertEqual(service.reads, ['b'])
        local = deepcopy(manager.workspace.active_schema)
        local['elements'].append({'id': 'new-local', 'code': 'LOCAL', 'type': 'FREE_TEXT'})
        manager.workspace.update(local)
        self.set_target(creator, 'a')
        await creator.on_parameter_change(creator)
        self.assertEqual(creator.controls['record_label_field_id'].options, ['FIRST', 'LOCAL'])
        self.assertEqual(service.writes, [])

    async def test_multiple_mode_and_invalid_target_have_no_field_suggestions(self):
        _, creator, service = await self.make_page()
        self.set_target(creator, 'A')
        await creator.on_parameter_change(creator)
        self.assertEqual(creator.controls['record_label_field_id'].options, ['FIRST'])
        creator.target_mode.set_value('Multiple')
        self.assertEqual(creator.controls['record_label_field_id'].options, [])
        await creator.on_parameter_change(creator)
        self.assertEqual(service.reads, [])
        creator._updating = True
        creator.target_mode.set_value('Single')
        creator.controls['form'].set_value('Missing')
        creator._updating = False
        await creator.on_parameter_change(creator)
        self.assertEqual(creator.controls['record_label_field_id'].options, [])

    async def test_late_schema_response_cannot_replace_new_target_suggestions(self):
        _, creator, service = await self.make_page()
        started, release = asyncio.Event(), asyncio.Event()
        original = service.get_form_schema
        async def delayed(identity):
            started.set()
            await release.wait()
            return await original(identity)
        service.get_form_schema = delayed
        self.set_target(creator, 'B')
        previous = asyncio.create_task(creator.on_parameter_change(creator))
        await started.wait()
        self.set_target(creator, 'A')
        await creator.on_parameter_change(creator)
        self.assertEqual(creator.controls['record_label_field_id'].options, ['FIRST'])
        release.set()
        await previous
        self.assertEqual(creator.controls['record_label_field_id'].options, ['FIRST'])
