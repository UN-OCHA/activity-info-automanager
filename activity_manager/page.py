from nicegui import app, ui

import json
import asyncio
from copy import deepcopy
from dataclasses import replace

from nicegui import context

from .app_state import AppStateStore
from .api import DEFAULT_BASE_URL, ActivityInfoService
from .command_creator import CommandCreatorRenderer, field_codes_from_schema
from .form_comparison_ui import FormComparisonRenderer
from .form_folders import form_location
from .schema_names import readable_resource, load_default_record_labels, unresolved_formula_references
from .command_execution import Diagnostic, EXECUTABLE_STATUSES
from .immutable import thaw
from .schema_display import underline_substitutions
from .schema_resolution import resolve_command_schema
from .command_list import CommandExecution, CommandListManager
from .commands import COMMAND_CREATORS
from .listbox import DatabaseListbox
from .confirmed_plan import AttemptBinding, ConfirmationAttempt
from .executor import Executor
from .execution_presentation import confirmation_summary
from .step_execution import StepExecutionDialog
from .application_results import DisplayResource, DisplayState, DisplayTab, display_payload
from .resource_state import ResourceKey, ResourceKind, Lifecycle, Existence


def ordered_result_tabs(tabs):
    """Sort databases and their form children by their displayed names."""
    tabs = sorted(tabs, key=lambda tab: (tab.label.casefold(), tab.identity))
    databases = [tab for tab in tabs if tab.key.kind == ResourceKind.DATABASE_RESOURCES]
    forms = [tab for tab in tabs if tab.key.kind == ResourceKind.FORM_SCHEMA]
    grouped = set()
    ordered = []
    for database in databases:
        ordered.append((database, False))
        for form in forms:
            if form.payload.get('databaseId') == database.key.identity:
                ordered.append((form, True))
                grouped.add(form.identity)
    ordered.extend((form, False) for form in forms if form.identity not in grouped)
    return ordered


