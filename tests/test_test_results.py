"""测试命令识别与结果解析。夹具都是合成输出，不读本机转录。"""
import importlib.util
import json
import os
import time
import unittest
from pathlib import Path

from test_board import BoardEnv, asst, jl, user

ROOT = Path(__file__).resolve().parents[1]
MARKER = 'SYNTHETIC_OUTPUT_DO_NOT_KEEP'


def load_results():
    spec = importlib.util.spec_from_file_location(
        'test_results_under_test', ROOT / 'team-board' / 'test_results.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load_results()


def run(command, output, block=None, extra=''):
    return R.from_invocation(command, output, block or {}, extra)


class DetectTest(unittest.TestCase):
    def test_named_runners_and_wrappers(self):
        cases = {
            'pytest -q': ['pytest'],
            'py.test tests/test_mod.py': ['pytest'],
            'python -m pytest -q': ['pytest'],
            'python3 -m unittest discover -s tests': ['unittest'],
            'coverage run -m pytest -q': ['pytest'],
            'npm test': ['npm'],
            'npm run test -- --watch': ['npm'],
            'npm run test:unit': ['npm'],
            'pnpm test': ['pnpm'],
            'pnpm run test-api': ['pnpm'],
            'yarn test': ['yarn'],
            'yarn run test_bot': ['yarn'],
            'go test ./...': ['go'],
            'cargo test -- --nocapture': ['cargo'],
            'cargo +nightly test': ['cargo'],
            'bun test': ['bun'],
            'deno test': ['deno'],
            'npx vitest run': ['vitest'],
            'mvn -q test': ['mvn'],
            'gradle test': ['gradle'],
            'make test': ['make'],
            'cd tests && FOO=1 pytest -q': ['pytest'],
            'bash -c "pytest -q"': ['pytest'],
            'pytest -q && cargo test': ['pytest', 'cargo'],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(R.detect_frameworks(command), expected)

    def test_lookalikes_are_not_tests(self):
        for command in (
            'echo pytest',
            'echo "pytest passed"',
            'npm install',
            'npm run start',
            'npm run pretest',
            'go env',
            'cargo build',
            'python -m pip install pytest',
            'python script.py',
            'grep pytest tests/test_mod.py',
            'make testsomething',
            '',
            None,
        ):
            with self.subTest(command=command):
                self.assertEqual(R.detect_frameworks(command), [])
                self.assertIsNone(run(command or '', '3 passed'))


class ParseTest(unittest.TestCase):
    def test_pytest_pass_fail_skip_collection_and_truncation(self):
        passed = run('pytest -q', '===== 3 passed, 1 skipped in 0.12s =====\n', {'is_error': False})
        self.assertEqual(passed['status'], 'pass')
        self.assertEqual((passed['passed'], passed['failed'], passed['skipped'], passed['errors']), (3, 0, 1, 0))
        self.assertEqual(passed['failed'], 0)
        self.assertEqual(passed['exitCode'], 0)
        self.assertEqual(passed['exitStatus'], 'zero')
        self.assertFalse(passed['truncated'])

        failed = run(
            'python -m pytest -q',
            '===== 2 failed, 5 passed, 1 skipped in 1.23s =====\n',
            {'is_error': True, 'exit_code': 1})
        self.assertEqual(failed['status'], 'fail')
        self.assertEqual((failed['passed'], failed['failed'], failed['skipped']), (5, 2, 1))
        self.assertEqual(failed['exitCode'], 1)

        collected = run(
            'pytest -q',
            'collected 0 items / 1 error\n'
            'Interrupted: 1 error during collection\n'
            '===== 1 error in 0.08s =====\n',
            {'is_error': True})
        self.assertEqual(collected['status'], 'fail')
        self.assertEqual(collected['errors'], 1)
        self.assertEqual(collected['passed'], 0)
        self.assertIsNone(collected['exitCode'])
        self.assertEqual(collected['exitStatus'], 'nonzero')

        interrupt = run('pytest', 'Interrupted: 2 errors during collection\n', {'is_error': True})
        self.assertEqual(interrupt['status'], 'fail')
        self.assertEqual(interrupt['errors'], 2)

        none = run('pytest -q', '===== no tests ran in 0.01s =====\n', {'is_error': False})
        self.assertEqual(none['status'], 'unknown')
        self.assertEqual(none['passed'], 0)

        cut = run(
            'pytest -q',
            'test session starts\ncollected 4 items\n\ntests/test_mod.py .F\n',
            {'is_error': True})
        self.assertEqual(cut['status'], 'unknown')
        self.assertTrue(cut['truncated'])
        self.assertIsNone(cut['passed'])
        self.assertIsNone(cut['failed'])
        self.assertNotIn(MARKER, json.dumps(cut))

        marked = run('pytest -q', 'tests/test_mod.py .F\n[truncated]\n', {'is_error': False})
        self.assertEqual(marked['status'], 'unknown')
        self.assertTrue(marked['truncated'])
        self.assertIsNone(marked['passed'])

    def test_pytest_summary_wins_over_an_earlier_truncation_mark(self):
        text = 'test session starts\n[truncated]\n===== 4 passed in 0.20s =====\n'
        result = run('pytest -q', text, {'is_error': False})
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['passed'], 4)
        self.assertFalse(result['truncated'])

    def test_ansi_and_conflict_are_not_guessed(self):
        colored = '\x1b[32m===== 2 passed in 0.01s =====\x1b[0m\n'
        result = run('pytest -q', colored, {'is_error': True})
        self.assertEqual(result['passed'], 2)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['exitStatus'], 'nonzero')

        forced = run('pytest -q', '===== 1 failed, 1 passed in 0.01s =====\n', {'is_error': False})
        self.assertEqual(forced['status'], 'fail')
        self.assertEqual(forced['exitCode'], 0)

    def test_unittest_pass_fail_and_truncation(self):
        ok = run(
            'python -m unittest discover -s tests',
            'Ran 5 tests in 0.001s\n\nOK (skipped=2)\n',
            {'is_error': False})
        self.assertEqual(ok['status'], 'pass')
        self.assertEqual((ok['passed'], ok['failed'], ok['skipped'], ok['errors']), (3, 0, 2, 0))

        bad = run(
            'python3 -m unittest',
            'Ran 4 tests in 0.002s\n\nFAILED (failures=1, errors=1, skipped=1)\n',
            {'is_error': True})
        self.assertEqual(bad['status'], 'fail')
        self.assertEqual((bad['passed'], bad['failed'], bad['skipped'], bad['errors']), (1, 1, 1, 1))

        cut = run('python -m unittest', 'Ran 3 tests in 0.001s\n', {'is_error': True})
        self.assertEqual(cut['status'], 'unknown')
        self.assertTrue(cut['truncated'])
        self.assertIsNone(cut['passed'])

    def test_npm_pnpm_yarn_outputs(self):
        jest = run(
            'npm test',
            'Test Suites: 1 failed, 1 total\nTests:       1 failed, 2 skipped, 12 passed, 15 total\n',
            {'is_error': True, 'exitCode': 1})
        self.assertEqual(jest['framework'], 'npm')
        self.assertEqual(jest['status'], 'fail')
        self.assertEqual((jest['passed'], jest['failed'], jest['skipped']), (12, 1, 2))

        vitest = run(
            'pnpm test',
            ' Test Files  1 failed | 2 passed (3)\n      Tests  2 failed | 10 passed | 1 skipped (13)\n',
            {'is_error': True})
        self.assertEqual(vitest['framework'], 'pnpm')
        self.assertEqual(vitest['status'], 'fail')
        self.assertEqual((vitest['passed'], vitest['failed'], vitest['skipped']), (10, 2, 1))

        mocha = run('yarn test', '  3 passing (12ms)\n  1 pending\n', {'is_error': False})
        self.assertEqual(mocha['status'], 'pass')
        self.assertEqual((mocha['passed'], mocha['failed'], mocha['skipped']), (3, 0, 1))

        cut = run('yarn test', '  3 passing (12ms)\n  1) suite\n       name:\n', {'is_error': True})
        self.assertEqual(cut['status'], 'unknown')
        self.assertTrue(cut['truncated'])
        self.assertIsNone(cut['failed'])

        empty = run('npm test', 'No tests found, exiting with code 1\n', {'is_error': True})
        self.assertEqual(empty['status'], 'fail')
        self.assertEqual(empty['passed'], 0)

        garbage = run('npm test', 'added 1 package\n', {'is_error': False})
        self.assertEqual(garbage['status'], 'unknown')
        self.assertFalse(garbage['truncated'])
        self.assertIsNone(garbage['passed'])

    def test_go_and_cargo(self):
        ok = run('go test ./...', 'ok  \texample.com/mod\t0.012s\n', {'is_error': False})
        self.assertEqual(ok['status'], 'pass')
        self.assertIsNone(ok['passed'])

        bad = run(
            'go test ./...',
            '--- PASS: TestOne (0.00s)\n--- FAIL: TestTwo (0.00s)\n--- SKIP: TestThree (0.00s)\n'
            'FAIL\texample.com/mod\t0.030s\n',
            {'is_error': True})
        self.assertEqual(bad['status'], 'fail')
        self.assertEqual((bad['passed'], bad['failed'], bad['skipped']), (1, 1, 1))

        build = run('go test ./...', 'FAIL\texample.com/mod [build failed]\n', {'is_error': True})
        self.assertEqual(build['status'], 'fail')
        self.assertIsNone(build['passed'])

        cut = run('go test -v ./...', '=== RUN   TestOne\n', {'is_error': True})
        self.assertEqual(cut['status'], 'unknown')
        self.assertTrue(cut['truncated'])
        self.assertIsNone(cut['passed'])

        cargo = run(
            'cargo test',
            'test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.01s\n'
            'test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.02s\n',
            {'is_error': True})
        self.assertEqual(cargo['status'], 'fail')
        self.assertEqual((cargo['passed'], cargo['failed'], cargo['skipped']), (5, 1, 1))

        compile_error = run('cargo test', 'error: could not compile `synthetic`\n', {'is_error': True})
        self.assertEqual(compile_error['status'], 'fail')
        self.assertIsNone(compile_error['passed'])

        cargo_cut = run('cargo test', 'running 4 tests\n', {'is_error': True})
        self.assertEqual(cargo_cut['status'], 'unknown')
        self.assertTrue(cargo_cut['truncated'])

    def test_bun_deno_and_surefire(self):
        bun = run('bun test', ' 2 pass\n 0 fail\nRan 2 tests across 1 file. [5.00ms]\n', {'is_error': False})
        self.assertEqual(bun['status'], 'pass')
        self.assertEqual(bun['passed'], 2)

        deno = run('deno test', 'FAILED | 2 passed | 1 failed | 1 skipped (8ms)\n', {'is_error': True})
        self.assertEqual(deno['status'], 'fail')
        self.assertEqual((deno['passed'], deno['failed'], deno['skipped']), (2, 1, 1))

        mvn = run(
            'mvn -q test',
            'Tests run: 5, Failures: 1, Errors: 1, Skipped: 2\n',
            {'is_error': True})
        self.assertEqual(mvn['framework'], 'mvn')
        self.assertEqual(mvn['status'], 'fail')
        self.assertEqual((mvn['passed'], mvn['failed'], mvn['skipped'], mvn['errors']), (1, 1, 2, 1))

        made = run('make test', '===== 6 passed in 0.04s =====\n', {'is_error': False})
        self.assertEqual(made['framework'], 'make')
        self.assertEqual(made['status'], 'pass')
        self.assertEqual(made['passed'], 6)

    def test_mixed_commands_do_not_pick_a_side(self):
        text = (
            '===== 2 passed in 0.01s =====\n'
            'test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n'
        )
        result = run('pytest -q && cargo test', text, {'is_error': True})
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['passed'])

    def test_exit_field_beats_a_boolean_and_bool_is_not_a_code(self):
        result = run('pytest -q', '===== 1 passed in 0.01s =====\n', {'is_error': True, 'exit_code': 2})
        self.assertEqual(result['exitCode'], 2)
        self.assertEqual(result['status'], 'unknown')
        flagged = run('pytest -q', '===== 1 passed in 0.01s =====\n', {'is_error': False, 'exitCode': True})
        self.assertEqual(flagged['exitCode'], 0)
        self.assertEqual(flagged['status'], 'pass')
        line = run('go test ./...', 'ok  \texample.com/mod\t0.01s\nexit code: 1\n', {})
        self.assertEqual(line['exitCode'], 1)
        self.assertEqual(line['status'], 'unknown')

    def test_output_and_ticket_path_stay_out_of_the_result(self):
        output = MARKER + '\n===== 1 passed in 0.01s =====\n'
        result = run(
            'pytest -q .team/tickets/01-store.md',
            output,
            {'is_error': False},
            'read .team/tickets/02-api.md and ../secret')
        self.assertEqual(result['ticket'], '02-api.md')
        blob = json.dumps(result)
        self.assertNotIn(MARKER, blob)
        self.assertNotIn('.team', blob)
        self.assertNotIn('..', blob)
        self.assertEqual(R.ticket_basename('proj/.team/tickets/../01-store.md'), '')
        self.assertEqual(R.ticket_basename('notes/01-store.md'), '')

    def test_attach_uses_exact_owner_and_the_newer_result(self):
        older = {'status': 'pass', 'ts': 1, 'passed': 1, 'failed': 0, 'skipped': 0}
        newer = {'status': 'fail', 'ts': 2, 'passed': 0, 'failed': 1, 'skipped': 0}
        tasks = [
            {'id': '1', 'owner': '测测'},
            {'id': '2', 'owner': '测测 '},
            {'id': '3', 'owner': None},
            {'id': '4', 'owner': '文文'},
        ]
        R.attach_last_tests(tasks, [
            {'id': 'a', 'name': '测测', 'lastTest': older},
            {'id': 'b', 'name': '测测', 'lastTest': newer},
            {'id': 'c', 'name': '文文', 'lastTest': {'status': 'maybe', 'ts': 3}},
        ])
        self.assertEqual(tasks[0]['lastTest']['status'], 'fail')
        self.assertEqual(tasks[0]['lastTest']['memberId'], 'b')
        self.assertNotIn('lastTest', tasks[1])
        self.assertNotIn('lastTest', tasks[2])
        self.assertNotIn('lastTest', tasks[3])


