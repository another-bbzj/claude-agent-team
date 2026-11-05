"""Offline HTML replay: embedded sanitized snapshot, no network, no invented totals."""
import contextlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('export_replay', ROOT / 'tools' / 'export_replay.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

# Same categories the fixture privacy scan rejects, plus a tilde path.
SECRET = re.compile(
    r'(?:/Users/|/home/|[A-Za-z]:[\\/]+Users[\\/]|~[/\\]'
    r'|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'
    r'|sk-ant-|sk-[A-Za-z0-9][A-Za-z0-9_\-]{7,}|ghp_|github_pat_|xox[baprs]-'
    r'|AKIA[0-9A-Z]{8,}|BEGIN [A-Z ]*PRIVATE KEY|api[_-]?key\s*[:=]'
    r'|Bearer\s+[A-Za-z0-9._\-]+)',
    re.IGNORECASE)

PLANTED = (
    'PRIVATE_TRANSCRIPT_MARKER',
    'TRANSCRIPT_BODY_SHOULD_NOT_LEAK',
    'READS_ONLY_MARKER',
    'DESCRIPTION_ONLY_MARKER',
    'ACCEPTANCE_ONLY_MARKER',
    'RAW_SNAPSHOT_MARKER',
    'SECRET_NAME',
    'TAILSHOULDNOTREMAIN9',
    '/home/ada/a',
    '/Users/ada/b',
    'C:\\Users\\ada\\c',
    'D:/Users/ada/d',
    '~/ada/e',
    'synthetic.person@example.com',
    'ada.person@example.com',
    'sk-ant-api03-TAILSHOULDNOTREMAIN9',
    'sk-proj-synthetickey',
    'sk-12345678',
    'ghp_synthetictoken',
    'github_pat_synthetic',
    'xoxb-synthetic',
    'AKIA12345678',
    '-----BEGIN PRIVATE KEY-----',
    'api_key=synthetic',
    'Bearer synthetic.token',
)


def _load(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))


def _payload(html):
    marker = '<script type="application/json" id="replay-data">'
    start = html.index(marker) + len(marker)
    end = html.index('</script>', start)
    return json.loads(html[start:end])


def _app_script(html):
    marker = '<script type="application/json" id="replay-data">'
    rest = html.split(marker, 1)[1]
    start = rest.index('<script>') + len('<script>')
    end = rest.index('</script>', start)
    return rest[start:end]


