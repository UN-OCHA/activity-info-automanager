from types import SimpleNamespace as NS
import unittest

from activity_manager.execution_presentation import confirmation_summary
from activity_manager.planning import ActionKind
from activity_manager.resource_state import FormPayload


def form(identity, label, database='db'):
    return NS(key=NS(identity=identity), payload=FormPayload({
        'id': identity, 'label': label, 'databaseId': database,
        'elements': [{'id': 'secret-field-id', 'label': 'Age'}]}))


def reviewed(kind, before, after=None, **kwargs):
    kwargs.setdefault('deleted_forms', ())
    return NS(action=NS(kind=kind, before=before, after=after or before, **kwargs), destinations=())


class ConfirmationSummaryTests(unittest.TestCase):
    def test_action_types_and_named_scopes_without_identifiers(self):
        a = form('secret-form-id', 'Household Survey')
        b = form('secret-other-id', 'Participant Register')
        deleted = reviewed(ActionKind.PERSISTED_FIELD_DELETE, b, deleted_member_id='secret-field-id')
        deletion = reviewed(ActionKind.FORM_DELETE, a, deleted_forms=(a, b))
        resources = NS(key=NS(identity='secret-database-id'), payload=NS(entries={
            'secret-form-id': {'label': 'Monthly Report'},
            'secret-folder-id': {'label': 'Archived Reports', 'parentId': 'secret-database-id'}}))
        move = reviewed(ActionKind.DATABASE_RESOURCE_PATCH, resources)
        move.action.key = resources.key
        move.destinations = (('secret-form-id', 'secret-folder-id'),)
        plan = NS(related_schemas={}, actions=(
            reviewed(ActionKind.FORM_CREATE, a),
            reviewed(ActionKind.FORM_SCHEMA_UPDATE, b), deleted, move, deletion))
        text = confirmation_summary(plan)
        self.assertIn('1. Create form \u2014 Household Survey', text)
        self.assertIn('2. Update form structure \u2014 Participant Register', text)
        self.assertIn('3. Delete field \u2014 Participant Register \u2192 Age', text)
        self.assertIn('4. Move forms \u2014 Monthly Report \u2192 Archived Reports', text)
        self.assertIn('5. Delete forms \u2014 Household Survey, Participant Register', text)
        self.assertNotIn('secret-', text)
        self.assertNotIn('commands [', text)

    def test_duplicate_names_are_qualified_and_renames_show_both_names(self):
        before = form('secret-one', 'Original')
        after = form('secret-one', 'Shared')
        other = form('secret-two', 'Shared', database='other')
        plan = NS(related_schemas={}, actions=(
            reviewed(ActionKind.FORM_SCHEMA_UPDATE, before, after),
            reviewed(ActionKind.FORM_CREATE, other)))
        text = confirmation_summary(plan, {'db': 'Local', 'other': 'Remote'})
        self.assertIn('Original \u2192 Local / Shared', text)
        self.assertIn('Create form \u2014 Remote / Shared', text)

    def test_no_actions(self):
        self.assertIn('No updates required.', confirmation_summary(NS(related_schemas={}, actions=())))
