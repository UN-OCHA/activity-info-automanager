from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch

from activity_manager.confirmed_plan import AttemptBinding, ConfirmationAttempt, materialize_plan
from activity_manager.planning import PlanBuilder, PlanError
from activity_manager.resource_state import BaselineEvidence, Existence
from test.test_planning import schema_state, schema_effect, field as planning_field


def field(*args):
    return dict(planning_field(*args), required=False)


def candidate():
    builder = PlanBuilder()
    added = schema_effect(schema_state(), [field('pending-field:x', 'X')], 'add')
    builder.add(added)
    builder.select_form(schema_state('b').key)
    builder.add(schema_effect(schema_state('b'), [field('pending-field:x', 'X')], 'other'))
    builder.select_form(added.key)
    builder.add(schema_effect(added.after, [], 'delete'))
    return builder.snapshot()


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.binding = AttemptBinding('evaluation', {'form': 'a'}, 'session')

    def test_stable_ids_across_actions_deletion_and_provenance(self):
        preview = candidate()
        plan = materialize_plan(preview, self.binding)
        created = plan.actions[0].action.after.payload.schema['elements'][0]['id']
        other = plan.actions[1].action.after.payload.schema['elements'][0]['id']
        self.assertNotEqual(created, other)
        self.assertFalse(created.startswith('pending-'))
        self.assertEqual(plan.actions[-1].deletions, (created,))
        self.assertEqual(plan.actions[-1].action.before.payload.schema['elements'][0]['id'], created)
        self.assertEqual(plan.candidate.member_provenance[0].member.member_id, created)
        self.assertEqual(preview.actions[0].after.payload.schema['elements'][0]['id'], 'pending-field:x')
        self.assertEqual(plan.actions[-1].recovery_limit, 'field_restoration_compensation')

    def test_record_label_and_formula_literals_keep_their_meaning(self):
        builder = PlanBuilder()
        x = field('pending-field:x', 'X')
        calc = {'id': 'calc', 'code': 'CALC', 'label': 'Calc', 'type': 'calculated', 'required': False,
                'typeParameters': {'formula': '[pending-field:x] + "[pending-field:x]"'}}
        effect = schema_effect(schema_state(), [x, calc], 'add')
        data = effect.after.payload.to_dict()
        data['recordLabelFieldId'] = 'pending-field:x'
        effect = replace(effect, after=replace(effect.after, payload=type(effect.after.payload)(data)))
        builder.add(effect)
        plan = materialize_plan(builder.snapshot(), self.binding)
        schema = plan.actions[0].request.schema
        real = schema['elements'][0]['id']
        self.assertEqual(schema['recordLabelFieldId'], real)
        self.assertEqual(schema['elements'][1]['typeParameters']['formula'], f'{real} + "[pending-field:x]"')

    def test_cross_form_formula_and_reverse_reference_use_the_target_forms_allocation(self):
        builder = PlanBuilder()
        a = schema_effect(schema_state(), [field('pending-field:x', 'X')], 'add-a')
        builder.add(a)
        reference = dict(field('ref', 'REF'), type='reference', typeParameters={'range': [{'formId': 'a'}]})
        calc = dict(field('calc', 'CALC'), type='calculated',
                    typeParameters={'formula': 'REF.[pending-field:x] + "pending-field:x"'})
        b = schema_effect(schema_state('b'), [field('pending-field:x', 'X'), reference, calc], 'add-b')
        builder.add(b)
        plan = materialize_plan(builder.snapshot(), self.binding)
        real_a = plan.actions[0].request.schema['elements'][0]['id']
        real_b = plan.actions[1].request.schema['elements'][0]['id']
        formula = plan.actions[1].request.schema['elements'][2]['typeParameters']['formula']
        self.assertEqual(formula, f'ref.{real_a} + "pending-field:x"')
        self.assertNotIn(real_b, formula)

    def test_options_defaults_lookup_rows_and_translation_share_allocations(self):
        builder = PlanBuilder()
        option = {'id': 'pending-option:x:0', 'label': 'Yes'}
        enum = dict(field('pending-field:x', 'X'), type='enumerated',
                    typeParameters={'values': [option]}, defaultValue=['pending-option:x:0'])
        translation = dict(field('translated', 'TR'), type='enumerated',
                           typeParameters={'values': [], 'translationConfig': {'fieldId': 'pending-field:x', 'languages': ['en']}})
        builder.add(schema_effect(schema_state(), [enum, translation], 'add'))
        preview = builder.snapshot()
        plan = materialize_plan(preview, self.binding)
        fields = plan.actions[0].request.schema['elements']
        self.assertEqual(fields[0]['defaultValue'][0], fields[0]['typeParameters']['values'][0]['id'])
        self.assertEqual(fields[1]['typeParameters']['translationConfig']['fieldId'], fields[0]['id'])
        self.assertEqual(preview.actions[0].after.payload.schema['elements'][0]['id'], 'pending-field:x')

    def test_confirmation_is_exact_immutable_and_does_not_reallocate(self):
        preview = candidate()
        attempt = ConfirmationAttempt(preview, self.binding)
        with self.assertRaises(PlanError):
            attempt.require_confirmed(self.binding, preview)
        with patch('activity_manager.schema_identity.secrets.token_hex', side_effect=AssertionError('reallocated')):
            confirmed = attempt.confirm(attempt.plan.attempt_id, self.binding, preview)
            self.assertIs(confirmed, attempt.require_confirmed(self.binding, preview))
        with self.assertRaises(FrozenInstanceError):
            confirmed.actions[0].action.after = None
        with self.assertRaises(TypeError):
            confirmed.actions[0].request.schema['label'] = 'changed'

    def test_cancel_and_each_binding_change_permanently_invalidate(self):
        preview = candidate()
        for binding in (replace(self.binding, evaluation_id='new'), replace(self.binding, source={'form': 'b'}),
                        replace(self.binding, session_id='new')):
            attempt = ConfirmationAttempt(preview, self.binding)
            attempt.confirm(attempt.plan.attempt_id, self.binding, preview)
            with self.assertRaises(PlanError):
                attempt.require_confirmed(binding, preview)
            with self.assertRaises(PlanError):
                attempt.require_confirmed(self.binding, preview)
        attempt = ConfirmationAttempt(preview, self.binding)
        attempt.cancel()
        with self.assertRaises(PlanError):
            attempt.confirm(attempt.plan.attempt_id, self.binding, preview)

    def test_changed_candidate_and_wrong_attempt_cannot_confirm(self):
        preview = candidate()
        attempt = ConfirmationAttempt(preview, self.binding)
        with self.assertRaises(PlanError):
            attempt.confirm('other-attempt', self.binding, preview)
        with self.assertRaises(PlanError):
            attempt.confirm(attempt.plan.attempt_id, self.binding, replace(preview, actions=preview.actions[:-1]))

    def test_eliminated_fields_allocate_nothing(self):
        builder = PlanBuilder()
        effect = schema_effect(schema_state(), [field('pending-field:x', 'X')], 'add')
        builder.add(effect)
        builder.add(schema_effect(effect.after, [], 'delete'))
        with patch('activity_manager.schema_identity.secrets.token_hex', side_effect=AssertionError('allocated')):
            plan = materialize_plan(builder.snapshot(), self.binding)
        self.assertEqual(plan.actions, ())
        self.assertEqual(len(plan.candidate.omitted_actions), 1)

    def test_fixed_metadata_identity_survives_later_actions_and_record_label(self):
        builder = PlanBuilder()
        metadata = dict(field('pending-field:CREATOR', 'CREATOR'), type='reference',
                        metadata='ADDED_BY', readOnly=True, typeParameters={'range': [{'formId': 'users'}]})
        effect = schema_effect(schema_state(), [metadata], 'add')
        data = effect.after.payload.to_dict()
        data['recordLabelFieldId'] = metadata['id']
        effect = replace(effect, after=replace(effect.after, payload=type(effect.after.payload)(data)))
        builder.add(effect)
        builder.select_form(schema_state('b').key)
        builder.add(schema_effect(schema_state('b'), [field('ordinary')], 'other'))
        builder.select_form(effect.key)
        builder.add(schema_effect(effect.after, [metadata, field('later')], 'later'))
        plan = materialize_plan(builder.snapshot(), self.binding)
        self.assertEqual(plan.actions[0].request.schema['recordLabelFieldId'], '_addedBy')
        self.assertEqual(plan.actions[-1].request.schema['elements'][0]['id'], '_addedBy')

    def test_baseline_members_are_preserved_even_when_their_spelling_looks_pending(self):
        before = schema_state(fields=[field('pending-field:x', 'X')])
        builder = PlanBuilder()
        builder.observe([BaselineEvidence(before.key, Existence.PRESENT, before)])
        builder.add(schema_effect(before, [], 'delete'))
        with patch('activity_manager.schema_identity.secrets.token_hex', side_effect=AssertionError('allocated')):
            plan = materialize_plan(builder.snapshot(), self.binding)
        self.assertEqual(plan.actions[0].deletions, ('pending-field:x',))

    def test_related_context_and_reference_labels_are_detached_and_pinned(self):
        related = {'remote': schema_state('remote', fields=[field('persisted')]).payload.to_dict()}
        defaults = {'remote': {'name': 'record'}}
        plan = materialize_plan(candidate(), self.binding, related_schemas=related, reference_defaults=defaults)
        self.assertEqual(related['remote']['elements'][0]['id'], 'persisted')
        related['remote']['label'] = 'later'
        defaults['remote']['name'] = 'different'
        self.assertEqual(plan.related_schemas['remote']['label'], 'REMOTE')
        self.assertEqual(plan.reference_defaults['remote']['name'], 'record')
