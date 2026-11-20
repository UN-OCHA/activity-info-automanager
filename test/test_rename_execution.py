"""Local label projections and schema-only execution and recovery."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import AsyncMock, patch

from activity_manager.confirmed_plan import AttemptBinding, ConfirmationAttempt
from activity_manager.endpoint_evidence import Observation, EndpointFailure, FailureKind, EndpointReceipt
from activity_manager.executor import Executor
from activity_manager.planning import ActionKind, PlanError
from activity_manager.resource_state import Existence, ResourceKind
from test import test_move_form as fixtures
from test.form_command_runtime import install


class RenameTests(unittest.IsolatedAsyncioTestCase):
    evaluate = fixtures.MoveFormTests.evaluate

    async def prepare(self, text='ModifyForm(currentForm=False, form="A", name="Renamed")', schemas=None):
        manager, service = await self.evaluate(text, schemas)
        self.assertTrue(all(item.status == 'applied' for item in manager.executions),
                        [item.message for item in manager.executions])
        install(service)
        candidate = manager.candidate_plan
        binding = AttemptBinding(str(manager._evaluation_version), {}, 'session')
        attempt = ConfirmationAttempt(candidate, binding, related_schemas=manager.related_schemas)
        attempt.confirm(attempt.plan.attempt_id, binding, candidate)
        return manager, service, candidate, binding, attempt

    async def run_prepared(self, prepared, choice='leave'):
        manager, service, candidate, binding, attempt = prepared
        choose = choice if callable(choice) else AsyncMock(return_value=choice)
        executor = Executor(service)
        report = await executor.execute(attempt, binding, candidate, choose)
        return report

    def fail_after_commit(self, service, indices):
        original = service.put_form_schema
        async def write(identity, schema):
            await original(identity, schema)
            if len(service.writes) in indices:
                raise ValueError('Response lost after commit')
        service.put_form_schema = write

    async def test_one_schema_request_and_schema_observations_only(self):
        prepared = await self.prepare()
        manager, service, _, _, attempt = prepared
        self.assertEqual(attempt.plan.actions[0].action.projections, ())
        original_read = service.observe_resource
        async def schema_only(key):
            self.assertEqual(key.kind, ResourceKind.FORM_SCHEMA)
            return await original_read(key)
        service.observe_resource = schema_only
        report = await self.run_prepared(prepared)
        self.assertEqual(report.status, 'success', report.messages)
        self.assertEqual(len(service.writes), 1)
        self.assertEqual(service.moves, [])
        self.assertEqual(report.journal[0].postimage.related, ())
        self.assertEqual(manager.workspace.latest['a']['label'], 'Renamed')
        display = manager.executions[0].display_state.tab_payloads()
        self.assertEqual(display['a']['label'], 'Renamed')
        self.assertEqual(next(row for row in display['database:db'] if row['id'] == 'a')['label'], 'Renamed')

    async def test_rename_then_schema_edit_share_one_schema_action(self):
        prepared = await self.prepare('ModifyForm(currentForm=False, form="A", name="Renamed")\nAddField(fieldCode="NEW", label="New")')
        report = await self.run_prepared(prepared)
        self.assertEqual(report.status, 'success', report.messages)
        self.assertEqual(len(report.journal), 1)
        self.assertEqual(report.journal[0].preimage.state.payload.schema['label'], 'A')
        self.assertEqual(report.journal[0].postimage.state.payload.schema['elements'][0]['code'], 'NEW')
        self.assertEqual(prepared[1].resources['db']['a']['label'], 'Renamed')

    async def test_move_rename_edit_move_recover_in_actual_reverse_order(self):
        prepared = await self.prepare('''MoveForm(currentForm=False, form="A", folder="North")
ModifyForm(currentForm=False, form="A", name="Renamed")
AddField(fieldCode="NEW", label="New")
MoveForm(currentForm=False, form="Renamed", folder="South")''')
        manager, service, _, _, attempt = prepared
        self.assertEqual([item.action.kind for item in attempt.plan.actions], [
            ActionKind.DATABASE_RESOURCE_PATCH, ActionKind.FORM_SCHEMA_UPDATE,
            ActionKind.DATABASE_RESOURCE_PATCH])
        self.assertIn(attempt.plan.actions[1].action.action_id, attempt.plan.actions[-1].action.dependency_ids)
        service.error_after_move = 2
        report = await self.run_prepared(prepared, 'rollback_all')
        self.assertEqual(report.status, 'rolled_back_all', report.messages)
        self.assertEqual(service.timeline, [('move', 'a', 'north'), ('schema', 'a'),
            ('move', 'a', 'south'), ('move', 'a', 'north'), ('schema', 'a'), ('move', 'a', 'db')])
        self.assertEqual(service.schemas['a']['label'], 'A')
        self.assertEqual(service.schemas['a']['elements'], [])
        self.assertEqual(service.resources['db']['a']['label'], 'A')

    async def test_multiple_renames_keep_projections_ordered_and_restore_each_label(self):
        prepared = await self.prepare('ModifyForm(currentForm=False, formPattern="A|B", name="New ${form.name}")')
        self.fail_after_commit(prepared[1], {2})
        report = await self.run_prepared(prepared, 'rollback_all')
        self.assertEqual(report.status, 'rolled_back_all', report.messages)
        self.assertEqual([identity for identity, _ in prepared[1].writes], ['a', 'b', 'b', 'a'])
        self.assertEqual(prepared[1].resources['db']['a']['label'], 'A')
        self.assertEqual(prepared[1].resources['db']['b']['label'], 'B')

    async def test_multiple_modify_and_field_edits_share_one_action_per_form(self):
        from test.test_modify_form import form
        for current in (False, True):
            for rename in (False, True):
                with self.subTest(current=current, rename=rename):
                    commands = []
                    if current:
                        commands.extend(['WithForm(formPattern="A|B")',
                                         'AddField(fieldCode="before", label="Before")'])
                    selector = 'currentForm=True' if current else 'currentForm=False, formPattern="A|B"'
                    name = ', name="New ${form.name}"' if rename else ''
                    commands.extend([f'ModifyForm({selector}, recordLabelFieldId="NAME"{name})',
                                     'AddField(fieldCode="after", label="${form.name}")'])
                    prepared = await self.prepare('\n'.join(commands), [form('a', 'A'), form('b', 'B')])
                    manager, service, candidate, _, attempt = prepared
                    self.assertEqual([a.key.identity for a in candidate.actions], ['a', 'b'])
                    self.assertEqual(len(attempt.plan.actions), 2)
                    self.assertTrue(all(len(a.application_ids) == (3 if current else 2)
                                        for a in candidate.actions))
                    result = manager.executions[-2]
                    self.assertTrue(result.form_iterations)
                    self.assertEqual(len(result.iterations), 2)
                    self.assertEqual([child.schema['recordLabelFieldId'] for child in result.iterations],
                                     ['a-name', 'b-name'])
                    if rename:
                        display = manager.executions[-1].display_state.tab_payloads()
                        self.assertEqual([display[k]['label'] for k in ('a', 'b')], ['New A', 'New B'])
                        entries = {row['id']: row for row in display['database:db']}
                        self.assertEqual([entries[k]['label'] for k in ('a', 'b')], ['New A', 'New B'])
                    report = await self.run_prepared(prepared)
                    self.assertEqual(report.status, 'success', report.messages)
                    self.assertEqual(len(service.writes), 2)
                    self.assertEqual(service.moves, [])
                    self.assertEqual(service.schemas['a']['recordLabelFieldId'], 'a-name')
                    self.assertEqual(service.schemas['b']['elements'][-1]['label'], 'B')

    async def test_repeated_scoped_renames_merge_and_compensate(self):
        prepared = await self.prepare('''WithForm(formPattern="A|B")
ModifyForm(currentForm=True, name="First ${form.name}")
ModifyForm(currentForm=True, name="Final ${form.name}")
AddField(fieldCode="NEW", label="New")''')
        self.assertEqual(len(prepared[2].actions), 2)
        self.fail_after_commit(prepared[1], {2})
        report = await self.run_prepared(prepared, 'rollback_all')
        self.assertEqual(report.status, 'rolled_back_all', report.messages)
        self.assertEqual([key for key, _ in prepared[1].writes], ['a', 'b', 'b', 'a'])
        self.assertEqual([prepared[1].schemas[k]['label'] for k in ('a', 'b')], ['A', 'B'])
        self.assertEqual([prepared[1].resources['db'][k]['label'] for k in ('a', 'b')], ['A', 'B'])

    async def test_scoped_move_seals_modify_and_field_edit_candidates(self):
        prepared = await self.prepare('''ModifyForm(currentForm=False, formPattern="A|B", name="New ${form.name}")
AddField(fieldCode="before", label="Before")
MoveForm(currentForm=True, folder="North")
AddField(fieldCode="after", label="After")''')
        actions = prepared[2].actions
        self.assertEqual([a.kind for a in actions], [ActionKind.FORM_SCHEMA_UPDATE,
            ActionKind.FORM_SCHEMA_UPDATE, ActionKind.DATABASE_RESOURCE_PATCH,
            ActionKind.FORM_SCHEMA_UPDATE, ActionKind.FORM_SCHEMA_UPDATE])
        self.assertEqual(len(actions[2].patches), 2)
        self.assertEqual([len(a.application_ids) for a in actions], [2, 2, 2, 1, 1])
        report = await self.run_prepared(prepared)
        self.assertEqual(report.status, 'success', report.messages)
        self.assertEqual(prepared[1].timeline, [('schema', 'a'), ('schema', 'b'),
            ('move', 'a', 'north'), ('move', 'b', 'north'), ('schema', 'a'), ('schema', 'b')])

    async def test_same_name_is_a_noop_without_a_write(self):
        prepared = await self.prepare('ModifyForm(currentForm=False, form="A", name="A")')
        report = await self.run_prepared(prepared)
        self.assertEqual(report.status, 'success')
        self.assertEqual(prepared[1].writes, [])
        self.assertEqual(prepared[4].plan.actions, ())

    async def test_stale_secondary_label_does_not_block_schema_write(self):
        prepared = await self.prepare()
        prepared[1].resources['db']['a']['label'] = 'Other'
        report = await self.run_prepared(prepared)
        self.assertEqual(report.status, 'success')
        self.assertEqual(len(prepared[1].writes), 1)

    async def test_unreadable_secondary_does_not_affect_schema_success(self):
        prepared = await self.prepare()
        service = prepared[1]
        original_read = service.observe_resource
        async def unreadable(key):
            if key.kind == ResourceKind.DATABASE_RESOURCES and service.writes:
                return Observation(key, Existence.UNKNOWN, failure=EndpointFailure(FailureKind.FORBIDDEN, 'Denied'))
            return await original_read(key)
        service.observe_resource = unreadable
        report = await self.run_prepared(prepared)
        self.assertEqual(report.status, 'success')
        self.assertEqual(report.journal[0].postimage.related, ())
        self.assertTrue(all(item.key.kind == ResourceKind.FORM_SCHEMA for item in report.observations))
        self.assertEqual(len(service.writes), 1)

    async def test_secondary_read_failure_is_not_consulted(self):
        prepared = await self.prepare()
        service = prepared[1]
        original_read = service.observe_resource
        failed = False
        async def once(key):
            nonlocal failed
            if key.kind == ResourceKind.DATABASE_RESOURCES and service.writes and not failed:
                failed = True
                raise ValueError('Lost read response')
            return await original_read(key)
        service.observe_resource = once
        report = await self.run_prepared(prepared, 'rollback_all')
        self.assertEqual(report.status, 'success', report.messages)
        self.assertFalse(failed)
        self.assertEqual(len(service.writes), 1)
        self.assertEqual(service.moves, [])

    async def test_schema_success_ignores_mismatched_resource_label(self):
        prepared = await self.prepare()
        service = prepared[1]
        original_write = service.put_form_schema
        async def inconsistent(identity, schema):
            await original_write(identity, schema)
            service.resources['db'][identity]['label'] = 'A'
        service.put_form_schema = inconsistent
        report = await self.run_prepared(prepared, 'rollback_all')
        self.assertEqual(report.status, 'success')
        self.assertEqual(report.journal[0].outcome, 'intended')
        self.assertEqual(len(service.writes), 1)

    async def test_compensation_checks_schema_only(self):
        prepared = await self.prepare()
        self.fail_after_commit(prepared[1], {1})
        async def choose(message):
            prepared[1].resources['db']['a']['label'] = 'Concurrent'
            return 'rollback_all'
        report = await self.run_prepared(prepared, choose)
        self.assertEqual(report.status, 'rolled_back_all')
        self.assertEqual(len(prepared[1].writes), 2)

    async def test_compensation_preserves_unowned_resource_changes(self):
        prepared = await self.prepare()
        service = prepared[1]
        self.fail_after_commit(service, {1})
        async def choose(message):
            service.resources['db']['a']['parentId'] = 'south'
            service.resources['db']['b']['label'] = 'Concurrent B'
            return 'rollback_all'
        report = await self.run_prepared(prepared, choose)
        self.assertEqual(report.status, 'rolled_back_all', report.messages)
        self.assertEqual(service.resources['db']['a']['parentId'], 'south')
        self.assertEqual(service.resources['db']['b']['label'], 'Concurrent B')
        self.assertEqual(service.resources['db']['a']['label'], 'A')

    async def test_recovery_error_stops_even_when_both_representations_are_restored(self):
        prepared = await self.prepare()
        self.fail_after_commit(prepared[1], {1, 2})
        report = await self.run_prepared(prepared, 'rollback_all')
        self.assertEqual(report.status, 'recovery_failed')
        record = report.journal[0].recovery[-1]
        self.assertEqual(record.status, 'verified_restored')
        self.assertEqual(record.observed.state.payload.schema['label'], 'A')
        self.assertIsNotNone(record.evidence.failure)

    async def test_recovery_verifies_schema_only(self):
        prepared = await self.prepare()
        service = prepared[1]
        original_write = service.put_form_schema
        async def wrong_restore(identity, schema):
            await original_write(identity, schema)
            if len(service.writes) == 1:
                raise ValueError('Lost response after commit')
            service.resources['db'][identity]['label'] = 'Wrong'
        service.put_form_schema = wrong_restore
        report = await self.run_prepared(prepared, 'rollback_all')
        self.assertEqual(report.status, 'rolled_back_all')
        self.assertEqual(report.journal[0].recovery[-1].status, 'verified_restored')

    async def test_subform_rename_uses_the_same_shared_label_contract(self):
        schema = dict(fixtures.fixtures.form('a', 'A'), parentFormId='parent')
        prepared = await self.prepare(schemas=[schema])
        report = await self.run_prepared(prepared)
        self.assertEqual(report.status, 'success', report.messages)
        self.assertEqual(prepared[1].resources['db']['a']['type'], 'SUB_FORM')
        self.assertEqual(prepared[1].resources['db']['a']['label'], 'Renamed')

    async def test_unchanged_rename_failure_rolls_back_the_prior_changed_action(self):
        prepared = await self.prepare('AddField(fieldCode="NEW", label="New")\nWithForm(formPattern="A")\nModifyForm(currentForm=False, form="A", name="Renamed")')
        service = prepared[1]
        original_write = service.put_form_schema
        async def reject_second(identity, schema):
            if len(service.writes) == 1:
                service.writes.append((identity, deepcopy(schema)))
                raise ValueError('Rejected before commit')
            await original_write(identity, schema)
        service.put_form_schema = reject_second
        report = await self.run_prepared(prepared, 'rollback_last')
        self.assertEqual(report.status, 'rolled_back_last', report.messages)
        self.assertEqual(report.journal[-1].outcome, 'unchanged')
        self.assertEqual(service.schemas['a']['label'], 'A')
        self.assertEqual(service.schemas['a']['elements'], [])

    async def test_inconsistent_apply_read_is_atomic(self):
        service = fixtures.FolderService([fixtures.fixtures.form('a', 'A')])
        service.resources['db']['a']['label'] = 'Inconsistent'
        with patch.object(fixtures.fixtures, 'MemoryService', return_value=service):
            manager, _ = await fixtures.fixtures.PreviewTests.evaluate(
                self, [fixtures.fixtures.form('a', 'A')], 'ModifyForm(currentForm=False, form="A", name="Renamed")')
        self.assertEqual(manager.executions[0].status, 'error')
        self.assertEqual(manager.workspace.active_schema['label'], 'A')
        self.assertEqual(manager.candidate_plan.actions, ())

    async def test_rename_confirmation_execute_and_report_tabs(self):
        import json
        from types import SimpleNamespace
        from nicegui import ui
        from nicegui.client import Client
        from activity_manager.page import connection_page
        from activity_manager.command_list import CommandListManager
        from activity_manager.command_parser import parse_command_text
        from activity_manager.listbox import DatabaseListbox
        from test.test_executor_page import callback
        service = fixtures.FolderService([fixtures.fixtures.form('a', 'A')])
        install(service)
        service.get_databases = AsyncMock(return_value={'db': 'Database'})
        service.get_forms = AsyncMock(return_value={'a': 'A'})
        service.get_form_tree = AsyncMock(side_effect=lambda identity: deepcopy(service.schemas))
        service.aclose = AsyncMock()
        managers = []
        def create(**kwargs):
            manager = CommandListManager(**kwargs)
            managers.append(manager)
            return manager
        with patch('activity_manager.page.ActivityInfoService', return_value=service), \
                patch('activity_manager.page.CommandListManager', side_effect=create):
            client = Client(ui.page('/'))
            with client, ui.column() as page:
                connection_page()
            with client:
                await callback(next(item for item in page.descendants() if isinstance(item, ui.button) and item.text == 'Connect'))()
                databases, forms = [item for item in page.descendants() if isinstance(item, DatabaseListbox)]
                await callback(databases)(SimpleNamespace(args='db'))
                await callback(forms)(SimpleNamespace(args='a'))
                manager = managers[0]
                manager.commands = parse_command_text('ModifyForm(currentForm=False, form="A", name="Renamed")')
                manager._evaluate_commands()
                await manager._evaluation_task
                manager.on_execute()
                summary = next(item for item in client.layout.descendants()
                               if isinstance(item, ui.label) and 'Execute these updates?' in item.text)
                self.assertIn('Update form structure — A → Renamed', summary.text)
                self.assertNotIn('commands [', summary.text)
                self.assertFalse(any(isinstance(item, ui.code) and item.content.startswith('[\n')
                                     for item in client.layout.descendants()))
                self.assertEqual(service.writes, [])
                await callback([item for item in client.layout.descendants() if isinstance(item, ui.button) and item.text == 'Execute'][-1])()
                self.assertEqual(manager.execution_report.status, 'success', manager.execution_report.messages)
                self.assertTrue(any(isinstance(item, ui.label) and item.text == '\n'.join(manager.execution_report.console_messages)
                                    for item in page.descendants()))
                self.assertEqual(len(service.writes), 1)
                self.assertEqual(service.moves, [])
                await manager._evaluation_task
                service.resources['db']['a']['label'] = 'Later'
                tabs = next(item for item in page.descendants() if isinstance(item, ui.tabs))
                tabs.set_value('a')
                code = next(item for item in page.descendants() if isinstance(item, ui.code))
                self.assertEqual(json.loads(code.content)['label'], 'Renamed')

    async def test_rename_and_pending_record_label_share_the_existing_allocation(self):
        prepared = await self.prepare('AddField(fieldCode="NEW", label="New")\nModifyForm(currentForm=False, form="A", name="Renamed", recordLabelFieldId="NEW")')
        report = await self.run_prepared(prepared)
        self.assertEqual(report.status, 'success', report.messages)
        service = prepared[1]
        self.assertEqual(len(service.writes), 1)
        self.assertEqual(service.writes[0][1]['elements'][0]['id'], service.writes[0][1]['recordLabelFieldId'])
        self.assertFalse(service.writes[0][1]['recordLabelFieldId'].startswith('pending-'))
        self.assertEqual(service.resources['db']['a']['label'], 'Renamed')

    async def test_confirmation_excludes_local_projection_verification(self):
        prepared = await self.prepare()
        candidate = prepared[2]
        action = candidate.actions[0]
        projection = replace(action.projections[0], footprints=action.projections[0].footprints[1:])
        malformed = replace(candidate, actions=(replace(action, projections=(projection,)),))
        attempt = ConfirmationAttempt(malformed, prepared[3])
        self.assertEqual(attempt.plan.actions[0].action.projections, ())
        self.assertEqual(prepared[1].writes, [])