class SnapshotWiringTest(unittest.TestCase):
    def setUp(self):
        self.saved = (os.environ.get('HOME'), os.environ.get('USERPROFILE'))
        self.env = BoardEnv()
        sub = self.env.claude / 'projects' / 'C--demo' / 'sess-0001' / 'subagents'
        ticket = str(self.env.tmp / 'proj' / '.team' / 'tickets' / '01-store.md')
        (sub / 'agent-c3.jsonl').write_text(jl(
            user('2026-01-01T10:01:00Z', '跑合成测试'),
            asst('2026-01-01T10:01:01Z', 'claude-sonnet-5', [
                {'type': 'tool_use', 'id': 'rd-ticket', 'name': 'Read', 'input': {'file_path': ticket}}]),
            user('2026-01-01T10:01:02Z', [{'type': 'tool_result', 'tool_use_id': 'rd-ticket', 'content': 'synthetic'}]),
            asst('2026-01-01T10:01:03Z', 'claude-sonnet-5', [
                {'type': 'tool_use', 'id': 'tu-pytest', 'name': 'Bash',
                 'input': {'command': 'pytest -q tests/test_mod.py', 'description': 'run synthetic tests'}}]),
            user('2026-01-01T10:01:04Z', [{
                'type': 'tool_result', 'tool_use_id': 'tu-pytest', 'is_error': False,
                'content': MARKER + '\n===== 3 passed, 1 skipped in 0.12s =====\n'}]),
        ), encoding='utf-8')
        (sub / 'agent-c3.meta.json').write_text(json.dumps({
            'agentType': 'qa-tester', 'description': '合成测试', 'toolUseId': 'tu-c3'}), encoding='utf-8')
        (sub / 'agent-d4.jsonl').write_text(jl(
            user('2026-01-01T10:02:00Z', '再跑一次'),
            asst('2026-01-01T10:02:01Z', 'claude-sonnet-5', [
                {'type': 'tool_use', 'id': 'tu-go', 'name': 'Bash', 'input': {'command': 'go test ./...'}}]),
            user('2026-01-01T10:02:02Z', [{
                'type': 'tool_result', 'tool_use_id': 'tu-go', 'is_error': True,
                'content': '--- FAIL: TestBroken (0.00s)\nFAIL\texample.com/mod\t0.01s\n' + MARKER}]),
            asst('2026-01-01T10:02:03Z', 'claude-sonnet-5', [
                {'type': 'text', 'text': '测试没过。'}], stop='end_turn'),
        ), encoding='utf-8')
        (sub / 'agent-d4.meta.json').write_text(json.dumps({
            'agentType': 'backend-dev', 'description': '合成失败', 'toolUseId': 'tu-d4'}), encoding='utf-8')
        lead = self.env.claude / 'projects' / 'C--demo' / 'sess-0001.jsonl'
        extra = jl(
            asst('2026-01-01T10:03:00Z', 'claude-opus-5', [
                {'type': 'tool_use', 'id': 'tu-lead', 'name': 'Bash', 'input': {'command': 'npm test'}}]),
            user('2026-01-01T10:03:01Z', [{
                'type': 'tool_result', 'tool_use_id': 'tu-lead', 'is_error': False,
                'content': 'Tests:       2 passed, 2 total\n'}]),
        )
        lead.write_text(lead.read_text(encoding='utf-8') + extra, encoding='utf-8')
        self.tb = self.env.load()
        self.snap = self.tb.build_snapshot(self.tb.argparse.Namespace(session=None, project=None, stale=600))

    def tearDown(self):
        self.env.cleanup()
        home, profile = self.saved
        if home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = home
        if profile is None:
            os.environ.pop('USERPROFILE', None)
        else:
            os.environ['USERPROFILE'] = profile

    def member(self, agent_type, description):
        return next(m for m in self.snap['members']
                    if m['agentType'] == agent_type and m['description'] == description)

    def test_members_feed_and_tasks_get_the_parsed_result(self):
        passed = self.member('qa-tester', '合成测试')
        failed = self.member('backend-dev', '合成失败')
        self.assertEqual(passed['lastTest']['status'], 'pass')
        self.assertEqual(passed['lastTest']['passed'], 3)
        self.assertEqual(passed['lastTest']['skipped'], 1)
        self.assertEqual(passed['lastTest']['ticket'], '01-store.md')
        self.assertEqual(passed['lastTest']['toolId'], 'tu-pytest')
        self.assertEqual(failed['lastTest']['status'], 'fail')
        self.assertEqual(failed['lastTest']['failed'], 1)
        self.assertEqual(failed['lastTest']['exitStatus'], 'nonzero')
        self.assertIsNone(self.snap['members'][0]['lastTest'])
        self.assertEqual(self.snap['lead']['lastTest']['framework'], 'npm')
        self.assertEqual(self.snap['lead']['lastTest']['status'], 'pass')
        blob = json.dumps({'tests': [passed['lastTest'], failed['lastTest'], self.snap['lead']['lastTest']],
                           'feed': [row.get('test') for row in self.snap['feed']]}, ensure_ascii=False)
        self.assertNotIn(MARKER, blob)
        self.assertNotIn(str(self.env.tmp), blob)
        self.assertNotIn('pytest -q', blob)
        row = next(item for item in self.snap['feed'] if item.get('toolId') == 'tu-pytest')
        self.assertEqual(row['test']['status'], 'pass')
        self.assertEqual(row['memberId'], passed['id'])
        view = {
            'available': True,
            'teamName': 'session-test0001',
            'lead': '合成队长',
            'warnings': [],
            'tasks': [
                {'id': '1', 'subject': 'Synthetic check', 'description': 'synthetic',
                 'status': 'in_progress', 'normalizedStatus': 'in_progress',
                 'owner': '测测', 'blockedBy': [], 'blocks': []},
                {'id': '2', 'subject': 'Near miss', 'description': '',
                 'status': 'pending', 'normalizedStatus': 'pending',
                 'owner': '测测 ', 'blockedBy': [], 'blocks': []},
                {'id': '3', 'subject': 'Lead check', 'description': '',
                 'status': 'pending', 'normalizedStatus': 'pending',
                 'owner': '合成队长', 'blockedBy': [], 'blocks': []},
            ],
            'members': [],
        }
        public = self.tb._snapshot_official_team(view, self.snap['members'], time.time(), self.snap['lead'])
        by_id = {task['id']: task for task in public['tasks']}
        self.assertEqual(by_id['1']['lastTest']['status'], 'pass')
        self.assertEqual(by_id['1']['lastTest']['memberId'], passed['id'])
        self.assertNotIn('lastTest', by_id['2'])
        self.assertEqual(by_id['3']['lastTest']['framework'], 'npm')
        self.assertNotIn(MARKER, json.dumps(public['tasks']))

    def test_timeline_and_feed_keep_the_latest_test_when_clipped(self):
        early = {'kind': 'tool', 'ts': 1, 'test': {'status': 'pass', 'toolId': 'early', 'ts': 1}, 'toolId': 'early'}
        filler = [{'kind': 'tool', 'ts': index, 'label': 'Read'} for index in range(2, 90)]
        shown = self.tb._clip_timeline([early] + filler, 10)
        self.assertTrue(any(item.get('toolId') == 'early' for item in shown))
        self.assertLessEqual(len(shown), 10)
        feed = [{'ts': index, 'memberId': 'm', 'label': 'x'} for index in range(30, 0, -1)]
        feed.append({'ts': 1, 'memberId': 'm', 'label': 'pytest', 'test': {'status': 'fail', 'ts': 1}})
        kept = self.tb._keep_test_rows(feed, 5)
        self.assertTrue(any(isinstance(row.get('test'), dict) for row in kept))
        self.assertGreater(len(kept), 5)


class PageContractTest(unittest.TestCase):
    def test_badges_and_feed_jump_are_in_the_page(self):
        html = (ROOT / 'team-board' / 'index.html').read_text(encoding='utf-8')
        for needle in (
            'test-badge', 'test-fail', 'testDetailText', 'openTestFeed', 'data-feed',
            '通过', '失败', '未知', '输出被截断', 'feedOpen',
        ):
            self.assertIn(needle, html)
        self.assertIn('.test-badge.pass', html)
        self.assertIn('.test-badge.fail', html)
        self.assertIn('.test-badge.unknown', html)
        self.assertLess(html.index('function testBadgeHtml'), html.index('function renderFeed'))
