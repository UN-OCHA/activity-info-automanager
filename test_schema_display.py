from html.parser import HTMLParser
import json
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

from nicegui.elements.markdown import prepare_content

from activity_manager.schema_display import substitution_ranges, underline_substitutions
from activity_manager.schema_names import SubstitutedText


class SchemaDisplayTests(unittest.TestCase):
    @unittest.skipUnless(Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe').exists(),
                         'Headless Edge is not installed')
    def test_underlines_survive_native_browser_sanitization(self):
        schema = {'value': SubstitutedText('NAME', [(0, 4, 'id')]), 'bad': 'Unknown'}
        plain = json.dumps(schema, indent=2)
        highlighted = prepare_content('```json\n' + plain + '\n```', extras='fenced-code-blocks')
        markup = underline_substitutions(highlighted, schema, [('bad',)])
        page_source = (Path(__file__).parent / 'activity_manager/page.py').read_text(encoding='utf-8')
        mark_css = re.search(r'\.result-code mark\s*\{[^}]+\}', page_source).group()
        with tempfile.TemporaryDirectory(prefix='cuid-underline-test-') as directory:
            page = Path(directory) / 'test.html'
            page.write_text('<style>' + mark_css + '#target code {color:rgb(150,40,60)}</style>'
                            + '<div id="target" class="result-code"></div><script>target.setHTML('
                            + json.dumps(markup) + ');'
                            + 'const u=target.querySelector("u");'
                            + 'document.body.dataset.underlined=u ? getComputedStyle(u).textDecorationLine : "missing";'
                            + 'document.body.dataset.label=u ? u.textContent : "missing";'
                            + 'const m=target.querySelector("mark");'
                            + 'document.body.dataset.inheritsColor=m && getComputedStyle(m).color === getComputedStyle(m.parentElement).color;'
                            + 'document.body.dataset.errorBackground=m ? getComputedStyle(m).backgroundColor : "missing";'
                            + 'document.body.dataset.errorLine=m ? getComputedStyle(m).textDecorationLine : "missing";'
                            + 'document.body.dataset.errorStyle=m ? getComputedStyle(m).textDecorationStyle : "missing";'
                            + '</script>', encoding='utf-8')
            result = subprocess.run([
                'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
                '--headless', '--disable-gpu', '--no-first-run',
                '--user-data-dir=' + str(Path(directory) / 'profile'), '--dump-dom', page.as_uri(),
            ], capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('data-underlined="underline"', result.stdout)
            self.assertIn('data-label="NAME"', result.stdout)
            self.assertIn('data-inherits-color="true"', result.stdout)
            self.assertIn('data-error-background="rgb(254, 243, 199)"', result.stdout)
            self.assertIn('data-error-line="underline"', result.stdout)
            self.assertIn('data-error-style="wavy"', result.stdout)

    def test_offsets_follow_pretty_json_escaping_and_highlighting(self):
        name = 'na"me\n<&> é 😀'
        schema = {'array': [True, None, {}, [], {'name': SubstitutedText(name, [(3, len(name), 'id')])}],
                  'unchanged': name}
        plain = json.dumps(schema, indent=2, ensure_ascii=False)
        ranges = substitution_ranges(schema)
        expected = json.dumps(name[3:], ensure_ascii=False)[1:-1]
        self.assertEqual(len(ranges), 1)
        self.assertEqual(plain[ranges[0][0]:ranges[0][1]], expected)
        highlighted = prepare_content('```json\n' + plain + '\n```', extras='fenced-code-blocks')
        marked = underline_substitutions(highlighted, schema)

        class Text(HTMLParser):
            def __init__(self):
                super().__init__()
                self.content = ''
                self.underlined = ''
                self.in_underline = False

            def handle_starttag(self, tag, attrs):
                if tag == 'u':
                    self.in_underline = True

            def handle_endtag(self, tag):
                if tag == 'u':
                    self.in_underline = False

            def handle_data(self, data):
                self.content += data
                if self.in_underline:
                    self.underlined += data

        before, after = Text(), Text()
        before.feed(highlighted)
        after.feed(marked)
        self.assertEqual(before.content, after.content)
        self.assertEqual(after.underlined, expected)
        self.assertIn('<u>', marked)
        self.assertNotIn('title=', marked)
        self.assertNotIn('<&>', marked)

    def test_unchanged_json_has_no_underlines(self):
        schema = {'code': 'NAME', 'id': 'id'}
        highlighted = prepare_content('```json\n' + json.dumps(schema, indent=2) + '\n```',
                                      extras='fenced-code-blocks')
        self.assertNotIn('<u>', underline_substitutions(highlighted, schema))
