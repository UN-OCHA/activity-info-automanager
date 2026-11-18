from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import parse_command_text, CommandTextError
from activity_manager.command_execution import ExecutionResult
from activity_manager.multi_form import prepare_operations
from activity_manager.schema_change import apply_schema_sequence
from activity_manager.schema_resolution import resolve_command_schema
from activity_manager.page import connection_page


def form(identity, label, database='db', elements=()):
    data = deepcopy(list(elements))
    for field in data:
        field.setdefault('required', False)
        if field.get('type') == 'text':
            field['type'] = 'FREE_TEXT'
    return dict(id=identity, label=label, databaseId=database, schemaVersion=1, elements=data)


class MemoryService:
    def __init__(self, schemas):
        self.schemas = {s['id']: deepcopy(s) for s in schemas}
        self.reads = []
        self.writes = []
        self.fail_at = None
        self.reject_at = None

    async def get_form_names(self, database):
        return {key: s['label'] for key, s in self.schemas.items() if s['databaseId'] == database}

    async def get_database_names(self):
        return {'db': 'Local', 'remote': 'Remote'}

    async def get_form_schema(self, identity):
        self.reads.append(identity)
        return deepcopy(self.schemas[identity])

    async def put_form_schema(self, identity, schema):
        self.writes.append((identity, deepcopy(schema)))
        if len(self.writes) == self.reject_at:
            raise ValueError('Rejected')
        self.schemas[identity] = deepcopy(schema)
        if len(self.writes) == self.fail_at:
            self.schemas[identity]['label'] = 'Unexpected'


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, schemas, text):
        service = MemoryService(schemas)
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(form_service=service, on_execute=AsyncMock())
        async def resolve(data):
            return await resolve_command_schema(service, data, manager.related_schemas, {}, {}, {},
                                                source_schema=manager.workspace.originals[data['id']])
        manager.resolve_schema = resolve
        manager.set_schema(schemas[0])
        manager.commands = parse_command_text(text)
        manager._evaluate_commands()
        await manager._evaluation_task
        return manager, service

    async def test_revisits_keep_history_and_local_cross_form_references(self):
        one = form('one', 'One', elements=[{'id': 'oldid', 'code': 'old', 'type': 'text', 'label': 'Old'}])
        two = form('two', 'Two', elements=[
            {'id': 'refid', 'code': 'REF', 'type': 'reference', 'label': 'Ref',
             'typeParameters': {'range': [{'formId': 'one'}]}},
            {'id': 'calcid', 'code': 'CALC', 'type': 'calculated', 'label': 'Calc',
             'typeParameters': {'formula': 'refid.oldid'}},
        ])
        manager, service = await self.evaluate([one, two], '''
AddField(fieldCode="new", label="New")
WithForm(form="Two")
ModifyField(fieldCode="CALC", fieldType="calculated", calculationFormula="REF.new")
WithForm(form="One")
DeleteField(fieldCode="old")
''')
        self.assertTrue(all(e.status == 'applied' for e in manager.executions),
                        [e.message for e in manager.executions])
        snapshots = manager.target_opschema()
        self.assertEqual([s['id'] for s in snapshots], ['one', 'two', 'one'])
        self.assertEqual([f['code'] for f in snapshots[0]['elements']], ['old', 'new'])
        self.assertEqual([f['code'] for f in snapshots[2]['elements']], ['new'])
        self.assertIn('pending-field:new', snapshots[1]['elements'][1]['typeParameters']['formula'])
        self.assertEqual(service.reads, ['two'])
        self.assertEqual(len(manager.executions[0].opschema), 1)
        self.assertEqual(len(manager.executions[2].opschema), 2)
        self.assertEqual(one['elements'][0]['code'], 'old')

        operations = prepare_operations(snapshots, manager.workspace.originals, {})
        first_id = operations[0][0]['elements'][-1]['id']
        self.assertEqual(operations[2][0]['elements'][0]['id'], first_id)
        self.assertEqual(operations[1][0]['elements'][1]['typeParameters']['formula'], f'refid.{first_id}')
        outcome = await apply_schema_sequence(service, manager.workspace.originals, snapshots, AsyncMock())
        self.assertEqual(outcome.status, 'success', outcome.messages)
        self.assertEqual([key for key, _ in service.writes], ['one', 'two', 'one'])

    async def test_active_database_variables_and_same_form_noop(self):
        one, two, three = form('one', 'One'), form('two', 'Two', 'remote'), form('three', 'Three', 'remote')
        manager, service = await self.evaluate([one, two, three], '''
SetVariable(name="target", method="literal", value="Remote/Two")
WithForm(form="${vars.target}")
WithForm(form="Two")
WithForm(form="Three")
''')
        self.assertEqual([s['id'] for s in manager.target_opschema()], ['one', 'two', 'three'])
        self.assertEqual(manager.executions[-1].schema['databaseId'], 'remote')
        self.assertEqual(service.reads, ['two', 'three'])

    async def test_failed_switch_preserves_state_and_blocks_later_commands(self):
        manager, service = await self.evaluate([form('one', 'One')], '''
WithForm(form="Missing")
AddField(fieldCode="new", label="New")
''')
        self.assertEqual(manager.executions[0].status, 'error')
        self.assertEqual(manager.executions[1].status, 'not_executed')
        self.assertEqual(manager.executions[0].schema['id'], 'one')
        self.assertEqual(len(manager.workspace.opschema), 1)

    async def test_reevaluation_does_not_reuse_previous_local_edits(self):
        manager, _ = await self.evaluate([form('one', 'One'), form('two', 'Two')], '''
AddField(fieldCode="new", label="New")
WithForm(form="Two")
''')
        manager.commands = parse_command_text('WithForm(form="Two")\nWithForm(form="One")')
        manager._evaluate_commands()
        await manager._evaluation_task
        self.assertEqual(manager.target_opschema()[-1]['elements'], [])

    async def test_deleted_pending_field_is_not_reused_for_a_new_field(self):
        manager, _ = await self.evaluate([form('one', 'One'), form('two', 'Two')], '''
AddField(fieldCode="new", label="First")
WithForm(form="Two")
WithForm(form="One")
DeleteField(fieldCode="new")
AddField(fieldCode="new", label="Replacement")
''')
        snapshots = manager.target_opschema()
        self.assertNotEqual(snapshots[0]['elements'][0]['id'], snapshots[-1]['elements'][0]['id'])
        operations = prepare_operations(snapshots, manager.workspace.originals, {})
        self.assertNotEqual(operations[0][0]['elements'][0]['id'], operations[-1][0]['elements'][0]['id'])

    async def test_zero_match_iteration_keeps_the_full_write_sequence(self):
        manager, _ = await self.evaluate([form('one', 'One'), form('two', 'Two')], '''
AddField(fieldCode="first", label="First")
WithForm(form="Two")
DeleteField(fieldPattern="missing.*")
''')
        self.assertEqual(manager.executions[-1].status, 'warning')
        self.assertEqual([s['id'] for s in manager.target_opschema()], ['one', 'two'])
        self.assertEqual(manager.target_opschema()[0]['elements'][0]['code'], 'first')

    async def test_unchanged_steps_retain_annotations_before_a_revisit(self):
        manager, _ = await self.evaluate([form('one', 'One'), form('two', 'Two')], '''
AddField(fieldCode="first", label="First", validationCondition="UNKNOWN > 0")
SetVariable(name="target", method="literal", value="Two")
DeleteField(fieldPattern="missing.*")
WithForm(form="${vars.target}")
WithForm(form="One")
WithForm(form="Missing")
''')
        for index in (0, 1, 2, 4, 5):
            with self.subTest(index=index):
                self.assertEqual(len(manager.executions[index].issues), 1)
                self.assertEqual(manager.executions[index].issues[0].path,
                                 ('elements', 0, 'validationCondition'))
        self.assertEqual(manager.executions[5].status, 'error')

    async def test_result_tabs_show_history_and_reuse_revisited_form(self):
        managers = []
        def make_manager(**kwargs):
            manager = CommandListManager(**kwargs)
            managers.append(manager)
            return manager
        with patch('activity_manager.page.CommandListManager', side_effect=make_manager):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
        manager = managers[0]
        manager.form_service = MemoryService([form('one', 'One'), form('two', 'Two')])
        manager.resolve_schema = AsyncMock(side_effect=lambda schema: ExecutionResult(schema))
        manager.set_schema(form('one', 'One'))
        manager.commands = parse_command_text('''
AddField(fieldCode="first", label="First")
WithForm(form="Two")
AddField(fieldCode="second", label="Second")
WithForm(form="One")
DeleteField(fieldCode="first")
''')
        manager._evaluate_commands()
        await manager._evaluation_task
        tabs = next(element for element in page.descendants() if isinstance(element, ui.tabs))
        code = next(element for element in page.descendants() if isinstance(element, ui.code))
        manager.select_command(0)
        self.assertEqual(len(list(tabs)), 1)
        manager.select_command(2)
        self.assertEqual(len(list(tabs)), 2)
        self.assertEqual(tabs.value, 'two')
        manager.select_command(4)
        self.assertEqual(len(list(tabs)), 2)
        self.assertEqual(tabs.value, 'one')
        import json
        self.assertEqual(json.loads(code.content)['elements'], [])
        tabs.set_value('two')
        self.assertEqual(json.loads(code.content)['elements'][0]['code'], 'second')
        manager.select_command(0)
        self.assertEqual(len(list(tabs)), 1)
        self.assertEqual(json.loads(code.content)['elements'][0]['code'], 'first')

    async def test_unresolved_highlighting_survives_tabs_revisits_and_history(self):
        managers = []

        def make_manager(**kwargs):
            manager = CommandListManager(**kwargs)
            managers.append(manager)
            return manager

        with patch('activity_manager.page.CommandListManager', side_effect=make_manager):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
        manager = managers[0]
        manager.form_service = MemoryService([form('one', 'One'), form('two', 'Two')])

        async def resolve(data):
            return await resolve_command_schema(manager.form_service, data, manager.related_schemas,
                                                {}, {}, {}, source_schema=manager.workspace.originals[data['id']])

        manager.resolve_schema = resolve
        manager.set_schema(form('one', 'One'))
        manager.commands = parse_command_text('''
AddField(fieldCode="first", label="First", validationCondition="UNKNOWN > 0")
WithForm(form="Two")
AddField(fieldCode="second", label="Second", validationCondition="OTHER > 0")
WithForm(form="One")
WithForm(form="One")
ModifyField(fieldCode="first", fieldType="FREE_TEXT", fieldVariant="text", validationCondition="")
WithForm(form="Two")
WithForm(form="One")
''')
        manager._evaluate_commands()
        await manager._evaluation_task
        tabs = next(element for element in page.descendants() if isinstance(element, ui.tabs))
        code = next(element for element in page.descendants() if isinstance(element, ui.code))
        toggle = next(element for element in page.descendants()
                      if isinstance(element, ui.toggle) and 'names' in element.options)

        def assert_marked(token):
            markup = code.markdown._props['innerHTML']
            self.assertIn('<mark>', markup)
            self.assertIn(token, markup)

        manager.select_command(2)
        assert_marked('OTHER')
        tabs.set_value('one')
        assert_marked('UNKNOWN')
        tabs.set_value('two')
        assert_marked('OTHER')
        manager.select_command(3)
        assert_marked('UNKNOWN')
        manager.select_command(4)  # Switching to the already active form.
        assert_marked('UNKNOWN')
        toggle.set_value('names')
        assert_marked('UNKNOWN')
        toggle.set_value('ids')
        assert_marked('UNKNOWN')
        self.assertEqual(manager.executions[3].status, 'applied')
        self.assertEqual(len(manager.executions[3].issues), 1)

        manager.select_command(5)  # Removing the condition removes its annotation.
        self.assertNotIn('<mark>', code.markdown._props['innerHTML'])
        tabs.set_value('two')
        assert_marked('OTHER')
        manager.select_command(7)
        self.assertNotIn('<mark>', code.markdown._props['innerHTML'])
        manager.select_command(3)  # Later fixes must not alter historical snapshots.
        assert_marked('UNKNOWN')
        self.assertEqual(len(manager.executions[3].form_issues['one']), 1)
        self.assertEqual(manager.executions[7].form_issues['one'], ())

    async def test_newly_visited_form_highlights_a_deleted_dotted_reference_immediately(self):
        managers = []

        def make_manager(**kwargs):
            manager = CommandListManager(**kwargs)
            managers.append(manager)
            return manager

        with patch('activity_manager.page.CommandListManager', side_effect=make_manager):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
        manager = managers[0]
        one = form('one', 'One')
        two = form('two', 'Two', elements=[
            {'id': 'fieldid1', 'code': 'PERSON', 'type': 'reference', 'label': 'Person',
             'typeParameters': {'range': [{'formId': 'one'}]}},
            {'id': 'calc', 'code': 'RESULT', 'type': 'calculated', 'label': 'Result',
             'typeParameters': {'formula': 'fieldid1.fieldid2'}},
        ])
        manager.form_service = MemoryService([one, two])
        manager.resolve_schema = AsyncMock(side_effect=AssertionError('No further command should be needed.'))
        manager.set_schema(one)
        manager.commands = parse_command_text('WithForm(form="Two")')
        manager._evaluate_commands()
        await manager._evaluation_task
        manager.select_command(0)
        manager.resolve_schema.assert_not_awaited()
        code = next(element for element in page.descendants() if isinstance(element, ui.code))
        toggle = next(element for element in page.descendants()
                      if isinstance(element, ui.toggle) and 'names' in element.options)
        badge = next(element for element in page.descendants()
                     if isinstance(element, ui.badge) and element.props.get('aria-label') == 'Unresolved names')
        self.assertIn('<mark>', code.markdown._props['innerHTML'])
        self.assertEqual(badge.text, 'Unresolved: 1')
        toggle.set_value('names')
        self.assertIn('PERSON.fieldid2', code.content)
        self.assertIn('<mark>', code.markdown._props['innerHTML'])
        self.assertEqual(two['elements'][1]['typeParameters']['formula'], 'fieldid1.fieldid2')

    def test_with_form_parameter_validation(self):
        for text in ('WithForm()', 'WithForm(form=" ")', 'WithForm(form=1)',
                     'WithForm(form="One", fieldPattern=".*")'):
            with self.subTest(text=text), self.assertRaises(CommandTextError):
                parse_command_text(text)


