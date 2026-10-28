"""Battle-card SVG: real snapshot fields only, same totals as the metrics export."""
import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
NS = '{http://www.w3.org/2000/svg}'
spec = importlib.util.spec_from_file_location('battle_card', ROOT / 'tools' / 'battle_card.py')
battle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(battle)


def _load(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))


def _texts(svg):
    root = ET.fromstring(svg.encode('utf-8'))
    return [''.join(node.itertext()) for node in root.iter() if node.tag in (NS + 'text', NS + 'desc', NS + 'title')]


def _js_svg(snapshot, theme='auto', static=True):
    node = shutil.which('node')
    if not node:
        raise unittest.SkipTest('node is not installed')
    html = (ROOT / 'team-board' / 'index.html').read_text(encoding='utf-8')
    start = html.index('/* BATTLE_CARD_BEGIN')
    end = html.index('/* BATTLE_CARD_END */') + len('/* BATTLE_CARD_END */')
    script = html[start:end] + '\nmodule.exports = { battleCardSvg };\n'
    handle = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8')
    handle.write(script)
    handle.close()
    try:
        proc = subprocess.run(
            [node, '-e',
             'const m=require(process.argv[1]); process.stdout.write(m.battleCardSvg(JSON.parse(process.argv[2]), JSON.parse(process.argv[3])));',
             handle.name, json.dumps(snapshot), json.dumps({'theme': theme, 'static': static})],
            capture_output=True, check=False)
    finally:
        Path(handle.name).unlink()
    if proc.returncode != 0:
        raise AssertionError(proc.stderr.decode('utf-8', 'replace'))
    return proc.stdout.decode('utf-8')


class BattleCardRenderTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = _load('examples/snapshot.synthetic.json')
        self.demo = _load('examples/board-demo.synthetic.json')

    def test_svg_parses_and_has_no_external_content(self):
        for static in (True, False):
            svg = battle.build_card(self.snapshot, static=static)
            root = ET.fromstring(svg.encode('utf-8'))
            tags = {node.tag.split('}')[-1] for node in root.iter()}
            self.assertNotIn('foreignObject', tags)
            self.assertNotIn('script', tags)
            self.assertNotIn('image', tags)
            self.assertNotIn('<script', svg.lower())
            urls = [attr for node in root.iter() for name, attr in node.attrib.items() if 'href' in name.lower()]
            self.assertEqual(urls, [])
            self.assertEqual(svg.count('http://www.w3.org/2000/svg'), 1)
            self.assertNotIn('https://', svg)
        self.assertIn('<animate ', battle.build_card(self.snapshot, static=False))
        self.assertNotIn('<animate ', battle.build_card(self.snapshot, static=True))
        self.assertNotIn('@keyframes', battle.build_card(self.snapshot, static=True))
        dark = battle.build_card(self.snapshot, theme='dark', static=True)
        self.assertIn('fill="#070a11"', dark)
        self.assertNotIn('#e7e9ed', dark)
        light = battle.build_card(self.snapshot, theme='light', static=True)
        self.assertIn('fill="#e7e9ed"', light)
        self.assertNotIn('#070a11', light)

    def test_numbers_match_export_metrics(self):
        metrics = battle.export_metrics(self.snapshot)
        svg = battle.build_card(self.snapshot, static=True)
        totals = metrics['totals']
        joined = '\n'.join(_texts(svg))
        self.assertIn(battle.num_text(metrics['memberCount']), joined)
        self.assertIn('$' + battle.num_text(totals['cost']), joined)
        self.assertIn('成员成本 ' + battle.num_text(totals['membersCost']), joined)
        self.assertIn('成员输出 ' + battle.num_text(totals['tokens']), joined)
        self.assertIn('未定价 ' + battle.num_text(totals['unpriced']), joined)
        self.assertNotIn('部分', svg)
        for group in ('usage', 'leadUsage', 'allUsage'):
            for key in ('in', 'out', 'cache_read', 'cache_write', 'total', 'cacheHit'):
                self.assertIn(battle.num_text(totals[group][key]), joined)
        self.assertIn(battle.num_text(totals['allUsage']['total']), joined)
        self.assertIn(battle.num_text(totals['allUsage']['cacheHit']), joined)

    def test_partial_cost_is_labeled_and_unpriced_zero_is_not(self):
        priced = battle.build_card(self.snapshot, static=True)
        self.assertNotIn('部分', priced)
        partial = json.loads(json.dumps(self.snapshot))
        partial['totals']['unpriced'] = 2
        svg = battle.build_card(partial, static=True)
        self.assertIn('部分', svg)
        self.assertIn('$' + battle.num_text(partial['totals']['cost']), svg)
        self.assertIn('未定价 2', svg)
        self.assertIn('成本是部分估算，未定价的没有计入', svg)

    def test_xml_escape(self):
        snapshot = {'members': [{'name': 'A & B <C> "D" \'E', 'status': 'completed'}]}
        svg = battle.build_card(snapshot, static=True)
        ET.fromstring(svg.encode('utf-8'))
        self.assertIn('A &amp; B &lt;C&gt; &quot;D&quot; &apos;E', svg)
        self.assertNotIn('<C>', svg)
        self.assertNotIn('<script', svg.lower())

    def test_missing_fields_are_not_invented(self):
        svg = battle.build_card({'members': [{'status': 'running'}]}, static=True)
        self.assertIn('进行中', svg)
        self.assertIn('成员 01', svg)
        self.assertNotIn('已完成', svg)
        self.assertNotIn('$', svg)
        self.assertNotIn('缓存', svg)
        self.assertNotIn('用时', svg)
        self.assertNotIn('token', svg.lower())
        self.assertNotIn('部分', svg)
        self.assertNotIn('研发部', svg)
        self.assertNotIn('0.00', svg)
        self.assertNotIn('停滞提醒', svg)
        self.assertNotIn('作战区', svg)
        self.assertNotIn('实时事件', svg)
        incomplete = {'members': [{'name': '甲', 'status': 'completed', 'cost': 1.5}], 'totals': {'cost': 9}}
        partial = battle.build_card(incomplete, static=True)
        self.assertIn('甲', partial)
        self.assertIn('$1.5', partial)
        self.assertNotIn('合计 token', partial)
        self.assertNotIn('估算成本', partial)
        self.assertNotIn('$9', partial)

    def test_invalid_totals_fail_without_echoing_values(self):
        bad = json.loads(json.dumps(self.snapshot))
        bad['totals']['cost'] = 'SECRET'
        with self.assertRaises(battle.ExportError) as raised:
            battle.build_card(bad)
        self.assertNotIn('SECRET', str(raised.exception))

    def test_private_fields_and_paths_are_omitted(self):
        snap = json.loads(json.dumps(self.demo))
        snap['session'] = {'id': 'SECRET_SESSION', 'cwd': '/home/SECRET_PATH/proj', 'project': 'C--Users-SECRET'}
        snap['sessions'] = [{'id': 'SECRET_SESSION'}]
        snap['lead']['lastUserPrompt'] = 'SECRET_PROMPT do the thing'
        snap['lead']['name'] = 'lead@example.com'
        snap['members'][0]['prompt'] = 'SECRET_PROMPT'
        snap['members'][0]['description'] = 'SECRET_DESC'
        snap['members'][0]['lastText'] = 'SECRET_TEXT'
        snap['members'][0]['id'] = 'SECRET_SESSION'
        snap['members'][0]['writes'] = [{'path': '/tmp/SECRET_PATH/file.py', 'label': 'src/SECRET_PATH.py'}]
        snap['members'][0]['timeline'] = [{'kind': 'tool', 'label': 'Bash · SECRET_ARG', 'name': 'Bash'}]
        snap['members'][0]['current'] = {'name': 'Bash', 'label': 'Bash · SECRET_ARG'}
        snap['officialTeam']['teamName'] = 'session-SECRETSESSION'
        snap['officialTeam']['tasks'][0]['description'] = 'SECRET_DESC pending'
        snap['officialTeam']['tasks'][1]['description'] = 'SECRET_DESC done'
        snap['officialTeam']['tasks'][1]['owner'] = 'owner@example.com'
        snap['comms'] = [{'text': 'SECRET_MESSAGE', 'file': '/tmp/SECRET_PATH'}]
        snap['feed'] = [{'label': 'Read · SECRET_ARG', 'name': 'SECRET_NAME'}]
        svg = battle.build_card(snap, static=True)
        ET.fromstring(svg.encode('utf-8'))
        for secret in (
                'SECRET', 'example.com', 'SECRET_PATH', 'SECRET_PROMPT', 'SECRET_ARG',
                'SECRET_DESC', 'SECRET_TEXT', 'SECRET_MESSAGE', 'SECRET_SESSION',
                'synthetic-session', 'synthetic-member', 'session-syntheti', '/tmp/', '/home/'):
            self.assertNotIn(secret, svg)
        self.assertIn('写入 1', svg)
        self.assertIn('Draft synthetic notes', svg)
        self.assertNotIn('Survey the synthetic module', svg)
        self.assertIn('演示后端', svg)
        self.assertNotIn('负责 ', svg)
        self.assertNotIn('owner@example.com', svg)

    def test_demo_snapshot_draws_only_completed_tasks_and_real_duration(self):
        svg = battle.build_card(self.demo, static=True)
        self.assertIn('Draft synthetic notes', svg)
        self.assertIn('负责 合成写手', svg)
        self.assertIn('用时 1m 50s', svg)
        self.assertIn('演示队长', svg)
        self.assertIn('研发部', svg)
        self.assertNotIn('Survey the synthetic module', svg)
        self.assertNotIn('合成演示：展示团队成员和指标', svg)
        self.assertNotIn('官方 Agent Team 合成演示', svg)
        self.assertNotIn('Read · synthetic example', svg)
        self.assertNotIn('backend-dev', svg)
        self.assertNotIn('synthetic-member-1', svg)
        self.assertNotIn('huhu-plan', svg)
        self.assertIn('作战区 1', svg)
        self.assertIn('实时事件 2', svg)
        self.assertNotIn('synthetic-repo', svg)
        self.assertNotIn('worktree-feature-board', svg)
        self.assertNotIn('toolu_synthetic_read', svg)
        self.assertNotIn('PreToolUse', svg)
        self.assertNotIn('停滞提醒', svg)

    def test_present_stall_alerts_are_counted_without_names(self):
        snap = {
            'members': [{'status': 'stalled', 'name': '不应出现的提醒名'}],
            'stallAlerts': {
                'thresholdSec': 600,
                'candidates': [
                    {'id': 'secret-id', 'name': '提醒甲', 'status': 'stalled', 'idleSeconds': 900},
                    {'name': '未到阈值', 'status': 'idle', 'idleSeconds': 100},
                    {'name': '出错的人', 'status': 'error', 'idleSeconds': 600},
                    {'name': '进行中', 'status': 'running', 'idleSeconds': 9999},
                    {'status': 'stalled'},
                    'not-a-member',
                ],
            },
            'theaters': [{'label': 'SECRET_REPO', 'sessions': [{'branch': 'SECRET_BRANCH'}]}, 'skip'],
            'live': {
                'available': True,
                'retained': 3,
                'events': [{'tool_name': 'SECRET_TOOL', 'session_id': 'SECRET_LIVE'}],
            },
        }
        svg = battle.build_card(snap, static=True)
        self.assertIn('1 停滞', svg)
        self.assertIn('停滞提醒 2', svg)
        self.assertIn('作战区 1', svg)
        self.assertIn('实时事件 3', svg)
        for secret in ('提醒甲', '未到阈值', '出错的人', 'secret-id', 'SECRET_REPO', 'SECRET_BRANCH', 'SECRET_TOOL', 'SECRET_LIVE'):
            self.assertNotIn(secret, svg)
        quiet = {'stallAlerts': {'thresholdSec': 600, 'candidates': [{'status': 'idle', 'idleSeconds': 10}]}}
        self.assertNotIn('停滞提醒', battle.build_card(quiet, static=True))
        broken = {'stallAlerts': {'thresholdSec': '600', 'candidates': []}, 'theaters': 'nope', 'live': {'available': False, 'retained': 4}}
        blank = battle.build_card(broken, static=True)
        self.assertNotIn('停滞提醒', blank)
        self.assertNotIn('作战区', blank)
        self.assertNotIn('实时事件', blank)

    def test_shipped_sample_matches_the_generator(self):
        expected = battle.build_card(self.snapshot, theme='auto', static=True)
        sample = (ROOT / 'docs' / 'img' / 'battle-card.svg').read_text(encoding='utf-8')
        self.assertEqual(sample, expected)

    def test_browser_svg_matches_python(self):
        partial = json.loads(json.dumps(self.snapshot))
        partial['totals']['unpriced'] = 1
        partial['members'][0]['name'] = 'A & B <C>'
        stall = {
            'members': [{'status': 'idle'}],
            'stallAlerts': {'thresholdSec': 600, 'candidates': [
                {'status': 'stalled', 'idleSeconds': 900},
                {'status': 'idle', 'idleSeconds': 10},
            ]},
            'theaters': [{'label': 'hidden-repo'}],
            'live': {'available': True, 'retained': 1, 'events': [{'tool_name': 'Hidden'}]},
        }
        cases = (
            (self.snapshot, 'auto', True),
            (self.snapshot, 'dark', False),
            (self.demo, 'light', True),
            (partial, 'auto', True),
            ({'members': [{'status': 'completed'}]}, 'auto', True),
            (stall, 'auto', True),
        )
        for snapshot, theme, static in cases:
            with self.subTest(theme=theme, static=static):
                self.assertEqual(
                    _js_svg(snapshot, theme, static),
                    battle.build_card(snapshot, theme=theme, static=static))


