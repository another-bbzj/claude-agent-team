"""解析基准：合成语料确定、结果结构、以及 --check 在假基线上告警。"""
import contextlib
import importlib.util
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bench_under_test', ROOT / 'tools' / 'bench.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

# task- 里含有 sk- 这两个字符，不能用裸子串。密钥形状要有前缀后面的一截。
SECRET_RES = (
    re.compile(r'(?<![A-Za-z0-9])sk-[A-Za-z0-9]{8,}'),
    re.compile(r'ghp_[A-Za-z0-9]'),
    re.compile(r'github_pat_'),
    re.compile(r'AKIA[0-9A-Z]{8,}'),
    re.compile(r'xox[baprs]-'),
    re.compile(r'BEGIN PRIVATE'),
    re.compile(r'api_key', re.I),
    re.compile(r'Bearer '),
    re.compile(r'/home/'),
    re.compile(r'/Users/'),
    re.compile(r'[A-Za-z]:\\Users'),
    re.compile(r'[A-Za-z]:/Users'),
    re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
)
REPORT_KEYS = (
    'schemaVersion', 'entry', 'events', 'seed', 'repeats', 'calibrationLoops',
    'corpusSha256', 'parseSeconds', 'parseSecondsMedian', 'calibrateSeconds',
    'calibrateSecondsMedian', 'ratio', 'peakBytes', 'members', 'toolCount',
    'python', 'platform', 'warmup', 'cachesCleared',
)


def _assert_clean(testcase, text):
    for pattern in SECRET_RES:
        testcase.assertIsNone(pattern.search(text), pattern.pattern)


def _files(root):
    found = {}
    for path in Path(root).rglob('*'):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def _protocol(**overrides):
    report = {
        'schemaVersion': 1,
        'entry': bench.ENTRY,
        'events': 10000,
        'seed': 29,
        'repeats': 5,
        'calibrationLoops': bench.DEFAULT_CALIBRATION_LOOPS,
        'corpusSha256': 'abc',
        'ratio': 1.0,
        'parseSecondsMedian': 0.4,
        'calibrateSecondsMedian': 0.1,
    }
    baseline = dict(report)
    report.update(overrides)
    return report, baseline


class CorpusTest(unittest.TestCase):
    def test_same_seed_writes_the_same_bytes(self):
        state = random.getstate()
        first = tempfile.mkdtemp(prefix='bench-a-')
        second = tempfile.mkdtemp(prefix='bench-b-')
        try:
            left = bench.generate_corpus(first, 48, 29)
            right = bench.generate_corpus(second, 48, 29)
            self.assertEqual(left['sha256'], right['sha256'])
            self.assertEqual(_files(first), _files(second))
            self.assertEqual(left['events'], 48)
            self.assertNotIn(first, left['sha256'])
            self.assertEqual(random.getstate(), state)
        finally:
            shutil.rmtree(first, ignore_errors=True)
            shutil.rmtree(second, ignore_errors=True)

    def test_seed_changes_the_corpus(self):
        first = tempfile.mkdtemp(prefix='bench-a-')
        second = tempfile.mkdtemp(prefix='bench-b-')
        try:
            left = bench.generate_corpus(first, 48, 29)
            right = bench.generate_corpus(second, 48, 1)
            self.assertNotEqual(left['sha256'], right['sha256'])
        finally:
            shutil.rmtree(first, ignore_errors=True)
            shutil.rmtree(second, ignore_errors=True)

    def test_default_budget_sums_to_ten_thousand(self):
        keys, counts = bench.allocate(bench.DEFAULT_EVENTS)
        self.assertEqual(keys[0], 'lead')
        self.assertEqual(sum(counts.values()), 10000)
        self.assertGreaterEqual(min(counts.values()), 2)
        self.assertIn('e5', counts)

    def test_shapes_and_no_secret_like_text(self):
        root = tempfile.mkdtemp(prefix='bench-shapes-')
        try:
            manifest = bench.generate_corpus(root, 200, 29)
            blob = []
            jsonl_lines = 0
            invalid = False
            for rel, data in _files(root).items():
                text = data.decode('utf-8')
                blob.append(rel)
                blob.append(text)
                if rel.endswith('.jsonl'):
                    lines = text.splitlines()
                    jsonl_lines += len(lines)
                    invalid = invalid or any(line == '{' for line in lines)
            body = '\n'.join(blob)
            self.assertEqual(jsonl_lines, 200)
            self.assertTrue(invalid)
            for needle in (
                '"type":"tool_use"', '"type":"tool_result"', '"usage":', '"thinking"',
                '"isSidechain":true', 'python -m unittest', 'task-notification',
                '"type":"queue-operation"', 'workflows/wave-1/agent-e5.jsonl',
                'agent-a1.meta.json', 'agent-a1.jsonl',
            ):
                self.assertIn(needle, body)
            _assert_clean(self, body)
            _assert_clean(self, bench.CALIB_LINE)
            self.assertEqual(len(manifest['files']), 11)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_rejects_counts_outside_the_benchmark_range(self):
        with self.assertRaises(bench.BenchError):
            bench.generate_corpus('unused', 11, 1)
        with self.assertRaises(bench.BenchError):
            bench.generate_corpus('unused', True, 1)
        with self.assertRaises(bench.BenchError):
            bench.calibrate(0)


