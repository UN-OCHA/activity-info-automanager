"""Behavioral action-order/provenance coverage; no endpoint writes."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import AsyncMock, patch

from activity_manager.applier import ACTIVE_DRAFT, Applier
from activity_manager.command_execution import ExecutionResult
from activity_manager.command_parser import parse_command_text
from activity_manager.planning import (
    ActionKind, MemberRef, PlanBuilder, PlanError, ResourcePatchEffect, SchemaEffect,
)
from activity_manager.resource_state import (
    BaselineEvidence, DatabaseResourcesPayload, Existence, FormPayload, LoadStatus,
    ResourceKey, ResourceState,
)
from test.test_multi_form import MemoryService, form


def schema_state(identity='a', fields=(), revision=0):
    return ResourceState(ResourceKey.form(identity),
        FormPayload(form(identity, identity.upper(), elements=fields)), revision, LoadStatus.LOADED)


def field(identity, code=None):
    return {'id': identity, 'code': code or identity, 'type': 'FREE_TEXT', 'label': code or identity}


def schema_effect(before, fields, label, **kwargs):
    data = before.payload.to_dict()
    data['elements'] = deepcopy(list(fields))
    after = replace(before, payload=FormPayload(data), revision=before.revision + 1)
    return SchemaEffect(label, label, before.key, before, after, **kwargs)


def database_state():
    return ResourceState(ResourceKey.database('db'), DatabaseResourcesPayload({
        'a': {'id': 'a', 'type': 'FORM', 'parentId': 'db', 'label': 'A', 'permissions': {'edit': True}},
        'b': {'id': 'b', 'type': 'FORM', 'parentId': 'db', 'label': 'B'},
        'folder': {'id': 'folder', 'type': 'FOLDER', 'parentId': 'db'},
    }), load_status=LoadStatus.LOADED)


def resource_effect(before, changes, label, **kwargs):
    entries = {identity: dict(entry) for identity, entry in before.payload.entries.items()}
    for (identity, prop), value in changes.items():
        entries[identity][prop] = value
    after = replace(before, payload=DatabaseResourcesPayload(entries, before.payload.order),
                    revision=before.revision + 1)
    return ResourcePatchEffect(label, label, before.key, before, after, tuple(changes), **kwargs)


class PlanBuilderTests(unittest.TestCase):
    def test_temporary_field_from_an_earlier_action_requires_persisted_deletion(self):
        builder = PlanBuilder(active_form=ResourceKey.form('a'))
        initial = schema_state()
        add_x = schema_effect(initial, [field('pending-field:x')], 'add-x')
        builder.add(add_x)
        creator = builder.snapshot().actions[0].action_id
        database = database_state()
        builder.add(resource_effect(database, {('a', 'parentId'): 'folder'}, 'move-a',
            subjects=(initial.key,), prerequisites=(MemberRef(database.key, 'folder'),)))
        add_y = schema_effect(add_x.after, [field('pending-field:x'), field('pending-field:y')], 'add-y')
        builder.add(add_y)
        builder.add(schema_effect(add_y.after, [field('pending-field:y')], 'delete-x'))
        plan = builder.snapshot()
        self.assertEqual([action.kind for action in plan.actions], [
            ActionKind.FORM_SCHEMA_UPDATE, ActionKind.DATABASE_RESOURCE_PATCH,
            ActionKind.FORM_SCHEMA_UPDATE, ActionKind.PERSISTED_FIELD_DELETE])
        self.assertEqual(plan.actions[-1].deleted_member_id, 'pending-field:x')
        self.assertIn('pending-field:x', plan.actions[2].start_member_ids)
        self.assertNotIn('pending-field:x', plan.actions[2].created_member_ids)
        self.assertEqual(next(row.creator_action_id for row in plan.member_provenance
                              if row.member == MemberRef(initial.key, 'pending-field:x')), creator)
        self.assertIn(creator, plan.actions[-1].dependency_ids)
        self.assertEqual(plan.actions[-1].before, add_y.after)
        self.assertEqual(plan.actions[-1].recovery_capability, 'schema_only_data_unverified')

    def test_pending_prefix_in_a_baseline_does_not_prove_same_action_creation(self):
        initial = schema_state(fields=[field('pending-field:persisted')])
        builder = PlanBuilder(active_form=initial.key)
        builder.observe([BaselineEvidence(initial.key, Existence.PRESENT, initial)])
        builder.add(schema_effect(initial, [], 'delete'))
        action = builder.snapshot().actions[0]
        self.assertEqual(action.kind, ActionKind.PERSISTED_FIELD_DELETE)
        self.assertEqual(action.deleted_member_id, 'pending-field:persisted')
        self.assertIsNone(builder.snapshot().member_provenance[0].creator_action_id)

    def test_same_action_add_delete_eliminates_write_without_mutating_old_plan(self):
        builder = PlanBuilder(active_form=ResourceKey.form('a'))
        added = schema_effect(schema_state(), [field('pending-field:x')], 'add')
        builder.add(added)
        old = builder.snapshot()
        builder.add(schema_effect(added.after, [], 'delete'))
        final = builder.snapshot()
        self.assertEqual(final.actions, ())
        self.assertEqual(len(final.omitted_actions), 1)
        self.assertEqual(final.action_ids_for_application('add'), ())
        self.assertEqual(old.actions[0].after.payload.schema['elements'][0]['id'], 'pending-field:x')
        self.assertEqual(final.omitted_actions[0].action_id, old.actions[0].action_id)
        self.assertEqual(final.effect_order, ('add', 'delete'))
        self.assertEqual(final.omitted_actions[0].application_ids, ('add', 'delete'))
        with self.assertRaises(FrozenInstanceError):
            old.actions[0].after = added.before
        with self.assertRaises(TypeError):
            old.actions[0].after.payload.schema['elements'][0]['label'] = 'Mutation'

    def test_persisted_deletions_are_isolated_and_later_edits_use_their_result(self):
        initial = schema_state(fields=[field('x'), field('y')])
        builder = PlanBuilder(active_form=initial.key)
        delete_x = schema_effect(initial, [field('y')], 'delete-x')
        builder.add(delete_x)
        delete_y = schema_effect(delete_x.after, [], 'delete-y')
        builder.add(delete_y)
        added = schema_effect(delete_y.after, [field('pending-field:z')], 'add-z')
        builder.add(added)
        actions = builder.snapshot().actions
        self.assertEqual([action.kind for action in actions],
                         [ActionKind.PERSISTED_FIELD_DELETE] * 2 + [ActionKind.FORM_SCHEMA_UPDATE])
        self.assertEqual([action.deleted_member_id for action in actions], ['x', 'y', None])
        self.assertEqual(actions[-1].before, delete_y.after)
        self.assertEqual(actions[1].dependency_ids, (actions[0].action_id,))
        self.assertEqual(len({action.group_id for action in actions}), 3)

    def test_adjacent_database_patches_batch_owned_properties_and_preserve_other_data(self):
        initial = database_state()
        builder = PlanBuilder()
        move_a = resource_effect(initial, {('a', 'parentId'): 'folder'}, 'move-a',
                                 subjects=(ResourceKey.form('a'),))
        builder.add(move_a)
        earlier = builder.snapshot()
        move_b = resource_effect(move_a.after, {('b', 'parentId'): 'folder'}, 'move-b',
            subjects=(ResourceKey.form('b'),), prerequisites=(MemberRef(initial.key, 'folder'),))
        builder.add(move_b)
        action, = builder.snapshot().actions
        self.assertEqual(action.kind, ActionKind.DATABASE_RESOURCE_PATCH)
        self.assertEqual(action.application_ids, ('move-a', 'move-b'))
        self.assertEqual(action.subjects, (ResourceKey.form('a'), ResourceKey.form('b')))
        self.assertEqual([(item.member_id, item.path) for item in action.write_set],
                         [('a', ('parentId',)), ('b', ('parentId',))])
        self.assertTrue(any(item.member_id == 'folder' and item.existence for item in action.read_set))
        self.assertTrue(action.after.payload.entries['a']['permissions']['edit'])
        self.assertEqual(earlier.actions[0].after.payload.entries['b']['parentId'], 'db')
        self.assertEqual(action.before, initial)

    def test_schema_effects_prevent_patch_reuse_across_endpoint_boundaries(self):
        builder = PlanBuilder()
        move = resource_effect(database_state(), {('a', 'parentId'): 'folder'}, 'move')
        builder.add(move)
        schema = schema_effect(schema_state(), [field('new')], 'schema')
        builder.add(schema)
        back = resource_effect(move.after, {('a', 'parentId'): 'db'}, 'back')
        builder.add(back)
        actions = builder.snapshot().actions
        self.assertEqual([action.kind for action in actions],
                         [ActionKind.DATABASE_RESOURCE_PATCH, ActionKind.FORM_SCHEMA_UPDATE,
                          ActionKind.DATABASE_RESOURCE_PATCH])
        self.assertEqual(actions[-1].dependency_ids, (actions[0].action_id,))
        self.assertEqual(len({action.group_id for action in actions}), 3)

    def test_patch_represents_missing_null_and_nested_scalar_types_explicitly(self):
        builder = PlanBuilder()
        initial = database_state()
        set_null = resource_effect(initial, {('a', 'optional'): None, ('b', 'flags'): [False]}, 'optional')
        builder.add(set_null)
        action, = builder.snapshot().actions
        optional = next(patch for patch in action.patches if patch.property_name == 'optional')
        self.assertFalse(optional.before_present)
        self.assertTrue(optional.after_present)
        self.assertIsNone(optional.after_value)
        bool_before = set_null.after
        builder.add(resource_effect(bool_before, {('b', 'flags'): [0]}, 'numeric'))
        numeric = next(patch for patch in builder.snapshot().actions[0].patches if patch.property_name == 'flags')
        self.assertEqual(numeric.after_value, (0,))
        entries = {identity: dict(entry) for identity, entry in set_null.after.payload.entries.items()}
        del entries['a']['optional']
        cleared = replace(set_null.after, payload=DatabaseResourcesPayload(entries))
        clearing = PlanBuilder()
        clearing.add(ResourcePatchEffect('clear', 'clear', cleared.key, set_null.after, cleared, (('a', 'optional'),)))
        patch, = clearing.snapshot().actions[0].patches
        self.assertTrue(patch.before_present)
        self.assertFalse(patch.after_present)
        self.assertIsNone(patch.before_value)

    def test_bad_predecessor_or_undeclared_patch_changes_leave_builder_unchanged(self):
        builder = PlanBuilder()
        move = resource_effect(database_state(), {('a', 'parentId'): 'folder'}, 'move')
        builder.add(move)
        before = builder.snapshot()
        with self.assertRaises(PlanError):
            builder.add(resource_effect(database_state(), {('b', 'parentId'): 'folder'}, 'wrong-predecessor'))
        bad = resource_effect(move.after, {('b', 'parentId'): 'folder', ('a', 'label'): 'Unexpected'}, 'bad')
        with self.assertRaises(PlanError):
            builder.add(replace(bad, changes=(('b', 'parentId'),)))
        self.assertEqual(builder.snapshot(), before)

    def test_explicit_creator_prerequisite_splits_without_general_dependency_analysis(self):
        builder = PlanBuilder()
        a, b = schema_state('a'), schema_state('b')
        builder.begin_scope('scope', (a.key, b.key))
        first_a = schema_effect(a, [field('new-a')], 'A1')
        first_b = schema_effect(b, [field('new-b')], 'B1')
        builder.add(first_a)
        builder.add(first_b)
        later_a = schema_effect(first_a.after, [field('new-a', 'renamed')], 'A2',
            prerequisites=(MemberRef(b.key, 'new-b'),))
        builder.add(later_a)
        actions = builder.snapshot().actions
        self.assertEqual([action.application_ids for action in actions], [('A1',), ('B1',), ('A2',)])
        self.assertIn(actions[1].action_id, actions[2].dependency_ids)
        self.assertIn(actions[0].action_id, actions[2].dependency_ids)

    def test_declared_external_use_prevents_eliminating_its_creator(self):
        builder = PlanBuilder()
        a, b = schema_state('a'), schema_state('b')
        builder.begin_scope('scope', (a.key, b.key))
        add_a = schema_effect(a, [field('new-a')], 'A1')
        builder.add(add_a)
        builder.add(schema_effect(b, [field('new-b')], 'B1', prerequisites=(MemberRef(a.key, 'new-a'),)))
        builder.add(schema_effect(add_a.after, [], 'delete-a'))
        actions = builder.snapshot().actions
        self.assertEqual([action.kind for action in actions],
                         [ActionKind.FORM_SCHEMA_UPDATE, ActionKind.FORM_SCHEMA_UPDATE,
                          ActionKind.PERSISTED_FIELD_DELETE])
        self.assertEqual(actions[1].dependency_ids, (actions[0].action_id,))
        self.assertEqual(actions[-1].deleted_member_id, 'new-a')

    def test_unknown_explicit_prerequisite_or_identity_reuse_does_not_mutate_plan(self):
        builder = PlanBuilder()
        initial = schema_state()
        added = schema_effect(initial, [field('x')], 'add')
        with self.assertRaises(PlanError):
            builder.add(replace(added, prerequisites=(MemberRef(ResourceKey.form('unknown'), 'x'),)))
        self.assertEqual(builder.snapshot().actions, ())
        builder.add(added)
        deleted = schema_effect(added.after, [], 'delete')
        builder.add(deleted)
        saved = builder.snapshot()
        with self.assertRaises(PlanError):
            builder.add(schema_effect(deleted.after, [field('x')], 'reuse'))
        self.assertEqual(builder.snapshot(), saved)


class PlanApplierTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, source, text, *, service=None, resolver=None, progress=None):
        engine = Applier(source, parse_command_text(text),
            form_service=service, resolve_schema=resolver or AsyncMock(side_effect=lambda data: ExecutionResult(data)),
            on_progress=progress)
        await engine.evaluate()
        return engine

    async def test_command_first_application_accumulates_form_first_actions_and_provenance(self):
        forms = [form('root', 'Root'), form('a', 'A'), form('b', 'B')]
        service = MemoryService(forms)
        trace = []
        async def resolve(data):
            trace.append((data['id'], len(data['elements'])))
            return ExecutionResult(data)
        engine = await self.evaluate(forms[0],
            'WithForm(formPattern="A|B")\nAddField(fieldCode="x", label="X")\nAddField(fieldCode="y", label="Y")',
            service=service, resolver=resolve)
        self.assertEqual(trace, [('a', 1), ('b', 1), ('a', 2), ('b', 2)])
        plan = engine.candidate_plan
        self.assertEqual([action.key.identity for action in plan.actions], ['a', 'b'])
        self.assertEqual([len(action.after.payload.schema['elements']) for action in plan.actions], [2, 2])
        self.assertEqual(len(plan.groups), 1)
        self.assertEqual(len(plan.actions[0].application_ids), 2)
        for result in engine.results[1:]:
            for application in result.applications:
                self.assertEqual(len(application.action_ids), 1)
                self.assertIn(application.command.application_id,
                    next(action for action in plan.actions if action.action_id == application.action_ids[0]).application_ids)
        self.assertEqual(len(plan.application_order), 6)
        self.assertEqual(len(plan.effect_order), 4)
        self.assertEqual({evidence.key for evidence in plan.baselines}, {ResourceKey.form('a'), ResourceKey.form('b')})
        self.assertEqual(service.writes, [])

    async def test_scope_exit_same_form_and_ordered_revisits_seal_candidates(self):
        a, b = form('a', 'A'), form('b', 'B')
        service = MemoryService([a, b])
        engine = await self.evaluate(a,
            'AddField(fieldCode="x", label="X")\nWithForm(form="A")\nAddField(fieldCode="y", label="Y")\n'
            'WithForm(form="B")\nWithForm(form="A")\nAddField(fieldCode="z", label="Z")',
            service=service)
        self.assertEqual([len(action.after.payload.schema['elements']) for action in engine.candidate_plan.actions], [2, 3])
        scoped = await self.evaluate(a,
            'WithForm(formPattern="A|B")\nAddField(fieldCode="x", label="X")\n'
            'WithForm(form="B")\nAddField(fieldCode="y", label="Y")', service=service)
        self.assertEqual([action.key.identity for action in scoped.candidate_plan.actions], ['a', 'b', 'b'])
        self.assertTrue(scoped.candidate_plan.groups[0].sealed)

    async def test_context_variables_and_zero_matches_produce_no_actions(self):
        source = form('a', 'A')
        engine = await self.evaluate(source,
            'SetVariable(name="x", method="literal", value="Value")\nWithForm(form="A")\n'
            'ModifyField(fieldPattern="missing", fieldType="FREE_TEXT", fieldVariant="text", label="Ignored")',
            service=MemoryService([source]))
        self.assertEqual(engine.candidate_plan.actions, ())
        self.assertEqual(engine.candidate_plan.groups, ())
        self.assertEqual(engine.results[-1].applications, ())
        self.assertEqual(len(engine.candidate_plan.application_order), 2)

    async def test_local_creation_elimination_and_persisted_delete_boundaries(self):
        source = form('a', 'A', elements=[field('persisted', 'old')])
        engine = await self.evaluate(source,
            'AddField(fieldCode="temp", label="Temporary")\nDeleteField(fieldCode="temp")\n'
            'DeleteField(fieldCode="old")\nAddField(fieldCode="after", label="After")')
        plan = engine.candidate_plan
        self.assertEqual([action.kind for action in plan.actions],
                         [ActionKind.PERSISTED_FIELD_DELETE, ActionKind.FORM_SCHEMA_UPDATE])
        self.assertEqual(engine.results[0].applications[0].action_ids, ())
        self.assertEqual(engine.results[1].applications[0].action_ids, ())
        self.assertEqual(plan.actions[0].deleted_member_id, 'persisted')
        self.assertEqual(plan.actions[1].before.payload.schema['elements'], ())
        self.assertEqual(len(plan.omitted_actions), 1)

    async def test_persisted_field_pattern_deletion_is_one_action_per_application(self):
        source = form('a', 'A', elements=[field('one', 'f_one'), field('two', 'f_two')])
        engine = await self.evaluate(source, 'DeleteField(fieldPattern="f_.*")')
        self.assertEqual([action.deleted_member_id for action in engine.candidate_plan.actions], ['one', 'two'])
        self.assertEqual(len(engine.results[0].applications), 2)
        self.assertTrue(all(len(application.action_ids) == 1 for application in engine.results[0].applications))

    async def test_plan_and_display_snapshots_survive_later_action_extension(self):
        saved = []
        def progress(engine):
            if len(engine.effects) == 1:
                saved.append((engine.candidate_plan, engine.results[0].display_state))
        engine = await self.evaluate(form('a', 'A'),
            'AddField(fieldCode="one", label="One")\nAddField(fieldCode="two", label="Two")', progress=progress)
        earlier, display = saved[0]
        self.assertEqual(len(earlier.actions[0].after.payload.schema['elements']), 1)
        self.assertEqual(len(display.form_payloads()['a']['elements']), 1)
        self.assertEqual(len(engine.candidate_plan.actions[0].after.payload.schema['elements']), 2)
        self.assertEqual(earlier.actions[0].action_id, engine.candidate_plan.actions[0].action_id)

    async def test_failed_application_does_not_change_actions_groups_or_creator_provenance(self):
        saved = []
        async def resolve(data):
            if any(element.get('code') == 'bad' for element in data['elements']):
                ACTIVE_DRAFT.get().database_names['invalid'] = object()
            return ExecutionResult(data)
        def progress(engine):
            if len(engine.effects) == 1:
                saved.append(engine.candidate_plan)
        engine = await self.evaluate(form('a', 'A'),
            'AddField(fieldCode="kept", label="Kept")\nAddField(fieldCode="bad", label="Bad")',
            resolver=resolve, progress=progress)
        self.assertEqual(engine.results[-1].status, 'error')
        self.assertEqual(engine.candidate_plan, saved[0])
        self.assertEqual(engine.results[-1].applications[0].action_ids, ())
        self.assertFalse(any(row.member.member_id == 'pending-field:bad' for row in engine.candidate_plan.member_provenance))

    async def test_scope_replacement_seals_previous_a_b_candidates(self):
        forms = [form('root', 'Root'), form('a', 'A'), form('b', 'B')]
        engine = await self.evaluate(forms[0],
            'WithForm(formPattern="A|B")\nAddField(fieldCode="x", label="X")\n'
            'WithForm(formPattern="A|B")\nAddField(fieldCode="y", label="Y")',
            service=MemoryService(forms))
        plan = engine.candidate_plan
        self.assertEqual([action.key.identity for action in plan.actions], ['a', 'b', 'a', 'b'])
        self.assertEqual(len(plan.groups), 2)
        self.assertTrue(plan.groups[0].sealed)
        self.assertNotEqual(plan.groups[0].scope_id, plan.groups[1].scope_id)

    async def test_failed_form_switch_does_not_seal_or_replace_the_published_group(self):
        source = form('a', 'A')
        saved = []
        def progress(engine):
            if len(engine.effects) == 1:
                saved.append(engine.candidate_plan)
        engine = await self.evaluate(source, 'AddField(fieldCode="x", label="X")\nWithForm(form="Missing")',
            service=MemoryService([source]), progress=progress)
        self.assertEqual(engine.results[-1].status, 'warning')
        self.assertEqual(engine.candidate_plan, saved[0])
        self.assertFalse(engine.candidate_plan.groups[0].sealed)

    async def test_retained_draft_cannot_extend_the_published_plan(self):
        retained = []
        async def resolve(data):
            retained.append(ACTIVE_DRAFT.get())
            return ExecutionResult(data)
        engine = await self.evaluate(form('a', 'A'), 'AddField(fieldCode="x", label="X")', resolver=resolve)
        saved = engine.candidate_plan
        draft = retained[0]
        before = draft.workspace.state.resources[ResourceKey.form('a')]
        fields = before.payload.to_dict()['elements'] + [field('late')]
        draft.plan_builder.add(schema_effect(before, fields, 'late-effect'))
        self.assertEqual(engine.candidate_plan, saved)
        self.assertEqual(len(draft.plan_builder.snapshot().actions[0].after.payload.schema['elements']), 2)

    async def test_builder_failure_is_an_atomic_application_error(self):
        source = form('a', 'A')
        with patch.object(PlanBuilder, 'add', side_effect=PlanError('Unsupported action')):
            engine = await self.evaluate(source, 'AddField(fieldCode="bad", label="Bad")')
        self.assertEqual(engine.workspace.active_schema, source)
        self.assertEqual(engine.effects, ())
        self.assertEqual(engine.candidate_plan.actions, ())
        self.assertEqual(engine.candidate_plan.application_order, ())
        self.assertEqual(engine.results[0].status, 'error')