class ReplayDocumentTest(unittest.TestCase):
    def setUp(self):
        self.demo = _load('examples/board-demo.synthetic.json')
        self.aggregate = _load('examples/snapshot.synthetic.json')

    def test_demo_document_keeps_real_fields_and_drops_private_ones(self):
        doc = replay.build_replay(self.demo)
        self.assertEqual(doc['schemaVersion'], 1)
        self.assertEqual(doc['kind'], 'claude-agent-team-replay')
        self.assertIs(doc['synthetic'], True)
        self.assertEqual(doc['metrics'], replay.export_metrics(self.demo))
        self.assertEqual(doc['session']['project'], 'synthetic-example')
        self.assertEqual(doc['session']['branch'], 'main')
        self.assertNotIn('id', doc['session'])
        names = [card['name'] for card in doc['members']]
        self.assertIn('演示后端', names)
        self.assertEqual(doc['lead']['name'], '演示队长')
        labels = [event['label'] for event in doc['events']]
        self.assertIn('Bash · go test ./...', labels)
        texts = [item['text'] for item in doc['comms']]
        self.assertIn('【合成演示】筛选交互已交接。请核对空结果、清空筛选和状态与负责人组合的边界。', texts)
        self.assertIn('filter_issues(issues, status=None, assignee=None)。两个条件按 AND 组合。', '\n'.join(texts))
        subjects = [task['subject'] for task in doc['tasks']]
        self.assertIn('Survey the synthetic module', subjects)
        self.assertEqual(doc['counts']['theaters'], 1)
        self.assertEqual(doc['counts']['liveRetained'], 2)
        self.assertNotIn('stalls', doc['counts'])
        self.assertIsNone(doc['budget'])
        blob = json.dumps(doc, ensure_ascii=False)
        for secret in (
            'synthetic-session',
            'toolu_synthetic_read',
            'session-syntheti',
            '合成示例：检查界面和团队协作流程。',
            '合成演示：展示团队成员和指标。',
            '官方 Agent Team 合成演示',
            '合成演示用的完整说明。看板上的卡片只露出开头',
            'tool-demo-go-fail',
        ):
            self.assertNotIn(secret, blob, secret)
        self.assertIsNone(SECRET.search(blob))
        running = next(card for card in doc['members'] if card['name'] == '演示后端')
        self.assertNotIn('duration', running)
        finished = next(card for card in doc['members'] if card['name'] == '演示前端')
        self.assertEqual(finished['duration'], '1m 50s')

    def test_aggregate_metrics_match_and_missing_totals_are_not_invented(self):
        doc = replay.build_replay(self.aggregate)
        self.assertEqual(doc['metrics'], replay.export_metrics(self.aggregate))
        self.assertEqual(doc['members'][0]['status'], 'completed')
        self.assertIs(doc['synthetic'], False)
        bare = replay.build_replay({
            'members': [{
                'name': '甲',
                'status': 'running',
                'cost': 1.25,
                'startedAt': 10,
                'endedAt': None,
                'prompt': 'PRIVATE_TRANSCRIPT_MARKER',
            }],
        })
        self.assertIsNone(bare['metrics'])
        self.assertEqual(bare['members'][0]['cost'], 1.25)
        self.assertNotIn('tokens', bare['members'][0])
        self.assertNotIn('duration', bare['members'][0])
        self.assertNotIn('PRIVATE_TRANSCRIPT_MARKER', json.dumps(bare, ensure_ascii=False))
        timed = replay.build_replay({
            'members': [{'name': '乙', 'status': 'completed', 'startedAt': 10, 'endedAt': 70}],
        })
        self.assertEqual(timed['members'][0]['duration'], '1m 0s')

    def test_budget_keeps_ratio_and_drops_names(self):
        quiet = replay.build_replay({'members': [], 'budget': {
            'enabled': True,
            'suggestions': [{'name': 'SECRET_NAME'}],
        }})
        self.assertIsNone(quiet['budget'])
        self.assertNotIn('SECRET_NAME', json.dumps(quiet))
        loud = replay.build_replay({'members': [], 'budget': {
            'enabled': True,
            'ratio': 1.25,
            'status': 'over',
            'lowerBound': True,
            'suggestions': [{'name': 'SECRET_NAME'}],
            'lowerBoundLabel': 'SECRET_NAME',
        }})
        self.assertEqual(loud['budget'], {'enabled': True, 'ratio': 1.25, 'status': 'over', 'lowerBound': True})
        self.assertNotIn('SECRET_NAME', json.dumps(loud))

    def test_latest_rows_are_kept_when_the_timeline_is_clipped(self):
        kept, dropped = replay._clip_tail([{'ts': 1}, {'ts': 2}, {'ts': 3}], 2)
        self.assertEqual(dropped, 1)
        self.assertEqual([row['ts'] for row in kept], [2, 3])

    def test_snapshot_must_be_an_object(self):
        with self.assertRaises(replay.ExportError):
            replay.build_replay([])


