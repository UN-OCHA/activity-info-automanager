import unittest
from types import SimpleNamespace
from unittest.mock import patch

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_card import command_preview
from activity_manager.command_list import CommandExecution, CommandListManager
from activity_manager.command_parser import parse_command_text


def elements(manager, kind):
    return [item for item in manager.simple_content.descendants() if isinstance(item, kind)]


def button(manager, label):
    return next(item for item in elements(manager, ui.button) if item.props.get('aria-label') == label)


class CommandCardTests(unittest.TestCase):
    def test_preview_preserves_quoted_commas_unicode_and_nested_calls(self):
        for first in ('fieldCode="café, \\"quoted\\""', 'label=Append("End", ",", "x")'):
            syntax = f'ModifyField({first}, fieldType="text", required=True)'
            self.assertEqual(command_preview(syntax), (f'ModifyField({first}, fieldType="text", ...', True))
        syntax = 'DeleteField(fieldCode="x,y")'
        self.assertEqual(command_preview(syntax), (syntax, False))

    def test_two_parameters_are_shown_in_full_without_expansion(self):
        syntax = 'MoveField(fieldCode="x", position="End")'
        self.assertEqual(command_preview(syntax), (syntax, False))
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager()
            manager.add_command(parse_command_text(syntax)[0])
            self.assertFalse(button(manager, 'Expand command text').enabled)
            self.assertIn(manager.commands[0].to_syntax(), [item.text for item in elements(manager, ui.label)])

    def test_single_parameter_is_full_and_toggle_disabled(self):
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager()
            manager.add_command(parse_command_text('DeleteField(fieldCode="x")')[0])
            self.assertFalse(button(manager, 'Expand command text').enabled)
            self.assertIn('DeleteField(fieldCode="x")', [item.text for item in elements(manager, ui.label)])
            actions = [button(manager, name) for name in ('Move command up', 'Move command down', 'Delete command')]
            self.assertTrue(all('w-8' in item.classes and 'h-8' in item.classes for item in actions))

    def test_iteration_counts_order_and_zero_count_toggle(self):
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager()
            manager.add_command(parse_command_text('DeleteField(fieldPattern="x.*")')[0])
            self.assertFalse(button(manager, 'Show iterations').enabled)
            self.assertEqual([item.text for item in elements(manager, ui.badge)], ['0x'])
            children = tuple(CommandExecution(status, None, '', field_code=f'x{i}')
                             for i, status in enumerate(('error', 'warning', 'applied', 'applied', 'not_executed', 'resolving')))
            manager.executions = [CommandExecution('error', None, '', iterations=children)]
            manager._render_simple_view()
            self.assertEqual([item.text for item in elements(manager, ui.badge)], ['6x'])
            icons = [item.props['name'] for item in elements(manager, ui.icon)]
            self.assertEqual(icons[:4], ['check_circle', 'warning', 'error', 'fiber_manual_record'])
            self.assertEqual([item.text for item in elements(manager, ui.label)][:4], ['2', '1', '1', '2'])
            self.assertTrue(button(manager, 'Show iterations').enabled)

    def test_text_and_iteration_expansion_are_independent_and_survive_selection(self):
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager()
            command = parse_command_text('MoveField(fieldPattern="x.*", position="End")')[0]
            manager.add_command(command)
            manager._toggle_command_text(0)
            manager.toggle_iterations(0)
            manager.select_command(0)
            self.assertIn(id(command), manager.expanded_text_commands)
            self.assertIn(id(command), manager.expanded_commands)
            self.assertIn(command.to_syntax(), [item.text for item in elements(manager, ui.label)])
            button(manager, 'Collapse command text')
            button(manager, 'Hide iterations')
            manager.delete_command(0)
            self.assertNotIn(id(command), manager.expanded_text_commands)


class CommandTextEditorTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrap_copy_and_paste_replace_draft_without_applying(self):
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager()
            manager.add_command(parse_command_text('DeleteField(fieldCode="x")')[0])
            manager._enter_text_view()
            self.assertIsInstance(manager.advanced_editor, ui.codemirror)
            self.assertTrue(manager.advanced_editor.line_wrapping)
            manager._toggle_word_wrap()
            self.assertFalse(manager.advanced_editor.line_wrapping)
            replacement = 'DeleteField(fieldCode="y")\nDeleteField(fieldCode="z")'
            manager._paste_text(SimpleNamespace(args={'text': replacement}))
            self.assertEqual(manager.advanced_editor.value, replacement)
            self.assertEqual(manager.advanced_draft, replacement)
            self.assertEqual(len(manager.commands), 1)
            self.assertTrue(manager.apply_button.enabled)
            manager._paste_text(SimpleNamespace(args={'text': ''}))
            self.assertEqual(manager.advanced_draft, '')

    async def test_copy_failure_reports_warning_without_changing_editor(self):
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager()
            manager._enter_text_view()
            manager.advanced_editor.set_value('keep this')
            with patch('nicegui.ui.notify') as notify:
                manager._copy_failed(SimpleNamespace(args={'error': 'failed'}))
                notify.assert_called_once()
            self.assertEqual(manager.advanced_editor.value, 'keep this')
            self.assertEqual(manager.advanced_draft, 'keep this')

    async def test_clipboard_failure_preserves_draft_and_busy_blocks_paste(self):
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager()
            manager._enter_text_view()
            manager.advanced_editor.set_value('keep this')
            for error in ('denied', 'unavailable'):
                with patch('nicegui.ui.notify') as notify:
                    manager._paste_text(SimpleNamespace(args={'error': error}))
                    notify.assert_called_once()
                self.assertEqual(manager.advanced_draft, 'keep this')
            self.assertEqual(manager.advanced_draft, 'keep this')
            manager.set_busy(True)
            self.assertFalse(manager.paste_button.enabled)
            manager._paste_text(SimpleNamespace(args={'text': 'do not replace'}))
            self.assertEqual(manager.advanced_draft, 'keep this')
            manager.set_busy(False)
            manager._discard_text()
            manager._paste_text(SimpleNamespace(args={'text': 'late prompt result'}))
            self.assertEqual(manager.advanced_draft, manager.stored_text)