class BattleCardCliTest(unittest.TestCase):
    def setUp(self):
        self.source = ROOT / 'examples' / 'snapshot.synthetic.json'
        self.snapshot = _load('examples/snapshot.synthetic.json')
        self.tmp = tempfile.TemporaryDirectory(prefix='battle-card-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_stdout_and_output_file(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(battle.main(['--input', str(self.source), '--static']), 0)
        svg = output.getvalue()
        ET.fromstring(svg.encode('utf-8'))
        target = self.root / 'card.svg'
        self.assertEqual(battle.main(['--input', str(self.source), '--static', '--output', str(target)]), 0)
        self.assertEqual(target.read_text(encoding='utf-8'), svg)

    def test_reader_is_the_export_snapshot_reader(self):
        self.assertIs(battle.read_snapshot, battle._exporter.read_snapshot)
        with mock.patch.object(battle, 'read_snapshot', return_value=self.snapshot) as read:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(battle.main(['--port', '7788', '--static']), 0)
        read.assert_called_once_with(None, 7788, session=None, project=None)

    def test_session_and_project_selectors_follow_the_exporter(self):
        with mock.patch.object(battle, 'read_snapshot', return_value=self.snapshot) as read:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(battle.main(['--port', '7788', '--session', 'sess-0001', '--static']), 0)
        read.assert_called_once_with(None, 7788, session='sess-0001', project=None)
        with mock.patch.object(battle, 'read_snapshot', return_value=self.snapshot) as read:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(battle.main(['--port', '7788', '--project', 'C--demo', '--static']), 0)
        read.assert_called_once_with(None, 7788, session=None, project='C--demo')
        rejected = (
            ['--input', str(self.source), '--session', 'sess-0001'],
            ['--input', str(self.source), '--project', 'C--demo'],
            ['--port', '7788', '--session', 'sess-0001', '--project', 'C--demo'],
            ['--port', '7788', '--session', '   '],
            ['--port', '7788', '--project', ''],
        )
        for args in rejected:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                battle.main(args)
            self.assertEqual(raised.exception.code, 2)

    def test_port_read_rejects_redirects_and_proxies(self):
        response = io.BytesIO(self.source.read_bytes())
        with mock.patch.object(battle._exporter.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = response
            loaded = battle.read_snapshot(port=7788)
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], battle._exporter.NoRedirect)
        build.return_value.open.assert_called_once_with('http://127.0.0.1:7788/snapshot.json', timeout=5)
        self.assertEqual(loaded['totals']['cost'], self.snapshot['totals']['cost'])
        with self.assertRaises(battle.ExportError):
            battle._exporter.NoRedirect().redirect_request(None, None, 302, 'Moved', {}, 'http://127.0.0.1/other')

    def test_failures_do_not_echo_payload(self):
        source = self.root / 'bad.json'
        source.write_bytes(b'{"SECRET":')
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(battle.main(['--input', str(source)]), 1)
        self.assertIn('battle card failed:', error.getvalue())
        self.assertNotIn('SECRET', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(battle.main(['--input', str(self.root / 'missing.json')]), 1)
        self.assertIn('cannot read snapshot', error.getvalue())

    def test_output_cannot_replace_the_input(self):
        source = self.root / 'source.json'
        source.write_bytes(self.source.read_bytes())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(battle.main(['--input', str(source), '--output', str(source)]), 1)
        self.assertEqual(source.read_bytes(), self.source.read_bytes())

    def test_invalid_arguments(self):
        for args in ([], ['--port', '0'], ['--theme', 'neon', '--input', str(self.source)],
                     ['--input', str(self.source), '--port', '7788']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                battle.main(args)
            self.assertEqual(raised.exception.code, 2)

    def test_page_exports_locally(self):
        html = (ROOT / 'team-board' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="btn-battle-card"', html)
        self.assertIn('不上传', html)
        handler = html.split("getElementById('btn-battle-card')", 1)[1].split('/* ---------- 演示数据', 1)[0]
        self.assertIn("link.download = 'battle-card.svg'", handler)
        self.assertIn('createObjectURL', handler)
        self.assertNotIn('fetch(', handler)
        self.assertNotIn('XMLHttpRequest', handler)
        self.assertNotIn('navigator.sendBeacon', handler)


if __name__ == '__main__':
    unittest.main()
