"""Deletion lifecycle, queue boundaries and version-addressed recovery."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import httpx
from nicegui import ui
from nicegui.client import Client

from activity_manager.applier import Applier
from activity_manager.api import ActivityInfoService
from activity_manager.commands.delete_form import DELETE_FORM
from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_parser import parse_command_text, CommandTextError
from activity_manager.endpoint_evidence import EndpointReceipt, WriteEvidence, EndpointFailure, FailureKind
from activity_manager.executor import Executor
from activity_manager.planning import ActionKind
from activity_manager.schema_resolution import resolve_command_schema
from activity_manager.resource_state import ResourceKey, Lifecycle
from test.test_add_form import CreationService
from test import test_add_form as creation_tests
from test.test_multi_form import form


class DeletionService(CreationService):
    def __init__(self):
        super().__init__()
        self.versions = {}
        self.version = 10
        self.lose_delete_response = False
        self.lose_revert_response = False
        self.partial_delete = False

    async def write_resources_evidence(self, database, payload):
        self.version += 1
        v = str(self.version)
        if 'resourceDeletions' in payload:
            self.versions[v] = (deepcopy(self.resources[database]), deepcopy(self.schemas))
            if self.partial_delete:
                payload = {'resourceDeletions': payload['resourceDeletions'][:1]}
        await super().write_resources_evidence(database, payload)
        if self.lose_delete_response:
            return WriteEvidence(failure=EndpointFailure(FailureKind.UNAVAILABLE, 'lost'))
        return WriteEvidence(EndpointReceipt(200, {}, '', {'version': v + '#1'}, v + '#1'))

    async def revert_database_update_evidence(self, database, version):
        self.calls.append(('revert', version))
        old_resources, old_schemas = self.versions[version]
        for identity, entry in old_resources.items():
            if identity not in self.resources[database]:
                self.resources[database][identity] = deepcopy(entry)
                self.schemas[identity] = deepcopy(old_schemas[identity])
        if self.lose_revert_response:
            return WriteEvidence(failure=EndpointFailure(FailureKind.UNAVAILABLE, 'lost revert'))
        return WriteEvidence()


class DeleteFormTests(unittest.IsolatedAsyncioTestCase):
    attempt = creation_tests.AddFormTests.attempt

    async def apply(self, text, initial='a', service=None):
        service = service or DeletionService()
        app = None
        async def resolve(schema):
            return await resolve_command_schema(service, schema, app.related_schemas, {}, {}, app.resolution_cache,
                source_schema=app.workspace.baseline_forms.get(schema['id']))
        app = Applier(deepcopy(service.schemas[initial]) if initial else None, parse_command_text(text),
                      initial_database_id='db', form_service=service, resolve_schema=resolve)
        await app.evaluate()
        self.assertTrue(all(e.status != 'error' for e in app.executions), [e.message for e in app.executions])
        return app, service

    async def execute(self, app, service):
        attempt, binding, candidate = self.attempt(app)
        executor = Executor(service)
        async def choose(_): return 'rollback_all'
        report = await executor.execute(attempt, binding, candidate, choose)
        return executor, report

    def test_parser_and_creator(self):
        for args in ('currentForm=True', 'currentForm=False, form="A"', 'currentForm=False, formPattern=".*"'):
            command = parse_command_text('DeleteForm(' + args + ')')[0]
            self.assertEqual(parse_command_text(command.to_syntax()), [command])
        for args in ('', 'currentForm=True, form="A"', 'currentForm=True, formPattern=".*"'):
            with self.assertRaises(CommandTextError): parse_command_text('DeleteForm(' + args + ')')
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((DELETE_FORM,), on_add=lambda _: None)
            self.assertFalse(creator.target_mode.visible)
            creator.controls['current_form'].set_value(False)
            self.assertTrue(creator.target_mode.visible)

    async def test_active_context_tombstone_and_missing_targets(self):
        app, _ = await self.apply('DeleteForm(currentForm=False, form="B")')
        self.assertEqual(app.workspace.active_schema['id'], 'a')
        self.assertEqual(app.workspace.state.active_database_id, 'db')
        self.assertNotIn('b', app.workspace.latest)
        tab = next(t for t in app.executions[-1].display_state.tabs() if t.key.identity == 'b')
        self.assertTrue(tab.deleted)
        self.assertNotIn('b', [e['id'] for e in app.executions[-1].display_state.tab_payloads()['database:db']])
        app, _ = await self.apply('DeleteForm(currentForm=True)\nDeleteForm(currentForm=True)\nWithForm(form="A")')
        self.assertIsNone(app.workspace.active_schema)
        self.assertEqual(app.workspace.state.active_database_id, 'db')
        self.assertEqual([e.status for e in app.executions[1:]], ['warning', 'warning'])

    async def test_open_modify_removed_and_prior_closed_modify_retained(self):
        app, service = await self.apply('ModifyForm(currentForm=True, name="Changed")\nDeleteForm(currentForm=True)')
        self.assertEqual([a.kind for a in app.candidate_plan.actions], [ActionKind.FORM_DELETE])
        self.assertEqual(app.candidate_plan.actions[0].before.payload.entries['a']['label'], 'A')
        executor, report = await self.execute(app, service)
        self.assertEqual(report.status, 'success', report.messages)
        report = await executor.recover('rollback_all')
        self.assertEqual(report.status, 'rolled_back_all', report.messages)
        self.assertEqual(service.schemas['a']['label'], 'A')
        app, _ = await self.apply('ModifyForm(currentForm=True, name="Changed")\nWithForm(form="B")\nDeleteForm(currentForm=False, form="Changed")')
        self.assertEqual([a.kind for a in app.candidate_plan.actions], [ActionKind.FORM_SCHEMA_UPDATE, ActionKind.FORM_DELETE])

    async def test_open_add_and_delete_cancel_but_closed_add_remains(self):
        app, _ = await self.apply('AddForm(name="New")\nAddField(fieldCode="X", label="X")\nDeleteForm(currentForm=True)')
        self.assertEqual(app.candidate_plan.actions, ())
        self.assertIsNone(app.workspace.active_schema)
        app, service = await self.apply('AddForm(name="New")\nAddField(fieldCode="X", label="X")\nWithForm(form="A")\nDeleteForm(currentForm=False, form="New")')
        self.assertEqual([a.kind for a in app.candidate_plan.actions], [ActionKind.FORM_CREATE, ActionKind.FORM_DELETE])
        executor, report = await self.execute(app, service)
        self.assertEqual(report.status, 'success', report.messages)
        report = await executor.recover('rollback_all')
        self.assertEqual(report.status, 'rolled_back_all', report.messages)

    async def test_consecutive_and_pattern_deletions_batch_and_revert(self):
        for text in ('DeleteForm(currentForm=False, form="A")\nDeleteForm(currentForm=False, form="B")',
                     'DeleteForm(currentForm=False, formPattern="[AB]")'):
            app, service = await self.apply(text)
            self.assertEqual(len(app.candidate_plan.actions), 1)
            executor, report = await self.execute(app, service)
            self.assertEqual(report.status, 'success', report.messages)
            self.assertEqual(service.calls, [('resources', {'resourceDeletions': ['a', 'b']})])
            report = await executor.recover('rollback_all')
            self.assertEqual(report.status, 'rolled_back_all', report.messages)
            self.assertEqual(service.calls[-1], ('revert', '11'))
            self.assertEqual(set(service.schemas), {'a', 'b'})
            self.assertIsNone(app.scope)

    async def test_current_form_preserves_iterator_and_later_commands_skip(self):
        app, _ = await self.apply('WithForm(formPattern="[AB]")\nDeleteForm(currentForm=True)\nAddField(fieldCode="X", label="X")')
        self.assertEqual(len(app.scope), 2)
        self.assertEqual(len(app.candidate_plan.actions), 1)
        self.assertTrue(all(e.status == 'warning' for e in app.executions[-1].iterations))
        self.assertEqual(app.workspace.latest, {})

    async def test_reference_warning_and_substitutions_disappear(self):
        service = DeletionService()
        service.schemas['b']['elements'].append({'id':'ref', 'code':'REF', 'label':'Reference', 'type':'reference',
            'typeParameters':{'range':[{'formId':'a'}]}})
        app, _ = await self.apply('WithForm(form="B")\nWithForm(form="A")\nDeleteForm(currentForm=True)', service=service)
        self.assertIn('breaks a reference', app.executions[-1].message)
        self.assertNotIn('a', app.executions[-1].display_state.related_schemas)
        self.assertEqual(app.executions[-1].display_state.tab_metadata()['b'][0]['elements'][-1]['typeParameters']['range'][0]['formId'], 'a')

    async def test_lost_delete_receipt_blocks_revert_and_partial_batch_blocks(self):
        for partial in (False, True):
            service = DeletionService()
            service.lose_delete_response = not partial
            service.partial_delete = partial
            app, _ = await self.apply('DeleteForm(currentForm=False, formPattern="[AB]")', service=service)
            _, report = await self.execute(app, service)
            self.assertEqual(report.status, 'recovery_blocked', report.messages)
            self.assertFalse(any(c[0] == 'revert' for c in service.calls))

    async def test_lost_revert_response_verified_by_reads(self):
        app, service = await self.apply('DeleteForm(currentForm=True)')
        executor, report = await self.execute(app, service)
        service.lose_revert_response = True
        report = await executor.recover('rollback_all')
        self.assertEqual(report.status, 'rolled_back_all', report.messages)

    async def test_revert_endpoint_encoding(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'version':'12#1'})
        with patch('httpx.AsyncClient', return_value=httpx.AsyncClient(base_url="https://example.org/resources", transport=httpx.MockTransport(respond))):
            service = ActivityInfoService('https://example.org/resources', 'test')
            evidence = await service.revert_database_update_evidence('db', '11')
            await service.aclose()
        self.assertIsNone(evidence.failure)
        self.assertEqual(evidence.receipt.version, '12#1')
        self.assertTrue(str(requests[0].url).endswith('/databases/db/versions/11/revert'))

    async def test_delete_boundaries_and_unrelated_move_retained(self):
        app, service = await self.apply('MoveForm(currentForm=True, folder="North")\nDeleteForm(currentForm=True)')
        self.assertEqual([a.kind for a in app.candidate_plan.actions], [ActionKind.DATABASE_RESOURCE_PATCH, ActionKind.FORM_DELETE])
        executor, report = await self.execute(app, service)
        self.assertEqual(report.status, 'success', report.messages)
        report = await executor.recover('rollback_all')
        self.assertEqual(report.status, 'rolled_back_all', report.messages)
        app, _ = await self.apply('DeleteForm(currentForm=False, form="B")\nWithForm(form="A")\nDeleteForm(currentForm=True)')
        self.assertEqual(len(app.candidate_plan.actions), 2)

    async def test_explicit_delete_exits_iterator_and_preserves_other_database_context(self):
        service = DeletionService()
        service.schemas['c'] = form('c', 'C', 'remote')
        service.resources['remote'] = {}
        service.resources['remote']['c'] = dict(id='c', label='C', type='FORM', parentId='remote')
        app, _ = await self.apply('WithForm(formPattern="[AB]")\nDeleteForm(currentForm=False, form="Remote/C")\nAddField(fieldCode="X", label="X")', service=service)
        self.assertIsNone(app.scope)
        self.assertEqual(app.workspace.state.active_database_id, 'db')
        self.assertEqual(app.workspace.active_schema['id'], 'b')
        self.assertNotIn('c', app.workspace.latest)

    async def test_reapply_without_delete_restores_selection_and_report_omits_deleted_schema(self):
        app, service = await self.apply('DeleteForm(currentForm=True)')
        async def resolve(schema):
            return schema
        fresh = Applier(service.schemas['a'], parse_command_text('WithForm(form="A")'),
            form_service=service, resolution_cache=app.resolution_cache, resolve_schema=resolve)
        await fresh.evaluate()
        self.assertEqual(fresh.executions[-1].status, 'applied')
        _, report = await self.execute(app, service)
        self.assertNotIn('a', report.actual_schemas)

    async def test_deleted_tab_placeholder_and_creator_suggestions(self):
        from test import test_move_form as move_tests
        manager, creator, service, page = await move_tests.MoveFormPageTests.make_page(self)
        manager.commands = parse_command_text('DeleteForm(currentForm=True)')
        manager._evaluate_commands()
        await manager._evaluation_task
        manager.select_command(0)
        tabs = next(item for item in page.descendants() if isinstance(item, ui.tabs))
        tabs.set_value('a')
        labels = [item for item in page.descendants() if isinstance(item, ui.label)]
        self.assertTrue(any(item.text == 'Form deleted' and item.visible for item in labels))
        deleted_label = next(item for item in labels if item.text == 'A' and 'line-through' in item.classes)
        self.assertIn('text-red-600', deleted_label.classes)
        self.assertNotIn('A', creator.controls.get('form').options if creator.controls.get('form') else [])
        toggle = next(item for item in page.descendants() if isinstance(item, ui.toggle) and 'names' in item.options)
        toggle.set_value('names')
        self.assertTrue(any(item.text == 'Form deleted' and item.visible for item in labels))

    async def test_unmatched_delete_exits_scope_without_breaking_active_form_edits(self):
        app, _ = await self.apply('WithForm(formPattern="[AB]")\nDeleteForm(currentForm=False, formPattern="Missing")\nAddField(fieldCode="X", label="X")')
        self.assertEqual(app.executions[1].status, 'warning')
        self.assertIsNone(app.scope)
        self.assertEqual(app.workspace.active_schema['id'], 'b')
        self.assertEqual(app.executions[-1].status, 'applied')

    async def test_later_failed_write_rolls_back_deletion_with_its_receipt(self):
        service = DeletionService()
        service.reject_schema = True
        app, _ = await self.apply('DeleteForm(currentForm=False, form="B")\nAddField(fieldCode="X", label="X")', service=service)
        _, report = await self.execute(app, service)
        self.assertEqual(report.status, 'rolled_back_all', report.messages)
        self.assertIn(('revert', '11'), service.calls)
        self.assertIn('b', service.schemas)