def connection_page():
    state_store = AppStateStore()
    restoring_state = False

    def save_state():
        if restoring_state:
            return
        try:
            state_store.save(api_token=token.value or '', dbid=databases.value,
                             formid=forms.value,
                             execute_step_by_step=command_list.step_checkbox.value,
                             commands='\n'.join(command.to_syntax() for command in command_list.commands))
        except (OSError, ValueError):
            console_message.set_text('Application state could not be saved.')

    ui.add_css('''
        .nicegui-content {
            padding: 0 !important;
        }
        .workbench {
            box-sizing: border-box;
            display: grid;
            grid-template-columns: minmax(260px, 300px) minmax(0, 1fr) minmax(480px, 570px);
            grid-template-rows: auto minmax(360px, 1fr) minmax(140px, 22vh);
            gap: 0.75rem;
            min-height: 720px;
            height: 100vh;
            padding: 0.75rem;
            width: 100%;
        }
        .workbench-panel {
            min-height: 0;
            min-width: 0;
            overflow: hidden;
        }
        .sources-panel {
            grid-column: 1;
            grid-row: 1 / 4;
            overflow-y: auto;
        }
        .source-listbox {
            box-sizing: border-box;
            border: 1px solid #cbd5e1;
            border-radius: 6px;
            padding: 10px;
            background-color: #fff;
            color: #334155;
            font: inherit;
        }
        .source-listbox option {
            padding: 4px 8px;
        }
        .source-listbox:focus-visible {
            outline: 2px solid #3b82f6;
            outline-offset: 2px;
        }
        .source-listbox:disabled {
            background-color: #f8fafc;
            color: #64748b;
        }
        .command-creator-panel {
            grid-column: 2 / 4;
            grid-row: 1;
            max-height: 45vh;
            overflow-y: auto;
        }
        .result-panel {
            grid-column: 2;
            grid-row: 2;
        }
        .command-list-panel {
            grid-column: 3;
            grid-row: 2;
        }
        .command-list-panel > :not(.command-list-body):not(.command-text-body) {
            flex-shrink: 0;
        }
        .command-list-body, .command-text-body {
            flex: 1 1 0;
            min-height: 0;
            flex-wrap: nowrap;
        }
        .command-list-body {
            overflow-y: auto;
        }
        .command-text-body {
            overflow: hidden;
        }
        .command-text-editor {
            flex: 1 1 0;
            min-height: 0;
            min-width: 0;
            overflow: hidden;
        }
        .command-text-editor .cm-editor {
            height: 100%;
            min-height: 0;
        }
        .command-text-editor .cm-scroller {
            overflow: auto;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 0.875rem;
        }
        .console-panel {
            grid-column: 2 / 4;
            grid-row: 3;
            flex-wrap: nowrap;
        }
        .console-panel > .panel-body {
            flex: 1 1 0;
        }
        .panel-body {
            min-height: 0;
            overflow: auto;
        }
        .result-body {
            flex: 1 1 0;
            min-height: 0;
            overflow: hidden;
            position: relative;
            width: 100%;
        }
        .result-placeholder {
            left: 50%;
            position: absolute;
            top: 50%;
            transform: translate(-50%, -50%);
        }
        .result-code {
            height: 100%;
            margin: 0;
            min-height: 0;
            width: 100%;
        }
        .result-code u { text-decoration: underline; text-underline-offset: 3px; }
        .result-tabs { width: 240px; min-height: 0; overflow: hidden; }
        .result-tabs .q-tabs__content {
            min-height: 0;
            justify-content: flex-start;
            overflow-x: hidden;
            overflow-y: auto;
            scrollbar-width: auto;
        }
        .result-tabs .q-tabs__content::-webkit-scrollbar { display: block; width: 8px; }
        .result-tabs .q-tabs__arrow { display: none; }
        .result-tabs .q-tab {
            flex: none;
            justify-content: flex-start;
            padding: 8px;
            min-height: 60px;
            background-color: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 4px;
            text-transform: none;
            opacity: 1;
        }
        .result-tabs .q-tab--active {
            background-color: #bfdbfe;
            border-color: #3b82f6;
        }
        /* Quasar's vertical tab container uses display: block, so gap has no effect. */
        .result-tabs .q-tab + .q-tab { margin-top: 0.5rem; }
        .result-tabs .q-tab__indicator { width: 4px; }
        .result-tabs .q-tab__label { display: none; }
        .result-tabs .q-tab__content { width: 100%; min-width: 0; align-items: flex-start; }
        .result-tabs .result-tab-name {
            white-space: normal;
            overflow-wrap: anywhere;
            text-align: left;
        }
        .result-tabs .result-tab-child { margin-left: 20px; overflow: visible; }
        .result-tabs .result-tab-child::before {
            content: '';
            position: absolute;
            left: -20px;
            top: -5px;
            bottom: -5px;
            border-left: 2px solid #cbd5e1;
        }
        .result-code mark {
            color: inherit;
            background-color: #fef3c7;
            text-decoration: underline wavy;
            text-underline-offset: 3px;
        }
        @media (max-width: 1100px) {
            .workbench {
                grid-template-columns: minmax(240px, 280px) minmax(0, 1fr);
                grid-template-rows: auto minmax(420px, 1fr) auto minmax(140px, 22vh);
                height: auto;
                min-height: 100vh;
            }
            .sources-panel { grid-column: 1; grid-row: 1 / 5; }
            .command-creator-panel { grid-column: 2; grid-row: 1; }
            .result-panel { grid-column: 2; grid-row: 2; }
            .command-list-panel {
                grid-column: 2;
                grid-row: 3;
                height: max(360px, 50vh);
            }
            .console-panel { grid-column: 2; grid-row: 4; }
        }
        @media (max-width: 700px) {
            .workbench {
                display: flex;
                flex-direction: column;
            }
            .workbench-panel { min-height: auto; }
            .result-panel { min-height: 60vh; }
            .console-panel { min-height: 10rem; }
        }
    ''')

    service: ActivityInfoService | None = None
    source_schema: dict | None = None
    pending_execution: dict | None = None
    text_mode = False
    form_request = 0
    schema_request = 0
    form_field_request = 0
    async def refresh_reference_suggestions(creator):
        nonlocal form_field_request
        form_field_request += 1
        request = form_field_request
        active = command_list.workspace.active_schema if command_list.workspace.form_snapshots else source_schema
        control = creator.controls.get('referenced_form')
        reference = control.value if control is not None else None
        data = None
        if reference and active:
            data = active if reference == active.get('id') else command_list.related_schemas.get(reference)
            if data is None:
                matches = []
                for identity, candidate in command_list.related_schemas.items():
                    db_id = candidate.get('databaseId')
                    name = candidate.get('label')
                    qualified = f'{command_list.database_names.get(db_id)}/{name}'
                    if (reference == name and db_id == active.get('databaseId')) or reference == qualified:
                        matches.append(candidate)
                if len(matches) == 1:
                    data = matches[0]
        creator.set_suggestions('reference_field_codes', field_codes_from_schema(data))
        command_key = creator.command_select.value
        active_database_target = command_key in ('add_form', 'add_modify_form')
        if not active_database_target and command_key not in ('modify_form', 'move_form'):
            return
        moving = active_database_target or command_key == 'move_form'
        multiple = creator._multiple_forms()
        source = 'folder_paths' if moving else 'form_field_codes'
        creator.set_suggestions(source, [])
        source_request = schema_request
        evaluation = command_list._evaluation_version
        if moving:
            active_database_id = command_list.workspace.state.active_database_id
            current_form = active_database_target or creator._current_form()
            selected = None if current_form else creator.controls['form'].value
            database_id = active_database_id
            try:
                from .form_folders import folder_choices
                if not multiple and selected and '/' in selected:
                    database_name = selected.split('/', 1)[0]
                    if '${' in database_name:
                        return
                    names = await command_list.form_service.get_database_names()
                    matches = [identity for identity, name in names.items() if name == database_name]
                    if len(matches) != 1:
                        return
                    database_id = matches[0]
                if not database_id:
                    return
                resources = await command_list.form_service.get_database_resources(database_id)
                suggestions = folder_choices(database_id, resources)
                if (request == form_field_request and source_request == schema_request
                        and evaluation == command_list._evaluation_version
                        and creator.command_select.value == command_key
                        and creator._multiple_forms() == multiple
                        and (active_database_target or creator._current_form() == current_form)
                        and (None if current_form else creator.controls['form'].value) == selected
                        and command_list.workspace.state.active_database_id == active_database_id):
                    creator.set_suggestions(source, suggestions)
            except (ValueError, KeyError, TypeError, AttributeError):
                pass  # Suggestions are optional; application validates the destination.
            return
        if multiple or not active:
            return
        if creator._current_form():
            creator.set_suggestions(source, field_codes_from_schema(active))
            return
        selected = creator.controls['form'].value
        if not selected or '${' in selected:
            return
        try:
            from .schema_names import resolve_form_names
            probe = {**active, 'elements': [{'type': 'reference', 'typeParameters': {
                'range': [{'formId': selected}]}}]}
            resolved = await resolve_form_names(command_list.form_service, probe,
                                                dict(command_list.related_schemas), load_related=False)
            identity = resolved['elements'][0]['typeParameters']['range'][0]['formId']
            data = command_list.workspace.latest.get(identity) or command_list.related_schemas.get(identity)
            if data is None:
                data = await command_list.form_service.get_form_schema(identity)
            suggestions = field_codes_from_schema(data)
            if (request == form_field_request and source_request == schema_request
                    and evaluation == command_list._evaluation_version
                    and creator.command_select.value == command_key and creator._multiple_forms() == multiple
                    and creator.controls['form'].value == selected):
                creator.set_suggestions(source, suggestions)
        except (ValueError, KeyError, TypeError, AttributeError):
            # Suggestions are optional; application reports invalid selectors.
            pass


    async def close_service():
        nonlocal service
        current, service = service, None
        if current:
            await current.aclose()

    report_schemas = None
    report_version = None
    report_context = {}
    report_display = None
    result_resources = {}
    result_context = {}
    result_database_names = {}
    result_historical = False
    displayed_result = None
    displayed_empty_message = 'No result'
    displayed_names = None
    record_cache = {}
    result_request = 0
    displayed_issues = ()

    class PreviewFormService:
        def __getattr__(self, name):
            from .schema_resolution import CachedResolutionService
            if service is None:
                raise ValueError('Connect before switching forms.')
            return getattr(CachedResolutionService(service, command_list.resolution_cache), name)

    async def resolve_preview(data):
        if service is None:
            raise ValueError('Connect before resolving command names.')
        return await resolve_command_schema(service, data, command_list.related_schemas,
            command_list.database_names, command_list.reference_defaults, command_list.resolution_cache,
            source_schema=command_list.workspace.baseline_forms.get(data.get("id"), data))

    async def lookup_variable(command, schema):
        from .commands.set_variable import query_variable
        from .schema_resolution import CachedResolutionService
        if service is None:
            raise ValueError('Connect before looking up variables.')
        return await query_variable(CachedResolutionService(service, command_list.resolution_cache),
                                    command, schema, command_list.related_schemas)

    def select_result_resource(identity):
        if identity in result_resources:
            tab = result_resources[identity]
            show_result(None if tab.deleted else thaw(tab.payload), 'Form deleted' if tab.deleted else 'No result', names=None if tab.deleted else thaw(tab.names), issues=tab.annotations,
                        kind=tab.key.kind, database_id=tab.key.identity)

    def show_results(snapshots, active=None, empty_message='No result', related=None, snapshot=None):
        nonlocal result_resources, result_context, result_database_names, result_historical
        result_historical = snapshot is not None
        if snapshot is not None:
            active = (('database:' if snapshot.preferred_key.kind.value == 'database_resources' else '') +
                      snapshot.preferred_key.identity) if snapshot.preferred_key is not None else None
            result_resources = {tab.identity: tab for tab in snapshot.tabs()}
            result_context = thaw(snapshot.related_schemas)
            result_database_names = thaw(snapshot.database_names)
        else:
            result_database_names = deepcopy(command_list.database_names)
            result_resources = {data.get('id', '__initial__'): DisplayTab(
                ResourceKey.form(data.get('id') or '__initial__'),
                data.get('label') or data.get('id') or 'Initial form', data) for data in snapshots}
            result_context = {**{identity: data for identity, data in command_list.related_schemas.items()
                                  if identity not in command_list.workspace.baseline_forms},
                              **command_list._base_related, **(related or {}),
                              **{identity: thaw(tab.payload) for identity, tab in result_resources.items()}}
        result_tabs.clear()
        with result_tabs:
            for tab, child in ordered_result_tabs(result_resources.values()):
                with ui.tab(tab.identity, label=tab.label).classes('result-tab-child' if child else ''):
                    ui.label('Database' if tab.key.kind == ResourceKind.DATABASE_RESOURCES else 'Form').classes(
                        'text-xs text-blue-700 normal-case')
                    ui.label(tab.label).classes('result-tab-name text-sm normal-case' + (' text-red-600 line-through' if tab.deleted else ''))
        result_tabs.set_visibility(bool(result_resources))
        identity = active if active in result_resources else next(iter(result_resources), None)
        result_tabs.set_value(identity)
        if identity is None:
            show_result(None, empty_message)
        else:
            select_result_resource(identity)

    def show_result(data: dict | list | None, empty_message='No result', names=None, issues=(),
                    kind=ResourceKind.FORM_SCHEMA, database_id=None):
        nonlocal displayed_result, displayed_names, result_request, displayed_issues, displayed_empty_message
        displayed_empty_message = empty_message
        # Loaded schemas and form switches may have no command diagnostics.
        # Detect formula failures from the displayed snapshot's own context,
        # preserving other annotations and avoiding duplicate formula warnings.
        annotated_paths = {issue.path for issue in issues}
        formula_issues = tuple(
            Diagnostic('warning', f'Unresolved formula reference {token!r}.', path,
                       code='unresolved_formula_reference')
            for path, token, _ in unresolved_formula_references(data, result_context)
            if path not in annotated_paths
        ) if data is not None and kind == ResourceKind.FORM_SCHEMA else ()
        displayed_issues = tuple(issues) + formula_issues
        result_request += 1
        displayed_result = data
        displayed_names = names if names is not None else (
            readable_resource(kind, data, result_context, result_database_names, database_id=database_id)
            if data is not None else None)
        render_result(empty_message)
        if (kind == ResourceKind.FORM_SCHEMA and not result_historical and names is None
                and data is not None and service is not None and any(
            field.get('type') in ('reference', 'multiselectreference') and field.get('defaultValue')
            for field in data.get('elements', [])
        )):
            asyncio.create_task(refresh_record_names(data, service, result_request, schema_request))

    async def refresh_record_names(data, current_service, request, source_request):
        nonlocal displayed_names
        # A cache belongs to one selected form/connection; stale reads cannot
        # update its successor, even when both reference the same record ID.
        cache = record_cache
        related, databases = deepcopy(result_context), deepcopy(result_database_names)
        generation = command_list._evaluation_version
        labels = await load_default_record_labels(current_service, data, related, cache)
        if (request != result_request or source_request != schema_request or service is not current_service
                or generation != command_list._evaluation_version or result_historical):
            return
        displayed_names = readable_resource(ResourceKind.FORM_SCHEMA, data, related, databases, record_labels=labels)
        render_result()

    def render_result(empty_message=None):
        empty_message = displayed_empty_message if empty_message is None else empty_message
        unresolved_badge.set_text(f'Unresolved: {len(displayed_issues)}')
        unresolved_badge.set_visibility(bool(displayed_issues))
        data = displayed_names if result_toggle.value == 'names' else displayed_result
        if data is None:
            schema.set_content('')
            schema.set_visibility(False)
            schema_placeholder.set_text(empty_message)
            schema_placeholder.set_visibility(True)
            return
        showing_names = result_toggle.value == 'names'
        # Use Markdown's post-render hook to retain the original code control,
        # highlighting, fonts, scrolling, copy action, and client sanitization.
        schema.markdown._sanitize = (
            (lambda html: underline_substitutions(html, data,
                [issue.path for issue in displayed_issues], underline=showing_names))
            if showing_names or displayed_issues else True)
        schema.set_content(json.dumps(data, indent=2, ensure_ascii=False))
        schema.markdown._handle_content_change(schema._render_markdown())
        schema.set_visibility(True)
        schema_placeholder.set_visibility(False)

    def selection_changed(
        selected_index: int | None,
        execution: CommandExecution | None,
    ) -> None:
        view = None
        if selected_index is not None and selected_index < len(command_list.results):
            view = command_list.results[selected_index]
            if command_list.selected_iteration is not None:
                view = view.applications[command_list.selected_iteration]
        attachment = getattr(view, 'rejected_candidate', None)
        if attachment is None and view is not None and hasattr(view, 'applications'):
            attachment = next((item.rejected_candidate for item in reversed(view.applications)
                               if item.rejected_candidate is not None), None)
        rejected_view.set_visibility(attachment is not None)
        rejected_code.set_content('')
        if attachment is not None:
            candidate = thaw(attachment.candidate)
            paths = tuple(item.path for item in attachment.diagnostics if item.path is not None)
            rejected_code.markdown._sanitize = lambda html: underline_substitutions(html, candidate, paths, underline=False)
            rejected_code.set_content(json.dumps(candidate, indent=2, ensure_ascii=False))
            rejected_code.markdown._handle_content_change(rejected_code._render_markdown())
        active = command_list.workspace.active_schema if command_list.workspace.form_snapshots else source_schema
        command_creator.set_schema(active)
        active_database = command_list.workspace.state.active_database_id
        if active_database:
            directory = command_list.resolution_cache.get(('get_form_names', json.dumps((active_database,))))
            if directory is None:
                directory = {identity: label.removesuffix(f' ({identity})')
                             for identity, label in forms.options.items()}
                if active_database != databases.value:
                    directory = {}
            directory = {**directory, **{
                identity: data.get('label') or identity for identity, data in command_list.related_schemas.items()
                if data.get('databaseId') == active_database}}
            deleted = {k.identity for k, state in command_list.workspace.state.resources.items() if state.lifecycle == Lifecycle.DELETED}
            directory = {i: n for i, n in directory.items() if i not in deleted}
            command_creator.set_suggestions('form_names', list(directory.values()))
        if selected_index is None:
            # Background evaluation still refreshes the source display, but its
            # per-command messages belong to executions until a command is selected.
            snapshots = report_schemas if report_version == command_list._evaluation_version else ([source_schema] if source_schema else [])
            show_results(snapshots or [], empty_message='Select a form to load its schema.',
                         related=report_context if report_version == command_list._evaluation_version else None,
                         snapshot=report_display if report_version == command_list._evaluation_version else command_list.applier.initial_display_state)
            if command_list.applier.initial_diagnostics:
                console_message.set_text('\n'.join(item.console_line() for item in command_list.applier.initial_diagnostics))
            return
        if view is None:
            console_message.set_text('Did not execute.')
            show_results([])
            return
        console_message.set_text(view.message)
        show_results([], snapshot=view.display_state)

    async def choose_recovery(diff_summary: str):
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-3xl'):
            dialog.props('persistent')
            ui.label('Execution did not fully succeed.').classes(
                'text-lg font-medium text-red-700'
            )
            ui.label('Execution has stopped. Restore the last update with a known or possible change, or restore all updates in reverse order. Unchanged attempts are skipped. Unknown outcomes require reconciliation; divergent or concurrent changes block automatic recovery.')
            ui.label('Execution details:')
            ui.label(diff_summary).classes(
                'w-full max-h-80 overflow-y-auto whitespace-pre-wrap break-all '
                'font-mono text-sm bg-slate-100 rounded p-2'
            )
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Leave', on_click=lambda: dialog.submit('leave')).props('flat')
                ui.button(
                    'Roll back last update', on_click=lambda: dialog.submit('rollback_last'),
                ).props('color=negative')
                ui.button('Roll back all updates', on_click=lambda: dialog.submit('rollback_all')).props('color=negative')
        return await dialog

    def execution_context():
        return (AttemptBinding(str(command_list._evaluation_version),
            {'schema': source_schema, 'form': forms.value, 'database': databases.value,
             'source_request': schema_request, 'view': command_list.current_view,
             'draft': command_list.advanced_draft,
             'commands': tuple(command.to_syntax() for command in command_list.commands)}, str(id(service))),
            command_list.candidate_plan)

    def open_execute_confirmation():
        nonlocal pending_execution
        if (
            not command_list.executions
            or not all(e.status in EXECUTABLE_STATUSES for e in command_list.executions)
            or service is None
        ):
            return
        cancel_execute_confirmation()
        try:
            binding, candidate = execution_context()
            attempt = ConfirmationAttempt(candidate, binding,
                related_schemas=command_list.related_schemas, reference_defaults=command_list.reference_defaults)
        except (ValueError, TypeError, KeyError) as exc:
            console_message.set_text(f'Could not prepare execution: {exc}')
            return
        pending_execution = {'service': service, 'form_id': forms.value, 'attempt': attempt,
                             'source_schema': deepcopy(source_schema),
                             'database_names': deepcopy(command_list.database_names),
                             'record_labels': {key: value for result in command_list.results if result.display_state
                                               for key, value in result.display_state.record_labels.items()}}
        confirmation_message.set_text(confirmation_summary(attempt.plan, command_list.database_names))
        confirmation_dialog.open()

    def cancel_execute_confirmation():
        nonlocal pending_execution
        if pending_execution is not None:
            pending_execution['attempt'].cancel()
        pending_execution = None
        confirmation_dialog.close()

    def set_write_busy(busy: bool) -> None:
        for control in (base_url, token, button):
            (control.disable if busy else control.enable)()
        if busy:
            databases.disable()
            forms.disable()
        else:
            (databases.enable if databases.options else databases.disable)()
            (forms.enable if forms.options else forms.disable)()
        command_creator.set_enabled(not busy and not text_mode)
        form_comparison.set_enabled(not busy and not text_mode)
        command_list.set_busy(busy)

    def text_mode_changed(editing: bool) -> None:
        nonlocal text_mode
        text_mode = editing
        command_creator.set_enabled(not editing and not command_list.busy)
        form_comparison.set_enabled(not editing and not command_list.busy)

    async def execute_confirmed():
        nonlocal pending_execution, source_schema, report_schemas, report_version, report_context, record_cache, report_display
        confirmation_dialog.close()
        execution = pending_execution
        pending_execution = None
        if execution is None:
            return
        set_write_busy(True)
        step_dialog = (StepExecutionDialog(base_url.value, database_names=execution['database_names'])
                       if command_list.step_checkbox.value else None)
        outcome = None
        unexpected_message = None
        try:
            binding, candidate = execution_context()
            attempt = execution['attempt']
            attempt.confirm(attempt.plan.attempt_id, binding, candidate)
            executor = Executor(execution['service'], step_control=step_dialog)
            command_list.execution_executor = executor
            outcome = await executor.execute(
                attempt, binding, candidate, choose_recovery, current_context=execution_context)
            if step_dialog is not None:
                await step_dialog.finish(executor, outcome)
            command_list.execution_report = outcome
            # All cached reads belong to the pre-execution state.
            record_cache = {}
            command_list.resolution_cache = {}
            command_list.reference_defaults = {}
            command_list.related_schemas = {}
            observed_ids = {item.key.identity for item in outcome.observations}
            refreshed = (outcome.actual_schemas.get(execution['form_id']) if execution['form_id'] in observed_ids
                         else execution['source_schema'])
            source_schema = deepcopy(refreshed) if refreshed is not None else None
            command_creator.set_schema(source_schema)
            command_list.set_schema(source_schema)
            report_context = {identity: data for identity, data in thaw(outcome.plan.related_schemas).items()
                              if identity not in observed_ids}
            report_context.update(outcome.actual_schemas)
            report_schemas = list(outcome.actual_schemas.values())
            report_display = DisplayState(tuple(DisplayResource(item.state, readable_resource(
                item.key.kind, display_payload(item.state), report_context, execution['database_names'],
                database_id=item.key.identity, record_labels=execution['record_labels']))
                for item in outcome.observations if item.state is not None) + tuple(
                    DisplayResource(replace(state, lifecycle=Lifecycle.DELETED))
                    for action in outcome.plan.actions for state in action.action.deleted_forms
                    if any(o.key == state.key and o.existence == Existence.ABSENT for o in outcome.observations)),
                report_context, execution['database_names'], execution['record_labels'])
            report_version = command_list._evaluation_version
        except Exception as exc:
            unexpected_message = f'Unexpected schema change error: {exc}'
        finally:
            record_cache = {}
            command_list.resolution_cache = {}
            command_list.reference_defaults = {}
            command_list.related_schemas = {}
            command_list.clear_selection()
            if outcome is not None:
                console_message.set_text('\n'.join(outcome.console_messages))
                show_results(list(outcome.actual_schemas.values()), related=report_context, snapshot=report_display)
            elif unexpected_message is not None:
                console_message.set_text(unexpected_message)
                show_result(None)
            pending_execution = None
            set_write_busy(False)
            if step_dialog is not None:
                step_dialog.dialog.close()
                if step_dialog.stopped:
                    selected_form = forms.value
                    await load_forms()
                    if selected_form in forms.options:
                        forms.set_value(selected_form)
                        await load_schema()

    def clear_schema():
        nonlocal schema_request, source_schema, record_cache
        record_cache = {}
        cancel_execute_confirmation()
        schema_request += 1
        source_schema = None
        form_comparison.set_initial(forms.value)
        command_creator.set_schema(None)
        command_list.related_schemas = {}
        command_list.database_names = {identity: label.removesuffix(f' ({identity})')
                                       for identity, label in databases.options.items()}
        command_list.reference_defaults = {}
        command_list.resolution_cache = {}
        command_list.clear_selection()
        command_list.initial_database_id = databases.value
        command_list.set_schema(None)
        schema_status.set_text('')

    async def load_schema():
        nonlocal source_schema
        save_state()
        clear_schema()
        request = schema_request
        if not forms.value or not service:
            return
        selected_form_id = forms.value
        selected_service = service
        schema_status.set_text('Loading schema...')
        try:
            related = await selected_service.get_form_tree(selected_form_id)
            data = related.get(selected_form_id)
            if not isinstance(data, dict) or not isinstance(data.get('elements'), list):
                raise ValueError('The form tree did not contain the selected form schema.')
            if request != schema_request:
                return
            db_names = {}
            warning = ''
            try:
                db_names = await selected_service.get_database_names()
            except ValueError as exc:
                warning = f'Some names could not be loaded: {exc}'
            if request == schema_request:
                command_list.related_schemas = related
                command_list.database_names = db_names
                source_schema = data
                command_creator.set_schema(data)
                command_list.set_schema(data)
                schema_status.set_text(warning)
        except ValueError as exc:
            if request == schema_request:
                schema_status.set_text(str(exc))

    def clear_forms():
        nonlocal form_request
        form_request += 1
        clear_schema()
        forms.set_options({})
        form_comparison.set_initial(None)
        form_comparison.set_forms({})
        command_creator.set_suggestions('form_names', [])
        form_status.set_text('')

    async def load_forms():
        clear_forms()
        save_state()
        request = form_request
        if not databases.value or not service:
            return
        form_status.set_text('Loading forms...')
        try:
            options = await service.get_forms(databases.value)
            if request == form_request:
                forms.set_options(options)
                form_comparison.set_forms(options)
                command_creator.set_suggestions('form_names', [
                    label.removesuffix(f' ({identity})') for identity, label in options.items()
                ])
                form_status.set_text('' if options else 'No forms available.')
        except ValueError as exc:
            if request == form_request:
                form_status.set_text('')
                console_message.set_text(f'Form list error:\n{exc}')

    def clear_databases():
        databases.set_options({})
        clear_forms()
        status.set_text('Connect to load databases.')

    async def credentials_changed():
        if restoring_state:
            return
        clear_databases()
        save_state()
        await close_service()

    async def connect():
        nonlocal service
        clear_databases()
        for control in (base_url, token, button):
            control.disable()
        status.set_text('Loading databases...')
        try:
            await close_service()
            service = ActivityInfoService(base_url.value, token.value)
            options = await service.get_databases()
            databases.set_options(options)
            command_list.set_schema(None)
            status.set_text(f'{len(options)} databases loaded.' if options else 'No databases available.')
        except ValueError as exc:
            await close_service()
            status.set_text('')
            console_message.set_text(f'Database list error:\n{exc}')
        finally:
            for control in (base_url, token, button):
                control.enable()

    def add_command(command):
        command_list.add_command(command)
        command_list.select_command(len(command_list.commands) - 1)

    def comparison_context():
        return (id(service), databases.value, forms.value, schema_request,
                command_list._evaluation_version,
                tuple(command.to_syntax() for command in command_list.commands),
                command_list.current_view, command_list.advanced_draft)

    def command_list_replace(commands):
        cancel_execute_confirmation()
        command_list.replace_commands(commands)

    async def load_comparison(model_id, create_new):
        selected_service = service
        database_id, initial_id = databases.value, forms.value
        if selected_service is None or not database_id:
            raise ValueError('Connect and select a database first.')
        model_tree = await selected_service.get_form_tree(model_id)
        model = model_tree[model_id]
        initial_tree = (await selected_service.get_form_tree(initial_id)
                        if initial_id and not create_new else {})
        initial = initial_tree.get(initial_id)
        if initial_id and not create_new and initial is None:
            raise ValueError('The initial form could not be loaded.')
        related = {**model_tree, **initial_tree}
        names = await selected_service.get_database_names()
        folder = '/'
        if initial is None:
            model_database_id = model['databaseId']
            resources = await selected_service.get_database_resources(model_database_id)
            path = form_location(model_database_id, resources, model_id)['path']
            folder = '/' + path.lstrip('/')
        record_labels = await load_default_record_labels(selected_service, model, related, {})
        return dict(model=model, initial=initial, related=related,
                    database_names=names, folder=folder, record_labels=record_labels)

    def creator_view_changed(event):
        single_creator.set_visibility(event.value == 'single')
        form_comparison.container.set_visibility(event.value == 'comparison')

    with ui.element('main').classes('workbench'):
        with ui.card().classes('workbench-panel sources-panel w-full no-shadow'):
            exit_button = ui.button('Exit App', icon='close').props('flat dense no-caps').classes('text-xs')
            exit_button.on('click', app.shutdown, js_handler='''() => {
                emit();
                window.close();
                window.setTimeout(() => {
                    document.open();
                    document.write(`<!doctype html><html lang="en"><head>
                        <meta charset="utf-8"><title>ActivityInfo Manager &mdash; Closed</title>
                        <style>body { margin: 0; min-height: 100vh; display: grid;
                        place-items: center; font: 16px system-ui, sans-serif;
                        color: #334155; background: #f8fafc; }</style>
                        </head><body><p>App closed&mdash;you can close this tab</p></body></html>`);
                    document.close();
                }, 150);
            }''')
            ui.label('Sources').classes('text-lg font-medium')
            ui.separator()
            with ui.expansion('Connection', icon='link', value=True).classes('w-full'):
                base_url = ui.input(
                    'Base URL', value=DEFAULT_BASE_URL, on_change=credentials_changed,
                ).classes('w-full')
                token = ui.input(
                    'API token', password=True, password_toggle_button=True,
                    on_change=credentials_changed,
                ).classes('w-full')
                button = ui.button('Connect', on_click=connect).classes('w-full')
            ui.separator()
            with ui.column().classes('w-full gap-3'):
                ui.label('Initial database').classes('text-base font-medium')
                databases = DatabaseListbox(on_change=load_forms)
                status = ui.label('Connect to load databases.').classes('text-sm text-slate-600')
            ui.separator()
            with ui.column().classes('w-full gap-3'):
                ui.label('Initial form').classes('text-base font-medium')
                forms = DatabaseListbox(
                    label='Forms', placeholder='Select a form', on_change=load_schema,
                )
                form_status = ui.label('').classes('text-sm text-slate-600')

        with ui.card().classes('workbench-panel command-creator-panel w-full no-shadow'):
            with ui.row().classes('items-center gap-3'):
                ui.label('Command Creator').classes('text-lg font-medium')
                ui.toggle({'single': 'Single Command', 'comparison': 'Form Comparison'},
                          value='single', on_change=creator_view_changed)
            with ui.column().classes('w-full') as single_creator:
                command_creator = CommandCreatorRenderer(
                    COMMAND_CREATORS, on_add=add_command,
                    on_parameter_change=refresh_reference_suggestions,
                )
            form_comparison = FormComparisonRenderer(load_comparison=load_comparison,
                replace_commands=command_list_replace, context_key=comparison_context)
            form_comparison.container.set_visibility(False)

        with ui.card().classes('workbench-panel result-panel w-full no-shadow flex flex-col'):
            with ui.row().classes('w-full items-center gap-3'):
                ui.label('Result').classes('text-lg font-medium')
                result_toggle = ui.toggle(
                    {'ids': 'IDs', 'names': 'Names'}, value='ids',
                    on_change=lambda: render_result(),
                )
                unresolved_badge = ui.badge('Unresolved: 0', color='amber-2', text_color='amber-10')
                unresolved_badge.props['aria-label'] = 'Unresolved names'
                unresolved_badge.set_visibility(False)
            ui.separator()
            schema_status = ui.label('').classes('text-sm text-red-700')
            with ui.row().classes('result-body flex-nowrap gap-2'):
                result_tabs = ui.tabs(on_change=lambda event: select_result_resource(event.value)).props('vertical dense').classes('result-tabs shrink-0 h-full')
                with ui.element('div').classes('relative grow min-w-0 h-full'):
                    schema_placeholder = ui.label(
                        'Select a form to load its schema.'
                    ).classes('result-placeholder text-sm text-slate-500')
                    schema = ui.code('', language='json').classes('result-code')
                    schema.props['aria-label'] = 'Form schema'
                    schema.set_visibility(False)
            with ui.expansion('Rejected candidate (not applied)').classes('w-full') as rejected_view:
                rejected_code = ui.code('', language='json').classes('w-full max-h-72 overflow-auto')
                rejected_code.props['aria-label'] = 'Rejected candidate'
            rejected_view.set_visibility(False)

        with ui.card().classes('workbench-panel command-list-panel w-full no-shadow flex flex-col'):
            command_list = CommandListManager(
                resolve_schema=resolve_preview,
                lookup_variable=lookup_variable,
                form_service=PreviewFormService(),
                on_selection_change=selection_changed,
                on_execute=open_execute_confirmation,
                on_commands_change=save_state,
                on_text_mode_change=text_mode_changed,
            )

        command_list.step_checkbox.on_value_change(lambda _: save_state())

        with ui.card().classes('workbench-panel console-panel w-full no-shadow flex flex-col'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('Console').classes('text-lg font-medium')
            ui.separator()
            with ui.column().classes('panel-body w-full grow'):
                console_message = ui.label('No messages.').classes(
                    'whitespace-pre-wrap font-mono text-sm text-slate-500'
                )

    with ui.dialog() as confirmation_dialog, ui.card().classes('w-full max-w-lg'):
        confirmation_dialog.props('persistent')
        confirmation_message = ui.label().classes('text-base whitespace-pre-wrap')
        with ui.row().classes('w-full justify-end gap-2'):
            ui.button('Cancel', on_click=cancel_execute_confirmation).props('flat')
            ui.button('Execute', on_click=execute_confirmed).props('color=primary')

    context.client.on_delete(close_service)

    async def restore_state():
        nonlocal restoring_state
        restoring_state = True
        try:
            try:
                saved, warning = state_store.load()
            except (OSError, ValueError):
                console_message.set_text('Saved application state could not be read.')
                return
            if saved is None:
                return
            try:
                command_list.step_checkbox.set_value(saved.get('execute_step_by_step') is True)
                token.set_value(saved['api_token'])
                await asyncio.sleep(0)
                if saved['api_token']:
                    await connect()
                    if saved.get('dbid') in databases.options:
                        databases.set_value(saved['dbid'])
                        await load_forms()
                        if saved.get('formid') in forms.options:
                            forms.set_value(saved['formid'])
                            await load_schema()
            finally:
                command_list.advanced_draft = saved.get('commands', '')
                command_list._apply_text()
                if warning:
                    console_message.set_text(warning)
        finally:
            restoring_state = False

    ui.timer(0, restore_state, once=True)
