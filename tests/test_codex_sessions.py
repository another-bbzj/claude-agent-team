"""Synthetic interactive rollouts: accounting, incremental reads and provenance."""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('codex_sessions', ROOT / 'team-board/codex_sessions.py')
codex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codex)


def event(kind, payload, second=0):
    return {'timestamp': '2026-01-01T00:00:{:02d}Z'.format(second), 'type': kind, 'payload': payload}


def meta(sid='demo-root', **kwargs):
    return event('session_meta', {'id': sid, 'cwd': 'demo-project', 'source': 'vscode', **kwargs})


def tokens(inputs, cached, outputs, kind='event_msg', **kwargs):
    value = {'input_tokens': inputs, 'cached_input_tokens': cached, 'output_tokens': outputs, **kwargs}
    if kind == 'event_msg':
        return event(kind, {'type': 'token_count', 'info': {'total_token_usage': value}})
    return event(kind, {'thread_token_usage': value})


class CodexSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        codex._cache.clear()
        codex._headers.clear()
        codex._index.clear()

    def write(self, name, events):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in events), encoding='utf-8')
        return path

    def append(self, path, events):
        with path.open('a', encoding='utf-8') as stream:
            stream.write(''.join(json.dumps(row) + '\n' for row in events))

    def parse(self, path=None):
        sessions = codex.discover_sessions(self.root)
        chosen = next((item for item in sessions if path is None or item['path'] == path), None)
        self.assertIsNotNone(chosen)
        return codex.parse_session(chosen, path.stat().st_mtime if path else chosen['mtime'], 600)

    def test_fixture_has_verified_children_and_unrelated_same_cwd(self):
        for path in (ROOT / 'examples/codex-sessions.synthetic').glob('*.jsonl'):
            shutil.copy2(str(path), str(self.root / path.name))
        sessions = codex.discover_sessions(self.root)
        self.assertEqual({item['session'] for item in sessions}, {'codex-demo-root', 'codex-demo-unrelated'})
        chosen = next(item for item in sessions if item['session'] == 'codex-demo-root')
        result = codex.parse_session(chosen, chosen['mtime'], 600)
        self.assertEqual({item['id'] for item in result['members']}, {'codex-demo-child', 'codex-demo-other-child'})
        self.assertEqual(result['lead']['usage'], {'in': 60, 'cache_read': 30, 'cache_write': 10, 'out': 20})
        self.assertEqual(result['lead']['model'], 'gpt-demo-code')
        self.assertEqual([row['text'] for row in result['conversation'] if row['agentId'] == '__lead'],
                         ['Summarize the synthetic project.', 'The synthetic summary is ready.'])
        self.assertTrue(all(member['model'].startswith('gpt-demo-') for member in result['members']))
        self.assertEqual(result['lead']['toolCount'], 2)
        self.assertTrue(result['lead']['timeline'][0]['done'])

    def test_usage_snapshots_replace_and_model_switch_is_measured(self):
        path = self.write('rollout.jsonl', [meta(), event('turn_context', {'model': 'model-a'}),
            tokens(100, 30, 20), tokens(100, 30, 20, 'token_usage_record'),
            tokens(250, 80, 50), tokens(250, 80, 50, 'token_usage_record'),
            event('turn_context', {'model': 'model-b'})])
        result = self.parse(path)
        self.assertEqual(result['lead']['usage'], {'in': 170, 'cache_read': 80, 'cache_write': 0, 'out': 50})
        self.assertEqual(result['lead']['model'], 'model-b')
        self.assertIsNone(result['lead']['cost'])

    def test_missing_and_invalid_usage_stays_unpriced(self):
        path = self.write('rollout.jsonl', [meta(), tokens(True, 0, 2), tokens(-1, 0, 2)])
        result = self.parse(path)
        self.assertEqual(result['lead']['model'], '')
        self.assertEqual(sum(result['lead']['usage'].values()), 0)
        self.assertIsNone(result['lead']['cost'])
        self.assertIn('Invalid token snapshot ignored.', result['warnings'])

    def test_reasoning_system_developer_and_tool_results_are_not_conversation(self):
        rows = [meta()]
        for role in ('system', 'developer'):
            rows.append(event('response_item', {'type': 'message', 'role': role, 'content': [{'type': 'input_text', 'text': 'not-visible'}]}))
        rows += [event('response_item', {'type': 'reasoning', 'encrypted_content': 'not-visible'}),
                 event('response_item', {'type': 'function_call_output', 'call_id': 'unmatched', 'output': 'not-visible'}),
                 event('event_msg', {'type': 'item_completed', 'item': {'type': 'Reasoning', 'id': 'r', 'raw_content': ['not-visible']}}),
                 event('response_item', {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'visible'}]})]
        result = self.parse(self.write('rollout.jsonl', rows))
        self.assertEqual([row['text'] for row in result['conversation']], ['visible'])
        self.assertNotIn('not-visible', json.dumps(result))

    def test_multi_representation_dedupe_preserves_same_user_text_on_later_turn(self):
        rows = [meta()]
        for second in (1, 3):
            rows += [event('event_msg', {'type': 'user_message', 'message': 'again'}, second),
                     event('response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'again'}]}, second),
                     event('event_msg', {'type': 'item_completed', 'item': {'type': 'UserMessage', 'id': 'u-' + str(second),
                                                                        'content': [{'type': 'text', 'text': 'again'}]}}, second)]
        result = self.parse(self.write('rollout.jsonl', rows))
        self.assertEqual([row['text'] for row in result['conversation']], ['again', 'again'])

    def test_duplicate_completed_item_id_does_not_inflate_tools(self):
        item = {'type': 'CommandExecution', 'id': 'cmd-1', 'command': ['python', '--version'], 'status': 'completed', 'exit_code': 0}
        path = self.write('rollout.jsonl', [meta(), event('event_msg', {'type': 'item_completed', 'item': item}),
                                          event('event_msg', {'type': 'item_completed', 'item': item})])
        result = self.parse(path)
        self.assertEqual(result['lead']['toolCount'], 1)
        self.assertEqual(result['lead']['timeline'][0]['label'], 'python --version')

    def test_repeated_text_on_new_turn_with_different_log_representation_is_retained(self):
        rows = [meta(), event('event_msg', {'type': 'task_started'}, 1),
                event('response_item', {'type': 'message', 'role': 'user', 'content': 'again'}, 1),
                event('event_msg', {'type': 'task_started'}, 8),
                event('event_msg', {'type': 'user_message', 'message': 'again'}, 8)]
        result = self.parse(self.write('rollout.jsonl', rows))
        self.assertEqual([row['text'] for row in result['conversation']], ['again', 'again'])

    def test_tool_start_result_pairing_uses_call_id(self):
        path = self.write('rollout.jsonl', [meta(),
            event('response_item', {'type': 'function_call', 'name': 'exec_command', 'call_id': 'call-1', 'arguments': '{"cmd":"echo synthetic"}'}, 1),
            event('response_item', {'type': 'function_call_output', 'call_id': 'call-1', 'output': 'not a visible message'}, 2)])
        row = self.parse(path)['lead']['timeline'][0]
        self.assertEqual(row['ms'], 1000)
        self.assertTrue(row['done'])

    def test_spawn_result_links_other_source_but_not_unrelated_session(self):
        self.write('child.jsonl', [meta('child-demo', source={'subagent': {'other': 'desktop'}})])
        self.write('other.jsonl', [meta('other-demo')])
        path = self.write('root.jsonl', [meta(),
            event('response_item', {'type': 'function_call', 'name': 'spawn_agent', 'call_id': 'call-spawn',
                                    'arguments': '{"agent_type":"researcher"}'}),
            event('response_item', {'type': 'function_call_output', 'call_id': 'call-spawn', 'output': '{"agent_id":"child-demo"}'})])
        result = self.parse(path)
        self.assertEqual([member['id'] for member in result['members']], ['child-demo'])
        self.assertEqual(result['members'][0]['agentType'], 'researcher')

    def test_unlinked_child_is_visible_with_warning_and_fork_is_not_parent(self):
        self.write('root.jsonl', [meta()])
        path = self.write('orphan.jsonl', [meta('orphan', source={'subagent': {'other': 'desktop'}}, forked_from_id='demo-root')])
        sessions = codex.discover_sessions(self.root)
        self.assertEqual(len(sessions), 2)
        result = self.parse(path)
        self.assertIn('Subagent parent was not verified', result['warnings'][0])
        self.assertEqual(result['members'], [])

    def test_cyclic_metadata_is_not_hidden(self):
        self.write('one.jsonl', [meta('one', parent_thread_id='two')])
        self.write('two.jsonl', [meta('two', parent_thread_id='one')])
        sessions = codex.discover_sessions(self.root)
        self.assertEqual({row['session'] for row in sessions}, {'one', 'two'})
        self.assertTrue(all('Cyclic' in row['meta']['warnings'][0] for row in sessions))

    def test_append_reads_only_new_bytes_and_returns_independent_copy(self):
        path = self.write('rollout.jsonl', [meta(), event('turn_context', {'model': 'model-a'}), tokens(100, 20, 5)])
        chosen = codex.discover_sessions(self.root)[0]
        first = codex.parse_session(chosen, path.stat().st_mtime, 600)
        first['lead']['usage']['in'] = 99999
        first['warnings'].append('injected')
        self.append(path, [tokens(140, 30, 8)])
        result = codex.parse_session(chosen, path.stat().st_mtime, 600)
        self.assertEqual(result['lead']['usage']['in'], 110)
        self.assertEqual(result['lead']['model'], 'model-a')
        self.assertNotIn('injected', result['warnings'])
        with mock.patch.object(Path, 'open', side_effect=AssertionError('unchanged log reopened')):
            again = codex.parse_session(chosen, path.stat().st_mtime, 600)
        self.assertEqual(again['lead']['usage'], result['lead']['usage'])

    def test_partial_append_waits_for_newline_and_malformed_line_is_bounded_warning(self):
        path = self.write('rollout.jsonl', [meta()])
        line = json.dumps(event('event_msg', {'type': 'user_message', 'message': 'complete after newline'})).encode()
        with path.open('ab') as stream:
            stream.write(b'{not-json}\n' + line)
        self.assertEqual(self.parse(path)['conversation'], [])
        with path.open('ab') as stream:
            stream.write(b'\n')
        result = self.parse(path)
        self.assertEqual(result['conversation'][0]['text'], 'complete after newline')
        self.assertEqual(result['warnings'].count('Malformed rollout line ignored.'), 1)

    def test_truncation_and_replacement_reset_old_state(self):
        path = self.write('rollout.jsonl', [meta(), event('turn_context', {'model': 'old-model'}), tokens(1000, 100, 100)])
        chosen = codex.discover_sessions(self.root)[0]
        codex.parse_session(chosen, path.stat().st_mtime, 600)
        path.write_text(json.dumps(meta()) + '\n', encoding='utf-8')
        result = codex.parse_session(chosen, path.stat().st_mtime, 600)
        self.assertEqual(result['lead']['model'], '')
        self.assertEqual(result['lead']['usage']['out'], 0)
        replacement = self.write('new.jsonl', [meta(), event('turn_context', {'model': 'new-model'}), tokens(12, 2, 1)])
        os.replace(str(replacement), str(path))
        result = codex.parse_session(chosen, path.stat().st_mtime, 600)
        self.assertEqual(result['lead']['model'], 'new-model')
        self.assertEqual(result['lead']['usage']['in'], 10)

    def test_bounded_tail_keeps_header_model_and_latest_cumulative_tokens(self):
        rows = [meta(), event('turn_context', {'model': 'header-model'})]
        rows += [event('response_item', {'type': 'reasoning', 'encrypted_content': 'x' * 100}) for _ in range(80)]
        rows += [tokens(900, 100, 30)]
        path = self.write('rollout.jsonl', rows)
        with mock.patch.object(codex, 'MAX_SCAN_BYTES', 1000), mock.patch.object(codex, 'MAX_HEADER_BYTES', 500):
            result = self.parse(path)
        self.assertEqual(result['lead']['model'], 'header-model')
        self.assertEqual(result['lead']['usage']['in'], 800)
        self.assertTrue(any('Older events' in warning for warning in result['warnings']))

    def test_discovery_limit_selects_newest_sessions(self):
        old = self.write('a-old.jsonl', [meta('old')])
        new = self.write('z-new.jsonl', [meta('new')])
        os.utime(str(old), (100, 100))
        os.utime(str(new), (200, 200))
        with mock.patch.object(codex, 'MAX_DISCOVER', 1):
            sessions = codex.discover_sessions(self.root)
        self.assertEqual(sessions[0]['session'], 'new')
        self.assertTrue(any('limit' in warning for warning in sessions[0]['meta']['warnings']))

    def test_native_fork_keeps_child_identity_and_omits_inherited_history(self):
        parent = meta('root')
        child = meta('child')
        child['payload'].update(parent_thread_id='root', agent_role='backend-dev',
                                subagent_history_start_ordinal=4)
        self.write('root.jsonl', [parent])
        path = self.write('child.jsonl', [child, parent,
            event('turn_context', {'model': 'parent-model'}),
            event('event_msg', {'type': 'user_message', 'message': 'inherited parent question'}),
            event('event_msg', {'type': 'task_started'}),
            event('turn_context', {'model': 'child-model'}),
            event('event_msg', {'type': 'agent_message', 'message': 'child-only result'}),
            tokens(30, 10, 5)])
        roots = codex.discover_sessions(self.root)
        self.assertEqual([item['session'] for item in roots], ['root'])
        result = codex.parse_session(roots[0], path.stat().st_mtime, 600)
        self.assertEqual(result['members'][0]['id'], 'child')
        self.assertEqual(result['members'][0]['model'], 'child-model')
        self.assertNotIn('inherited parent question', json.dumps(result['conversation']))


if __name__ == '__main__':
    unittest.main()
