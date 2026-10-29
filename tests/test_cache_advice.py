"""缓存诊断：只在转录信号够、并且价格表能算出正的差价时给建议。"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cache_advice_under_test', ROOT / 'team-board' / 'cache_advice.py')
advice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(advice)

SONNET = {
    'in': 3,
    'out': 15,
    'cache_read': 0.3,
    'cache_write': 3.75,
}
OPUS = {
    'in': 5,
    'out': 25,
    'cache_read': 0.5,
    'cache_write': 6.25,
}
PRICES = {'sonnet': SONNET, 'opus': OPUS}
SECRET = (
    'do-not-leak-system-prompt',
    '/home/secret/tool',
    'person@example.com',
)


def turn(**kwargs):
    base = {
        'actor': 'a1',
        'name': '阿服',
        'subagent': True,
        'ts': 1_700_000_000,
        'model': 'claude-sonnet-4-5',
        'cache_read': 0,
        'cache_write': 5000,
        'input': 200,
        'output': 20,
        'system': 'synthetic-system-stable',
        'tools': ['Read', 'Bash'],
    }
    base.update(kwargs)
    return base


def cold_series(count, gap=20, **kwargs):
    rows = []
    for index in range(count):
        rows.append(turn(ts=1_700_000_000 + index * gap, **kwargs))
    return rows


class ConfigTest(unittest.TestCase):
    def test_missing_block_uses_defaults_and_false_turns_it_off(self):
        config, error = advice.normalize_config(None)
        self.assertEqual(error, '')
        self.assertTrue(config['enabled'])
        self.assertEqual(config['hitRatio'], 0.5)
        self.assertEqual(config['ttlSeconds'], 300)
        off, error = advice.normalize_config(False)
        self.assertEqual(error, '')
        self.assertFalse(off['enabled'])
        disabled, error = advice.normalize_config({'enabled': False, 'hitRatio': 0.2})
        self.assertFalse(disabled['enabled'])
        self.assertEqual(advice.diagnose([], PRICES, False)['status'], 'off')
        self.assertEqual(advice.diagnose([], PRICES, False)['suggestions'], [])

    def test_invalid_threshold_does_not_invent_a_cutoff(self):
        config, error = advice.normalize_config({'hitRatio': 2})
        self.assertIsNone(config)
        self.assertIn('hitRatio', error)
        result = advice.diagnose(cold_series(4), PRICES, {'hitRatio': '0.2'})
        self.assertEqual(result['status'], 'invalid')
        self.assertEqual(result['suggestions'], [])
        self.assertIn('hitRatio', result['error'])

    def test_partial_override_keeps_the_other_defaults(self):
        config, error = advice.normalize_config({'ttlSeconds': 3600, 'hitRatio': 0.2})
        self.assertEqual(error, '')
        self.assertEqual(config['ttlSeconds'], 3600)
        self.assertEqual(config['hitRatio'], 0.2)
        self.assertEqual(config['minRequests'], advice.DEFAULT_MIN_REQUESTS)


class SignalTest(unittest.TestCase):
    def test_repeated_cold_writes_quote_tokens_and_the_price_row(self):
        result = advice.diagnose(cold_series(4, cache_write=40000, input=1200), PRICES)
        self.assertEqual(result['status'], 'advice')
        self.assertEqual([item['id'] for item in result['suggestions']], ['cache-rebuild'])
        item = result['suggestions'][0]
        # 三次重复写入，第一次冷启动不算进可省的部分。
        self.assertEqual(item['savingsUsd'], round(120000 * (3.75 - 0.3) / 1e6, 6))
        self.assertIn('120,000', item['action'])
        self.assertIn('sonnet', item['action'])
        self.assertIn('3.75', item['action'])
        self.assertIn('0.30', item['action'])
        self.assertIn('估算', item['action'])
        self.assertNotIn(SECRET[0], item['action'])
        self.assertTrue(result['note'])

    def test_high_hit_rate_and_short_context_stay_quiet(self):
        healthy = [
            turn(ts=1, cache_read=0, cache_write=80000, input=1000),
            turn(ts=30, cache_read=80000, cache_write=0, input=200),
            turn(ts=900, cache_read=70000, cache_write=1000, input=200),
        ]
        self.assertEqual(advice.diagnose(healthy, PRICES)['suggestions'], [])
        tiny = cold_series(4, cache_write=10, input=1)
        self.assertEqual(advice.diagnose(tiny, PRICES)['suggestions'], [])

    def test_two_requests_are_not_enough_to_call_it_a_rebuild(self):
        self.assertEqual(advice.diagnose(cold_series(2), PRICES)['suggestions'], [])

    def test_ttl_gap_is_quoted_and_a_hit_after_the_gap_is_not(self):
        rows = [
            turn(ts=1_000, cache_read=0, cache_write=80000, input=2000),
            turn(ts=1_060, cache_read=80000, cache_write=0, input=400),
            turn(ts=1_900, cache_read=0, cache_write=80000, input=2000),
            turn(ts=1_960, cache_read=80000, cache_write=0, input=400),
        ]
        result = advice.diagnose(rows, PRICES)
        self.assertEqual([item['id'] for item in result['suggestions']], ['cache-ttl'])
        item = result['suggestions'][0]
        self.assertEqual(item['savingsUsd'], round(80000 * (3.75 - 0.3) / 1e6, 6))
        self.assertIn('840', item['action'])
        self.assertIn('300', item['action'])
        self.assertIn('80,000', item['action'])
        still_hot = [
            turn(ts=1_000, cache_read=0, cache_write=20000, input=500),
            turn(ts=1_900, cache_read=20000, cache_write=0, input=100),
        ]
        self.assertEqual(advice.diagnose(still_hot, PRICES)['suggestions'], [])

    def test_missing_timestamps_do_not_invent_a_ttl_miss(self):
        rows = cold_series(4)
        for row in rows:
            row['ts'] = None
        result = advice.diagnose(rows, PRICES)
        self.assertEqual([item['id'] for item in result['suggestions']], ['cache-rebuild'])
        self.assertIn('时间戳不全', result['suggestions'][0]['action'])
        self.assertNotIn('间隔超过缓存 TTL', result['suggestions'][0]['action'])

    def test_system_and_tool_changes_need_the_field_and_a_following_miss(self):
        changed = [
            turn(ts=1, system='synthetic-prefix-a', tools=['Read']),
            turn(ts=20, system='synthetic-prefix-b', tools=['Read']),
            turn(ts=40, system='synthetic-prefix-b', tools=['Read', 'WebSearch']),
        ]
        ids = [item['id'] for item in advice.diagnose(changed, PRICES)['suggestions']]
        self.assertEqual(ids, ['cache-system', 'cache-tools'])
        text = ' '.join(item['action'] for item in advice.diagnose(changed, PRICES)['suggestions'])
        self.assertNotIn('synthetic-prefix-a', text)
        self.assertIn(advice.fingerprint('synthetic-prefix-a'), text)
        self.assertIn('Read → Read、WebSearch', text)
        hidden = [
            turn(ts=1, system=None, tools=None),
            turn(ts=20, system=None, tools=None),
        ]
        self.assertEqual(advice.diagnose(hidden, PRICES)['suggestions'], [])
        still_read = [
            turn(ts=1, system='synthetic-prefix-a', cache_write=80000),
            turn(ts=20, system='synthetic-prefix-b', cache_read=80000, cache_write=0),
        ]
        self.assertFalse(any(item['id'] == 'cache-system' for item in advice.diagnose(still_read, PRICES)['suggestions']))

    def test_tool_names_that_look_like_paths_are_not_copied_out(self):
        rows = [
            turn(ts=1, tools=['Read']),
            turn(ts=20, tools=['/home/secret/tool']),
        ]
        text = advice.diagnose(rows, PRICES)['suggestions'][0]['action']
        self.assertNotIn('/home/secret', text)
        self.assertNotIn('secret', text)
        self.assertIn('指纹', text)

    def test_frequent_model_switches_are_priced_per_request(self):
        rows = [
            turn(ts=1, model='claude-sonnet-4-5', cache_write=50000, input=1500),
            turn(ts=30, model='claude-opus-4-5', cache_write=50000, input=1500),
            turn(ts=60, model='claude-sonnet-4-5', cache_write=50000, input=1500),
        ]
        result = advice.diagnose(rows, PRICES)
        self.assertEqual([item['id'] for item in result['suggestions']], ['cache-model'])
        expected = 50000 * (6.25 - 0.5) / 1e6 + 50000 * (3.75 - 0.3) / 1e6
        self.assertEqual(result['suggestions'][0]['savingsUsd'], round(expected, 6))
        self.assertIn('claude-sonnet-4-5 → claude-opus-4-5 → claude-sonnet-4-5', result['suggestions'][0]['action'])
        once = rows[:2]
        self.assertEqual(advice.diagnose(once, PRICES)['suggestions'], [])

    def test_one_switch_with_a_long_gap_stays_on_the_ttl_rule(self):
        rows = [
            turn(ts=1_000, model='claude-sonnet-4-5', cache_write=10000, input=100),
            turn(ts=2_000, model='claude-opus-4-5', cache_write=10000, input=100),
        ]
        result = advice.diagnose(rows, PRICES)
        self.assertEqual([item['id'] for item in result['suggestions']], ['cache-ttl'])
        self.assertEqual(result['suggestions'][0]['savingsUsd'], round(10000 * (6.25 - 0.5) / 1e6, 6))

    def test_model_switch_claims_the_token_before_ttl_can(self):
        rows = [
            turn(ts=1_000, model='claude-sonnet-4-5', cache_write=10000, input=400),
            turn(ts=2_000, model='claude-opus-4-5', cache_write=10000, input=400),
            turn(ts=3_000, model='claude-sonnet-4-5', cache_write=10000, input=400),
        ]
        result = advice.diagnose(rows, PRICES)
        self.assertEqual([item['id'] for item in result['suggestions']], ['cache-model'])

    def test_unpriced_model_does_not_get_a_made_up_dollar_figure(self):
        rows = cold_series(4, model='totally-unknown-model')
        self.assertEqual(advice.diagnose(rows, PRICES)['suggestions'], [])

    def test_subagent_cold_starts_use_a_shared_prefix_when_it_was_observed(self):
        rows = [
            turn(actor='s1', name='甲', ts=10, cache_write=10000, input=100),
            turn(actor='s2', name='乙', ts=20, cache_write=10000, input=100),
        ]
        result = advice.diagnose(rows, PRICES)
        self.assertEqual([item['id'] for item in result['suggestions']], ['cache-subagent'])
        self.assertEqual(result['suggestions'][0]['savingsUsd'], round(10000 * (3.75 - 0.3) / 1e6, 6))
        self.assertIn('甲', result['suggestions'][0]['action'])
        self.assertIn('乙', result['suggestions'][0]['action'])
        self.assertNotIn('synthetic-system-stable', result['suggestions'][0]['action'])

    def test_subagent_without_a_prefix_only_prices_the_write_premium(self):
        rows = [
            turn(actor='s1', name='甲', ts=10, system=None, tools=None, cache_write=10000, input=100),
            turn(actor='s2', name='乙', ts=20, system=None, tools=None, cache_write=10000, input=100),
        ]
        result = advice.diagnose(rows, PRICES)
        self.assertEqual(result['suggestions'][0]['savingsUsd'], round(20000 * (3.75 - 3) / 1e6, 6))
        self.assertIn('写入价减去输入价', result['suggestions'][0]['action'])
        flat = {'sonnet': {'in': 3, 'out': 15, 'cache_read': 0.3, 'cache_write': 3}}
        self.assertEqual(advice.diagnose(rows, flat)['suggestions'], [])

    def test_diagnose_does_not_mutate_the_caller_list(self):
        rows = cold_series(4)
        before = json.dumps(rows, sort_keys=True)
        advice.diagnose(rows, PRICES)
        self.assertEqual(json.dumps(rows, sort_keys=True), before)


class TranscriptTest(unittest.TestCase):
    def test_last_usage_snapshot_wins_and_system_text_stays_hashed(self):
        secret = SECRET[0]
        entries = [
            {'type': 'user', 'timestamp': '2026-01-01T00:00:00Z', 'system': secret, 'tools': ['Read', 'Bash'],
             'message': {'role': 'user', 'content': 'synthetic'}},
            {'type': 'assistant', 'timestamp': '2026-01-01T00:00:01Z', 'message': {
                'id': 'm1', 'model': 'claude-sonnet-4-5',
                'usage': {'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 10}}},
            {'type': 'assistant', 'timestamp': '2026-01-01T00:00:02Z', 'message': {
                'id': 'm1', 'model': 'claude-sonnet-4-5',
                'usage': {'input_tokens': 4, 'output_tokens': 5, 'cache_read_input_tokens': 6, 'cache_creation_input_tokens': 7}}},
            {'type': 'assistant', 'timestamp': '2026-01-01T00:00:03Z', 'isSidechain': True, 'message': {
                'id': 'side', 'model': 'claude-sonnet-4-5',
                'usage': {'input_tokens': 9, 'output_tokens': 9, 'cache_read_input_tokens': 9, 'cache_creation_input_tokens': 9}}},
        ]
        turns = advice.turns_from_entries(entries, 'a1', '阿服', subagent=True, skip_sidechain=True)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]['input'], 4)
        self.assertEqual(turns[0]['cache_write'], 7)
        self.assertEqual(turns[0]['system'], advice.fingerprint(secret))
        self.assertEqual(turns[0]['tools'], ['Read', 'Bash'])
        self.assertNotIn(secret, json.dumps(turns))
        kept = advice.turns_from_entries(entries, 'a1', '阿服', skip_sidechain=False)
        self.assertEqual(len(kept), 2)

    def test_a_bad_usage_line_does_not_replace_the_previous_snapshot(self):
        entries = [
            {'type': 'assistant', 'message': {'id': 'm1', 'model': 'claude-sonnet-4-5', 'usage': {
                'input_tokens': 3, 'output_tokens': 4, 'cache_read_input_tokens': 5, 'cache_creation_input_tokens': 6}}},
            {'type': 'assistant', 'message': {'id': 'm1', 'model': 'claude-sonnet-4-5', 'usage': {
                'input_tokens': True, 'output_tokens': 1, 'cache_read_input_tokens': 1, 'cache_creation_input_tokens': 1}}},
        ]
        turns = advice.turns_from_entries(entries, 'a1', '阿服')
        self.assertEqual(turns[0]['input'], 3)
        self.assertEqual(turns[0]['cache_read'], 5)


class DemoAndPageTest(unittest.TestCase):
    def test_synthetic_demo_has_at_least_two_priced_suggestions(self):
        spec = importlib.util.spec_from_file_location('demo_cache_advice_test', ROOT / 'tools' / 'demo_board.py')
        demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo)
        source = json.loads(demo.FIXTURE.read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(source['cacheTurns']), 8)
        blob = json.dumps(source['cacheTurns'], ensure_ascii=False)
        for needle in ('@', 'sk-', '/home/', '/Users/', 'ghp_', 'api_key'):
            self.assertNotIn(needle, blob)
        snapshot = demo.snapshot_at(source, source['generatedAt'])
        self.assertNotIn('cacheTurns', snapshot)
        suggestions = snapshot['cacheAdvice']['suggestions']
        self.assertGreaterEqual(len(suggestions), 2)
        ids = [item['id'] for item in suggestions]
        self.assertIn('cache-ttl', ids)
        self.assertIn('cache-model', ids)
        self.assertIn('cache-rebuild', ids)
        for item in suggestions:
            self.assertGreater(item['savingsUsd'], 0)
            self.assertIn('估算', item['action'])
            self.assertTrue(any(ch.isdigit() for ch in item['action']))
        html = (ROOT / 'team-board' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="cache-advice"', html)
        self.assertIn('function renderCacheAdvice', html)
        self.assertIn('cache-advice:start', html)
        self.assertLess(html.find('id="bars"'), html.find('id="cache-advice"'))

    def test_merge_keeps_a_user_cache_block_and_ships_a_default(self):
        import install
        src = ROOT / 'team-board' / 'team.json'
        shipped = json.loads(src.read_text(encoding='utf-8'))
        self.assertEqual(shipped['cacheAdvice']['hitRatio'], 0.5)
        self.assertEqual(shipped['cacheAdvice']['ttlSeconds'], 300)
        tmp = Path(tempfile.mkdtemp(prefix='cache-advice-merge-'))
        try:
            dst = tmp / 'team.json'
            old = json.loads(src.read_text(encoding='utf-8'))
            old['cacheAdvice'] = {'hitRatio': 0.2, 'ttlSeconds': 3600}
            dst.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')
            install.merge_team_json(src, dst, {'_team_json_pricing': shipped['pricing_usd_per_mtok']}, {})
            merged = json.loads(dst.read_text(encoding='utf-8'))
            self.assertEqual(merged['cacheAdvice']['hitRatio'], 0.2)
            self.assertEqual(merged['cacheAdvice']['ttlSeconds'], 3600)
            plain = tmp / 'plain.json'
            plain.write_text(json.dumps({'departments': old['departments']}, ensure_ascii=False), encoding='utf-8')
            install.merge_team_json(src, plain, {'_team_json_pricing': shipped['pricing_usd_per_mtok']}, {})
            self.assertEqual(json.loads(plain.read_text(encoding='utf-8'))['cacheAdvice']['hitRatio'], 0.5)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BoardWiringTest(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get('HOME')
        self._profile = os.environ.get('USERPROFILE')
        self.tmp = Path(tempfile.mkdtemp(prefix='cache-advice-board-'))
        self.home = self.tmp / 'home'
        board = self.home / '.claude' / 'team-board'
        shutil.copytree(ROOT / 'team-board', board, ignore=shutil.ignore_patterns('bus', 'backdrops', '__pycache__'))
        os.environ['HOME'] = str(self.home)
        os.environ['USERPROFILE'] = str(self.home)
        os.environ['CODEX_HOME'] = str(self.tmp / 'codex')
        os.environ['PETDEX_HOME'] = str(self.tmp / 'petdex')
        spec = importlib.util.spec_from_file_location('team_board_cache_advice', board / 'team_board.py')
        self.tb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tb)

    def tearDown(self):
        if self._home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self._home
        if self._profile is None:
            os.environ.pop('USERPROFILE', None)
        else:
            os.environ['USERPROFILE'] = self._profile
        shutil.rmtree(self.tmp, ignore_errors=True)

    def snap(self):
        return self.tb.build_snapshot(Namespace(session=None, project=None, stale=600))

    def write_session(self, entries):
        sub = self.home / '.claude' / 'projects' / 'C--demo' / 'sess-cache' / 'subagents'
        sub.mkdir(parents=True)
        lines = ''.join(json.dumps(entry, ensure_ascii=False) + '\n' for entry in entries)
        (sub / 'agent-a1.jsonl').write_text(lines, encoding='utf-8')
        (sub / 'agent-a1.meta.json').write_text(json.dumps({'agentType': 'backend-dev', 'description': '合成缓存'}), encoding='utf-8')

    def test_low_hit_transcript_becomes_one_rebuild_suggestion(self):
        secret = SECRET[0]
        entries = [{'type': 'user', 'timestamp': '2026-01-01T00:00:00Z', 'system': secret,
                    'message': {'role': 'user', 'content': 'synthetic task'}}]
        for index in range(4):
            entries.append({'type': 'assistant', 'timestamp': '2026-01-01T00:00:%02dZ' % (10 + index * 10),
                            'message': {'id': 'm{}'.format(index), 'model': 'claude-sonnet-4-5', 'stop_reason': 'end_turn',
                                        'content': [{'type': 'text', 'text': 'synthetic'}],
                                        'usage': {'input_tokens': 100, 'output_tokens': 20,
                                                  'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 5000}}})
        self.write_session(entries)
        snapshot = self.snap()
        self.assertNotIn('cacheTurns', snapshot['members'][0])
        self.assertNotIn(secret, json.dumps(snapshot['cacheAdvice'], ensure_ascii=False))
        suggestions = snapshot['cacheAdvice']['suggestions']
        self.assertEqual([item['id'] for item in suggestions], ['cache-rebuild'])
        self.assertEqual(suggestions[0]['name'], '阿服')
        self.assertEqual(suggestions[0]['savingsUsd'], round(15000 * (3.75 - 0.3) / 1e6, 6))
        self.assertIn('15,000', suggestions[0]['action'])

    def test_shipped_high_hit_sample_stays_quiet_and_usage_matches(self):
        import sys
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        from test_board import BoardEnv
        env = BoardEnv()
        home, profile = os.environ.get('HOME'), os.environ.get('USERPROFILE')
        try:
            board = env.load()
            snapshot = board.build_snapshot(Namespace(session=None, project=None, stale=600))
            self.assertEqual(snapshot['cacheAdvice']['suggestions'], [])
            self.assertNotIn('cacheTurns', snapshot['members'][0])
            sub = env.claude / 'projects' / 'C--demo' / 'sess-0001' / 'subagents' / 'agent-a1.jsonl'
            turns = board._cache_advice.turns_from_entries(board.read_jsonl(sub), 'a1', 'a1', True)
            member = next(item for item in snapshot['members'] if item['id'] == 'a1')
            self.assertEqual(sum(item['cache_read'] for item in turns), member['usage']['cache_read'])
            self.assertEqual(sum(item['cache_write'] for item in turns), member['usage']['cache_write'])
            self.assertEqual(sum(item['input'] for item in turns), member['usage']['in'])
            self.assertEqual(sum(item['output'] for item in turns), member['usage']['out'])
        finally:
            env.cleanup()
            if home is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = home
            if profile is None:
                os.environ.pop('USERPROFILE', None)
            else:
                os.environ['USERPROFILE'] = profile


if __name__ == '__main__':
    unittest.main()
