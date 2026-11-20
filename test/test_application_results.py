from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client

from activity_manager.application_results import (
    ActualCommand, ApplicationResult, DiagnosticAttachment, DisplaySnapshotBuilder,
)
from activity_manager.command_execution import Diagnostic, ExecutionResult
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import parse_command_text
from activity_manager.immutable import freeze, thaw
from activity_manager.page import connection_page
from activity_manager.resource_state import ApplicationState, DatabaseResourcesPayload, ResourceKey
from test.test_multi_form import MemoryService, form


class DisplaySnapshotTests(unittest.TestCase):
    def test_database_tabs_keep_array_payload_and_pinned_names_separate(self):
        state = ApplicationState()
        state.load_form(form('db', 'Form sharing database ID'), affected=True)
        entries = {'a': {'id': 'a', 'parentId': 'db', 'label': 'A', 'type': 'FORM'}}
        state.put(ResourceKey.database('db'), DatabaseResourcesPayload(entries))
        databases = {'db': 'Local'}
        snapshot = DisplaySnapshotBuilder(1).capture(state, {}, databases, {})
        databases['db'] = 'Renamed'
        tabs = {tab.identity: tab for tab in snapshot.tabs()}
        self.assertEqual(tabs['db'].label, 'Form sharing database ID')
        self.assertEqual(tabs['database:db'].label, 'Local')
        self.assertEqual(thaw(tabs['database:db'].payload), list(entries.values()))
        self.assertEqual(thaw(tabs['database:db'].names)[0]['parentId'], 'Local')
        state.put(ResourceKey.database('db'), DatabaseResourcesPayload({}))
        enriched = snapshot.enrich({})
        self.assertEqual(enriched.tab_metadata()['database:db'][0][0]['parentId'], 'Local')
        detached = snapshot.tab_payloads()
        detached['database:db'][0]['parentId'] = 'elsewhere'
        self.assertEqual(snapshot.tab_payloads()['database:db'][0]['parentId'], 'db')
        self.assertEqual(snapshot.resources[1].state.payload.to_list(), list(entries.values()))

    def test_empty_database_array_and_missing_name(self):
        state = ApplicationState()
        state.put(ResourceKey.database('db'), DatabaseResourcesPayload({}))
        snapshot = DisplaySnapshotBuilder(1).capture(state, {}, {}, {})
        self.assertEqual(snapshot.tabs()[0].label, 'db')
        self.assertEqual(snapshot.tab_payloads(), {'database:db': []})
        self.assertEqual(snapshot.tab_metadata()['database:db'][0], [])

    def test_related_names_labels_and_annotations_are_captured_independently(self):
        source = form('a', 'A', elements=[
            {'id': 'ref', 'code': 'REF', 'type': 'reference', 'defaultValue': 'people:r',
             'typeParameters': {'range': [{'formId': 'people'}]}},
            {'id': 'calc', 'code': 'CALC', 'type': 'calculated',
             'typeParameters': {'formula': 'ref.name'}}])
        related = {'people': form('people', 'People', 'remote',
                    [{'id': 'name', 'code': 'OLD', 'type': 'FREE_TEXT'}])}
        databases, labels = {'remote': 'Remote'}, {('people', 'r'): 'Alice'}
        state = ApplicationState()
        state.load_form(source, affected=True)
        issue = Diagnostic('warning', 'Pinned', ('elements', 0))
        builder = DisplaySnapshotBuilder(3)
        before = builder.capture(state, related, databases, {'a': [issue]}, labels=labels)
        names = before.tab_metadata()['a'][0]
        self.assertIn('Remote/People:Alice', names['elements'][0]['defaultValue'])
        self.assertEqual(names['elements'][1]['typeParameters']['formula'], 'REF.OLD')
        related['people']['elements'][0]['code'] = 'NEW'
        databases['remote'], labels[('people', 'r')] = 'Renamed', 'Bob'
        state.put_form(form('a', 'Changed'))
        builder.capture(state, related, databases, {}, labels=labels)
        self.assertEqual(before.tab_metadata()['a'][0], names)
        self.assertEqual(before.resources[0].annotations, (issue,))
        self.assertEqual(before.affected_keys, (ResourceKey.form('a'),))

    def test_enrichment_pins_known_labels_and_retains_old_revision(self):
        state = ApplicationState()
        state.load_form(form('a', 'A', elements=[
            {'id': 'ref', 'type': 'reference', 'defaultValue': ['people:r', 'people:s'],
             'typeParameters': {'range': [{'formId': 'people'}]}}]), affected=True)
        before = DisplaySnapshotBuilder(2).capture(state, {'people': form('people', 'People')}, {}, {},
            labels={('people', 'r'): 'Alice'})
        after = before.enrich({('people', 'r'): 'Changed', ('people', 's'): 'Bob'})
        self.assertNotIn(('people', 's'), before.record_labels)
        self.assertEqual(after.record_labels[('people', 'r')], 'Alice')
        self.assertEqual(after.record_labels[('people', 's')], 'Bob')
        self.assertEqual(after.resources[0].state, before.resources[0].state)
        self.assertNotEqual(after.snapshot_id, before.snapshot_id)
        self.assertEqual(after.revision, before.revision + 1)

    def test_rejected_candidate_is_detached_and_unexecuted_has_no_poststate(self):
        candidate = form('a', 'Rejected')
        attachment = DiagnosticAttachment(candidate, [Diagnostic('error', 'Invalid', ('label',))])
        candidate['label'] = 'changed'
        self.assertEqual(attachment.candidate['label'], 'Rejected')
        command = ActualCommand('app', 'authored', 0, 0, None, None, [ResourceKey.form('a')], ['db', 'A'])
        with self.assertRaises(ValueError):
            ApplicationResult(command, 'not_executed', 'Waiting',
                DisplaySnapshotBuilder(1).capture(ApplicationState(), {}, {}, {}))


class ResultIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, text, forms=None, *, page=False, related=None, resolver=None):
        managers = []
        def create(**kwargs):
            manager = CommandListManager(**kwargs)
            managers.append(manager)
            return manager
        if page:
            with patch('activity_manager.page.CommandListManager', side_effect=create):
                with Client(ui.page('/')), ui.column() as container:
                    connection_page()
            manager = managers[0]
        else:
            with Client(ui.page('/')), ui.column() as container:
                manager = CommandListManager()
        forms = forms or [form('a', 'A')]
        manager.form_service = MemoryService(forms)
        manager.related_schemas = deepcopy(related or {})
        manager.resolve_schema = resolver or AsyncMock(side_effect=lambda data: ExecutionResult(data))
        manager.set_schema(forms[0])
        manager.commands = parse_command_text(text)
        manager._evaluate_commands()
        await manager._evaluation_task
        return manager, container

    async def test_concrete_results_survive_execution_input_and_output_mutation(self):
        template = '$' + '{vars.title}'
        manager, _ = await self.evaluate(
            'SetVariable(name="title", method="literal", value="Added")\n'
            f'AddField(fieldCode="new", label="{template}")\nDeleteField(fieldCode="new")')
        authored = manager.results[1]
        actual = authored.applications[0]
        self.assertEqual(actual.command.parameters['label'], 'Added')
        self.assertIn(template, authored.command.syntax)
        self.assertEqual(actual.command.targets, (ResourceKey.form('a'),))
        self.assertEqual(actual.command.member_ids, ('pending-field:new',))
        self.assertEqual(manager.results[-1].applications[0].command.member_ids, ('pending-field:new',))
        manager.executions[1].schema['elements'].clear()
        manager.executions[1].form_snapshots[0]['elements'].clear()
        authored.display_state.form_payloads()['a']['elements'].clear()
        self.assertEqual(actual.display_state.form_payloads()['a']['elements'][0]['label'], 'Added')
        self.assertEqual(manager.workspace.state.forms['a']['elements'], [])
        self.assertTrue(actual.display_state.resources[0].state.payload.schema['elements'][0]['id'].startswith('pending-field:'))

    async def test_authored_identities_survive_reordering_and_duplicate_instances(self):
        manager, _ = await self.evaluate('DeleteField(fieldCode="missing1")\nDeleteField(fieldCode="missing2")')
        identities = [item.authored_id for item in manager.authored_commands]
        manager.move_command(0, 1)
        await manager._evaluation_task
        self.assertEqual([item.authored_id for item in manager.authored_commands], identities[::-1])
        self.assertEqual([item.order for item in manager.authored_commands], [0, 1])
        command = manager.commands[0]
        manager.commands = [command, command]
        manager._evaluate_commands()
        await manager._evaluation_task
        self.assertNotEqual(manager.authored_commands[0].authored_id, manager.authored_commands[1].authored_id)

    async def test_zero_match_parent_and_known_unexecuted_targets_are_honest(self):
        forms = [form('root', 'Root'), form('a', 'A', elements=[
            {'id': 'f1', 'code': 'f_i', 'label': 'Old', 'type': 'FREE_TEXT'},
            {'id': 'f2', 'code': 'f_ii', 'label': 'Old', 'type': 'FREE_TEXT'}]),
            form('b', 'B', elements=[{'id': 'f3', 'code': 'f_i', 'label': 'Old', 'type': 'FREE_TEXT'}])]
        manager, _ = await self.evaluate(
            'WithForm(formPattern="A|B")\n'
            'ModifyField(fieldPattern="missing", fieldType="FREE_TEXT", fieldVariant="text", label="Ignored")\n'
            'ModifyField(fieldPattern="f_.*", fieldType="FREE_TEXT", fieldVariant="text", newCode="collision")',
            forms)
        self.assertEqual(manager.results[1].applications, ())
        self.assertEqual(manager.results[1].status, 'warning')
        self.assertEqual(len(manager.results[1].display_state.resources), 4)
        applications = manager.results[2].applications
        self.assertEqual([item.status for item in applications], ['applied', 'error', 'not_executed'])
        self.assertIsNone(applications[-1].display_state)
        self.assertEqual(applications[-1].command.targets, (ResourceKey.form('b'),))
        self.assertEqual(applications[-1].command.member_ids, ('f3',))
        self.assertIn('B', applications[-1].command.context_path)
        self.assertEqual(len({item.command.application_id for item in applications}), 3)
        self.assertEqual(manager.results[0].applications[0].command.parameters['form'], 'a')

    async def test_current_enrichment_is_guarded_and_preserves_execution_payloads(self):
        source = form('a', 'A', elements=[{'id': 'ref', 'type': 'reference', 'defaultValue': 'people:r',
                  'typeParameters': {'range': [{'formId': 'people'}]}}])
        manager, _ = await self.evaluate('DeleteField(fieldCode="missing")', [source],
                                        related={'people': form('people', 'People')})
        before, target = manager.results[-1].display_state, manager.target_forms()
        self.assertIsNone(manager.enrich_current_display(before.snapshot_id, before.generation - 1, {}))
        enriched = manager.enrich_current_display(before.snapshot_id, before.generation, {('people', 'r'): 'Alice'})
        self.assertIn('Alice', enriched.tab_metadata()['a'][0]['elements'][0]['defaultValue'])
        self.assertNotIn(('people', 'r'), before.record_labels)
        self.assertEqual(manager.target_forms(), target)
        self.assertIsNone(manager.enrich_current_display(before.snapshot_id, before.generation, {}))
        manager._evaluate_commands()
        await manager._evaluation_task
        self.assertIsNone(manager.enrich_current_display(enriched.snapshot_id, enriched.generation, {}))

    async def test_historical_ui_ignores_later_context_and_execution_payload_mutations(self):
        related = {'people': form('people', 'People', elements=[{'id': 'name', 'code': 'OLD', 'type': 'FREE_TEXT'}])}
        source = form('a', 'A', elements=[
            {'id': 'ref', 'code': 'REF', 'type': 'reference', 'typeParameters': {'range': [{'formId': 'people'}]}},
            {'id': 'calc', 'code': 'CALC', 'type': 'calculated', 'typeParameters': {'formula': 'ref.name'}}])
        manager, container = await self.evaluate('DeleteField(fieldCode="missing")\nWithForm(form="B")',
            [source, form('b', 'B')], page=True, related=related)
        active = manager.workspace.state.active_form_key
        manager.related_schemas['people']['elements'][0]['code'] = 'NEW'
        manager._base_related['people']['label'] = 'Renamed'
        manager.executions[0].schema['elements'].clear()
        manager.executions[0] = replace(manager.executions[0], names_schema={'elements': []})
        manager.select_command(0)
        toggle = next(item for item in container.descendants() if isinstance(item, ui.toggle) and 'names' in item.options)
        toggle.set_value('names')
        code = next(item for item in container.descendants() if isinstance(item, ui.code))
        self.assertIn('REF.OLD', code.content)
        self.assertNotIn('Renamed', code.content)
        tabs = next(item for item in container.descendants() if isinstance(item, ui.tabs))
        self.assertEqual(len(list(tabs)), 2)
        self.assertEqual(manager.workspace.state.active_form_key, active)

    async def test_record_labels_survive_context_only_commands_without_queries(self):
        source = form('a', 'A', elements=[{'id': 'ref', 'code': 'REF', 'type': 'reference',
            'defaultValue': 'people:r', 'typeParameters': {'range': [{'formId': 'people'}]}}])
        resolver = AsyncMock(side_effect=lambda data: ExecutionResult(data, record_labels=freeze({('people', 'r'): 'Alice'})))
        manager, _ = await self.evaluate(
            'DeleteField(fieldCode="missing")\nSetVariable(name="x", method="literal", value="v")',
            [source], related={'people': form('people', 'People')}, resolver=resolver)
        for result in manager.results:
            self.assertIn('Alice', result.display_state.tab_metadata()['a'][0]['elements'][0]['defaultValue'])
        self.assertEqual(resolver.await_count, 1)
