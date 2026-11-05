"""官方任务的估算成本只来自对得上的负责人、且落在进行中区间里的用量。"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from test_agent_teams_fixture import SECRET
from test_board import BoardEnv, asst, jl, user

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'examples' / 'task-cost.synthetic.json'
DEMO = ROOT / 'examples' / 'board-demo.synthetic.json'
BROWSER = Path(__file__).resolve().parent / 'check_task_cost_browser.js'


def load_task_cost():
    spec = importlib.util.spec_from_file_location(
        'task_cost_under_test', ROOT / 'team-board' / 'task_cost.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(board):
    return board.build_snapshot(board.argparse.Namespace(
        session=None, project=None, stale=600, source='all'))


def zeros():
    return {'input_tokens': 0, 'output_tokens': 0,
            'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}


def usage(incoming, outgoing):
    return {'input_tokens': incoming, 'output_tokens': outgoing,
            'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}


def with_id(entry, message_id):
    entry['message']['id'] = message_id
    return entry


def sonnet_price(model, buckets):
    if 'sonnet' not in (model or ''):
        return None
    return (buckets['in'] * 3 + buckets['out'] * 15
            + buckets['cache_read'] * 0.3 + buckets['cache_write'] * 3.75) / 1e6


class TaskCostRulesTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_task_cost()

    def test_fixture_covers_owned_unowned_and_unpriced(self):
        raw = FIXTURE.read_text(encoding='utf-8')
        self.assertIsNone(SECRET.search(raw))
        payload = json.loads(raw)
        tasks = [dict(item) for item in payload['tasks']]
        self.mod.attach_estimates(
            tasks, payload['sources'], payload['marks'], payload['now'], sonnet_price)
        by_id = {task['id']: task for task in tasks}
        owned = by_id['cost-1']['estimate']
        self.assertEqual(owned['status'], 'known')
        self.assertFalse(owned['partial'])
        self.assertFalse(owned['unpriced'])
        self.assertEqual(owned['basis'], 'transcript')
        self.assertEqual(owned['tokens'], {'in': 3000, 'out': 600, 'cache_read': 0, 'cache_write': 0})
        self.assertEqual(owned['cost'], round(sonnet_price('claude-sonnet-5', owned['tokens']), 4))
        self.assertNotIn('estimate', by_id['cost-2'])
        unpriced = by_id['cost-3']['estimate']
        self.assertIsNone(unpriced['cost'])
        self.assertEqual(unpriced['status'], 'partial')
        self.assertTrue(unpriced['partial'])
        self.assertTrue(unpriced['unpriced'])
        self.assertEqual(unpriced['tokens'], {'in': 100, 'out': 40, 'cache_read': 0, 'cache_write': 0})
        self.assertNotIn('example-local', json.dumps(unpriced))

    def test_file_mtime_is_a_lower_bound_and_shared_files_stay_unknown(self):
        tasks = [
            {'id': 'a', 'owner': 'synthetic-researcher', 'ownerMatch': 'synthetic-researcher',
             'normalizedStatus': 'in_progress', 'stateSince': 100, 'stateSinceKind': 'file'},
            {'id': 'b', 'owner': 'synthetic-researcher', 'ownerMatch': 'synthetic-researcher',
             'normalizedStatus': 'in_progress', 'stateSince': 100, 'stateSinceKind': 'file-shared'},
        ]
        sources = {'synthetic-researcher': {'events': [
            {'ts': 50, 'model': 'claude-sonnet-5', 'usage': {'in': 100, 'out': 0, 'cache_read': 0, 'cache_write': 0}},
            {'ts': 150, 'model': 'claude-sonnet-5', 'usage': {'in': 10, 'out': 0, 'cache_read': 0, 'cache_write': 0}},
        ]}}
        self.mod.attach_estimates(tasks, sources, [], 200, sonnet_price)
        self.assertEqual(tasks[0]['estimate']['basis'], 'file')
        self.assertTrue(tasks[0]['estimate']['partial'])
        self.assertEqual(tasks[0]['estimate']['tokens']['in'], 10)
        self.assertEqual(tasks[1]['estimate']['status'], 'unknown')
        self.assertIsNone(tasks[1]['estimate']['cost'])

    def test_overlap_is_not_split_and_untimed_usage_stays_partial(self):
        tasks = [
            {'id': 'a', 'owner': 'synthetic-researcher', 'ownerMatch': 'synthetic-researcher',
             'normalizedStatus': 'completed', 'stateSince': 100, 'stateSinceKind': 'file'},
            {'id': 'b', 'owner': 'synthetic-researcher', 'ownerMatch': 'synthetic-researcher',
             'normalizedStatus': 'completed', 'stateSince': 150, 'stateSinceKind': 'file'},
        ]
        marks = [
            {'ts': 0, 'id': 'a', 'status': 'in_progress', 'owner': 'synthetic-researcher'},
            {'ts': 100, 'id': 'a', 'status': 'completed'},
            {'ts': 50, 'id': 'b', 'status': 'in_progress', 'owner': 'synthetic-researcher'},
            {'ts': 150, 'id': 'b', 'status': 'completed'},
        ]
        sources = {'synthetic-researcher': {'events': [
            {'ts': 10, 'model': 'claude-sonnet-5', 'usage': {'in': 10, 'out': 0, 'cache_read': 0, 'cache_write': 0}},
            {'ts': 60, 'model': 'claude-sonnet-5', 'usage': {'in': 99, 'out': 0, 'cache_read': 0, 'cache_write': 0}},
            {'ts': 120, 'model': 'claude-sonnet-5', 'usage': {'in': 20, 'out': 0, 'cache_read': 0, 'cache_write': 0}},
            {'ts': None, 'model': 'claude-sonnet-5', 'usage': {'in': 5, 'out': 0, 'cache_read': 0, 'cache_write': 0}},
        ]}}
        self.mod.attach_estimates(tasks, sources, marks, 200, sonnet_price)
        self.assertEqual(tasks[0]['estimate']['tokens']['in'], 10)
        self.assertEqual(tasks[1]['estimate']['tokens']['in'], 20)
        self.assertTrue(tasks[0]['estimate']['partial'])
        self.assertTrue(tasks[1]['estimate']['partial'])
        self.assertNotEqual(tasks[0]['estimate']['tokens']['in'], 99)

    def test_owner_change_limits_the_window_and_a_near_miss_has_no_estimate(self):
        tasks = [
            {'id': 'a', 'owner': 'synthetic-researcher', 'ownerMatch': 'synthetic-researcher',
             'normalizedStatus': 'completed', 'stateSince': 100, 'stateSinceKind': 'file'},
            {'id': 'b', 'owner': 'synthetic-researcher ', 'ownerMatch': None,
             'normalizedStatus': 'in_progress', 'stateSince': 0, 'stateSinceKind': 'file'},
        ]
        marks = [
            {'ts': 0, 'id': 'a', 'status': 'in_progress', 'owner': 'synthetic-researcher'},
            {'ts': 50, 'id': 'a', 'status': 'in_progress', 'owner': 'synthetic-other'},
            {'ts': 100, 'id': 'a', 'status': 'completed', 'owner': 'synthetic-other'},
        ]
        sources = {'synthetic-researcher': {'events': [
            {'ts': 10, 'model': 'claude-sonnet-5', 'usage': {'in': 7, 'out': 0, 'cache_read': 0, 'cache_write': 0}},
            {'ts': 75, 'model': 'claude-sonnet-5', 'usage': {'in': 70, 'out': 0, 'cache_read': 0, 'cache_write': 0}},
        ]}}
        self.mod.attach_estimates(tasks, sources, marks, 200, sonnet_price)
        self.assertEqual(tasks[0]['estimate']['tokens']['in'], 7)
        self.assertEqual(tasks[0]['estimate']['status'], 'known')
        self.assertNotIn('estimate', tasks[1])

    def test_ambiguous_name_and_missing_transcript_are_unknown(self):
        task = {'id': 'a', 'owner': 'synthetic-researcher', 'ownerMatch': 'synthetic-researcher',
                'normalizedStatus': 'in_progress', 'stateSince': 10, 'stateSinceKind': 'file'}
        marks = [{'ts': 10, 'id': 'a', 'status': 'in_progress', 'owner': 'synthetic-researcher'}]
        events = [{'ts': 20, 'model': 'claude-sonnet-5', 'usage': {'in': 1, 'out': 0, 'cache_read': 0, 'cache_write': 0}}]
        self.mod.attach_estimates([task], {'synthetic-researcher': {'ambiguous': True, 'events': events}},
                                  marks, 30, sonnet_price)
        self.assertEqual(task['estimate']['status'], 'unknown')
        self.assertIsNone(task['estimate']['cost'])
        self.mod.attach_estimates([task], {}, marks, 30, sonnet_price)
        self.assertEqual(task['estimate']['status'], 'unknown')

    def test_demo_card_uses_the_sonnet_price_row(self):
        demo = json.loads(DEMO.read_text(encoding='utf-8'))
        tasks = {task['id']: task for task in demo['officialTeam']['tasks']}
        owned = tasks['1']['estimate']
        self.assertEqual(owned['cost'], round(sonnet_price('claude-sonnet-5', owned['tokens']), 4))
        self.assertEqual(owned['cost'], 0.018)
        self.assertNotIn('estimate', tasks['3'])
        self.assertIsNone(tasks['2']['estimate']['cost'])
        self.assertTrue(tasks['2']['estimate']['unpriced'])
        self.assertIsNone(SECRET.search(DEMO.read_text(encoding='utf-8')))


class TaskCostSnapshotTest(unittest.TestCase):
    def test_snapshot_uses_owner_usage_inside_the_window_only(self):
        env = BoardEnv()
        try:
            team = env.claude / 'teams' / 'session-sess-000'
            (team / 'inboxes').mkdir(parents=True)
            tasks = env.claude / 'tasks' / 'session-sess-000'
            tasks.mkdir(parents=True)
            (team / 'config.json').write_text(json.dumps({
                'members': [
                    {'name': 'synthetic-lead', 'agentType': 'team-lead', 'agentId': 'synthetic-agent-lead'},
                ],
            }, ensure_ascii=False), encoding='utf-8')
            (tasks / 'tasks.json').write_text(json.dumps({'tasks': [
                {'id': '1', 'subject': 'Owned synthetic', 'status': 'completed', 'owner': '阿服'},
                {'id': '2', 'subject': 'Unowned synthetic', 'status': 'pending'},
                {'id': '3', 'subject': 'Unpriced synthetic', 'status': 'in_progress', 'owner': '探探'},
                {'id': '4', 'subject': 'Lead synthetic', 'status': 'completed', 'owner': 'synthetic-lead'},
            ]}, ensure_ascii=False), encoding='utf-8')
            alone = tasks / 'alone.json'
            alone.write_text(json.dumps({
                'task': {'id': '5', 'subject': 'File clock synthetic', 'status': 'in_progress', 'owner': '小界'},
            }, ensure_ascii=False), encoding='utf-8')
            boundary = datetime.fromisoformat('2026-01-01T10:00:41+00:00').timestamp()
            os.utime(alone, (boundary, boundary))
            sub = env.claude / 'projects' / 'C--demo' / 'sess-0001' / 'subagents'
            (sub / 'agent-a1.jsonl').write_text(jl(
                user('2026-01-01T09:59:00Z', 'synthetic'),
                with_id(asst('2026-01-01T10:00:00Z', 'claude-sonnet-5',
                             [{'type': 'text', 'text': 'before'}], usage=usage(999, 999)), 'm-before'),
                asst('2026-01-01T10:05:00Z', 'claude-sonnet-5', [
                    {'type': 'tool_use', 'id': 'tu-start', 'name': 'TaskUpdate',
                     'input': {'taskId': '1', 'status': 'in_progress', 'owner': '阿服',
                               'description': 'synthetic status only'}},
                ], usage=zeros()),
                with_id(asst('2026-01-01T10:05:00Z', 'claude-sonnet-5',
                             [{'type': 'text', 'text': 'edge'}], usage=usage(100, 10)), 'm-edge'),
                with_id(asst('2026-01-01T10:06:00Z', 'claude-sonnet-5',
                             [{'type': 'text', 'text': 'first'}], usage=usage(1000, 40)), 'm-in'),
                with_id(asst('2026-01-01T10:06:30Z', 'claude-sonnet-5',
                             [{'type': 'text', 'text': 'last copy'}], stop='end_turn', usage=usage(1000, 50)), 'm-in'),
                asst('2026-01-01T10:10:00Z', 'claude-sonnet-5', [
                    {'type': 'tool_use', 'id': 'tu-done', 'name': 'TaskUpdate',
                     'input': {'taskId': '1', 'status': 'completed', 'owner': '阿服'}},
                ], usage=zeros()),
                with_id(asst('2026-01-01T10:11:00Z', 'claude-sonnet-5',
                             [{'type': 'text', 'text': 'after'}], stop='end_turn', usage=usage(500, 500)), 'm-after'),
            ), encoding='utf-8')
            (sub / 'agent-c3.jsonl').write_text(jl(
                user('2026-01-01T10:04:00Z', 'synthetic'),
                asst('2026-01-01T10:05:00Z', 'local-board-model', [
                    {'type': 'tool_use', 'id': 'tu-local', 'name': 'TaskUpdate',
                     'input': {'taskId': '3', 'status': 'in_progress', 'owner': '探探'}},
                ], usage=usage(100, 40)),
            ), encoding='utf-8')
            (sub / 'agent-c3.meta.json').write_text(json.dumps({
                'agentType': 'researcher', 'description': 'synthetic unpriced',
            }), encoding='utf-8')
            lead_path = env.claude / 'projects' / 'C--demo' / 'sess-0001.jsonl'
            lead_path.write_text(lead_path.read_text(encoding='utf-8') + jl(
                asst('2026-01-01T12:00:00Z', 'claude-opus-5', [
                    {'type': 'tool_use', 'id': 'tu-lead', 'name': 'TaskUpdate',
                     'input': {'taskId': '4', 'status': 'in_progress', 'owner': 'synthetic-lead'}},
                ], usage=zeros()),
                asst('2026-01-01T12:01:00Z', 'claude-opus-5',
                     [{'type': 'text', 'text': 'lead slice'}], usage=usage(200, 20)),
                asst('2026-01-01T12:05:00Z', 'claude-opus-5', [
                    {'type': 'tool_use', 'id': 'tu-lead-done', 'name': 'TaskUpdate',
                     'input': {'taskId': '4', 'status': 'completed', 'owner': 'synthetic-lead'}},
                ], stop='end_turn', usage=zeros()),
            ), encoding='utf-8')
            original = (tasks / 'tasks.json').read_bytes()
            board = env.load()
            board._TASK_STATE_CLOCK.clear()
            snap = snapshot(board)
            rows = {row['id']: row for row in snap['officialTeam']['tasks']}
            owned_usage = {'in': 1100, 'out': 60, 'cache_read': 0, 'cache_write': 0}
            owned = rows['1']['estimate']
            self.assertEqual(owned['status'], 'known')
            self.assertEqual(owned['basis'], 'transcript')
            self.assertFalse(owned['partial'])
            self.assertEqual(owned['tokens'], owned_usage)
            self.assertEqual(owned['cost'], round(board.estimate_cost('claude-sonnet-5', owned_usage), 4))
            self.assertNotIn('estimate', rows['2'])
            unpriced = rows['3']['estimate']
            self.assertIsNone(unpriced['cost'])
            self.assertTrue(unpriced['unpriced'])
            self.assertEqual(unpriced['status'], 'partial')
            self.assertEqual(unpriced['tokens']['in'], 100)
            self.assertEqual(unpriced['tokens']['out'], 40)
            lead_usage = {'in': 200, 'out': 20, 'cache_read': 0, 'cache_write': 0}
            self.assertEqual(rows['4']['estimate']['cost'], round(board.estimate_cost('claude-opus-5', lead_usage), 4))
            self.assertEqual(rows['4']['estimate']['tokens'], lead_usage)
            lowered = rows['5']['estimate']
            one_slice = board._task_cost.normalize_usage({
                'input_tokens': 10, 'output_tokens': 50,
                'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 100,
            })
            self.assertEqual(lowered['basis'], 'file')
            self.assertTrue(lowered['partial'])
            self.assertEqual(lowered['tokens'], one_slice)
            self.assertEqual(lowered['cost'], round(board.estimate_cost('claude-sonnet-5', one_slice), 4))
            member = next(item for item in snap['members'] if item['name'] == '阿服')
            self.assertGreater(member['cost'], owned['cost'])
            blob = json.dumps(snap, ensure_ascii=False)
            self.assertNotIn('usageEvents', blob)
            self.assertNotIn('taskMarks', blob)
            official = json.dumps(snap['officialTeam'], ensure_ascii=False)
            self.assertNotIn('synthetic status only', official)
            self.assertNotIn('input_tokens', official)
            self.assertIsNone(SECRET.search(official))
            self.assertEqual((tasks / 'tasks.json').read_bytes(), original)
            self.assertNotIn('estimate', original.decode('utf-8'))
        finally:
            env.cleanup()


class TaskCostBrowserTest(unittest.TestCase):
    def test_demo_kanban_shows_cost_and_drawer_without_secrets(self):
        if not shutil.which('node'):
            self.skipTest('node is not installed')
        import headless_chrome
        import test_demo_board
        binary = headless_chrome.chrome_binary()
        if not binary:
            self.skipTest('chrome is not installed')
        server = test_demo_board.demo.DemoServer(0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='task-cost-shots-') as directory:
                with headless_chrome.launch_headless_chrome(binary) as chrome:
                    completed = subprocess.run(
                        [shutil.which('node'), str(BROWSER),
                         'http://127.0.0.1:%d/' % server.server_port,
                         str(chrome.port), directory],
                        check=False, capture_output=True, text=True, timeout=60)
                    if completed.returncode != 0:
                        detail = (completed.stdout or '') + (completed.stderr or '')
                        self.fail(detail[-4000:])
                    report = json.loads(completed.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertIn('$0.018', report['card1'])
        self.assertNotIn('$', report['card3'])
        self.assertNotIn('估算', report['card3'])
        self.assertIn('部分', report['card2'])
        self.assertIn('估算', report['drawer1'])
        self.assertIn('输入', report['drawer1'])
        self.assertNotIn('估算', report['drawer3'])
        self.assertIn('部分 · 未知', report['drawer2'])
        self.assertIn('$0.018', report['narrow'])
        self.assertNotIn('sk-', report['board'])
        self.assertNotIn('@', report['board'])
