"""Interactive gates around the production executor; no independent API writes."""
import asyncio
import json
from urllib.parse import quote

from nicegui import ui

from .action_handlers import SchemaRequest, RevertDatabaseRequest
from .executor import StepStopped
from .execution_presentation import recovery_console_message, action_summaries
from .immutable import thaw
from .planning import ActionKind, PlanError


def request_details(base_url, request):
    if request is None:
        return 'Not required', None
    if isinstance(request, SchemaRequest):
        path = '/form/' + quote(request.form_id, safe='') + '/schema'
    else:
        path = '/databases/' + quote(request.database_id, safe='')
        if isinstance(request, RevertDatabaseRequest):
            path += '/versions/' + quote(request.version, safe='') + '/revert'
        elif 'formClass' in thaw(request.schema):
            path += '/forms'
    return 'POST ' + base_url.rstrip('/') + path, (None if isinstance(request, RevertDatabaseRequest) else thaw(request.schema))




class StepExecutionDialog:
    def __init__(self, base_url, *, database_names=None):
        self.base_url = base_url
        self.database_names = dict(database_names or {})
        self.stopped = False
        self.pending = None
        self.rollback_credit = False
        self.rolling_back = False
        self.ended = False
        self.console_action = None
        self.console_lines = []
        self.seen_console_entries = set()
        with ui.dialog().props('persistent') as self.dialog:
            with ui.card().classes('w-full max-w-4xl'):
                self.title = ui.label('Preparing execution').classes('text-lg font-medium')
                self.url = ui.label().classes('w-full break-all font-mono text-sm')
                with ui.expansion('Payload').classes('w-full'):
                    self.payload = ui.code('', language='json').classes('w-full max-h-64 overflow-auto')
                with ui.expansion('Rollback request').classes('w-full'):
                    self.rollback_url = ui.label().classes('w-full break-all font-mono text-sm')
                    self.rollback_payload = ui.code('', language='json').classes('w-full max-h-64 overflow-auto')
                with ui.row().classes('w-full flex-nowrap'):
                    self.rollback = ui.button('Execute rollback', on_click=lambda: self.submit('rollback')).classes('flex-1')
                    self.execute = ui.button('Execute', on_click=lambda: self.submit('execute')).classes('flex-1')
                self.console = ui.label().classes('w-full h-56 overflow-auto whitespace-pre-wrap font-mono text-sm bg-slate-100 p-2')
                with ui.row().classes('w-full'):
                    self.back = ui.button('Continue back', on_click=lambda: self.submit('back')).classes('flex-1')
                    self.stop_button = ui.button('Stop', on_click=self.stop).props('color=negative').classes('flex-1')
                    self.next = ui.button('Continue', on_click=lambda: self.submit('continue')).classes('flex-1')
        self.controls = (self.execute, self.rollback, self.back, self.next)
        self.disable()
        self.dialog.open()

    def disable(self):
        for control in self.controls:
            control.disable()

    def submit(self, value):
        if self.pending is not None and not self.pending.done():
            self.disable()
            self.pending.set_result(value)

    def stop(self):
        self.stopped = True
        self.submit('stop')
        self.dialog.close()

    async def wait(self, *enabled):
        if self.stopped:
            raise StepStopped()
        self.pending = asyncio.get_running_loop().create_future()
        self.disable()
        for control in enabled:
            control.enable()
        self.stop_button.set_enabled(not any(control.text == 'End' for control in enabled))
        try:
            result = await self.pending
        finally:
            self.disable()
            self.pending = None
        if self.stopped:
            raise StepStopped()
        return result

    def show(self, executor, reviewed, request=None):
        if reviewed is not None:
            index = executor.plan.actions.index(reviewed) + 1
            summary = action_summaries(executor.plan, self.database_names)[index - 1]
            self.title.set_text(f'{index}/{len(executor.plan.actions)}: {summary}')
            url, payload = request_details(self.base_url, reviewed.request)
            self.url.set_text(url)
            self.payload.set_content(json.dumps(payload, indent=2))
            try:
                entry = next((e for e in reversed(executor.journal) if e.reviewed == reviewed), None)
                handler = executor.handler(reviewed.action)
                if request is None:
                    preimage = entry.preimage.state if entry else reviewed.action.before
                    request = (handler.recovery_request(reviewed.action, preimage, entry.evidence if entry else None)
                               if reviewed.action.kind == ActionKind.FORM_DELETE else
                               handler.recovery_request(reviewed.action, preimage))
                url, payload = request_details(self.base_url, request)
            except PlanError as exc:
                url, payload = str(exc), None
            self.rollback_url.set_text(url)
            self.rollback_payload.set_content(json.dumps(payload, indent=2))
        entries = list(enumerate(executor.console_messages))
        if reviewed is not None and reviewed != self.console_action:
            self.console_action = reviewed
            self.console_lines = []
        else:
            self.console_lines.extend(line for key, line in entries if key not in self.seen_console_entries)
        self.seen_console_entries.update(key for key, _ in entries)
        self.console.set_text('\n'.join(self.console_lines))

    async def __call__(self, executor, phase, reviewed, request):
        if self.stopped:
            raise StepStopped()
        if phase in ('rollback', 'rollback_skip', 'restored'):
            self.rolling_back = True
        self.show(executor, reviewed, request if phase in ('rollback', 'restored') else None)
        if phase == 'forward':
            self.next.set_text('Continue')
            await self.wait(self.execute)
        elif phase == 'applied':
            last = reviewed == executor.plan.actions[-1]
            self.next.set_text('End' if last else 'Continue')
            choice = await self.wait(self.next, self.rollback)
            if choice == 'rollback':
                self.rolling_back = True
                self.rollback_credit = True
                return 'rollback'
            self.ended = last
        elif phase == 'rollback':
            if self.rollback_credit:
                self.rollback_credit = False
            else:
                await self.wait(self.rollback)
        elif phase in ('rollback_skip', 'restored'):
            if phase == 'rollback_skip':
                self.rollback_url.set_text('No rollback API call required: this attempt made no changes.')
                self.rollback_payload.set_content('null')
            self.rollback_credit = False
            if reviewed != executor.journal[0].reviewed:
                await self.wait(self.back)
        return None

    async def finish(self, executor, outcome):
        self.show(executor, None)
        if not self.stopped and not self.ended:
            self.rolling_back = self.rolling_back or outcome.status in ('recovery_blocked', 'recovery_failed')
            end_button = self.back if self.rolling_back else self.next
            end_button.set_text('End')
            try:
                await self.wait(end_button)
            except StepStopped:
                pass
        self.dialog.close()
