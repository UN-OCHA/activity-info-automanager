"""Resource ownership, batching and capability gates through the real Applier."""
import unittest
from activity_manager.confirmed_plan import AttemptBinding, materialize_plan
from activity_manager.planning import ActionKind, PlanError
from activity_manager.resource_state import ResourceKind
from test import test_move_form as fixtures
from test.form_command_runtime import execute


class ResourceExecutionTests(unittest.IsolatedAsyncioTestCase):
    evaluate = fixtures.MoveFormTests.evaluate

    async def test_adjacent_moves_batch_before_schema_actions_without_schema_metadata(self):
        manager, service = await self.evaluate('MoveForm(formPattern="A|B", folder="North")\nAddField(fieldCode="NEW", label="New")')
        plan = materialize_plan(manager.candidate_plan, AttemptBinding('evaluation', {}, 'session'))
        self.assertEqual([item.action.kind for item in plan.actions], [ActionKind.DATABASE_RESOURCE_PATCH,
            ActionKind.FORM_SCHEMA_UPDATE, ActionKind.FORM_SCHEMA_UPDATE])
        self.assertEqual(len(plan.actions[0].request.schema['resourceUpdates']), 2)
        self.assertEqual(set(plan.actions[0].request.schema), {'resourceUpdates'})
        self.assertTrue(all('__activity_manager' not in str(item.request.schema) for item in plan.actions))
        report = await execute(manager, service)
        self.assertEqual(report.status, 'success')
        self.assertEqual(len(report.journal), 3)

    async def test_partial_batch_is_divergent_and_not_owned_for_compensation(self):
        manager, service = await self.evaluate('MoveForm(formPattern="A|B", folder="North")')
        service.reject_move = 2
        report = await execute(manager, service)
        self.assertEqual(report.status, 'recovery_blocked')
        self.assertEqual(report.journal[0].outcome, 'divergent')
        self.assertEqual(service.resources['db']['a']['parentId'], 'north')
        self.assertEqual(len(service.moves), 2)

    async def test_owned_property_compensation_preserves_unrelated_concurrent_labels(self):
        manager, service = await self.evaluate('MoveForm(form="A", folder="North")')
        original = service.move_form
        async def change_label(database, identity, parent):
            await original(database, identity, parent)
            if len(service.moves) == 1:
                service.resources[database][identity]['label'] = 'Concurrent label'
                raise ValueError('Lost response after commit')
        service.move_form = change_label
        report = await execute(manager, service)
        self.assertEqual(report.status, 'rolled_back_all')
        self.assertEqual(service.resources['db']['a']['parentId'], 'db')
        self.assertEqual(service.resources['db']['a']['label'], 'Concurrent label')
        request = report.journal[0].recovery[-1].request.schema
        self.assertEqual(set(request['resourceUpdates'][0]), {'id', 'parentId'})

    async def test_destination_type_changes_block_preflight(self):
        manager, service = await self.evaluate('MoveForm(form="A", folder="North")')
        service.resources['db']['north']['type'] = 'FORM'
        report = await execute(manager, service)
        self.assertEqual(report.status, 'preflight_failed')
        self.assertEqual(service.moves, [])

    async def test_unrelated_resource_edits_do_not_invalidate_owned_preflight(self):
        manager, service = await self.evaluate('MoveForm(form="A", folder="North")')
        service.resources['db']['b']['label'] = 'Concurrent B'
        report = await execute(manager, service)
        self.assertEqual(report.status, 'success')
        self.assertEqual(service.resources['db']['b']['label'], 'Concurrent B')

    async def test_local_rename_projects_both_labels_and_emits_one_schema_request(self):
        manager, service = await self.evaluate('ModifyForm(form="A", name="Renamed")')
        self.assertEqual(manager.executions[0].status, 'applied', manager.executions[0].message)
        snapshot = manager.executions[0].display_state
        resources = next(item for item in snapshot.resources if item.key.kind == ResourceKind.DATABASE_RESOURCES)
        self.assertEqual(resources.state.payload.entries['a']['label'], 'Renamed')
        self.assertEqual(manager.workspace.active_schema['label'], 'Renamed')
        plan = materialize_plan(manager.candidate_plan, AttemptBinding('evaluation', {}, 'session'))
        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.actions[0].action.kind, ActionKind.FORM_SCHEMA_UPDATE)
        report = await execute(manager, service)
        self.assertEqual(report.status, 'success', report.messages)
        self.assertEqual(service.moves, [])
        self.assertEqual(len(service.writes), 1)
