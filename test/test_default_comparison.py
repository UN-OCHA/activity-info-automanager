from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from activity_info_api_client.models.form_schema import FormSchema

from activity_manager.schema_change import compare_schemas
from test.execution_fixtures import execute_schema_fixture
from test.test_add_field import schema


def with_default(value, field_type='reference', form_id='people'):
    result = schema()
    result['elements'].append({
        'id': 'defaultfield', 'code': 'DEFAULT', 'label': 'Default', 'required': False,
        'type': field_type, 'defaultValue': value,
        'typeParameters': {'values': [{'id': 'yesId', 'label': 'Yes'}, {'id': 'noId', 'label': 'No'}]}
        if field_type == 'enumerated' else {'range': [{'formId': form_id}]},
    })
    return result


class DefaultComparisonTests(unittest.TestCase):
    def test_enumerated_scalar_and_array_labels_match_ids_without_mutation(self):
        for ids, labels in [('yesId', 'Yes'), (['yesId', 'noId'], ['Yes', 'No'])]:
            expected, actual = with_default(ids, 'enumerated'), with_default(labels, 'enumerated')
            before = deepcopy(actual)
            comparison = compare_schemas(FormSchema.from_dict(expected), FormSchema.from_dict(actual))
            self.assertTrue(comparison.matches, comparison.summary)
            self.assertEqual(actual, before)
        wrong_order = with_default(['No', 'Yes'], 'enumerated')
        self.assertFalse(compare_schemas(FormSchema.from_dict(expected), FormSchema.from_dict(wrong_order)).matches)

    def test_reference_aliases_are_scoped_typed_and_only_equate_resolved_ids(self):
        aliases = {('people', 'str', 'Alice'): 'people:first', ('people', 'int', '12'): 'people:number'}
        canonical = FormSchema.from_dict(with_default('people:first'))
        self.assertTrue(compare_schemas(canonical, FormSchema.from_dict(with_default('Alice')),
                                        reference_defaults=aliases).matches)
        for value, form_id in [('people:second', 'people'), ('Unknown', 'people'), ('Alice', 'other')]:
            self.assertFalse(compare_schemas(canonical, FormSchema.from_dict(with_default(value, form_id=form_id)),
                                             reference_defaults=aliases).matches)
        numeric = FormSchema.from_dict(with_default('people:number'))
        self.assertFalse(compare_schemas(numeric, FormSchema.from_dict(with_default('12')),
                                         reference_defaults=aliases).matches)
        self.assertFalse(compare_schemas(canonical, FormSchema.from_dict(with_default('Alice'))).matches)


class ExecutionDefaultComparisonTests(unittest.IsolatedAsyncioTestCase):
    def service(self, reads, rows):
        return SimpleNamespace(get_form_schema=AsyncMock(side_effect=[reads[0], *reads]),
            get_form_tree=AsyncMock(return_value={'people': {'recordLabelFieldId': 'name', 'elements': []}}),
            query_rows=AsyncMock(return_value=rows), put_form_schema=AsyncMock())

    async def test_label_only_change_skips_put_and_id_allocation(self):
        loaded = with_default('people:first')
        target = with_default('Alice')
        service = self.service([loaded], [{'_id': 'first'}, {'_id': 'second'}])
        with patch('activity_manager.schema_identity.secrets.token_hex') as allocate:
            outcome = await execute_schema_fixture(service, 'form1', loaded, target, AsyncMock())
        self.assertEqual(outcome.status, 'success', outcome.messages)
        service.put_form_schema.assert_not_awaited()
        service.query_rows.assert_awaited_once()
        allocate.assert_not_called()

    async def test_stale_check_ignores_default_representation(self):
        loaded, latest = with_default('Alice'), with_default('people:first')
        service = self.service([latest], [{'_id': 'first'}])
        outcome = await execute_schema_fixture(service, 'form1', loaded, latest, AsyncMock())
        self.assertEqual(outcome.status, 'success', outcome.messages)
        service.query_rows.assert_awaited_once()

    async def test_verification_accepts_label_response_for_written_id(self):
        loaded = with_default('people:old')
        target, actual = with_default('Alice'), with_default('Alice')
        service = self.service([loaded, actual], [{'_id': 'first'}])
        outcome = await execute_schema_fixture(service, 'form1', loaded, target, AsyncMock())
        self.assertEqual(outcome.status, 'success', outcome.messages)
        written = service.put_form_schema.await_args.args[1]
        self.assertEqual(written['elements'][-1]['defaultValue'], 'people:first')
        service.query_rows.assert_awaited_once()

    async def test_enum_label_only_change_is_unchanged(self):
        loaded, target = with_default('yesId', 'enumerated'), with_default('Yes', 'enumerated')
        service = self.service([loaded], [])
        outcome = await execute_schema_fixture(service, 'form1', loaded, target, AsyncMock())
        self.assertEqual(outcome.status, 'success', outcome.messages)
        service.query_rows.assert_not_awaited()
        service.put_form_schema.assert_not_awaited()

    async def test_no_match_stops_before_put(self):
        loaded = with_default('people:old')
        service = self.service([loaded, with_default('Alice')], [])
        outcome = await execute_schema_fixture(service, 'form1', loaded, with_default('Alice'), AsyncMock())
        self.assertEqual(outcome.status, 'preparation_failed', outcome.messages)
        service.put_form_schema.assert_not_awaited()
        service.query_rows.assert_awaited_once()

    async def test_query_failure_stops_before_put(self):
        loaded = with_default('people:old')
        service = self.service([loaded], [])
        service.query_rows.side_effect = ValueError('HTTP 403')
        outcome = await execute_schema_fixture(service, 'form1', loaded, with_default('Alice'), AsyncMock())
        self.assertEqual(outcome.status, 'preparation_failed', outcome.messages)
        service.put_form_schema.assert_not_awaited()

    async def test_numeric_multiple_reference_defaults_match_ids(self):
        loaded = with_default(['people:number', 'people:existing'], 'multiselectreference')
        target = with_default([123, 'people:existing'], 'multiselectreference')
        service = self.service([loaded], [{'_id': 'number'}])
        outcome = await execute_schema_fixture(service, 'form1', loaded, target, AsyncMock())
        self.assertEqual(outcome.status, 'success', outcome.messages)
        service.put_form_schema.assert_not_awaited()
        service.query_rows.assert_awaited_once()

    async def test_divergent_reference_label_outcome_blocks_recovery_without_queries(self):
        loaded, target = with_default('people:original'), with_default('Alice')
        partial, restored = with_default('Alice'), with_default('Original')
        partial['description'] = 'Unexpected change'
        service = self.service([loaded, partial, restored], [])

        async def query(body):
            return [{'_id': 'first' if body['filter'] == 'name == "Alice"' else 'original'}]

        service.query_rows.side_effect = query
        outcome = await execute_schema_fixture(
            service, 'form1', loaded, target, AsyncMock(return_value='rollback'))
        self.assertEqual(outcome.status, 'recovery_blocked', outcome.messages)
        self.assertEqual(service.put_form_schema.await_count, 1)
        self.assertEqual(service.query_rows.await_count, 1)