class ReplayPrivacyTest(unittest.TestCase):
    def test_planted_secrets_do_not_survive_into_the_html(self):
        snapshot = {
            'demo': False,
            'version': 'sk-12345678',
            'notes': 'RAW_SNAPSHOT_MARKER',
            'session': {
                'id': 'ghp_synthetictoken',
                'project': '/home/ada/a',
                'branch': 'main',
                'worktree': 'C:\\Users\\ada\\c',
            },
            'lead': {
                'name': '队长',
                'phase': 'coordinating',
                'lastUserPrompt': 'PRIVATE_TRANSCRIPT_MARKER synthetic.person@example.com',
                'prompt': 'PRIVATE_TRANSCRIPT_MARKER',
                'timeline': [{
                    'kind': 'tool',
                    'label': 'Read /Users/ada/b extra',
                    'ts': 10,
                    'done': True,
                }],
            },
            'members': [{
                'name': '成员甲',
                'status': 'running',
                'prompt': 'PRIVATE_TRANSCRIPT_MARKER sk-ant-api03-TAILSHOULDNOTREMAIN9',
                'lastText': 'TRANSCRIPT_BODY_SHOULD_NOT_LEAK',
                'description': 'DESCRIPTION_ONLY_MARKER Bearer synthetic.token',
                'reads': ['READS_ONLY_MARKER /home/ada/a'],
                'messages': [{'text': 'PRIVATE_TRANSCRIPT_MARKER'}],
                'current': {'label': 'Edit C:\\Users\\ada\\c and D:/Users/ada/d and ~/ada/e'},
                'timeline': [{
                    'kind': 'tool',
                    'label': 'Bash · go test ./...',
                    'ts': 20,
                    'done': True,
                }],
            }],
            'comms': [
                {
                    'ts': 30,
                    'kind': 'report',
                    'fromName': '成员甲',
                    'toName': '队长',
                    'text': '【合成演示】接口已对齐。',
                },
                {
                    'ts': 31,
                    'kind': 'peer',
                    'fromName': '成员甲',
                    'toName': 'ada.person@example.com',
                    'text': 'send ada.person@example.com sk-proj-synthetickey github_pat_synthetic '
                            'xoxb-synthetic AKIA12345678 -----BEGIN PRIVATE KEY-----',
                },
            ],
            'officialTeam': {'tasks': [{
                'id': '1',
                'subject': 'Ship api_key=synthetic tomorrow',
                'description': 'DESCRIPTION_ONLY_MARKER sk-ant-api03-TAILSHOULDNOTREMAIN9',
                'acceptance': {
                    'items': [{
                        'text': 'ACCEPTANCE_ONLY_MARKER sk-ant-api03-TAILSHOULDNOTREMAIN9',
                        'checked': False,
                    }],
                    'checklist': {'done': 0, 'total': 1},
                },
                'normalizedStatus': 'pending',
            }]},
            'budget': {'enabled': True, 'ratio': 0.4, 'suggestions': [{'name': 'SECRET_NAME'}]},
        }
        html = replay.render_html(replay.build_replay(snapshot))
        doc = _payload(html)
        for secret in PLANTED:
            self.assertNotIn(secret, html, secret)
        self.assertNotIn('Users\\ada', html)
        self.assertNotIn('Users\\\\ada', html)
        self.assertIsNone(SECRET.search(html))
        self.assertIn('Bash · go test ./...', html)
        self.assertIn('【合成演示】接口已对齐。', html)
        self.assertIn('[path]', json.dumps(doc, ensure_ascii=False))
        self.assertIn('[redacted]', json.dumps(doc, ensure_ascii=False))
        self.assertEqual(doc['session'].get('branch'), 'main')
        self.assertNotIn('project', doc['session'])
        self.assertNotIn('version', doc)
        self.assertEqual(doc['budget']['ratio'], 0.4)
        self.assertNotIn('SECRET_NAME', html)

    def test_script_breakout_stays_inside_the_json_slot(self):
        label = '</script><img src=x onerror=alert(1)>'
        html = replay.render_html(replay.build_replay({
            'members': [{'name': '甲', 'status': 'running', 'timeline': [{'kind': 'tool', 'label': label, 'ts': 1}]}],
        }))
        self.assertEqual(html.count('</script>'), 2)
        self.assertNotIn('<img', html)
        self.assertNotIn('</script><', html)
        self.assertNotIn('innerHTML', html)
        self.assertEqual(_payload(html)['events'][0]['label'], label)


