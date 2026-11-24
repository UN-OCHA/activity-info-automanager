from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from nicegui import ui
from nicegui.client import Client

from activity_manager.commands import AddFieldCommand, ModifyFieldCommand
from activity_manager.command_execution import apply_command, apply_command_locally
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import parse_command_text
from activity_manager.schema_identity import prepare_schema_for_put
from activity_manager.schema_resolution import resolve_command_schema


def element(kind='quantity', code='target', label='Target', identity='fid', params=None, **extra):
    result = dict(id=identity, type=kind, label=label, **extra)
    if code is not None:
        result['code'] = code
    if params is not None:
        result['typeParameters'] = deepcopy(params)
    return result


def form(*elements):
    return {'id': 'form', 'databaseId': 'db', 'label': 'Form', 'elements': list(elements)}


class ModifyFieldExecutionTests(unittest.TestCase):
    def test_optional_reference_match_preserves_code_and_label_lookup(self):
        source = form(element('reference', params={'range': [{'formId': 'people'}]}))
        related = {'people': {'id': 'people', 'label': 'People', 'databaseId': 'db'}}
        for selector, reference, status in (
            ('target', None, 'applied'), ('target', 'people', 'applied'),
            ('Target', 'People', 'applied'), ('target', 'Other', 'warning'),
        ):
            with self.subTest(selector=selector, reference=reference):
                command = ModifyFieldCommand(selector, 'reference', 'reference',
                                             {'label': 'Edited'}, referenced_form=reference)
                result = apply_command_locally(command, source, related)
                self.assertEqual(result.status, status, result.message)
                if status == 'warning':
                    self.assertEqual(result.schema, source)
                    self.assertEqual(result.diagnostics[0].code, 'target_type_mismatch')
                else:
                    field = result.schema['elements'][0]
                    self.assertEqual(field['label'], 'Edited')
                    self.assertEqual(field['typeParameters'], source['elements'][0]['typeParameters'])

    def execute(self, source, changes, kind='quantity', variant=None, target='target', success=True):
        before = deepcopy(source)
        command = ModifyFieldCommand(target, kind, variant, changes)
        result = command.execute(source)
        self.assertEqual(source, before)
        self.assertEqual(command.changes, changes)
        self.assertEqual(result.has_errors, not success, result.message)
        if not success:
            self.assertEqual(result.schema, before)
        return result

    def test_partial_set_clear_false_and_unknown_properties_are_preserved(self):
        source = form(element(description='Old', required=True, unique=True, unknown={'keep': 1},
                              params={'units': 'kg', 'unknown': [1]}), element(code='other', identity='other'))
        field = self.execute(source, {'description': None, 'required': False, 'units': 'people',
                                      'hide_from_entry': False}).schema['elements'][0]
        self.assertNotIn('description', field)
        self.assertIs(field['required'], False)
        self.assertIs(field['dataEntryVisible'], True)
        self.assertIs(field['unique'], True)
        self.assertEqual(field['id'], 'fid')
        self.assertEqual(field['unknown'], {'keep': 1})
        self.assertEqual(field['typeParameters'], {'units': 'people', 'unknown': [1]})

    def test_code_precedence_type_and_variant_mismatches_do_not_fallback(self):
        source = form(element('FREE_TEXT'), element('quantity', code='other', label='target', identity='other'))
        result = self.execute(source, {'label': 'New'})
        self.assertEqual(result.status, 'warning')
        self.assertEqual(result.schema, source)
        self.assertIn('different type or variant', result.message)
        for kind, actual, expected in (
            ('FREE_TEXT', {'barcode': True}, 'text'),
            ('enumerated', {'cardinality': 'multiple', 'values': [{'id': 'o', 'label': 'One'}]}, 'single'),
            ('reference', {'range': [{'formId': 'db@users'}]}, 'reference'),
        ):
            with self.subTest(kind=kind):
                source = form(element(kind, params=actual))
                result = self.execute(source, {'label': 'New'}, kind, expected)
                self.assertEqual(result.status, 'warning')
                self.assertEqual(result.schema, source)

    def test_mismatch_warning_allows_following_command(self):
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(on_execute=AsyncMock())
            source = form(element('date'))
            manager.set_schema(source)
            manager.add_command(ModifyFieldCommand('target', 'quantity', changes={'label': 'Skipped'}))
            manager.add_command(ModifyFieldCommand('target', 'date', changes={'label': 'Applied'}))
            self.assertEqual([item.status for item in manager.executions], ['warning', 'applied'])
            self.assertEqual(manager.executions[0].schema, source)
            self.assertEqual(manager.target_schema()['elements'][0]['label'], 'Applied')
            self.assertTrue(manager.execute_button.enabled)

    def test_exact_label_fallback_includes_type_and_variant(self):
        source = form(element('quantity', code='q', label='Section'),
                      element('section', code=None, label='Section', identity='section'))
        result = self.execute(source, {'label': 'Renamed'}, 'section', target='Section')
        self.assertEqual(result.schema['elements'][1]['label'], 'Renamed')
        self.assertEqual(result.schema['elements'][1]['id'], 'section')
        self.assertNotIn('code', result.schema['elements'][1])
        self.assertEqual(result.schema['elements'][0], source['elements'][0])
        source = form(element('FREE_TEXT', code='a', label='Shared', params={'barcode': True}),
                      element('FREE_TEXT', code='b', label='Shared', identity='text'))
        result = self.execute(source, {'description': 'Text only'}, 'FREE_TEXT', 'text', target='Shared')
        self.assertNotIn('description', result.schema['elements'][0])

    def test_ambiguous_codes_and_labels_fail(self):
        for source, target in (
            (form(element(), element(code='TARGET', identity='other')), 'target'),
            (form(element(code='a'), element(code='b', identity='other')), 'Target'),
        ):
            with self.subTest(target=target):
                self.assertIn('Ambiguous', self.execute(source, {'label': 'New'}, target=target, success=False).message)

    def test_single_selector_code_label_and_id_order(self):
        source = form(element(label='Primary', identity='PrimaryID'),
                      element(code='other', label='TARGET', identity='SecondaryID'),
                      element(code=None, label='Uncoded', identity='Primary'))
        for selector in ('TARGET', 'primary', 'PrimaryID'):
            with self.subTest(selector=selector):
                result = self.execute(source, {'label': 'New'}, target=selector)
                self.assertEqual(result.status, 'applied')
                self.assertEqual(result.schema['elements'][0]['label'], 'New')
                self.assertEqual(result.target_member_ids, ('PrimaryID',))
        result = self.execute(source, {'label': 'New'}, target='primaryid')
        self.assertEqual(result.status, 'warning')
        self.assertEqual(result.schema, source)

    def test_delete_move_and_add_share_field_lookup(self):
        source = form(element(label='First field', identity='FirstID'),
                      element(code=None, label='Anchor field', identity='AnchorID'))
        for selector in ('TARGET', 'first FIELD', 'FirstID'):
            with self.subTest(selector=selector):
                deleted = apply_command_locally(parse_command_text(f'DeleteField(fieldCode="{selector}")')[0], source)
                self.assertEqual(deleted.target_member_ids, ('FirstID',))
                self.assertEqual(deleted.schema['elements'], source['elements'][1:])
        for anchor in ('ANCHOR FIELD', 'AnchorID'):
            with self.subTest(anchor=anchor):
                move = parse_command_text(f'MoveField(fieldCode="FirstID", position="After field", beforeAfterField="{anchor}")')[0]
                moved = apply_command_locally(move, source)
                self.assertEqual(moved.schema['elements'], source['elements'][::-1])
                add = parse_command_text(f'AddField(fieldCode="new", fieldType="quantity", label="New", position="Before field", beforeAfterField="{anchor}")')[0]
                added = apply_command_locally(add, source)
                self.assertFalse(added.has_errors, added.message)
                self.assertEqual([field['label'] for field in added.schema['elements']],
                                 ['First field', 'New', 'Anchor field'])
        move = parse_command_text('MoveField(fieldCode="FirstID", position="Before field", beforeAfterField="TARGET")')[0]
        self.assertEqual(apply_command_locally(move, source).diagnostics[0].code, 'self_relative_move')

    def test_renaming_and_removing_code_preserves_identity_and_position(self):
        source = form(element(), element(code='other', identity='other'))
        result = self.execute(source, {'new_code': 'renamed', 'label': 'New'})
        self.assertEqual(result.schema['elements'][0]['id'], 'fid')
        self.assertEqual(result.schema['elements'][0]['code'], 'renamed')
        cleared = self.execute(result.schema, {'new_code': None}, target='renamed')
        self.assertNotIn('code', cleared.schema['elements'][0])
        unrestricted = self.execute(source, {'new_code': 'code with spaces'})
        self.assertEqual(unrestricted.schema['elements'][0]['code'], 'code with spaces')
        self.execute(source, {'new_code': 'other'}, success=False)

    def test_option_collection_replacement_order_ids_and_unknown_values(self):
        source = form(element('enumerated', params={'cardinality': 'multiple', 'presentation': 'automatic',
            'unknown': 7, 'values': [{'id': 'old-a', 'label': 'A'}, {'id': 'old-b', 'label': 'B', 'extra': True}]}))
        changes = {'option_list': {'values': [{'label': 'B'}, {'label': 'C'}]}}
        result = self.execute(source, changes, 'enumerated', 'multiple')
        params = result.schema['elements'][0]['typeParameters']
        self.assertEqual(params['values'][0], {'id': 'old-b', 'label': 'B', 'extra': True})
        self.assertEqual(params['values'][1], {'id': 'pending-option:fid:0', 'label': 'C'})
        self.assertEqual(params['presentation'], 'automatic')
        self.assertEqual(params['unknown'], 7)
        self.assertEqual(self.execute(source, changes, 'enumerated', 'multiple').schema, result.schema)
        target = prepare_schema_for_put(result.schema)
        self.assertEqual(target['elements'][0]['typeParameters']['values'][0]['id'], 'old-b')
        self.assertFalse(target['elements'][0]['typeParameters']['values'][1]['id'].startswith('pending-'))

    def test_partial_option_presentation_preserves_collection_and_clears_property(self):
        source = form(element('enumerated', params={'cardinality': 'multiple', 'presentation': 'dropdown',
                                                    'values': [{'id': 'a', 'label': 'A'}]}))
        result = self.execute(source, {'option_list': {'presentation': None}}, 'enumerated', 'multiple')
        self.assertNotIn('presentation', result.schema['elements'][0]['typeParameters'])
        self.assertEqual(result.schema['elements'][0]['typeParameters']['values'],
                         source['elements'][0]['typeParameters']['values'])

    def test_unrelated_null_properties_are_preserved(self):
        source = form(element())
        source['elements'][0]['typeParameters'] = None
        result = self.execute(source, {'description': 'Changed'})
        self.assertIsNone(result.schema['elements'][0]['typeParameters'])
        source = form(element('enumerated', params={'values': [{'id': 'a', 'label': 'A'}]}, defaultValue=None))
        result = self.execute(source, {'label': 'Changed'}, 'enumerated', 'single')
        self.assertIn('defaultValue', result.schema['elements'][0])
        self.assertIsNone(result.schema['elements'][0]['defaultValue'])

    def test_option_deletion_invalid_defaults_and_duplicate_labels_fail_atomically(self):
        source = form(element('enumerated', params={'cardinality': 'single', 'values': [{'id': 'a', 'label': 'A'}]},
                              defaultValue='a'))
        for changes in ({'option_list': None}, {'option_list': {'values': None}},
                        {'option_list': {'values': [{'label': 'B'}]}},
                        {'option_list': {'values': [{'label': 'B'}, {'label': 'B'}]}}):
            with self.subTest(changes=changes):
                self.execute(source, changes, 'enumerated', 'single', success=False)
        self.execute(source, {'option_list': {'values': [{'label': 'B'}]}, 'default_value': None},
                     'enumerated', 'single')
        source['elements'][0]['typeParameters']['values'].append({'id': 'b', 'label': 'A'})
        self.execute(source, {'option_list': {'values': [{'label': 'A'}]}}, 'enumerated', 'single', success=False)

    def test_lookup_replacement_preserves_matching_id_updates_formula_and_clears(self):
        source = form(element('reference', params={'range': [{'formId': 'remote'}],
            'lookupConfigs': [{'id': 'lookup-a', 'lookupLabel': 'A', 'formula': 'old', 'extra': 3},
                              {'id': 'lookup-b', 'lookupLabel': 'B', 'formula': 'other'}]}))
        changes = {'lookup_fields': [{'label': 'A', 'formula': 'new'}, {'label': 'C', 'formula': 'code'}]}
        result = self.execute(source, changes, 'reference', 'reference')
        params = result.schema['elements'][0]['typeParameters']
        self.assertEqual(params['lookupConfigs'][0], {'id': 'lookup-a', 'lookupLabel': 'A', 'formula': 'new', 'extra': 3})
        self.assertEqual(params['lookupConfigs'][1]['id'], 'pending-lookup:fid:0')
        self.assertEqual(params['range'], [{'formId': 'remote'}])
        target = prepare_schema_for_put(result.schema)
        self.assertEqual(target['elements'][0]['typeParameters']['lookupConfigs'][0]['id'], 'lookup-a')
        cleared = self.execute(source, {'lookup_fields': None}, 'reference', 'reference')
        self.assertNotIn('lookupConfigs', cleared.schema['elements'][0]['typeParameters'])
        for rows in ([{'label': 'A', 'formula': 'x'}, {'label': 'A', 'formula': 'y'}],):
            self.execute(source, {'lookup_fields': rows}, 'reference', 'reference', success=False)
        source['elements'][0]['typeParameters']['lookupConfigs'][1]['lookupLabel'] = 'A'
        self.execute(source, changes, 'reference', 'reference', success=False)

    def test_new_pending_ids_do_not_reuse_removed_or_retained_ids(self):
        source = form(element('enumerated', params={'cardinality': 'single',
            'values': [{'id': 'pending-option:fid:0', 'label': 'A'}]}))
        result = self.execute(source, {'option_list': {'values': [{'label': 'B'}]}}, 'enumerated', 'single')
        self.assertEqual(result.schema['elements'][0]['typeParameters']['values'][0]['id'], 'pending-option:fid:1')

    def test_add_then_modify_uses_one_pending_namespace_for_options_and_lookups(self):
        for kind, variant, collection, prefix, initial, changes in (
            ('reference', 'reference', 'lookupConfigs', 'pending-lookup:',
             {'referenced_form': 'people', 'lookup_fields': [
                 {'label': 'First', 'formula': 'name'}, {'label': 'Second', 'formula': 'code'}]},
             {'lookup_fields': [{'label': 'First', 'formula': 'code'}, {'label': 'Third', 'formula': 'name'}]}),
            ('enumerated', 'single', 'values', 'pending-option:',
             {'option_list': {'values': [{'label': 'First'}, {'label': 'Second'}]}},
             {'option_list': {'values': [{'label': 'First'}, {'label': 'Third'}]}}),
        ):
            with self.subTest(kind=kind):
                source = AddFieldCommand('REF1', 'Reference', field_type=kind, field_variant=variant,
                                         **initial).execute(form()).schema
                original = source['elements'][0]['typeParameters'][collection]
                self.assertEqual([row['id'] for row in original], [prefix + 'REF1:0', prefix + 'REF1:1'])
                result = self.execute(source, changes, kind, variant, target='REF1')
                rows = result.schema['elements'][0]['typeParameters'][collection]
                self.assertEqual([row['id'] for row in rows], [prefix + 'REF1:0', prefix + 'REF1:2'])
                if kind == 'reference':
                    self.assertEqual(rows[0]['formula'], 'code')
                repeated = self.execute(result.schema, changes, kind, variant, target='REF1')
                self.assertEqual(repeated.schema, result.schema)
                # Renaming the owner must keep its original pending namespace.
                renamed = self.execute(source, {'new_code': 'RENAMED'}, kind, variant, target='REF1')
                renamed = self.execute(renamed.schema, changes, kind, variant, target='RENAMED')
                self.assertEqual([row['id'] for row in renamed.schema['elements'][0]['typeParameters'][collection]],
                                 [prefix + 'REF1:0', prefix + 'REF1:2'])
                prepared = prepare_schema_for_put(result.schema)
                self.assertTrue(all(not row['id'].startswith('pending-')
                                    for row in prepared['elements'][0]['typeParameters'][collection]))

    def test_missing_cardinality_is_single_and_variants_cover_metadata(self):
        source = form(element('enumerated', params={'values': [{'id': 'a', 'label': 'A'}]}))
        result = self.execute(source, {'label': 'New'}, 'enumerated', 'single')
        self.assertNotIn('cardinality', result.schema['elements'][0]['typeParameters'])
        source = form(element('reference', metadata='ADDED_BY', readOnly=True,
                              params={'range': [{'formId': 'db@users'}]}))
        self.execute(source, {'hide_in_table': True}, 'reference', 'metadata')
        self.execute(source, {'description': 'No'}, 'reference', 'metadata', success=False)

    def test_applicability_is_enforced_for_text_commands_and_clear_requests(self):
        cases = [('quantity', None, {}, {'key': True}), ('note', None, {}, {'new_code': 'code'}),
                 ('FREE_TEXT', 'barcode', {'barcode': True}, {'translation': False}),
                 ('reference', 'user', {'range': [{'formId': 'db@users'}]}, {'lookup_fields': None}),
                 ('serial', None, {}, {'unique': False}),
                 ('enumerated', 'single', {'cardinality': 'single', 'values': [{'id': 'a', 'label': 'A'}]},
                  {'option_list': {'presentation': None}})]
        for kind, variant, params, changes in cases:
            with self.subTest(kind=kind, changes=changes):
                self.execute(form(element(kind, params=params)), changes, kind, variant, success=False)

    def test_result_validation_is_atomic(self):
        for source, changes, kind in (
            (form(element()), {'label': None, 'description': 'Must not survive'}, 'quantity'),
            (form(element('calculated', params={'formula': '1'})), {'calculation_formula': None}, 'calculated'),
            (form(element('section', code=None)), {'indentation_level': 4}, 'section'),
            (form(element('geopoint')), {'geo_accuracy': -1}, 'geopoint'),
            (form(element()), {'required': True, 'hide_from_entry': True}, 'quantity'),
            (form(element('attachment', params={'captureMethods': ['FILE'], 'fileTypes': ['PDF']})),
             {'upload_type': ['AUDIO']}, 'attachment'),
        ):
            with self.subTest(changes=changes):
                target = 'Target' if kind == 'section' else 'target'
                self.execute(source, changes, kind, target=target, success=False)
        source = form(element('attachment', params={'captureMethods': ['AUDIO']}))
        self.execute(source, {'upload_type': ['FILE'], 'allowed_file_types': ['PDF']}, 'attachment')

    def test_translation_partial_changes_and_disable(self):
        source = form(element('FREE_TEXT', params={'translationConfig': {'fieldId': 'translated', 'languages': ['fr'], 'extra': 1}}))
        result = self.execute(source, {'translation_language': 'de, es'}, 'FREE_TEXT', 'text')
        self.assertEqual(result.schema['elements'][0]['typeParameters']['translationConfig'],
                         {'fieldId': 'translated', 'languages': ['de', 'es'], 'extra': 1})
        self.execute(source, {'translation_field': None}, 'FREE_TEXT', 'text', success=False)
        result = self.execute(source, {'translation': False}, 'FREE_TEXT', 'text')
        self.assertNotIn('translationConfig', result.schema['elements'][0]['typeParameters'])

    def test_add_modify_rename_and_clear_code_preserve_pending_ids_until_write(self):
        source = AddFieldCommand('new', 'New', field_type='enumerated', field_variant='single',
                                 option_list={'values': [{'label': 'A'}]}).execute(form()).schema
        result = self.execute(source, {'new_code': 'renamed', 'option_list': {'values': [{'label': 'A'}, {'label': 'B'}]}},
                              'enumerated', 'single', target='new')
        self.assertEqual(result.schema['elements'][0]['id'], 'pending-field:new')
        self.assertEqual(result.schema['elements'][0]['typeParameters']['values'][0]['id'], 'pending-option:new:0')
        prepared = prepare_schema_for_put(result.schema)
        self.assertFalse(prepared['elements'][0]['id'].startswith('pending-'))
        self.assertTrue(all(not row['id'].startswith('pending-') for row in prepared['elements'][0]['typeParameters']['values']))
        source = AddFieldCommand('new', 'New', field_type='quantity').execute(form()).schema
        source['elements'].append(element('calculated', code='calc', identity='calc', params={
            'formula': '[pending-field:new] + 1 + "[pending-field:new]"'}))
        result = self.execute(source, {'new_code': None}, target='new')
        prepared = prepare_schema_for_put(result.schema)
        identity = prepared['elements'][0]['id']
        self.assertEqual(prepared['elements'][1]['typeParameters']['formula'], f'{identity} + 1 + "[pending-field:new]"')

    def test_reusing_renamed_code_for_new_field_does_not_reuse_pending_ids(self):
        add = AddFieldCommand('old', 'Old', field_type='enumerated', field_variant='single',
                              option_list={'values': [{'label': 'A'}]})
        source = add.execute(form()).schema
        renamed = self.execute(source, {'new_code': 'renamed'}, 'enumerated', 'single', target='old').schema
        result = add.execute(renamed)
        self.assertFalse(result.has_errors, result.message)
        self.assertEqual(len({field['id'] for field in result.schema['elements']}), 2)
        self.assertEqual(len({field['typeParameters']['values'][0]['id'] for field in result.schema['elements']}), 2)
        prepared = prepare_schema_for_put(result.schema)
        self.assertEqual([field['code'] for field in prepared['elements']], ['renamed', 'old'])


class ModifyFieldPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_match_resolves_qualified_names_before_planning(self):
        source = form(element('reference', params={'range': [{'formId': 'people'}]}))
        related = {}
        service = SimpleNamespace(
            get_database_names=AsyncMock(return_value={'directory': 'Directory'}),
            get_form_names=AsyncMock(return_value={'people': 'People', 'places': 'Places'}),
            get_form_tree=AsyncMock(side_effect=lambda identity: {
                identity: {'id': identity, 'databaseId': 'directory',
                           'label': identity.title(), 'elements': []},
            }),
        )

        async def resolve(candidate):
            return await resolve_command_schema(service, candidate, related, {}, {}, {}, source_schema=source)

        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(resolve_schema=resolve)
        manager.set_schema(source)
        manager.advanced_draft = '\n'.join(
            f'ModifyField(fieldCode="target", fieldType="reference", fieldVariant="reference", '
            f'referencedForm="Directory/{name}", label="{name}")' for name in ('Places', 'People')
        )
        commands = parse_command_text(manager.advanced_draft)
        self.assertEqual([command.referenced_form for command in commands],
                         ['Directory/Places', 'Directory/People'])
        manager._apply_text()
        await manager._evaluation_task
        skipped, applied = manager.executions
        self.assertEqual(skipped.status, 'warning')
        self.assertEqual(skipped.schema, source)
        self.assertFalse(skipped.effect_ids)
        self.assertEqual(applied.status, 'applied', applied.message)
        self.assertEqual(applied.schema['elements'][0]['label'], 'People')
        self.assertEqual(len(manager.candidate_plan.actions), 1)

    async def test_translation_changes_use_existing_resolver_and_validate_target(self):
        source = form(element('FREE_TEXT'), element('FREE_TEXT', code='french', identity='frenchid'))
        async def resolver(candidate):
            return await resolve_command_schema(SimpleNamespace(), candidate, {}, {}, {}, {})
        result = await apply_command(ModifyFieldCommand('target', 'FREE_TEXT', 'text', {
            'translation_field': 'french', 'translation_language': 'fr'}), source, resolver)
        self.assertFalse(result.has_errors, result.message)
        self.assertEqual(result.schema['elements'][0]['typeParameters']['translationConfig'],
                         {'fieldId': 'frenchid', 'languages': ['fr']})
        result = await apply_command(ModifyFieldCommand('target', 'FREE_TEXT', 'text', {
            'translation_field': 'missing', 'translation_language': 'fr'}), source, resolver)
        self.assertTrue(result.has_errors)

    async def test_lookup_formulas_resolve_in_referenced_form(self):
        source = form(element('reference', params={'range': [{'formId': 'remote'}],
            'lookupConfigs': [{'id': 'lookup', 'lookupLabel': 'Name', 'formula': 'old'}]}))
        remote = {'id': 'remote', 'label': 'Remote', 'databaseId': 'db',
                  'elements': [element('FREE_TEXT', code='name', identity='remoteid')]}
        service = SimpleNamespace(get_form_names=AsyncMock(return_value={'remote': 'Remote'}))
        related = {'remote': remote}
        async def resolver(candidate):
            return await resolve_command_schema(service, candidate, related, {}, {}, {})
        result = await apply_command(ModifyFieldCommand('target', 'reference', 'reference', {
            'lookup_fields': [{'label': 'Name', 'formula': 'name'}]}), source, resolver)
        self.assertFalse(result.has_errors, result.message)
        self.assertEqual(result.schema['elements'][0]['typeParameters']['lookupConfigs'],
                         [{'id': 'lookup', 'lookupLabel': 'Name', 'formula': 'remoteid'}])

    async def test_pipeline_resolves_new_option_defaults_and_formula_ids(self):
        source = form(element('enumerated', params={'cardinality': 'multiple', 'values': [{'id': 'a', 'label': 'A'}]}),
                      element(code='other', identity='otherid'))
        service = SimpleNamespace()
        async def resolver(candidate):
            return await resolve_command_schema(service, candidate, {}, {}, {}, {})
        command = ModifyFieldCommand('target', 'enumerated', 'multiple', {
            'option_list': {'values': [{'label': 'A'}, {'label': 'B'}]}, 'default_value': ['B'],
            'validation_condition': 'other > 0'})
        result = await apply_command(command, source, resolver)
        self.assertFalse(result.has_errors, result.message)
        field = result.schema['elements'][0]
        self.assertEqual(field['defaultValue'], [field['typeParameters']['values'][1]['id']])
        self.assertEqual(field['validationCondition'], 'otherid > 0')
        prepared = prepare_schema_for_put(result.schema)
        self.assertEqual(prepared['elements'][0]['defaultValue'],
                         [prepared['elements'][0]['typeParameters']['values'][1]['id']])

    async def test_chain_missing_target_continues_and_errors_stop_later_commands(self):
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager()
        source = form(element())
        manager.set_schema(source)
        manager.add_command(ModifyFieldCommand('missing', 'quantity', changes={'label': 'Ignored'}))
        manager.add_command(ModifyFieldCommand('target', 'quantity', changes={'label': 'New'}))
        self.assertEqual([execution.status for execution in manager.executions], ['warning', 'applied'])
        self.assertEqual(manager.target_schema()['elements'][0]['label'], 'New')
        manager.add_command(ModifyFieldCommand('target', 'quantity', changes={'label': None}))
        manager.add_command(AddFieldCommand('later', 'Later'))
        self.assertEqual([execution.status for execution in manager.executions], ['warning', 'applied', 'error', 'not_executed'])
        self.assertTrue(manager.execute_button.props['disable'])
