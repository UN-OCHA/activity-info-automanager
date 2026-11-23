"""Behavioral coverage of evaluation without UI orchestration and atomic failures."""

import asyncio
from dataclasses import dataclass
import unittest
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client

from activity_manager.applier import ACTIVE_DRAFT, Applier
from activity_manager.command_execution import Diagnostic, ExecutionResult
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import parse_command_text
from activity_manager.immutable import thaw
from activity_manager.resource_state import ResourceKey
from activity_manager.page import connection_page
from test.test_multi_form import MemoryService, form


@dataclass(frozen=True)
class MutatingFailure:
    raises: bool = False

    def to_syntax(self):
        return f'MutatingFailure(raises={self.raises!r})'

    def execute(self, schema, *, context=None):
        schema['label'] = 'Rejected'
        context.variables['vars.keep'] = 'Rejected'
        context.related_schemas['related']['label'] = 'Rejected'
        context.database_names['db'] = 'Rejected'
        context.reserved_identities.add('pending-field:rejected')
        if self.raises:
            raise RuntimeError('Transformation failed')
        return ExecutionResult(schema, (Diagnostic('error', 'Transformation failed', ('label',)),))


class HeadlessApplierTests(unittest.TestCase):
    def test_local_evaluation_publishes_effects_without_ui_or_real_id_allocation(self):
        commands = parse_command_text('AddField(fieldCode="new", label="New")\nDeleteField(fieldCode="missing")')
        with patch('nicegui.ui.column', side_effect=AssertionError('No UI orchestration')):
            engine = Applier(form('a', 'A'), commands)
            engine.evaluate_local()
        self.assertEqual([result.status for result in engine.results], ['applied', 'warning'])
        self.assertEqual(len(engine.effects), 1)
        effect = engine.effects[0]
        self.assertEqual(effect.application_id, engine.results[0].applications[0].command.application_id)
        self.assertEqual(engine.results[0].applications[0].effect_ids, (effect.effect_id,))
        self.assertEqual(effect.before.payload.to_dict()['elements'], [])
        self.assertEqual(effect.after.payload.to_dict()['elements'][0]['id'], 'pending-field:new')

    def test_returned_and_raised_transformation_failures_discard_all_local_changes(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                source = form('a', 'A', elements=[{'id': 'existing', 'code': 'old', 'type': 'FREE_TEXT'}])
                engine = Applier(source, (*parse_command_text(
                    'SetVariable(name="keep", method="literal", value="Kept")'), MutatingFailure(raises)),
                    related_schemas={'related': form('related', 'Related')}, database_names={'db': 'Database'})
                engine.evaluate_local()
                self.assertEqual(engine.workspace.active_schema, source)
                self.assertEqual(source['label'], 'A')
                self.assertEqual(engine.variables, {'vars.keep': 'Kept'})
                self.assertEqual(thaw(engine.workspace.state.variables), engine.variables)
                self.assertEqual(engine.related_schemas['related']['label'], 'Related')
                self.assertEqual(engine.database_names['db'], 'Database')
                self.assertNotIn('pending-field:rejected', engine.workspace.reserved['a'])
                self.assertEqual(engine.effects, ())
                self.assertEqual(engine.results[-1].applications[0].rejected_candidate.candidate['label'], 'Rejected')

    def test_substitution_is_single_pass_in_headless_evaluation(self):
        dollar = '$'
        commands = parse_command_text(
            f'SetVariable(name="value", method="literal", value="{dollar}{dollar}{{vars.undefined}}")\n'
            f'AddField(fieldCode="new", label="{dollar}{{vars.value}}")')
        engine = Applier(form('a', 'A'), commands)
        engine.evaluate_local()
        self.assertEqual(engine.workspace.active_schema['elements'][0]['label'], dollar + '{vars.undefined}')
        self.assertEqual(engine.results[-1].status, 'applied')


class AsyncApplierTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolution_error_discards_schema_identities_effects_and_context(self):
        source = form('a', 'A')
        calls = 0
        async def resolve(data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ExecutionResult(data)
            draft = ACTIVE_DRAFT.get()
            draft.variables['vars.leak'] = 'Rejected'
            draft.reference_defaults['leak'] = 'Rejected'
            draft.related_schemas['rejected'] = form('rejected', 'Rejected')
            draft.database_names['db'] = 'Rejected'
            draft.resolution_cache['successful-read'] = {'id': 'read-evidence'}
            return ExecutionResult(data, (Diagnostic('error', 'Bad reference', ('elements', 1, 'label')),))
        engine = Applier(source, parse_command_text(
            'AddField(fieldCode="kept", label="Kept")\nAddField(fieldCode="rejected", label="Rejected")\n'
            'AddField(fieldCode="later", label="Later")'), resolve_schema=resolve)
        await engine.evaluate()
        self.assertEqual([item.status for item in engine.results], ['applied', 'error', 'not_executed'])
        failed = engine.results[1].applications[0]
        self.assertEqual([item['code'] for item in engine.workspace.active_schema['elements']], ['kept'])
        self.assertNotIn('pending-field:rejected', engine.workspace.reserved['a'])
        self.assertEqual(engine.variables, {})
        self.assertNotIn('leak', engine.reference_defaults)
        self.assertNotIn('rejected', engine.related_schemas)
        self.assertNotIn('db', engine.database_names)
        self.assertIn('successful-read', engine.resolution_cache)
        self.assertEqual(len(engine.effects), 1)
        self.assertEqual(failed.effect_ids, ())
        self.assertEqual(failed.display_state.resources, engine.results[0].display_state.resources)
        self.assertEqual(engine.executions[1].issues, ())
        self.assertEqual(len(failed.rejected_candidate.candidate['elements']), 2)

    async def test_capture_failure_discards_already_staged_schema_effects(self):
        async def resolve(data):
            if any(field.get('code') == 'rejected' for field in data['elements']):
                ACTIVE_DRAFT.get().database_names['invalid'] = object()
            return ExecutionResult(data)
        engine = Applier(form('a', 'A'), parse_command_text(
            'AddField(fieldCode="kept", label="Kept")\nAddField(fieldCode="rejected", label="Rejected")'),
            resolve_schema=resolve)
        await engine.evaluate()
        self.assertEqual([result.status for result in engine.results], ['applied', 'error'])
        self.assertEqual([field['code'] for field in engine.workspace.active_schema['elements']], ['kept'])
        self.assertEqual(len(engine.effects), 1)
        self.assertNotIn('pending-field:rejected', engine.workspace.reserved['a'])
        self.assertNotIn('invalid', engine.database_names)
        failed = engine.results[1].applications[0]
        self.assertEqual(failed.effect_ids, ())
        self.assertEqual(len(failed.rejected_candidate.candidate['elements']), 2)

    async def test_retained_draft_references_cannot_mutate_published_state(self):
        retained = []
        async def resolve(data):
            retained.append(ACTIVE_DRAFT.get())
            return ExecutionResult(data)
        engine = Applier(form('a', 'A'), parse_command_text('AddField(fieldCode="kept", label="Kept")'),
            related_schemas={'related': form('related', 'Related')}, resolve_schema=resolve)
        await engine.evaluate()
        draft = retained[0]
        changed = draft.workspace.active_schema
        changed['label'] = 'Changed after publication'
        draft.workspace.update(changed)
        draft.related_schemas['related']['label'] = 'Changed after publication'
        draft.reference_defaults['leak'] = 'Changed after publication'
        draft.variables['vars.leak'] = 'Changed after publication'
        draft.resolution_cache['leak'] = 'Changed after publication'
        self.assertEqual(engine.workspace.active_schema['label'], 'A')
        self.assertEqual(engine.related_schemas['related']['label'], 'Related')
        self.assertNotIn('leak', engine.reference_defaults)
        self.assertNotIn('leak', engine.resolution_cache)
        self.assertEqual(engine.variables, {})
        self.assertEqual(engine.results[0].display_state.form_payloads()['a']['label'], 'A')

    async def test_raised_resolution_error_retains_the_transformed_candidate(self):
        engine = Applier(form('a', 'A'), parse_command_text('AddField(fieldCode="rejected", label="Rejected")'),
                         resolve_schema=AsyncMock(side_effect=RuntimeError('Resolver failed')))
        await engine.evaluate()
        result = engine.results[0].applications[0]
        self.assertEqual(result.status, 'error')
        self.assertEqual(engine.workspace.active_schema['elements'], [])
        self.assertEqual(result.rejected_candidate.candidate['elements'][0]['code'], 'rejected')
        self.assertEqual(engine.effects, ())

    async def test_postprocessing_failure_does_not_publish_lookup_variable(self):
        async def lookup(command, schema):
            # Fail during immutable display capture, after SetVariable stored its value.
            ACTIVE_DRAFT.get().database_names['invalid'] = object()
            return [{'value': 'Rejected'}]
        engine = Applier(form('a', 'A'), parse_command_text(
            'SetVariable(name="keep", method="literal", value="Kept")\n'
            'SetVariable(name="keep", method="lookup", referencedForm="A", filter="TRUE", returnFormula="x")'),
            resolve_schema=AsyncMock(side_effect=lambda schema: ExecutionResult(schema)), lookup_variable=lookup)
        await engine.evaluate()
        self.assertEqual(engine.results[-1].status, 'error')
        self.assertEqual(engine.variables, {'vars.keep': 'Kept'})
        self.assertNotIn('invalid', engine.database_names)
        self.assertEqual(engine.effects, ())

    async def test_failed_scope_switch_retains_outer_variables_and_clears_form_context(self):
        forms = [form('root', 'Root'), form('a', 'A'), form('b', 'B')]
        service = MemoryService(forms)
        engine = Applier(forms[0], parse_command_text(
            'SetVariable(name="x", method="literal", value="Outer")\n'
            'WithForm(formPattern="A|B")\n'
            'SetVariable(name="x", method="literal", value="Scoped")\n'
            'WithForm(form="Missing")'), form_service=service,
            resolve_schema=AsyncMock(side_effect=lambda schema: ExecutionResult(schema)))
        await engine.evaluate()
        self.assertEqual(engine.results[-1].status, 'warning')
        self.assertEqual(engine.variables, {'vars.x': 'Outer'})
        self.assertIsNone(engine.scope)
        state = engine.workspace.state
        self.assertFalse(state.active_scope)
        self.assertIsNone(state.active_form_key)
        self.assertEqual(state.variables['vars.x'], 'Outer')
        self.assertFalse(state.scoped_variables)

    async def test_failed_b_application_retains_a_state_and_b_target_metadata(self):
        forms = [form('root', 'Root'), form('a', 'A'), form('b', 'B')]
        async def resolve(data):
            if data['id'] == 'b':
                return ExecutionResult(data, (Diagnostic('error', 'B rejected', ('elements', 0)),))
            return ExecutionResult(data)
        service = MemoryService(forms)
        engine = Applier(forms[0], parse_command_text(
            'WithForm(formPattern="A|B")\nAddField(fieldCode="new", label="New")'),
            form_service=service, resolve_schema=resolve)
        await engine.evaluate()
        applications = engine.results[1].applications
        self.assertEqual([item.status for item in applications], ['applied', 'error'])
        self.assertEqual(applications[1].command.targets, (ResourceKey.form('b'),))
        self.assertEqual(applications[0].display_state.resources, applications[1].display_state.resources)
        self.assertEqual(engine.workspace.state.active_form_key, ResourceKey.form('a'))
        self.assertEqual(engine.workspace.latest['b']['elements'], [])
        self.assertEqual(len(engine.effects), 1)
        self.assertEqual(service.writes, [])

    async def test_cancellation_ignoring_resolver_cannot_publish(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def resolve(data):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return ExecutionResult(data)
        engine = Applier(form('a', 'A'), parse_command_text('AddField(fieldCode="new", label="New")'),
                         resolve_schema=resolve)
        task = asyncio.create_task(engine.evaluate())
        await entered.wait()
        engine.cancel()
        task.cancel()
        release.set()
        await task
        self.assertEqual(engine.workspace.active_schema['elements'], [])
        self.assertEqual(engine.effects, ())

    async def test_stale_legacy_callback_cannot_mutate_the_new_manager_context(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def resolve(data):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                manager.related_schemas['stale'] = form('stale', 'Stale')
                manager.reference_defaults['stale'] = 'Stale'
                manager.resolution_cache['stale'] = 'Stale'
                changed = manager.workspace.active_schema
                changed['label'] = 'Stale'
                manager.workspace.update(changed)
            return ExecutionResult(data)
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(resolve_schema=resolve)
        manager.set_schema(form('a', 'A'))
        manager.commands = parse_command_text('AddField(fieldCode="old", label="Old")')
        manager._evaluate_commands()
        await entered.wait()
        old_task, old_engine = manager._evaluation_task, manager.applier
        manager.commands = parse_command_text('AddField(fieldCode="new", label="New")')
        manager._evaluate_commands()
        await manager._evaluation_task
        release.set()
        await old_task
        self.assertEqual(manager.workspace.active_schema['label'], 'A')
        self.assertEqual(manager.workspace.active_schema['elements'][0]['code'], 'new')
        self.assertNotIn('stale', manager.related_schemas)
        self.assertNotIn('stale', manager.reference_defaults)
        self.assertNotIn('stale', manager.resolution_cache)
        self.assertEqual(old_engine.effects, ())

    async def test_rejected_candidate_ui_does_not_annotate_the_successful_schema(self):
        managers = []
        def create(**kwargs):
            manager = CommandListManager(**kwargs)
            managers.append(manager)
            return manager
        with patch('activity_manager.page.CommandListManager', side_effect=create):
            with Client(ui.page('/')), ui.column() as container:
                connection_page()
        manager = managers[0]
        manager.resolve_schema = AsyncMock(side_effect=lambda data: ExecutionResult(
            data, (Diagnostic('error', 'Bad label', ('elements', 0, 'label')),)))
        manager.set_schema(form('a', 'A'))
        manager.commands = parse_command_text('AddField(fieldCode="rejected", label="Rejected")')
        manager._evaluate_commands()
        await manager._evaluation_task
        manager.select_command(0)
        main = next(item for item in container.descendants()
                    if isinstance(item, ui.code) and item.props.get('aria-label') == 'Form schema')
        candidate = next(item for item in container.descendants()
                         if isinstance(item, ui.code) and item.props.get('aria-label') == 'Rejected candidate')
        self.assertNotIn('rejected', main.content)
        self.assertNotIn('<mark>', main.markdown._props['innerHTML'])
        self.assertIn('rejected', candidate.content)
        self.assertIn('<mark>', candidate.markdown._props['innerHTML'])