class ReplayHtmlTest(unittest.TestCase):
    def test_demo_page_is_offline_and_embeds_the_document(self):
        snapshot = _load('examples/board-demo.synthetic.json')
        html = replay.render_html(replay.build_replay(snapshot))
        doc = _payload(html)
        self.assertEqual(doc['kind'], 'claude-agent-team-replay')
        self.assertIn('<!doctype html>', html)
        self.assertIn('charset="utf-8"', html)
        self.assertIn('id="replay-data"', html)
        self.assertIn('type="application/json"', html)
        self.assertIn('id="replay-play"', html)
        self.assertIn('id="member-cards"', html)
        self.assertIn('id="timeline"', html)
        self.assertIn('id="comms"', html)
        self.assertIn('data-replay="offline"', html)
        self.assertIn("default-src 'none'", html)
        self.assertNotIn('https://', html)
        self.assertNotIn('http://', html)
        self.assertNotIn('<script src=', html)
        self.assertNotIn('<link ', html)
        self.assertNotIn('innerHTML', html)
        self.assertIn('演示后端', html)
        self.assertIn(json.dumps(snapshot['totals']['cost']), html)
        self.assertIsNone(SECRET.search(html))

    def test_inline_script_parses_when_node_is_present(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        html = replay.render_html(replay.build_replay(_load('examples/board-demo.synthetic.json')))
        handle = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8')
        handle.write(_app_script(html))
        handle.close()
        try:
            proc = subprocess.run([node, '--check', handle.name], capture_output=True, check=False)
        finally:
            Path(handle.name).unlink()
        if proc.returncode != 0:
            self.fail(proc.stderr.decode('utf-8', 'replace'))


class ReplayCliTest(unittest.TestCase):
    def setUp(self):
        self.demo = ROOT / 'examples' / 'board-demo.synthetic.json'
        self.tmp = tempfile.TemporaryDirectory(prefix='export-replay-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_demo_writes_the_same_html_as_stdout(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(replay.main(['--demo']), 0)
        target = self.root / 'replay.html'
        self.assertEqual(replay.main(['--demo', '-o', str(target)]), 0)
        self.assertEqual(target.read_text(encoding='utf-8'), output.getvalue())
        self.assertTrue(output.getvalue().startswith('<!doctype html>'))
        self.assertEqual(_payload(target.read_text(encoding='utf-8'))['synthetic'], True)

    def test_reader_is_the_export_snapshot_reader(self):
        self.assertIs(replay.read_snapshot, replay._exporter.read_snapshot)
        with mock.patch.object(replay, 'read_snapshot', return_value={'members': []}) as read:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(replay.main(['--port', '7788']), 0)
        read.assert_called_once_with(None, 7788, session=None, project=None)

    def test_session_and_project_selectors_require_a_port(self):
        snapshot = {'members': []}
        with mock.patch.object(replay, 'read_snapshot', return_value=snapshot) as read:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(replay.main(['--port', '7788', '--session', 'sess-0001']), 0)
        read.assert_called_once_with(None, 7788, session='sess-0001', project=None)
        rejected = (
            ['--demo', '--session', 'sess-0001'],
            ['--input', str(self.demo), '--project', 'C--demo'],
            ['--port', '7788', '--session', 'sess-0001', '--project', 'C--demo'],
            ['--port', '7788', '--session', '   '],
            ['--demo', '--input', str(self.demo)],
        )
        for args in rejected:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                replay.main(args)
            self.assertEqual(raised.exception.code, 2)

    def test_output_must_differ_from_the_input(self):
        target = self.root / 'snapshot.json'
        target.write_text('{"members": []}\n', encoding='utf-8')
        with contextlib.redirect_stderr(io.StringIO()) as error:
            code = replay.main(['--input', str(target), '-o', str(target)])
        self.assertEqual(code, 1)
        self.assertEqual(target.read_text(encoding='utf-8'), '{"members": []}\n')
        self.assertIn('output must differ', error.getvalue())
        self.assertNotIn('members', error.getvalue())

    def test_invalid_json_does_not_write_or_echo_the_body(self):
        source = self.root / 'bad.json'
        source.write_text('{"leak":"sk-ant-api03-TAILSHOULDNOTREMAIN9"', encoding='utf-8')
        target = self.root / 'replay.html'
        with contextlib.redirect_stderr(io.StringIO()) as error:
            code = replay.main(['--input', str(source), '--output', str(target)])
        self.assertEqual(code, 1)
        self.assertFalse(target.exists())
        self.assertNotIn('TAILSHOULDNOTREMAIN9', error.getvalue())
        self.assertNotIn('sk-ant', error.getvalue())


if __name__ == '__main__':
    unittest.main()
