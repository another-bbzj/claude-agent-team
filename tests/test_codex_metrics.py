"""Offline exec-event metrics, lifecycle accounting and privacy regressions."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('codex_metrics_tested', ROOT / 'tools' / 'codex_metrics.py')
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


def capture(*events):
    return io.BytesIO(('\n'.join(json.dumps(event) for event in events) + '\n').encode('utf-8'))


THREAD = {'type': 'thread.started', 'thread_id': 'synthetic-thread'}
START = {'type': 'turn.started'}
COMPLETE = {'type': 'turn.completed', 'usage': {
    'input_tokens': 100, 'cached_input_tokens': 60, 'output_tokens': 25,
}}


class LifecycleTest(unittest.TestCase):
    def test_fixture_and_equal_usage_in_distinct_turns_are_counted(self):
        with (ROOT / 'examples' / 'codex-exec.synthetic.jsonl').open('rb') as stream:
            report = metrics.summarize(stream)
        self.assertEqual(report['counts']['threads'], 1)
        self.assertEqual(report['counts']['turnsCompleted'], 2)
        self.assertEqual(report['counts']['toolCallsCompleted'], 2)
        self.assertEqual(report['counts']['itemsCompleted'], 3)
        self.assertEqual(report['usage'], {
            'inputTokens': 200, 'cachedInputTokens': 120, 'outputTokens': 50,
            'totalTokens': 250, 'reasoningOutputTokens': 5, 'reasoningMeasuredTurns': 1,
        })

    def test_duplicate_terminal_is_rejected_without_double_counting(self):
        with self.assertRaisesRegex(metrics.MetricsError, 'line 4: terminal'):
            metrics.summarize(capture(THREAD, START, COMPLETE, COMPLETE))

    def test_item_completion_replay_is_deduped_per_turn_only(self):
        item = {'type': 'item.completed', 'item': {'id': 'one', 'type': 'command_execution', 'status': 'failed'}}
        report = metrics.summarize(capture(THREAD, START, item, item, COMPLETE, START, item, COMPLETE))
        self.assertEqual(report['counts']['itemsCompleted'], 2)
        self.assertEqual(report['counts']['toolCallsCompleted'], 2)
        self.assertEqual(report['counts']['toolCallsFailed'], 2)

    def test_item_id_cannot_change_type_after_completion(self):
        first = {'type': 'item.completed', 'item': {'id': 'one', 'type': 'agent_message'}}
        second = {'type': 'item.completed', 'item': {'id': 'one', 'type': 'reasoning'}}
        with self.assertRaisesRegex(metrics.MetricsError, 'changed type'):
            metrics.summarize(capture(THREAD, START, first, second))

    def test_failed_interrupted_and_fatal_error_captures_are_explicit(self):
        report = metrics.summarize(capture(THREAD, START, {'type': 'turn.failed', 'error': {'message': 'PRIVATE'}},
                                           START, {'type': 'error', 'message': 'PRIVATE'}))
        self.assertEqual(report['counts']['turnsFailed'], 1)
        self.assertEqual(report['counts']['turnsIncomplete'], 1)
        self.assertEqual(report['counts']['errors'], 1)
        self.assertEqual(report['usage']['totalTokens'], 0)
        self.assertEqual(metrics.summarize(capture({'type': 'error'}))['counts']['errors'], 1)

    def test_invalid_lifecycle_and_multiple_threads_are_rejected(self):
        for events in ((START,), (THREAD, START, START), (THREAD, COMPLETE), (THREAD, THREAD),
                       (THREAD, {'type': 'item.completed', 'item': {'id': 'a', 'type': 'agent_message'}})):
            with self.subTest(events=events), self.assertRaises(metrics.MetricsError):
                metrics.summarize(capture(*events))

    def test_private_and_unknown_values_never_escape(self):
        thread = dict(THREAD, thread_id='PRIVATE_THREAD', cwd='PRIVATE_PATH')
        item = {'type': 'item.completed', 'item': {
            'id': 'PRIVATE_ITEM', 'type': 'PRIVATE_NEW_TOOL', 'text': 'PRIVATE_TEXT',
            'command': 'PRIVATE_COMMAND', 'aggregated_output': 'PRIVATE_OUTPUT',
        }}
        report = metrics.summarize(capture(thread, START, item, {'type': 'PRIVATE_EVENT', 'data': 'PRIVATE_DATA'}, COMPLETE))
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertEqual(report['counts']['unknownEvents'], 1)
        self.assertEqual(report['counts']['unknownItems'], 1)
        self.assertEqual(set(report['itemTypes']), set(metrics.ITEM_TYPES))
        self.assertNotIn('reasoningOutputTokens', report['usage'])

    def test_wire_todo_list_and_error_item_types_are_recognized(self):
        report = metrics.summarize(capture(
            THREAD, START,
            {'type': 'item.completed', 'item': {'id': 'plan', 'type': 'todo_list', 'items': [{'text': 'PRIVATE', 'completed': True}]}},
            {'type': 'item.completed', 'item': {'id': 'warning', 'type': 'error', 'message': 'PRIVATE'}},
            COMPLETE))
        self.assertEqual(report['itemTypes']['todo_list'], 1)
        self.assertEqual(report['itemTypes']['error'], 1)
        self.assertEqual(report['counts']['unknownItems'], 0)
        self.assertEqual(report['counts']['errors'], 0, 'top-level error events are a distinct count')
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_usage_must_be_nonnegative_integer_and_cached_subset(self):
        for field in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens'):
            for value in (True, -1, 1.5, None, 'PRIVATE'):
                terminal = copy.deepcopy(COMPLETE)
                terminal['usage'][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(metrics.MetricsError) as raised:
                    metrics.summarize(capture(THREAD, START, terminal))
                self.assertNotIn('PRIVATE', str(raised.exception))
        terminal = copy.deepcopy(COMPLETE)
        terminal['usage']['cached_input_tokens'] = 101
        with self.assertRaisesRegex(metrics.MetricsError, 'cached input'):
            metrics.summarize(capture(THREAD, START, terminal))

    def test_wrong_shape_rollout_and_malformed_json_fail_safely(self):
        for data in (b'', b'\n', b'null\n', b'[]\n', b'{}\n', b'{"type":"PRIVATE"}\n',
                     b'{"type":"session_meta","payload":{}}\n', b'{"PRIVATE":\n', b'\xff\n',
                     b'{"type":"error","private":NaN}\n'):
            with self.subTest(data=data), self.assertRaises(metrics.MetricsError) as raised:
                metrics.summarize(io.BytesIO(data))
            self.assertNotIn('PRIVATE', str(raised.exception))

    def test_total_and_line_limits_are_enforced(self):
        with mock.patch.object(metrics, 'MAX_BYTES', 1):
            with self.assertRaisesRegex(metrics.MetricsError, 'size limit'):
                metrics.summarize(capture(THREAD))
        with mock.patch.object(metrics, 'MAX_LINE_BYTES', 1):
            with self.assertRaisesRegex(metrics.MetricsError, 'size limit'):
                metrics.summarize(capture(THREAD))


class CliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='codex-metrics-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'capture.jsonl'
        self.source.write_bytes((ROOT / 'examples' / 'codex-exec.synthetic.jsonl').read_bytes())

    def test_cli_stdout_and_atomic_file_match(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(metrics.main(['--input', str(self.source)]), 0)
        target = self.root / 'metrics.json'
        self.assertEqual(metrics.main(['--input', str(self.source), '--output', str(target)]), 0)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), json.loads(output.getvalue()))
        self.assertEqual(list(self.root.glob('.codex-metrics-*')), [])

    def test_invalid_capture_does_not_replace_existing_output(self):
        self.source.write_bytes(b'PRIVATE_INVALID_JSON')
        output = self.root / 'metrics.json'
        output.write_text('keep', encoding='utf-8')
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(metrics.main(['--input', str(self.source), '--output', str(output)]), 1)
        self.assertEqual(output.read_text(encoding='utf-8'), 'keep')
        self.assertNotIn('PRIVATE', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())

    def test_same_path_and_hardlink_alias_are_not_overwritten(self):
        original = self.source.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(metrics.main(['--input', str(self.source), '--output', str(self.source)]), 1)
        alias = self.root / 'alias.json'
        try:
            os.link(str(self.source), str(alias))
        except OSError:
            self.skipTest('hardlinks unavailable in this test environment')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(metrics.main(['--input', str(self.source), '--output', str(alias)]), 1)
        self.assertEqual(self.source.read_bytes(), original)

    def test_write_failure_is_sanitized_and_existing_output_survives(self):
        target = self.root / 'metrics.json'
        target.write_text('keep', encoding='utf-8')
        with mock.patch.object(metrics.os, 'replace', side_effect=OSError('PRIVATE_PATH')), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(metrics.main(['--input', str(self.source), '--output', str(target)]), 1)
        self.assertEqual(target.read_text(encoding='utf-8'), 'keep')
        self.assertNotIn('PRIVATE', error.getvalue())
        self.assertEqual(list(self.root.glob('.codex-metrics-*')), [])

    def test_cleanup_failure_cannot_replace_safe_write_or_replace_error(self):
        target = self.root / 'metrics.json'
        factory = metrics.tempfile.NamedTemporaryFile

        @contextlib.contextmanager
        def failed_write(*args, **kwargs):
            with factory(*args, **kwargs) as handle:
                with mock.patch.object(handle, 'write', side_effect=PermissionError('PRIVATE_WRITE')):
                    yield handle

        for stage in ('write', 'replace'):
            with self.subTest(stage=stage):
                target.write_text('keep', encoding='utf-8')
                temporary_factory = failed_write if stage == 'write' else factory
                with mock.patch.object(metrics.tempfile, 'NamedTemporaryFile', side_effect=temporary_factory), \
                        mock.patch.object(metrics.os, 'replace', side_effect=PermissionError('PRIVATE_REPLACE')), \
                        mock.patch.object(metrics.Path, 'unlink', side_effect=PermissionError('PRIVATE_CLEANUP')), \
                        contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(metrics.main(['--input', str(self.source), '--output', str(target)]), 1)
                self.assertEqual(error.getvalue(),
                                 'metrics failed: cannot write output; check directory and permissions\n')
                self.assertEqual(target.read_text(encoding='utf-8'), 'keep')
                leftovers = list(self.root.glob('.codex-metrics-*'))
                self.assertEqual(len(leftovers), 1, 'failed cleanup may leave one aggregate-only temporary file')
                leftovers[0].unlink()

    def test_missing_file_has_actionable_safe_error(self):
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(metrics.main(['--input', str(self.root / 'PRIVATE_MISSING')]), 1)
        self.assertIn('cannot read', error.getvalue())
        self.assertNotIn('PRIVATE', error.getvalue())


if __name__ == '__main__':
    unittest.main()
