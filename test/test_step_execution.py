import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from nicegui import Client, ui

from activity_manager.app_state import AppStateStore
from activity_manager.executor import Executor
from activity_manager.execution_presentation import action_summaries
from activity_manager.step_execution import StepExecutionDialog, request_details, recovery_console_message
from activity_manager.action_handlers import ResourceRequest, RevertDatabaseRequest
from test.test_executor import setup_plan, MemoryEvidenceService


class StepExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def run_interactive(self, choices, *, modes=(), mutate=None):
        initial, candidate, binding, attempt = setup_plan()
        service = MemoryEvidenceService(initial, modes)
        with Client(ui.page('/')):
            class RecordingDialog(StepExecutionDialog):
                def __init__(self, base_url):
                    super().__init__(base_url)
                    self.displayed_messages = []

                def show(self, executor, reviewed, request=None):
                    previous_action = self.console_action
                    previous_count = len(self.console_lines)
                    super().show(executor, reviewed, request)
                    if reviewed is not None:
                        index = executor.plan.actions.index(reviewed)
                        expected = action_summaries(executor.plan)[index]
                        if self.title.text != f'{index + 1}/{len(executor.plan.actions)}: {expected}':
                            raise AssertionError(self.title.text)
                    start = previous_count if self.console_action == previous_action else 0
                    self.displayed_messages.extend(self.console_lines[start:])

            dialog = RecordingDialog('https://example.test/resources')
            executor = Executor(service, step_control=dialog)
            task = asyncio.create_task(executor.execute(attempt, binding, candidate, None))
            try:
                previous_action = None
                for choice, enabled in choices:
                    for _ in range(100):
                        if dialog.pending is not None:
                            break
                        if task.done():
                            self.fail(f'Execution ended early: {task.result()}')
                        await asyncio.sleep(0)
                    self.assertIsNotNone(dialog.pending)
                    if dialog.console_action != previous_action:
                        self.assertEqual(dialog.console.text, '')
                        previous_action = dialog.console_action
                    elif choice == 'continue' and 'rollback' in enabled:
                        self.assertIn('Updated and verified (HTTP 200)', dialog.console.text)
                        self.assertEqual(len(dialog.console.text.splitlines()), 1)
                        self.assertNotIn('intended', dialog.console.text)
                    active = {name for name in ('execute', 'rollback', 'back', 'next') if getattr(dialog, name).enabled}
                    self.assertEqual(active, set(enabled))
                    self.assertEqual(dialog.stop_button.enabled, not any(
                        getattr(dialog, name).text == 'End' for name in enabled))
                    if mutate and choice == 'rollback':
                        service.schemas['a']['label'] = 'Concurrent edit'
                    if choice == 'stop':
                        dialog.stop()
                    else:
                        dialog.submit(choice)
                    await asyncio.sleep(0)
                report = await asyncio.wait_for(task, 3)
                finish = asyncio.create_task(dialog.finish(executor, report))
                await asyncio.sleep(0)
                if not finish.done():
                    end_button = dialog.back if dialog.rolling_back else dialog.next
                    self.assertTrue(end_button.enabled)
                    self.assertEqual(end_button.text, 'End')
                    self.assertFalse(dialog.stop_button.enabled)
                    if dialog.rolling_back:
                        self.assertFalse(dialog.next.enabled)
                        if report.status == 'rolled_back_all':
                            self.assertIn('update(s) restored and verified', dialog.console.text)
                    dialog.submit('back' if dialog.rolling_back else 'continue')
                await asyncio.wait_for(finish, 3)
                remaining = iter(report.console_messages)
                for displayed in dialog.displayed_messages:
                    self.assertTrue(any(line == displayed for line in remaining), displayed)
                return report, service
            finally:
                if not task.done():
                    task.cancel()
                dialog.dialog.close()

    async def test_forward_controls_and_single_write(self):
        report, service = await self.run_interactive([
            ('execute', ['execute']), ('continue', ['next', 'rollback']),
            ('execute', ['execute']), ('continue', ['next', 'rollback'])])
        self.assertEqual(report.status, 'success')
        self.assertEqual(len(service.writes), 2)

    async def test_forced_rollback_locks_direction(self):
        report, service = await self.run_interactive([
            ('execute', ['execute']), ('rollback', ['next', 'rollback'])])
        self.assertEqual(report.status, 'rolled_back_all')
        self.assertEqual(len(service.writes), 2)

    async def test_failure_unchanged_skips_rollback_write(self):
        report, service = await self.run_interactive([
            ('execute', ['execute'])], modes=['unchanged_error'])
        self.assertEqual(report.status, 'rolled_back_all')
        self.assertEqual(len(service.writes), 1)

    async def test_stop_before_write(self):
        report, service = await self.run_interactive([('stop', ['execute'])])
        self.assertEqual(report.status, 'interrupted')
        self.assertEqual(service.writes, [])

    async def test_failure_rolls_back_in_reverse(self):
        report, service = await self.run_interactive([
            ('execute', ['execute']), ('continue', ['next', 'rollback']),
            ('execute', ['execute']), ('rollback', ['rollback']),
            ('back', ['back']), ('rollback', ['rollback'])],
            modes=['commit', 'commit_error'])
        self.assertEqual(report.status, 'rolled_back_all')
        self.assertEqual([identity for identity, _ in service.writes], ['a', 'b', 'b', 'a'])

    async def test_regular_and_step_execution_have_identical_complete_logs(self):
        stepped, _ = await self.run_interactive([
            ('execute', ['execute']), ('continue', ['next', 'rollback']),
            ('execute', ['execute']), ('rollback', ['rollback']),
            ('back', ['back']), ('rollback', ['rollback'])],
            modes=['commit', 'commit_error'])
        initial, candidate, binding, attempt = setup_plan()
        service = MemoryEvidenceService(initial, modes=['commit', 'commit_error'])
        async def choose_recovery(_):
            return 'rollback_all'
        regular = await Executor(service).execute(attempt, binding, candidate, choose_recovery)
        self.assertEqual(regular.console_messages, stepped.console_messages)
        lines = regular.console_messages
        sent = lines.index('Rollback: Request sent; awaiting verification.')
        verified = lines.index('Rollback: Restored and verified.')
        self.assertLess(sent, verified)
        self.assertTrue(any('HTTP 200' in line for line in lines))
        self.assertEqual(lines[-1], '2 update(s) restored and verified.')

    async def test_concurrent_change_blocks_paused_rollback(self):
        report, service = await self.run_interactive([
            ('execute', ['execute']), ('rollback', ['next', 'rollback'])], mutate=True)
        self.assertEqual(report.status, 'recovery_blocked')
        self.assertEqual(len(service.writes), 1)

    async def test_stop_during_write_retains_verified_evidence(self):
        initial, candidate, binding, attempt = setup_plan()
        service = MemoryEvidenceService(initial)
        with Client(ui.page('/')):
            dialog = StepExecutionDialog('https://example.test')
            service.before_write = lambda *_: dialog.stop()
            executor = Executor(service, step_control=dialog)
            task = asyncio.create_task(executor.execute(attempt, binding, candidate, None))
            await asyncio.sleep(0)
            dialog.submit('execute')
            report = await asyncio.wait_for(task, 3)
            self.assertEqual(report.status, 'interrupted')
            self.assertEqual(len(service.writes), 1)
            self.assertEqual(report.journal[0].status, 'verified_applied')
            self.assertIsNotNone(report.journal[0].postimage)

    async def test_rollback_rechecks_after_pause(self):
        initial, candidate, binding, attempt = setup_plan()
        service = MemoryEvidenceService(initial)
        async def control(executor, phase, reviewed, request):
            if phase == 'applied':
                return 'rollback'
            if phase == 'rollback':
                service.schemas['a']['label'] = 'Changed during pause'
        executor = Executor(service, step_control=control)
        report = await executor.execute(attempt, binding, candidate, None)
        self.assertEqual(report.status, 'recovery_blocked')
        self.assertEqual(len(service.writes), 1)

    def test_recovery_console_uses_plain_language(self):
        for outcome, expected in (
                ('intended', 'applied and verified'),
                ('unchanged', 'No changes were made'),
                ('divergent', 'differs from the requested changes'),
                ('unknown', 'could not be verified')):
            message = recovery_console_message(SimpleNamespace(status='reconciled', message=outcome))
            self.assertIn(expected, message)
            self.assertNotIn('intended', message)
        message = recovery_console_message(SimpleNamespace(status='verified_restored', message=''))
        self.assertEqual(message, 'Rollback: Restored and verified.')

    def test_preference_and_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AppStateStore(Path(directory) / 'state.json')
            store.save(api_token='', dbid=None, formid=None, commands='', execute_step_by_step=True)
            self.assertTrue(store.load()[0]['execute_step_by_step'])
        self.assertEqual(request_details('https://example.test', ResourceRequest('db', {'formClass': {}}))[0],
                         'POST https://example.test/databases/db/forms')
        self.assertEqual(request_details('https://example.test', RevertDatabaseRequest('db', '2')),
                         ('POST https://example.test/databases/db/versions/2/revert', None))
