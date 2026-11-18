import unittest

from activity_manager.commands import AddFieldCommand, ModifyFieldCommand
from activity_manager.schema_identity import (
    allocate_pending_field_id, pending_row_id, schema_identities, prepare_schema_for_put,
)


class PendingIdentityAllocationTests(unittest.TestCase):
    def test_coded_field_allocations_check_collisions_and_reserve_immediately(self):
        used = {'persisted', 'pending-field:REF1', 'pending-field:REF1_1'}
        self.assertEqual(allocate_pending_field_id('REF1', used), 'pending-field:REF1_2')
        self.assertEqual(allocate_pending_field_id('REF1', used), 'pending-field:REF1_3')
        self.assertIn('pending-field:REF1_2', used)
        self.assertIn('pending-field:REF1_3', used)
        self.assertEqual(allocate_pending_field_id('REF2', used), 'pending-field:REF2')
        self.assertIn('persisted', used)

    def test_structural_allocations_share_reservation_and_keep_distinct_namespaces(self):
        used = set()
        self.assertEqual(allocate_pending_field_id(None, used, field_type='note'), 'pending-field:note:0')
        self.assertEqual(allocate_pending_field_id(None, used, field_type='note'), 'pending-field:note:1')
        self.assertEqual(allocate_pending_field_id(None, used, field_type='section'), 'pending-field:section:0')
        self.assertEqual(len(used), 3)

    def test_invalid_allocations_do_not_reserve_anything(self):
        for code, kind in (('bad code', 'FREE_TEXT'), ('', 'quantity'), (None, 'quantity'), (None, None)):
            with self.subTest(code=code, kind=kind):
                used = {'existing'}
                with self.assertRaises(ValueError):
                    allocate_pending_field_id(code, used, field_type=kind)
                self.assertEqual(used, {'existing'})

    def test_field_and_row_allocators_respect_the_same_reserved_id_set(self):
        source = {'elements': [{'id': 'persisted', 'typeParameters': {
            'values': [{'id': 'pending-field:REF1'}],
            'lookupConfigs': [{'id': 'pending-lookup:REF1_1:0'}]}}]}
        used = schema_identities(source)
        identity = allocate_pending_field_id('REF1', used)
        self.assertEqual(identity, 'pending-field:REF1_1')
        self.assertEqual(pending_row_id('pending-lookup:', identity, 0, used), 'pending-lookup:REF1_1:1')
        self.assertEqual(pending_row_id('pending-option:', identity, 0, used), 'pending-option:REF1_1:0')

    def test_renames_preserve_ids_and_later_additions_use_reserved_namespace(self):
        source = {'id': 'form', 'label': 'Form', 'databaseId': 'db', 'schemaVersion': 1, 'elements': []}
        add = AddFieldCommand('REF1', 'Reference', field_type='reference', field_variant='reference',
                              referenced_form='remote', lookup_fields=[{'label': 'Name', 'formula': 'name'}])
        initial = add.execute(source).schema
        renamed = ModifyFieldCommand('REF1', 'reference', 'reference', {'new_code': 'RENAMED'}).execute(initial)
        self.assertFalse(renamed.has_errors, renamed.message)
        self.assertEqual(renamed.schema['elements'][0]['id'], initial['elements'][0]['id'])
        self.assertEqual(renamed.schema['elements'][0]['typeParameters']['lookupConfigs'],
                         initial['elements'][0]['typeParameters']['lookupConfigs'])
        again = add.execute(renamed.schema)
        self.assertFalse(again.has_errors, again.message)
        self.assertEqual([field['id'] for field in again.schema['elements']],
                         ['pending-field:REF1', 'pending-field:REF1_1'])
        prepared = prepare_schema_for_put(again.schema)
        self.assertEqual(len({field['id'] for field in prepared['elements']}), 2)
        self.assertTrue(all(not field['id'].startswith('pending-') for field in prepared['elements']))
