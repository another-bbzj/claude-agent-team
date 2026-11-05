"""只读验收：有条目才出现，没有就不编造，清单计数只在勾选状态真实时出现。"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import headless_chrome
from test_agent_teams_fixture import EDGE, GET_MTIME, _materialize_edge
from test_board import BoardEnv
from test_claude_teams import adapter

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'examples' / 'agent-teams.synthetic' / 'claude-home'
HTML = ROOT / 'team-board' / 'index.html'
BROWSER = Path(__file__).resolve().parent / 'check_task_acceptance_browser.js'
DOC = ROOT / 'docs' / 'AGENT_TEAMS.md'


def script_text():
    html = HTML.read_text(encoding='utf-8')
    start = html.index('<script>') + len('<script>')
    end = html.rindex('</script>')
    return html[start:end]


def extract_function(source, name):
    token = 'function %s(' % name
    start = source.index(token)
    opening = source.index('{', start)
    depth = 0
    quote = ''
    escape = False
    for index in range(opening, len(source)):
        char = source[index]
        if quote:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == quote:
                quote = ''
            continue
        if char in ('"', "'", '`'):
            quote = char
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError('unclosed function ' + name)


def run_node(code):
    binary = shutil.which('node')
    if not binary:
        raise unittest.SkipTest('node is not installed')
    # Windows 上把带换行和中文的脚本放进 node -e 时，标准输出可能是空的。
    # 写到临时文件，并按 UTF-8 读回，和依赖弧线的检查一样。
    with tempfile.TemporaryDirectory(prefix='task-acceptance-') as directory:
        program = Path(directory) / 'accept-check.js'
        program.write_text(code, encoding='utf-8')
        completed = subprocess.run(
            [binary, str(program)],
            check=False,
            capture_output=True,
            encoding='utf-8',
            timeout=20,
        )
    if completed.returncode != 0 or not completed.stdout:
        raise AssertionError(completed.stderr or completed.stdout or 'node produced no output')
    return json.loads(completed.stdout)


class ExtractAcceptanceTest(unittest.TestCase):
    def test_plain_prose_and_loose_bullets_are_not_criteria(self):
        self.assertIsNone(adapter.extract_acceptance({}))
        self.assertIsNone(adapter.extract_acceptance({
            'description': (
                'Draft the note. The word acceptance appears here, but this is not a section.\n'
                '- this bullet is a step, not a criterion'
            ),
        }))
        self.assertIsNone(adapter.extract_acceptance({
            'description': 'Hold the note. No checklist and no acceptance heading.',
        }))
        self.assertIsNone(adapter.extract_acceptance({
            'activeForm': 'Holding the synthetic checklist',
            'subject': 'Acceptance criteria live in the title only',
        }))

    def test_marked_section_checklist_and_dedicated_fields(self):
        section = adapter.extract_acceptance({
            'description': (
                'Review the chain.\n\n验收标准：\n'
                '- 依赖链仍被挡住\n'
                '- 未认领的任务保持未认领\n\n'
                'This paragraph is after the label, so it is not a criterion.'
            ),
        })
        self.assertEqual(section['items'], [
            {'text': '依赖链仍被挡住', 'checked': None},
            {'text': '未认领的任务保持未认领', 'checked': None},
        ])
        self.assertNotIn('checklist', section)

        heading = adapter.extract_acceptance({
            'description': (
                'Preamble is not a criterion.\n\n## Acceptance\n'
                '- [x] The detail lists this checklist\n'
                '- [ ] A missing mark stays open\n\n'
                '## Notes\n- [x] This box is outside the section'
            ),
        })
        self.assertEqual(heading['checklist'], {'done': 1, 'total': 2})
        self.assertEqual([item['text'] for item in heading['items']], [
            'The detail lists this checklist',
            'A missing mark stays open',
        ])
        self.assertEqual([item['checked'] for item in heading['items']], [True, False])

        bare = adapter.extract_acceptance({
            'description': (
                'File the note.\n'
                '- [x] Sources are collected\n'
                '- [X] Outline is done\n'
                '- [ ] The note is filed\n'
                '- a plain bullet stays out'
            ),
        })
        self.assertEqual(bare['checklist'], {'done': 2, 'total': 3})
        self.assertEqual(len(bare['items']), 3)

        prose = adapter.extract_acceptance({
            'description': '## Definition of Done\nThe note lists the sources.\nIt names the module.',
        })
        self.assertEqual([item['text'] for item in prose['items']], [
            'The note lists the sources.',
            'It names the module.',
        ])
        self.assertTrue(all(item['checked'] is None for item in prose['items']))

        fenced = adapter.extract_acceptance({
            'description': 'Sample:\n```\n- [x] not real\n```\n- [x] real',
        })
        self.assertEqual(fenced['items'], [{'text': 'real', 'checked': True}])

        dedicated = adapter.extract_acceptance({
            'description': '## Acceptance\n- [ ] ignored because the field is present',
            'acceptance': ['The synthetic draft exists', 'The note names the module'],
            'metadata': {'spec_path': 'not-a-criterion', 'acceptanceCriteria': ['also ignored']},
        })
        self.assertEqual([item['text'] for item in dedicated['items']], [
            'The synthetic draft exists',
            'The note names the module',
        ])
        self.assertNotIn('checklist', dedicated)

        objects = adapter.extract_acceptance({
            'metadata': {'acceptanceCriteria': [
                {'text': '  The newer copy is the one on the board  ', 'done': True, 'path': 'drop me'},
                {'title': 'The older subject is not shown', 'done': False},
                {'criterion': 'A string done flag is not a checkbox', 'done': 'yes'},
            ]},
        })
        self.assertEqual(objects['checklist'], {'done': 1, 'total': 2})
        self.assertEqual(objects['items'][2]['checked'], None)
        self.assertNotIn('path', objects['items'][0])

    def test_empty_or_invalid_fields_fall_through_and_do_not_invent(self):
        fallen = adapter.extract_acceptance({
            'acceptance': [],
            'acceptanceCriteria': 3,
            'metadata': {'acceptance': None, 'acceptance_criteria': ''},
            'description': 'Done when:\n\n- [x] The fallback still counts',
        })
        self.assertEqual(fallen['items'], [{'text': 'The fallback still counts', 'checked': True}])
        self.assertIsNone(adapter.extract_acceptance({
            'acceptance': [1, '', {'done': True}],
            'metadata': 'not-an-object',
            'description': '```\n- [x] trapped in a fence\n',
        }))
        inline = adapter.extract_acceptance({
            'task_description': '**验收：**\n\n抽屉里能看到这一句',
        })
        self.assertEqual(inline['items'], [{'text': '抽屉里能看到这一句', 'checked': None}])


class FixtureAcceptanceTest(unittest.TestCase):
    def test_shipped_files_keep_present_and_absent_criteria(self):
        before = {
            path: path.read_bytes()
            for path in (FIXTURE / 'tasks').rglob('*.json')
        }
        team = adapter.load_team(FIXTURE, 'session-a1b2c3d4')
        self.assertEqual(team['warnings'], [])
        by_id = {task['id']: task for task in team['tasks']}
        self.assertNotIn('acceptance', by_id['1'])
        self.assertNotIn('acceptance', by_id['2'])
        self.assertEqual([item['text'] for item in by_id['3']['acceptance']['items']], [
            'The synthetic draft exists',
            'The note names the module',
        ])
        self.assertNotIn('checklist', by_id['3']['acceptance'])
        self.assertEqual(by_id['4']['acceptance']['checklist'], {'done': 1, 'total': 2})
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_board_rows_drop_metadata_paths_and_keep_the_newer_duplicate(self):
        env = BoardEnv()
        try:
            tmp = tempfile.TemporaryDirectory(prefix='acceptance-edge-')
            self.addCleanup(tmp.cleanup)
            root = Path(tmp.name)
            _materialize_edge(root)
            secret = root / 'tasks' / EDGE / 'secret-path.json'
            secret.write_text(json.dumps({'task': {
                'id': '16',
                'subject': 'Replace the plain note',
                'description': 'Hold the note. No checklist and no acceptance heading.',
                'status': 'pending',
                'owner': 'synthetic-not-on-team',
                'metadata': {
                    'spec_path': '/home/synthetic/notes',
                    'acceptance': ['Only the criterion text is public'],
                },
            }}), encoding='utf-8')
            os.utime(secret, (GET_MTIME + 50, GET_MTIME + 50))
            original = secret.read_bytes()
            team = adapter.load_team(root, EDGE)
            board = env.load()
            warnings = []
            rows = board.task_board_rows(team['tasks'], team['teamName'], GET_MTIME + 80, clock={}, warnings=warnings)
            by_id = {row['id']: row for row in rows}
            self.assertEqual(by_id['12']['acceptance']['checklist'], {'done': 1, 'total': 2})
            self.assertEqual(by_id['12']['acceptance']['items'][0]['text'], 'The newer copy is the one on the board')
            self.assertNotIn('checklist', by_id['13']['acceptance'])
            self.assertIn('依赖链仍被挡住', by_id['13']['acceptance']['items'][0]['text'])
            self.assertEqual(by_id['15']['acceptance']['checklist'], {'done': 2, 'total': 3})
            for ident in ('10', '11', '14'):
                self.assertNotIn('acceptance', by_id[ident])
            self.assertEqual(by_id['16']['acceptance']['items'], [
                {'text': 'Only the criterion text is public', 'checked': None},
            ])
            blob = json.dumps(rows, ensure_ascii=False)
            self.assertNotIn('spec_path', blob)
            self.assertNotIn('/home/synthetic', blob)
            self.assertNotIn('sourceMtime', blob)
            self.assertNotIn('raw', blob)
            self.assertNotIn('metadata', blob)
            self.assertEqual(secret.read_bytes(), original)
            poisoned = board.task_board_rows([{
                'id': '1',
                'subject': 'Count only real boxes',
                'status': 'pending',
                'normalizedStatus': 'pending',
                'acceptance': {
                    'items': [
                        {'text': ' open ', 'checked': False, 'path': '/home/synthetic/notes'},
                        {'text': 'plain', 'checked': 'yes'},
                    ],
                    'checklist': {'done': 9, 'total': 9},
                },
            }], 'session-accept', 10, clock={})
            self.assertEqual(poisoned[0]['acceptance'], {
                'items': [
                    {'text': 'open', 'checked': False},
                    {'text': 'plain', 'checked': None},
                ],
                'checklist': {'done': 0, 'total': 1},
            })
            self.assertNotIn('acceptance', board.task_board_rows([{
                'id': '2', 'subject': 'No criteria', 'status': 'pending', 'normalizedStatus': 'pending',
            }], 'session-accept', 10, clock={})[0])
        finally:
            env.cleanup()

    def test_doc_describes_the_section_and_the_omission(self):
        text = DOC.read_text(encoding='utf-8')
        for needle in ('验收 / Acceptance', 'acceptanceCriteria', '不发明', 'checklist', 'metadata'):
            self.assertIn(needle, text)


class AcceptanceUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = script_text()
        esc = source[source.index('const esc = '):source.index('\n', source.index('const esc = '))]
        names = ('acceptanceItems', 'acceptanceChecklist', 'acceptanceHtml', 'acceptanceCardHtml')
        cls.prelude = esc + '\n' + '\n'.join(extract_function(source, name) for name in names)

    def run_case(self, task):
        return run_node(
            self.prelude
            + '\nconst task = ' + json.dumps(task) + ';\n'
            + 'process.stdout.write(JSON.stringify({html: acceptanceHtml(task), card: acceptanceCardHtml(task)}));\n'
        )

    def test_drawer_section_and_card_count_follow_the_snapshot(self):
        html = HTML.read_text(encoding='utf-8')
        self.assertIn('${acceptanceHtml(task)}', html)
        self.assertIn('${acceptanceCardHtml(task)}', html)
        self.assertIn('验收 <small>Acceptance</small>', html)
        self.assertIn('type="checkbox" disabled', html)
        present = self.run_case({
            'acceptance': {
                'items': [
                    {'text': 'The detail lists this criterion', 'checked': True},
                    {'text': 'An open criterion stays unchecked', 'checked': False},
                ],
                'checklist': {'done': 1, 'total': 2},
            },
        })
        self.assertIn('验收', present['html'])
        self.assertIn('Acceptance', present['html'])
        self.assertIn('The detail lists this criterion', present['html'])
        self.assertIn('disabled', present['html'])
        self.assertIn('checked', present['html'])
        self.assertEqual(present['card'], '<span class="kaccept" title="验收清单 1/2">1/2</span>')
        prose = self.run_case({
            'acceptance': {'items': [{'text': 'The owner is the lead name', 'checked': None}]},
        })
        self.assertIn('The owner is the lead name', prose['html'])
        self.assertNotIn('checkbox', prose['html'])
        self.assertEqual(prose['card'], '')
        missing = self.run_case({'description': '## Acceptance\n- [x] not parsed in the browser'})
        self.assertEqual(missing['html'], '')
        self.assertEqual(missing['card'], '')
        hostile = self.run_case({
            'acceptance': {
                'items': [{'text': '<img src=x onerror=alert(1)>', 'checked': False}],
                'checklist': {'done': 2, 'total': 1},
            },
        })
        self.assertNotIn('<img', hostile['html'])
        self.assertIn('&lt;img', hostile['html'])
        self.assertEqual(hostile['card'], '')

    def test_headless_drawer_shows_and_hides_the_section(self):
        if not shutil.which('node'):
            self.skipTest('node is not installed')
        binary = headless_chrome.chrome_binary()
        if not binary:
            self.skipTest('chrome is not installed')
        import test_demo_board
        server = test_demo_board.demo.DemoServer(0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='acceptance-shots-') as directory:
                shots = Path(directory)
                with headless_chrome.launch_headless_chrome(binary) as chrome:
                    completed = subprocess.run(
                        [
                            shutil.which('node'), str(BROWSER),
                            'http://127.0.0.1:%d/' % server.server_port,
                            str(chrome.port), str(shots),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if completed.returncode != 0:
                        tail = chrome.stderr_tail().strip()
                        detail = completed.stderr or completed.stdout or 'browser check failed'
                        if tail:
                            detail = '%s\n--- chrome stderr tail ---\n%s' % (detail.rstrip(), tail)
                        self.fail(detail)
                    report = json.loads(completed.stdout)
                    kept = Path('/opt/cursor/artifacts/task-acceptance')
                    try:
                        kept.mkdir(parents=True, exist_ok=True)
                        for shot in shots.glob('*.png'):
                            (kept / shot.name).write_bytes(shot.read_bytes())
                    except OSError:
                        pass
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        listed = report['listed']
        self.assertEqual(listed['badge'], '1/2')
        self.assertTrue(listed['detail']['open'])
        self.assertTrue(any('验收' in heading and 'Acceptance' in heading for heading in listed['detail']['headings']))
        self.assertIn('The detail lists this criterion', listed['detail']['text'])
        self.assertIn('An open criterion stays unchecked', listed['detail']['text'])
        self.assertEqual(listed['detail']['checks'], [
            {'disabled': True, 'checked': True},
            {'disabled': True, 'checked': False},
        ])
        self.assertFalse(listed['detail']['form'])
        self.assertEqual(listed['afterClick']['checked'], True)
        prose = report['prose']
        self.assertEqual(prose['badge'], '')
        self.assertTrue(any('验收' in heading and 'Acceptance' in heading for heading in prose['detail']['headings']))
        self.assertIn('The owner is the lead name', prose['detail']['text'])
        self.assertEqual(prose['detail']['checks'], [])
        missing = report['missing']
        self.assertEqual(missing['badge'], '')
        self.assertNotIn('验收', ''.join(missing['detail']['headings']))
        self.assertNotIn('Acceptance', missing['detail']['text'])
        self.assertEqual(report['badges'], {'1': '', '2': '', '5': ''})
        self.assertTrue(report['narrow']['open'])
        self.assertTrue(any('验收' in heading and 'Acceptance' in heading for heading in report['narrow']['headings']))
        self.assertGreater(report['narrow']['width'], 200)
        self.assertNotEqual(report['light'], report['dark'])
