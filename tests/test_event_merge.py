"""钩子事件和转录动作流并成一条。只用合成数据，不读真实会话。"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'SECRET_DO_NOT_FORWARD'


def load_board(home):
    os.environ['HOME'] = str(home)
    os.environ['USERPROFILE'] = str(home)
    spec = importlib.util.spec_from_file_location('event_merge_board', ROOT / 'team-board' / 'team_board.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def feed_row(ts, tool, tid, label, fp='', done=True, member='a1'):
    row = {
        'ts': ts, 'memberId': member, 'name': '阿服', 'avatar': 'server-engineer',
        'label': label, 'error': False, 'done': done, 'tool': tool,
    }
    if tid:
        row['toolId'] = tid
    if fp:
        row['_inputFp'] = fp
    return row


def hook(event, ts, tool='', tid='', agent='agent-a1', fp='', seq=1, session='sess-0001'):
    ev = {
        'v': 1, 'source': 'claude-hook', 'session_id': session, 'event': event, 'ts': ts, 'seq': seq,
    }
    if tool:
        ev['tool_name'] = tool
    if tid:
        ev['tool_use_id'] = tid
    if agent:
        ev['agent_id'] = agent
    if fp:
        ev['input_fp'] = fp
    return ev


def snapshot(feed, events, tool_count=1, tool_ids=None, cost=1.25, available=True, source='claude'):
    ids = list(tool_ids or [])
    return {
        'source': source,
        'session': {'id': 'sess-0001', 'source': source},
        'members': [{
            'id': 'a1', 'name': '阿服', 'avatar': 'server-engineer', 'agentType': 'backend-dev',
            'toolCount': tool_count, 'cost': cost, 'timeline': [], 'current': None,
        }],
        'lead': {'toolCount': 0, 'cost': 0.5, 'timeline': [], 'current': None},
        'departments': [{'id': 'dev', 'members': ['a1'], 'tools': tool_count}],
        'totals': {'cost': round(cost + 0.5, 4)},
        'feed': feed,
        '_activityIndex': {'lead': [], 'members': {'a1': ids}},
        'live': {
            'v': 1, 'source': 'claude-hook', 'merged': False, 'correlate': 'tool_use_id',
            'available': available, 'fresh': bool(events), 'events': events,
        },
    }


class ActivityKeyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env = {key: os.environ.get(key) for key in ('HOME', 'USERPROFILE')}
        cls.tmp = tempfile.TemporaryDirectory(prefix='event-merge-key-')
        cls.board = load_board(Path(cls.tmp.name) / 'home')

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()

    def test_id_ignores_clock_name_and_fingerprint(self):
        left = self.board.activity_key('sess-0001', 'Bash', 'toolu_1', 1, 'aaa')
        right = self.board.activity_key('sess-0001', 'Read', 'toolu_1', 99, 'bbb')
        self.assertEqual(left, ('id', 'sess-0001', 'toolu_1'))
        self.assertEqual(left, right)

    def test_window_and_fingerprint_split_calls_without_an_id(self):
        same = self.board.activity_key('sess-0001', 'Read', '', 1.0, 'abc')
        nearby = self.board.activity_key('sess-0001', 'Read', '', 1.2, 'abc')
        other_fp = self.board.activity_key('sess-0001', 'Read', '', 1.0, 'def')
        later = self.board.activity_key('sess-0001', 'Read', '', 9.0, 'abc')
        self.assertEqual(same, nearby)
        self.assertNotEqual(same, other_fp)
        self.assertNotEqual(same[3], later[3])
        fp = self.board.tool_input_fingerprint({'command': SECRET})
        self.assertEqual(len(fp), 16)
        self.assertNotIn(SECRET, fp)
        self.assertNotIn(SECRET, str(self.board.activity_key('sess-0001', 'Bash', '', 3, fp)))
        self.assertEqual(self.board.tool_input_fingerprint({}), '')
        self.assertEqual(self.board.tool_input_fingerprint(None), '')


class MergeFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env = {key: os.environ.get(key) for key in ('HOME', 'USERPROFILE')}
        cls.tmp = tempfile.TemporaryDirectory(prefix='event-merge-fx-')
        cls.board = load_board(Path(cls.tmp.name) / 'home')

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()

    def merge(self, raw):
        return self.board.merge_hook_events(raw, raw.get('live'))

    def test_transcript_only_keeps_one_row_and_cost(self):
        raw = snapshot(
            [feed_row(10, 'Bash', 'toolu_1', 'Bash · echo synthetic-ok', fp='abc')],
            [], tool_ids=['toolu_1'],
        )
        out = self.merge(raw)
        self.assertEqual(len(out['feed']), 1)
        row = out['feed'][0]
        self.assertNotIn('source', row)
        self.assertNotIn('tool', row)
        self.assertNotIn('_inputFp', row)
        self.assertEqual(row['label'], 'Bash · echo synthetic-ok')
        self.assertEqual(row['toolId'], 'toolu_1')
        self.assertEqual(row['ts'], 10)
        self.assertEqual(out['members'][0]['toolCount'], 1)
        self.assertEqual(out['departments'][0]['tools'], 1)
        self.assertEqual(out['totals']['cost'], raw['totals']['cost'])
        self.assertEqual(out['members'][0]['cost'], raw['members'][0]['cost'])
        self.assertTrue(out['live']['merged'])
        self.assertNotIn('_activityIndex', out)
        self.assertNotIn('_inputFp', json.dumps(out))
        self.assertIn('_inputFp', raw['feed'][0])
        self.assertEqual(raw['members'][0]['toolCount'], 1)

    def test_hooks_off_does_not_join(self):
        raw = snapshot(
            [feed_row(10, 'Bash', 'toolu_1', 'Bash · echo synthetic-ok')],
            [hook('PreToolUse', 9, 'Bash', 'toolu_1')],
            available=False, tool_ids=['toolu_1'],
        )
        out = self.merge(raw)
        self.assertFalse(out['live']['merged'])
        self.assertEqual(len(out['feed']), 1)
        self.assertNotIn('source', out['feed'][0])
        self.assertEqual(out['members'][0]['toolCount'], 1)
        self.assertEqual(out['totals']['cost'], raw['totals']['cost'])

    def test_hook_then_transcript_is_one_earlier_row(self):
        events = [
            hook('PreToolUse', 12, 'Read', 'toolu_9', seq=1),
            hook('PostToolUse', 18, 'Read', 'toolu_9', seq=2),
        ]
        raw = snapshot(
            [feed_row(20, 'Read', 'toolu_9', 'Read · src/app.js')],
            events, tool_ids=['toolu_9'],
        )
        out = self.merge(raw)
        rows = [row for row in out['feed'] if row.get('toolId') == 'toolu_9']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source'], 'merged')
        self.assertEqual(rows[0]['ts'], 12)
        self.assertTrue(rows[0]['done'])
        self.assertEqual(rows[0]['label'], 'Read · src/app.js')
        self.assertEqual(out['members'][0]['toolCount'], 1)
        self.assertEqual(out['departments'][0]['tools'], 1)
        self.assertEqual(out['members'][0]['cost'], 1.25)
        self.assertEqual(out['lead']['cost'], 0.5)
        self.assertEqual(out['totals']['cost'], raw['totals']['cost'])
        self.assertEqual(len(out['live']['events']), 2)
        again = self.merge(raw)
        self.assertEqual(again['members'][0]['toolCount'], out['members'][0]['toolCount'])
        self.assertEqual([row['ts'] for row in again['feed']], [row['ts'] for row in out['feed']])

    def test_id_joins_outside_the_window(self):
        raw = snapshot(
            [feed_row(100, 'Bash', 'toolu_late', 'Bash · echo synthetic-ok')],
            [hook('PreToolUse', 1, 'Bash', 'toolu_late')],
            tool_ids=['toolu_late'],
        )
        out = self.merge(raw)
        self.assertEqual(len(out['feed']), 1)
        self.assertEqual(out['feed'][0]['source'], 'merged')
        self.assertEqual(out['feed'][0]['ts'], 1)
        self.assertEqual(out['members'][0]['toolCount'], 1)

    def test_hooks_only_pre_and_post_are_one_row(self):
        raw = snapshot([], [
            hook('PreToolUse', 5, 'Bash', 'toolu_h', seq=1),
            hook('PostToolUse', 8, 'Bash', 'toolu_h', seq=2),
            hook('Stop', 9, agent='', seq=3),
        ], tool_count=0, cost=0)
        out = self.merge(raw)
        tools = [row for row in out['feed'] if row.get('toolId') == 'toolu_h']
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]['source'], 'hook')
        self.assertEqual(tools[0]['ts'], 5)
        self.assertTrue(tools[0]['done'])
        self.assertEqual(tools[0]['label'], 'Bash')
        self.assertEqual(sum(1 for row in out['feed'] if row.get('hookEvent') == 'Stop'), 1)
        self.assertEqual(out['members'][0]['toolCount'], 1)
        self.assertEqual(out['departments'][0]['tools'], 1)
        self.assertEqual(out['totals']['cost'], 0.5)
        self.assertEqual(out['members'][0]['cost'], 0)
        self.assertEqual(out['members'][0]['current']['toolId'], 'toolu_h')
        self.assertTrue(out['members'][0]['current']['live'])
        self.assertNotIn(SECRET, json.dumps(out['feed']))

    def test_ambiguous_window_does_not_guess(self):
        raw = snapshot([
            feed_row(10, 'Read', 'toolu_a', 'Read · one'),
            feed_row(12, 'Read', 'toolu_b', 'Read · two'),
        ], [hook('PreToolUse', 11, 'Read')], tool_count=2, tool_ids=['toolu_a', 'toolu_b'])
        out = self.merge(raw)
        self.assertEqual(len(out['feed']), 2)
        self.assertTrue(all('source' not in row for row in out['feed']))
        self.assertEqual(out['members'][0]['toolCount'], 2)
        self.assertEqual(out['totals']['cost'], raw['totals']['cost'])

    def test_fingerprint_picks_one_row_in_the_window(self):
        raw = snapshot([
            feed_row(10, 'Read', 'toolu_a', 'Read · one', fp='aaa'),
            feed_row(12, 'Read', 'toolu_b', 'Read · two', fp='bbb'),
        ], [hook('PreToolUse', 11, 'Read', fp='bbb')], tool_count=2, tool_ids=['toolu_a', 'toolu_b'])
        out = self.merge(raw)
        self.assertEqual(len(out['feed']), 2)
        merged = [row for row in out['feed'] if row.get('source') == 'merged']
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['toolId'], 'toolu_b')
        self.assertEqual(merged[0]['ts'], 11)
        self.assertNotIn('_inputFp', json.dumps(out))
        self.assertEqual(out['members'][0]['toolCount'], 2)

    def test_name_mismatch_keeps_the_transcript_row(self):
        raw = snapshot(
            [feed_row(10, 'Read', 'toolu_x', 'Read · src/app.js')],
            [hook('PreToolUse', 9, 'Bash', 'toolu_x')],
            tool_ids=['toolu_x'],
        )
        out = self.merge(raw)
        self.assertEqual(len(out['feed']), 1)
        self.assertNotIn('source', out['feed'][0])
        self.assertEqual(out['feed'][0]['label'], 'Read · src/app.js')
        self.assertEqual(out['members'][0]['toolCount'], 1)

    def test_codex_feed_is_not_joined(self):
        raw = snapshot(
            [feed_row(10, 'Bash', 'call-1', 'Bash · echo synthetic-ok')],
            [hook('PreToolUse', 9, 'Bash', 'call-1')],
            source='codex', tool_ids=['call-1'],
        )
        out = self.merge(raw)
        self.assertFalse(out['live']['merged'])
        self.assertEqual(len(out['feed']), 1)
        self.assertNotIn('source', out['feed'][0])


def _write(path, entries):
    path.write_text('\n'.join(json.dumps(entry, ensure_ascii=False) for entry in entries) + '\n', encoding='utf-8')


class ParserMergeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='event-merge-parse-')
        cls.home = Path(cls.tmp.name) / 'home'
        cls.proj = cls.home / '.claude' / 'projects' / 'C--synthetic'
        cls.sub = cls.proj / 'sess-0001' / 'subagents'
        cls.sub.mkdir(parents=True)
        cls._env = {key: os.environ.get(key) for key in ('HOME', 'USERPROFILE')}
        cls.board = load_board(cls.home)
        cls.hook_ts = cls.board.parse_iso('2026-11-04T00:00:02Z')
        cls.tool_ts = '2026-11-04T00:00:20Z'

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()

    def setUp(self):
        self.board.HOOK_LOG.clear()
        self.board._agent_cache.clear()
        self.board._lead_cache.clear()
        self.board.Handler._cache = (0.0, None)

    def snap(self):
        return self.board.build_snapshot(self.board.argparse.Namespace(
            session='sess-0001', project=None, stale=600, source='claude'))

    def member(self, snap):
        return next(item for item in snap['members'] if item['id'] == 'a1')

    def write_member(self, with_tool):
        usage = {
            'input_tokens': 200, 'output_tokens': 80,
            'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0,
        }
        rows = [{
            'type': 'user', 'timestamp': '2026-11-04T00:00:01Z',
            'message': {'role': 'user', 'content': 'synthetic merge fixture'},
        }]
        if with_tool:
            rows.append({
                'type': 'assistant', 'timestamp': self.tool_ts,
                'message': {
                    'id': 'msg-synthetic-1', 'role': 'assistant', 'model': 'claude-sonnet-5',
                    'stop_reason': 'tool_use', 'usage': usage,
                    'content': [{
                        'type': 'tool_use', 'id': 'toolu_once', 'name': 'Bash',
                        'input': {'command': 'echo synthetic-ok'},
                    }],
                },
            })
        _write(self.sub / 'agent-a1.jsonl', rows)
        (self.sub / 'agent-a1.meta.json').write_text(
            json.dumps({'agentType': 'backend-dev', 'description': 'synthetic'}), encoding='utf-8')
        _write(self.proj / 'sess-0001.jsonl', [{
            'type': 'user', 'timestamp': '2026-11-04T00:00:01Z', 'cwd': '/tmp/synthetic-project',
            'message': {'role': 'user', 'content': 'synthetic lead'},
        }])

    def add_hooks(self):
        for event, seq in (('PreToolUse', 1), ('PostToolUse', 2)):
            self.board.HOOK_LOG.add({
                'v': 1, 'source': 'claude-hook', 'session_id': 'sess-0001', 'event': event,
                'tool_name': 'Bash', 'tool_use_id': 'toolu_once', 'agent_id': 'agent-a1',
                'agent_type': 'backend-dev', 'ts': self.hook_ts, 'seq': seq,
            })

    def test_transcript_and_hooks_count_the_call_once(self):
        self.write_member(True)
        before = self.snap()
        self.assertGreater(before['totals']['cost'], 0)
        self.assertEqual(self.member(before)['toolCount'], 1)
        self.assertEqual(sum(1 for row in before['feed'] if row.get('toolId') == 'toolu_once'), 1)
        self.assertNotIn('source', before['feed'][0])
        self.assertNotIn('_inputFp', json.dumps(before))
        self.add_hooks()
        after = self.snap()
        rows = [row for row in after['feed'] if row.get('toolId') == 'toolu_once']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source'], 'merged')
        self.assertEqual(rows[0]['ts'], self.hook_ts)
        self.assertLess(rows[0]['ts'], self.board.parse_iso(self.tool_ts))
        self.assertIn('echo synthetic-ok', rows[0]['label'])
        self.assertEqual(self.member(after)['toolCount'], 1)
        self.assertEqual(after['totals']['cost'], before['totals']['cost'])
        self.assertEqual(after['members'][0]['cost'], before['members'][0]['cost'])
        self.assertTrue(after['live']['merged'])
        self.assertNotIn(SECRET, json.dumps(after))
        self.assertNotIn('_activityIndex', after)

    def test_hook_shows_before_transcript_then_collapses(self):
        self.write_member(False)
        self.add_hooks()
        early = self.snap()
        early_rows = [row for row in early['feed'] if row.get('toolId') == 'toolu_once']
        self.assertEqual(len(early_rows), 1)
        self.assertEqual(early_rows[0]['source'], 'hook')
        self.assertEqual(early_rows[0]['ts'], self.hook_ts)
        self.assertEqual(self.member(early)['toolCount'], 1)
        early_cost = early['totals']['cost']
        self.write_member(True)
        later = self.snap()
        rows = [row for row in later['feed'] if row.get('toolId') == 'toolu_once']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source'], 'merged')
        self.assertEqual(rows[0]['ts'], self.hook_ts)
        self.assertEqual(self.member(later)['toolCount'], 1)
        self.assertGreater(later['totals']['cost'], early_cost or 0)
        plain = self.member(later)['cost']
        self.add_hooks()
        again = self.snap()
        self.assertEqual(again['totals']['cost'], later['totals']['cost'])
        self.assertEqual(self.member(again)['cost'], plain)
        self.assertEqual(sum(1 for row in again['feed'] if row.get('toolId') == 'toolu_once'), 1)


class FeedUiContractTest(unittest.TestCase):
    def test_merged_feed_is_not_rendered_twice(self):
        html = (ROOT / 'team-board' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('同一 tool_use_id 只留一条', html)
        self.assertIn('尚未与转录合并', html)
        self.assertIn("f.source === 'hook'", html)
        self.assertIn('snap.live.merged', html)


if __name__ == '__main__':
    unittest.main()