class SequenceTests(unittest.IsolatedAsyncioTestCase):
    def setup_sequence(self):
        one, two = form('one', 'One'), form('two', 'Two')
        a, b, c = deepcopy(one), deepcopy(two), deepcopy(one)
        a['label'], b['label'], c['label'] = 'One intermediate', 'Two final', 'One final'
        return MemoryService([one, two]), {'one': one, 'two': two}, [a, b, c]

    async def test_reverse_rollback_includes_repeated_form_and_failed_update(self):
        service, originals, steps = self.setup_sequence()
        service.fail_at = 3
        outcome = await apply_schema_sequence(service, originals, steps, AsyncMock(return_value='rollback_all'))
        self.assertEqual(outcome.status, 'rolled_back', outcome.messages)
        self.assertEqual([i for i, _ in service.writes], ['one', 'two', 'one', 'one', 'two', 'one'])
        self.assertEqual(service.schemas, originals)
        self.assertEqual(service.writes[3][1]['label'], 'One intermediate')

    async def test_last_only_restores_preceding_visit(self):
        service, originals, steps = self.setup_sequence()
        service.fail_at = 3
        outcome = await apply_schema_sequence(service, originals, steps, AsyncMock(return_value='rollback_last'))
        self.assertEqual(outcome.status, 'rolled_back_last')
        self.assertEqual(service.schemas['one'], steps[0])
        self.assertEqual(service.schemas['two'], steps[1])

    async def test_leave_stops_forward_execution(self):
        service, originals, steps = self.setup_sequence()
        service.fail_at = 2
        outcome = await apply_schema_sequence(service, originals, steps, AsyncMock(return_value='leave'))
        self.assertEqual(outcome.status, 'partial_left')
        self.assertEqual(len(service.writes), 2)

    async def test_rollback_failure_stops_immediately(self):
        service, originals, steps = self.setup_sequence()
        service.fail_at, service.reject_at = 3, 4
        outcome = await apply_schema_sequence(service, originals, steps, AsyncMock(return_value='rollback_all'))
        self.assertEqual(outcome.status, 'rollback_failed')
        self.assertEqual(len(service.writes), 4)

    async def test_rollback_verification_failure_stops_immediately(self):
        service, originals, steps = self.setup_sequence()
        write = service.put_form_schema
        async def partial(identity, schema):
            await write(identity, schema)
            if len(service.writes) in (3, 4):
                service.schemas[identity]['label'] = 'Partial change'
        service.put_form_schema = partial
        outcome = await apply_schema_sequence(service, originals, steps, AsyncMock(return_value='rollback_all'))
        self.assertEqual(outcome.status, 'rollback_failed')
        self.assertEqual(len(service.writes), 4)

    async def test_single_form_and_unchanged_visits(self):
        one = form('one', 'One')
        service = MemoryService([one])
        choose = AsyncMock()
        outcome = await apply_schema_sequence(service, {'one': one}, [one], choose)
        self.assertEqual(outcome.status, 'success')
        self.assertEqual(service.writes, [])
        changed = {**one, 'label': 'Updated'}
        outcome = await apply_schema_sequence(service, {'one': one}, [changed], choose)
        self.assertEqual(outcome.status, 'success')
        self.assertEqual(service.schemas['one'], changed)
        choose.assert_not_awaited()

    async def test_preflight_checks_all_forms_before_writing(self):
        service, originals, steps = self.setup_sequence()
        service.schemas['two']['label'] = 'Concurrent edit'
        outcome = await apply_schema_sequence(service, originals, steps, AsyncMock())
        self.assertEqual(outcome.status, 'stale')
        self.assertEqual(service.writes, [])

    async def test_rejected_write_offers_full_rollback(self):
        service, originals, steps = self.setup_sequence()
        service.reject_at = 2
        choose = AsyncMock(return_value='rollback_all')
        outcome = await apply_schema_sequence(service, originals, steps, choose)
        self.assertEqual(outcome.status, 'rolled_back')
        choose.assert_awaited_once()
        self.assertEqual(service.schemas, originals)

    async def test_verification_read_failure_can_recover(self):
        service, originals, steps = self.setup_sequence()
        read = service.get_form_schema
        failed = False
        async def flaky(identity):
            nonlocal failed
            if len(service.writes) == 2 and not failed:
                failed = True
                raise ValueError('Read unavailable')
            return await read(identity)
        service.get_form_schema = flaky
        outcome = await apply_schema_sequence(service, originals, steps, AsyncMock(return_value='rollback_all'))
        self.assertEqual(outcome.status, 'rolled_back', outcome.messages)
        self.assertEqual(service.schemas, originals)

    async def test_later_stale_check_offers_recovery(self):
        service, originals, steps = self.setup_sequence()
        write = service.put_form_schema
        async def concurrent(identity, schema):
            await write(identity, schema)
            if len(service.writes) == 1:
                service.schemas['two']['label'] = 'Concurrent edit'
        service.put_form_schema = concurrent
        outcome = await apply_schema_sequence(service, originals, steps, AsyncMock(return_value='rollback_all'))
        self.assertEqual(outcome.status, 'rolled_back', outcome.messages)
        self.assertEqual(service.schemas['one'], originals['one'])
        self.assertEqual(service.schemas['two']['label'], 'Concurrent edit')

    def test_pending_ids_are_scoped_to_form(self):
        one, two = form('one', 'One'), form('two', 'Two')
        a, b = deepcopy(one), deepcopy(two)
        for schema in (a, b):
            schema['elements'] = [{'id': 'pending-field:new', 'code': 'new', 'type': 'text', 'label': 'New'}]
        operations = prepare_operations([a, b, a], {'one': one, 'two': two}, {})
        ids = [schema['elements'][0]['id'] for schema, _ in operations]
        self.assertEqual(ids[0], ids[2])
        self.assertNotEqual(ids[0], ids[1])
