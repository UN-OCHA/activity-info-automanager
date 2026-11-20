"""Mocked page workflows through real Apply, confirmation, executor and reporting."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import parse_command_text
from activity_manager.listbox import DatabaseListbox
from activity_manager.page import connection_page
from activity_manager.immutable import thaw
from test.test_executor import MemoryEvidenceService
from test.test_planning import schema_state


class PageService(MemoryEvidenceService):
    async def get_form_names(self, database):
        return {key: data['label'] for key, data in self.schemas.items() if data.get('databaseId') == database}

    async def query_rows(self, query):
        self.queries = getattr(self, 'queries', 0) + 1
        return [{'_id': 'first', 'label': 'Alice'}]
    async def get_databases(self):
        return {'db': 'Database'}

    async def get_forms(self, database):
        return {key: data['label'] for key, data in self.schemas.items()}

    async def get_database_names(self):
        return {'db': 'Database'}

    async def get_form_tree(self, identity):
        return deepcopy(self.schemas)

    async def get_form_schema(self, identity):
        return deepcopy(self.schemas[identity])

    async def aclose(self):
        pass


def callback(button):
    handler = next(iter(button._event_listeners.values())).handler
    return handler.__closure__[0].cell_contents if isinstance(button, ui.button) else handler


class ExecutorPageTests(unittest.IsolatedAsyncioTestCase):
    async def build(self, modes=(), text='AddField(fieldCode="NEW", label="New")', initial=None):
        service = PageService(initial or {'a': schema_state()}, modes)
        managers = []

        def make_manager(**kwargs):
            manager = CommandListManager(**kwargs)
            managers.append(manager)
            return manager

        with patch('activity_manager.page.ActivityInfoService', return_value=service), \
                patch('activity_manager.page.CommandListManager', side_effect=make_manager):
            self.client = Client(ui.page('/'))
            with self.client, ui.column() as page:
                connection_page()
            button = next(item for item in page.descendants() if isinstance(item, ui.button) and item.text == 'Connect')
            connect = callback(button)
            await connect()
            databases, forms = [item for item in page.descendants() if isinstance(item, DatabaseListbox)]
            await callback(databases)(SimpleNamespace(args='db'))
            await callback(forms)(SimpleNamespace(args='a'))
        manager = managers[0]
        manager.commands = parse_command_text(text)
        manager._regenerate_advanced_draft()
        manager._evaluate_commands()
        await manager._evaluation_task
        self.assertTrue(all(item.status == 'applied' for item in manager.executions),
                        [item.message for item in manager.executions])
        return service, manager, page

    async def finish_evaluation(self, manager):
        if manager._evaluation_task:
            await manager._evaluation_task

    async def test_confirmed_payload_is_exactly_sent_and_report_survives_reevaluation(self):
        service, manager, page = await self.build()
        with self.client:
            manager.on_execute()
            self.assertEqual(service.writes, [])
            self.assertFalse(any(isinstance(item, ui.code) and item.content.startswith('[\n')
                                 for item in self.client.layout.descendants()))
            ok = [item for item in self.client.layout.descendants() if isinstance(item, ui.button) and item.text == 'Execute'][-1]
            execute = callback(ok)
            closure = dict(zip(execute.__code__.co_freevars, (cell.cell_contents for cell in execute.__closure__)))
            reviewed = [thaw(item.request.schema) for item in closure['pending_execution']['attempt'].plan.actions]
            await callback(ok)()
            report = manager.execution_report
            self.assertEqual(report.status, 'success')
            self.assertTrue(any(isinstance(item, ui.label) and item.text == '\n'.join(report.console_messages)
                                for item in page.descendants()))
            self.assertEqual(service.writes[0][1], reviewed[0])
            self.assertEqual(manager.resolution_cache, {})
            self.assertEqual(manager.reference_defaults, {})
            await self.finish_evaluation(manager)
            self.assertIs(manager.execution_report, report)
            self.assertFalse(manager.busy)
            service.schemas['a']['label'] = 'Later change'
            self.assertEqual(report.actual_schemas['a']['label'], 'A')

    async def test_cancel_then_stale_ok_sends_no_requests(self):
        service, manager, page = await self.build()
        with self.client:
            manager.on_execute()
            cancel = next(item for item in self.client.layout.descendants() if isinstance(item, ui.button) and item.text == 'Cancel')
            callback(cancel)()
            ok = [item for item in self.client.layout.descendants() if isinstance(item, ui.button) and item.text == 'Execute'][-1]
            await callback(ok)()
        self.assertEqual(service.writes, [])
        self.assertEqual(service.reads, [])

    async def test_edit_after_confirmation_invalidates_without_any_endpoint_reads(self):
        service, manager, page = await self.build()
        with self.client:
            manager.on_execute()
            manager.commands = parse_command_text('AddField(fieldCode="DIFFERENT", label="Different")')
            manager._evaluate_commands()
            await self.finish_evaluation(manager)
            ok = [item for item in self.client.layout.descendants() if isinstance(item, ui.button) and item.text == 'Execute'][-1]
            await callback(ok)()
        self.assertEqual(service.writes, [])
        self.assertEqual(service.reads, [])
        self.assertFalse(manager.busy)

    async def test_each_recovery_choice_uses_new_journal_and_preserves_forward_evidence(self):
        for choice, expected_writes in (('leave', 1), ('rollback_last', 2), ('rollback_all', 2)):
            service, manager, page = await self.build(modes=('commit_error',))
            with self.client:
                manager.on_execute()
                ok = [item for item in self.client.layout.descendants() if isinstance(item, ui.button) and item.text == 'Execute'][-1]
                async def click():
                    with self.client:
                        await callback(ok)()
                task = asyncio.create_task(click())
                for _ in range(30):
                    await asyncio.sleep(0)
                    dialogs = [item for item in self.client.layout.descendants()
                               if isinstance(item, ui.dialog) and item.value]
                    if dialogs:
                        break
                self.assertTrue(dialogs)
                dialogs[-1].submit(choice)
                await asyncio.wait_for(task, 3)
                self.assertEqual(len(service.writes), expected_writes)
                report = manager.execution_report
                self.assertIsNotNone(report.journal[0].evidence.failure)
                self.assertEqual(report.journal[0].outcome, 'intended')
                self.assertEqual(report.status, 'left' if choice == 'leave' else
                                 'rolled_back_last' if choice == 'rollback_last' else 'rolled_back_all')
                await self.finish_evaluation(manager)

    async def test_schema_change_while_dialog_open_cancels_the_attempt(self):
        service, manager, page = await self.build()
        with self.client:
            manager.on_execute()
            forms = [item for item in page.descendants() if isinstance(item, DatabaseListbox)][1]
            await callback(forms)(SimpleNamespace(args=None))
            ok = [item for item in self.client.layout.descendants() if isinstance(item, ui.button) and item.text == 'Execute'][-1]
            await callback(ok)()
        self.assertEqual(service.writes, [])

    async def test_reference_default_survives_frozen_confirmation_without_requery(self):
        from dataclasses import replace
        from activity_manager.resource_state import FormPayload
        people = schema_state('people')
        data = people.payload.to_dict()
        data.update(recordLabelFieldId='name', elements=[dict(id='name', code='NAME', label='Name', type='FREE_TEXT', required=False)])
        people = replace(people, payload=FormPayload(data))
        service, manager, page = await self.build(initial={'a': schema_state(), 'people': people},
            text='AddField(fieldCode="REF", label="Reference", fieldType="reference", referencedForm="people", defaultValue="Alice")')
        with self.client:
            queries = service.queries
            manager.on_execute()
            self.assertEqual(service.queries, queries)
            ok = [item for item in self.client.layout.descendants() if isinstance(item, ui.button) and item.text == 'Execute'][-1]
            await callback(ok)()
            report = manager.execution_report
            self.assertEqual(report.status, 'success', report.messages)
            self.assertEqual(service.queries, queries)
            self.assertEqual(report.plan.reference_defaults[('people', 'str', 'Alice')], 'people:first')
            self.assertEqual(service.writes[0][1]['elements'][-1]['defaultValue'], 'people:first')
            await self.finish_evaluation(manager)

    async def test_remote_stale_edit_is_reported_before_any_write(self):
        service, manager, page = await self.build()
        service.schemas['a']['label'] = 'Changed remotely'
        with self.client:
            manager.on_execute()
            ok = [item for item in self.client.layout.descendants() if isinstance(item, ui.button) and item.text == 'Execute'][-1]
            await callback(ok)()
            report = manager.execution_report
            self.assertEqual(report.status, 'preflight_failed')
            self.assertEqual(service.writes, [])
            self.assertEqual(report.actual_schemas['a']['label'], 'Changed remotely')
            self.assertEqual(manager.resolution_cache, {})
            await self.finish_evaluation(manager)