class JudgeTest(unittest.TestCase):
    def test_within_and_faster_ratios_pass(self):
        report, baseline = _protocol(ratio=1.1)
        baseline['ratio'] = 1.0
        verdict = bench.judge(report, baseline, 0.30, 'baseline.json')
        self.assertTrue(verdict['ok'])
        self.assertEqual(verdict['kind'], 'ok')
        self.assertIn('检查通过', verdict['message'])
        self.assertNotIn('告警', verdict['message'])

        report['ratio'] = 0.5
        faster = bench.judge(report, baseline, 0.30, 'baseline.json')
        self.assertTrue(faster['ok'])
        self.assertLess(faster['change'], 0)

    def test_exact_threshold_passes_and_over_warns(self):
        report, baseline = _protocol(ratio=1.5)
        baseline['ratio'] = 1.0
        exact = bench.judge(report, baseline, 0.5, 'baseline.json')
        self.assertTrue(exact['ok'])
        over = bench.judge(report, baseline, 0.30, 'baseline.json')
        self.assertFalse(over['ok'])
        self.assertEqual(over['kind'], 'regression')
        self.assertIn('告警', over['message'])
        self.assertIn('30%', over['message'])
        warning = bench.github_warning(over)
        self.assertTrue(warning.startswith('::warning title=解析性能回退::'))
        self.assertNotIn('\n', warning)
        self.assertEqual(bench.github_warning(exact), '')

    def test_threshold_argument_is_not_taken_from_the_file(self):
        report, baseline = _protocol(ratio=1.4)
        baseline['ratio'] = 1.0
        baseline['threshold'] = 10
        verdict = bench.judge(report, baseline, bench.DEFAULT_THRESHOLD, 'baseline.json')
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['kind'], 'regression')

    def test_protocol_mismatch_is_not_a_performance_alarm(self):
        report, baseline = _protocol()
        report['events'] = 36
        verdict = bench.judge(report, baseline, 0.30, 'baseline.json')
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['kind'], 'protocol')
        self.assertIn('不一致', verdict['message'])
        self.assertNotIn('告警', verdict['message'])
        self.assertEqual(bench.github_warning(verdict), '')

    def test_bool_ratio_is_invalid(self):
        report, baseline = _protocol()
        report['ratio'] = True
        verdict = bench.judge(report, baseline, 0.30, 'baseline.json')
        self.assertEqual(verdict['kind'], 'invalid')
        self.assertNotIn('告警', verdict['message'])

    def test_python_note_only_when_minor_version_differs(self):
        self.assertEqual(bench.python_note({'python': '3.12.3'}, {'python': '3.12.8'}), '')
        note = bench.python_note({'python': '3.8.19'}, {'python': '3.12.3'})
        self.assertIn('3.12.3', note)
        self.assertIn('3.8.19', note)

    def test_isolated_board_paths(self):
        home = Path(tempfile.mkdtemp(prefix='bench-home-'))
        try:
            good = type('Board', (), {})()
            good.PROJECTS = home / '.claude' / 'projects'
            good.CODEX_SESSIONS = home / '.codex' / 'sessions'
            good.AGENTS_DIRS = [home / '.claude' / 'agents']
            good.BUS = home / 'bench-bus'
            good.TEAM_CONFIG_PATH = ROOT / 'team-board' / 'team.json'
            bench.assert_isolated(good, home)
            good.PROJECTS = Path.home() / '.claude' / 'projects'
            with self.assertRaises(bench.BenchError) as caught:
                bench.assert_isolated(good, home)
            self.assertIn('Claude', str(caught.exception))
        finally:
            shutil.rmtree(str(home), ignore_errors=True)

    def test_baseline_roundtrip_and_bad_files(self):
        report, _baseline = _protocol()
        report.update({
            'peakBytes': 10, 'python': '3.12.3', 'platform': 'Linux',
            'parseSecondsMedian': 0.4, 'calibrateSecondsMedian': 0.1,
        })
        root = tempfile.mkdtemp(prefix='bench-base-')
        try:
            path = Path(root) / 'baseline.json'
            written = bench.write_baseline(path, report)
            loaded = bench.load_baseline(path)
            self.assertEqual(loaded['ratio'], report['ratio'])
            self.assertEqual(loaded['corpusSha256'], 'abc')
            self.assertIn('recordedAt', written)
            self.assertIn('30%', written['note'])
            with self.assertRaises(bench.BenchError):
                bench.write_baseline(Path(root) / 'baseline.txt', report)
            with self.assertRaises(bench.BenchError):
                bench.load_baseline(Path(root) / 'missing.json')
            (Path(root) / 'bad.json').write_text('[]', encoding='utf-8')
            with self.assertRaises(bench.BenchError):
                bench.load_baseline(Path(root) / 'bad.json')
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_check_and_write_cannot_be_combined(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                bench.parse_args(['--check', '--write-baseline'])

    def test_calibration_is_deterministic(self):
        self.assertEqual(bench.calibrate(32), bench.calibrate(32))
        self.assertGreater(bench.calibrate(8), 0)
        self.assertEqual(bench.median([3.0, 1.0, 2.0]), 2.0)


class BenchmarkRunTest(unittest.TestCase):
    def test_small_run_schema_and_restores_environment(self):
        before = {key: os.environ.get(key) for key in bench.ENV_KEYS}
        report = bench.run_benchmark(events=36, seed=29, repeats=1, calibration_loops=200)
        self.assertEqual({key: os.environ.get(key) for key in bench.ENV_KEYS}, before)
        for key in REPORT_KEYS:
            self.assertIn(key, report)
        self.assertEqual(report['entry'], 'team_board.build_snapshot')
        self.assertEqual(report['members'], 5)
        self.assertEqual(report['toolCount'], 12)
        self.assertEqual(len(report['parseSeconds']), 1)
        self.assertGreater(report['ratio'], 0)
        self.assertGreater(report['peakBytes'], 0)
        self.assertTrue(report['warmup'])
        blob = json.dumps(report)
        self.assertNotIn('bench-corpus-', blob)
        self.assertNotIn('/home/', blob)
        self.assertNotIn('/Users/', blob)
        table = bench.format_table(report)
        self.assertIn('解析中位数', table)
        self.assertIn('校准样本', table)
        self.assertIn('team_board.build_snapshot', table)

    def test_cli_check_fails_on_an_injected_fast_baseline(self):
        fail = _run_cli(1e-9)
        self.assertEqual(fail.returncode, 1, fail.stderr)
        self.assertIn('告警', fail.stderr)
        self.assertIn('::warning title=解析性能回退::', fail.stderr)
        payload = json.loads(fail.stdout)
        self.assertEqual(payload['events'], 36)
        self.assertEqual(payload['members'], 5)
        self.assertNotIn('解析性能基准', fail.stdout)

    def test_cli_check_passes_on_an_injected_slow_baseline(self):
        passed = _run_cli(1e9)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertIn('检查通过', passed.stderr)
        self.assertNotIn('告警', passed.stderr)
        payload = json.loads(passed.stdout)
        self.assertEqual(payload['schemaVersion'], 1)
        self.assertEqual(payload['toolCount'], 12)
        self.assertEqual(payload['calibrationLoops'], 200)


def _small_baseline(ratio):
    root = tempfile.mkdtemp(prefix='bench-sha-')
    try:
        manifest = bench.generate_corpus(root, 36, 29)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return {
        'schemaVersion': 1,
        'entry': bench.ENTRY,
        'events': 36,
        'seed': 29,
        'repeats': 1,
        'calibrationLoops': 200,
        'corpusSha256': manifest['sha256'],
        'ratio': ratio,
        'python': '3.12.3',
        'platform': 'test',
    }


def _run_cli(ratio):
    root = tempfile.mkdtemp(prefix='bench-cli-')
    try:
        path = Path(root) / 'baseline.json'
        path.write_text(json.dumps(_small_baseline(ratio)), encoding='utf-8')
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        completed = subprocess.run(
            [sys.executable, str(ROOT / 'tools' / 'bench.py'),
             '--check', '--json', '--events', '36', '--seed', '29', '--repeats', '1',
             '--calibration-loops', '200', '--baseline', str(path)],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        completed.stdout = completed.stdout.decode('utf-8')
        completed.stderr = completed.stderr.decode('utf-8')
        return completed
    finally:
        shutil.rmtree(root, ignore_errors=True)


class CommittedBaselineTest(unittest.TestCase):
    def test_file_matches_generator_and_default_protocol(self):
        path = ROOT / 'benchmarks' / 'baseline.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(data['schemaVersion'], 1)
        self.assertEqual(data['entry'], bench.ENTRY)
        self.assertEqual(data['events'], bench.DEFAULT_EVENTS)
        self.assertEqual(data['seed'], bench.DEFAULT_SEED)
        self.assertEqual(data['repeats'], bench.DEFAULT_REPEATS)
        self.assertEqual(data['calibrationLoops'], bench.DEFAULT_CALIBRATION_LOOPS)
        self.assertGreater(data['ratio'], 0)
        self.assertGreater(data['parseSecondsMedian'], 0)
        self.assertGreater(data['calibrateSecondsMedian'], 0)
        self.assertGreater(data['peakBytes'], 0)
        root = tempfile.mkdtemp(prefix='bench-committed-')
        try:
            manifest = bench.generate_corpus(root, data['events'], data['seed'])
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(manifest['sha256'], data['corpusSha256'])
        _assert_clean(self, path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
