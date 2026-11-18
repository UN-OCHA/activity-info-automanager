from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from activity_manager.commands import AddFieldCommand
from activity_manager.schema_identity import prepare_schema_for_put, prepare_schema_for_write
from activity_manager.schema_names import schema_with_ids, substitute_record_label
from activity_manager.schema_change import apply_form_schema_change
from test.test_add_field import schema
from test.test_field_parameters import OPTIONS


class FormulaWriteTests(unittest.TestCase):
    def test_functions_literals_keywords_and_numbers_are_preserved(self):
        source = {'id': 'local', 'elements': [
            {'id': 'idX', 'code': 'X'}, {'id': 'idLeft', 'code': 'LEFT'},
            {'id': 'idExponent', 'code': 'e3'}, {'id': 'idTrue', 'code': 'TRUE'},
            {'id': 'calc', 'typeParameters': {'formula':
                'LEFT (X, 1) + [LEFT] + X + "X" + \'LEFT(X)\' + 1e3 + TRUE + [TRUE] + UNKNOWN'}},
        ]}
        result = schema_with_ids(source)
        self.assertEqual(result['elements'][-1]['typeParameters']['formula'],
                         'LEFT (idX, 1) + idLeft + idX + "X" + \'LEFT(X)\' + 1e3 + TRUE + idTrue + UNKNOWN')
        self.assertEqual(schema_with_ids(result), result)

    def test_scoped_paths_lookup_context_and_existing_ids(self):
        remote = {'id': 'remote', 'elements': [
            {'id': 'same', 'code': 'NAME'},
            {'id': 'teamId', 'code': 'TEAM', 'typeParameters': {'range': [{'formId': 'third'}]}},
        ]}
        source = {'id': 'local', 'elements': [
            {'id': 'same', 'code': 'PERSON', 'typeParameters': {'range': [{'formId': 'remote'}],
                'lookupConfigs': [{'id': 'lookup', 'formula': 'NAME + TEAM.TITLE'}]}},
            {'id': 'calc', 'relevanceCondition': 'PERSON.TEAM.TITLE == "1"',
             'requiredCondition': 'PERSON.NAME != ""', 'validationCondition': 'same.NAME != ""',
             'defaultValueFormula': 'PERSON.NAME'},
        ]}
        before = deepcopy(source)
        result = schema_with_ids(source, {'remote': remote, 'third': {
            'elements': [{'id': 'same', 'code': 'TITLE'}]}})
        self.assertEqual(result['elements'][1]['relevanceCondition'], 'same.teamId.same == "1"')
        for key in ('requiredCondition', 'validationCondition'):
            self.assertEqual(result['elements'][1][key], 'same.same != ""')
        self.assertEqual(result['elements'][1]['defaultValueFormula'], 'same.same')
        self.assertEqual(result['elements'][0]['typeParameters']['lookupConfigs'],
                         [{'id': 'lookup', 'formula': 'same + teamId.same'}])
        self.assertEqual(source, before)

    def test_new_field_codes_resolve_after_id_allocation(self):
        source = AddFieldCommand('REFCODE', 'Reference code').execute(schema()).schema
        preview = AddFieldCommand('check', 'Check', validation_condition='REFCODE == "1"').execute(source).schema
        with patch('activity_manager.schema_identity.secrets.token_hex', side_effect=['a' * 24, 'b' * 24]):
            target = prepare_schema_for_put(preview)
        self.assertEqual(target['elements'][-1]['validationCondition'], 'c' + 'a' * 24 + ' == "1"')
        self.assertEqual(preview['elements'][-1]['validationCondition'], 'REFCODE == "1"')

    def test_enumerated_labels_codes_and_ids(self):
        command = AddFieldCommand('choice', 'Choice', field_type='enumerated', option_list=OPTIONS,
                                  default_value=['Yes', 'no'])
        preview = command.execute(schema()).schema
        target = prepare_schema_for_put(preview)
        values = target['elements'][-1]['typeParameters']['values']
        self.assertEqual(target['elements'][-1]['defaultValue'], [option['id'] for option in values])
        self.assertEqual(prepare_schema_for_put(target), target)
        self.assertEqual(preview['elements'][-1]['defaultValue'][0], 'Yes')


class ReferenceWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_label_query_first_match_no_match_and_literal_types(self):
        for label in ('Alice"\\', 123):
            service = SimpleNamespace(query_rows=AsyncMock(return_value=[{'_id': 'first'}, {'_id': 'second'}]))
            result = await substitute_record_label(service, 'people', label,
                                                   {'people': {'recordLabelFieldId': 'calculated_label'}})
            self.assertEqual(result, 'people:first')
            query = service.query_rows.await_args.args[0]
            self.assertEqual(query['columns'], [{'id': '_id', 'formula': '_id'}])
            self.assertEqual(json.loads(query['filter'].removeprefix('calculated_label == ')), label)
            service.query_rows.return_value = []
            self.assertEqual(await substitute_record_label(service, 'people', label,
                             {'people': {'recordLabelFieldId': 'calculated_label'}}), label)

    async def test_missing_label_field_never_queries(self):
        service = SimpleNamespace(query_rows=AsyncMock())
        self.assertEqual(await substitute_record_label(service, 'people', 'Alice', {}), 'Alice')
        service.query_rows.assert_not_awaited()

    async def test_unavailable_tree_schema_stops_default_resolution(self):
        preview = AddFieldCommand('ref', 'Reference', field_type='reference', referenced_form='unavailable',
                                  default_value='Alice', validation_condition='old != ""').execute(schema()).schema
        service = SimpleNamespace(get_form_tree=AsyncMock(return_value={}),
                                  get_form_schema=AsyncMock(), query_rows=AsyncMock())
        with self.assertRaisesRegex(ValueError, 'Reference default'):
            await prepare_schema_for_write(service, preview)
        service.get_form_schema.assert_not_awaited()
        service.query_rows.assert_not_awaited()

    async def test_execute_preparation_numeric_multiple_and_canonical_defaults(self):
        source = AddFieldCommand('ref', 'Reference', field_type='reference', referenced_form='people',
                                 default_value=123).execute(schema()).schema
        preview = AddFieldCommand('refs', 'References', field_type='multiselectreference',
                                  referenced_form='people', default_value=[123, 'people:existing']).execute(source).schema
        service = SimpleNamespace(query_rows=AsyncMock(return_value=[{'_id': 'first'}]))
        related = {'people': {'recordLabelFieldId': 'calculated_label', 'elements': []}}
        target = await prepare_schema_for_write(service, preview, related)
        self.assertEqual(target['elements'][-2]['defaultValue'], 'people:first')
        self.assertEqual(target['elements'][-1]['defaultValue'], ['people:first', 'people:existing'])
        self.assertEqual(service.query_rows.await_count, 1)
        self.assertEqual(preview['elements'][-2]['defaultValue'], 123)

    async def test_write_verifies_materialized_formula_and_record_target(self):
        loaded = schema()
        preview = AddFieldCommand('ref', 'Reference', field_type='reference', referenced_form='people',
                                  default_value='Alice', validation_condition='old != ""').execute(loaded).schema
        written = None

        async def write(form_id, target):
            nonlocal written
            written = deepcopy(target)

        async def read(form_id):
            return loaded if written is None else written

        service = SimpleNamespace(get_form_schema=AsyncMock(side_effect=read),
            get_form_tree=AsyncMock(return_value={'people': {'recordLabelFieldId': 'label', 'elements': []}}),
            put_form_schema=AsyncMock(side_effect=write), query_rows=AsyncMock(return_value=[{'_id': 'person'}]))
        outcome = await apply_form_schema_change(service, loaded['id'], loaded, preview, AsyncMock())
        self.assertEqual(outcome.status, 'success', outcome.messages)
        self.assertEqual(written['elements'][-1]['defaultValue'], 'people:person')
        self.assertEqual(written['elements'][-1]['validationCondition'], loaded['elements'][0]['id'] + ' != ""')
        service.get_form_tree.assert_awaited_once()
