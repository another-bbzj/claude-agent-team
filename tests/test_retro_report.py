"""Retrospective Markdown copies snapshot fields and does not invent totals."""
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('retro_report', ROOT / 'tools' / 'retro_report.py')
retro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retro)

TIMELINE = """\
| 对象 | 开始 | 停滞或最后活动 | 结束 | 用时 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 演示文档 · 文档部 | 2026-01-01 09:45:10 UTC | 2026-01-01 09:47:20 UTC | — | — | 停滞 |
| 演示队长 · 指挥部 | 2026-01-01 10:00:00 UTC | 2026-01-01 10:02:20 UTC | — | — | 协调中 |
| 演示后端 · 研发部 | 2026-01-01 10:00:00 UTC | 2026-01-01 10:01:40 UTC | — | — | 进行中 |
| 演示前端 · 研发部 | 2026-01-01 10:00:00 UTC | 2026-01-01 10:01:50 UTC | 2026-01-01 10:01:50 UTC | 1m 50s | 已完成 |
| 演示测试 · 质量部 | 2026-01-01 10:00:00 UTC | 2026-01-01 10:02:00 UTC | 2026-01-01 10:02:00 UTC | 2m 0s | 已完成 |
| 合成研究员 · 研究部 | 2026-01-01 10:00:00 UTC | 2026-01-01 10:02:16 UTC | — | — | 进行中 |
| 合成写手 · 研究部 | 2026-01-01 10:00:00 UTC | 2026-01-01 10:02:05 UTC | 2026-01-01 10:02:05 UTC | 2m 5s | 已完成 |
| 会话 · synthetic-worktree | — | 2026-01-01 10:01:20 UTC | — | — | — |
| 会话 · synthetic-example | — | 2026-01-01 10:02:20 UTC | — | — | — |
"""


def _load(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))


def _section(report, heading, following=None):
    body = report.split(heading, 1)[1]
    if following and following in body:
        body = body.split(following, 1)[0]
    return body


class RetroReportRenderTest(unittest.TestCase):
    def setUp(self):
        self.demo = _load('examples/board-demo.synthetic.json')

    def test_demo_timeline_and_cost_come_from_the_fixture(self):
        report = retro.render_report(self.demo)
        self.assertTrue(report.startswith('# 团队复盘\n'))
        self.assertTrue(report.endswith('\n'))
        self.assertIn('这份报告用的是仓库里的合成演示快照，不代表真实用量。', report)
        self.assertIn('快照时间：2026-01-01 10:02:20 UTC', report)
        self.assertIn(TIMELINE, report)
        self.assertIn('全队估算成本：$2.0392', report)
        self.assertIn('成员估算成本：$2.0342\n', report)
        self.assertIn('未定价条目：0', report)
        self.assertIn('成员输出 token：1040', report)
        self.assertIn('| 合计 | 646380 | 170 | 646550 |', report)
        self.assertIn('| 缓存命中 | 0.2492 | 0.6667 | 0.2493 |', report)
        self.assertIn('| 研发部 | 进行中 | $1.3768 | 479940 | 0.3338 | 0 |', report)
        self.assertIn('| 演示队长 · 指挥部 | $0.005 | 170 | 0.6667 |', report)
        self.assertIn('| 合成研究员 · 研究部 | $0 | 0 | 0 |', report)
        self.assertEqual(report.count('$2.0392'), 1)
        self.assertEqual(report.count('$2.0342'), 1)
        self.assertNotIn('（部分）', report)
        self.assertNotIn('2m 10s', report)
        self.assertIn('## 阻塞与停滞', report)
        self.assertIn('## 改进建议', report)
        blockers = _section(report, '## 阻塞与停滞', '## 改进建议')
        suggestions = _section(report, '## 改进建议')
        self.assertIn('演示文档 · 文档部 · 停滞。', blockers)
        self.assertIn('演示后端 · 研发部 的动作流里有 1 条 error。', blockers)
        self.assertIn('最近测试失败（failed 1，passed 2，框架 go）。', blockers)
        self.assertIn('最近测试失败（failed 2，passed 1，框架 pytest）。', blockers)
        self.assertIn('任务「Wait for the synthetic survey」的 blocked 为 true。waitingOn 有 2 项。', blockers)
        self.assertNotIn('演示前端', blockers)
        self.assertNotIn('未知', blockers)
        self.assertIn('结束时间是 null，这里不用当前时间补上。', suggestions)
        self.assertIn('最近一次测试是未知。truncated 为 true。它不能算通过，也不能算失败。', suggestions)
        self.assertIn('演示测试 · 质量部 的 cacheHit 是 0，同一处的 token 数是 165000。', suggestions)
        self.assertIn('低于本报告的参考线 0.2。参考线不是快照字段。这里不估算能省下的金额。', suggestions)
        self.assertNotIn('合成写手', suggestions)
        self.assertNotIn('预算', report)
        for hidden in (
                'synthetic-session', 'synthetic-member', 'synthetic-official', 'tool-demo',
                'session-syntheti', 'wt-synthetic', 'huhu-plan', 'backend-dev',
                'worktree-feature-board', 'go test', 'pytest -q', 'npm test',
                '【合成演示】', '合成演示：展示', '官方 Agent Team', '检查界面',
                'Ask the synthetic lead', 'Near-miss', 'Draft synthetic notes',
                'sk-', '/tmp', '/home', '@'):
            self.assertNotIn(hidden, report)

    def test_render_does_not_mutate_or_depend_on_now(self):
        blob = json.dumps(self.demo, sort_keys=True)
        with mock.patch('time.time', side_effect=AssertionError('clock')):
            retro.render_report(self.demo)
        self.assertEqual(blob, json.dumps(self.demo, sort_keys=True))

    def test_missing_fields_omit_sections_and_zeros(self):
        report = retro.render_report({'members': [{'status': 'running'}]})
        self.assertIn('没有可写入时间线、成本、阻塞或改进建议的字段。', report)
        for absent in ('$', '（部分）', '未知', '## 时间线', '## 成本与 token', '## 阻塞与停滞', '## 改进建议', '0.00'):
            self.assertNotIn(absent, report)

    def test_null_cost_is_unknown_and_rows_are_not_summed(self):
        report = retro.render_report({
            'members': [
                {'name': '甲', 'cost': 1.25, 'totalTokens': 10, 'status': 'completed'},
                {'name': '乙', 'cost': None, 'totalTokens': 4, 'status': 'completed'},
                {'name': '丙', 'status': 'running'},
            ],
            'departments': [
                {'id': 'dev', 'name': '研发部', 'cost': 1.25, 'tokens': 10, 'unpriced': 0},
                {'id': 'qa', 'name': '质量部', 'cost': None, 'unpriced': 1},
            ],
        })
        self.assertEqual(report.count('$1.25'), 2)
        self.assertIn('| 乙 | 未知 | 4 |', report)
        self.assertIn('| 质量部 | 未知 | — | 1 |', report)
        self.assertNotIn('丙', report)
        self.assertNotIn('$0', report)
        self.assertNotIn('合计', report)
        self.assertNotIn('（部分）', report)
        self.assertNotIn('## 时间线', report)

    def test_present_total_is_kept_when_rows_differ(self):
        report = retro.render_report({
            'members': [{'name': '甲', 'cost': 1.5}],
            'totals': {'cost': 9},
        })
        self.assertIn('全队估算成本：$9', report)
        self.assertIn('| 甲 | $1.5 |', report)
        self.assertNotIn('（部分）', report)
        self.assertNotIn('成员估算成本', report)

    def test_partial_mark_uses_unpriced_count(self):
        report = retro.render_report({
            'members': [{'name': '甲', 'status': 'completed', 'cost': 0.012}],
            'totals': {'cost': 0.012, 'membersCost': 0.012, 'unpriced': 2, 'tokens': 9},
        })
        self.assertIn('全队估算成本：$0.012（部分）', report)
        self.assertIn('成员估算成本：$0.012（部分）', report)
        self.assertIn('未定价条目：2', report)
        self.assertIn('未定价条目是 2。对应成本是部分估算，缺的价格没有按 0 计入。', report)
        self.assertNotIn('全队估算成本：未知', report)
        self.assertIn('| 甲 | $0.012 |', report)

    def test_usage_total_is_not_summed_from_parts(self):
        report = retro.render_report({'totals': {'usage': {'in': 3, 'out': 4}}})
        self.assertIn('| 输入 | 3 |', report)
        self.assertIn('| 输出 | 4 |', report)
        self.assertNotIn('合计', report)
        self.assertNotIn('| 7 |', report)
        self.assertNotIn('$', report)

    def test_invalid_numbers_are_dropped_without_echo(self):
        report = retro.render_report({
            'members': [{'name': '甲', 'cost': 'SECRET', 'totalTokens': 'SECRET', 'outputTokens': 5}],
            'totals': {'cost': 'SECRET', 'unpriced': True, 'tokens': 1.5},
        })
        self.assertNotIn('SECRET', report)
        self.assertNotIn('True', report)
        self.assertNotIn('$', report)
        self.assertNotIn('（部分）', report)
        self.assertNotIn('输出 5', report)
        self.assertNotIn('1.5', report)

    def test_output_tokens_are_labeled_when_total_is_absent(self):
        report = retro.render_report({'members': [{'name': '甲', 'outputTokens': 5}]})
        self.assertIn('| 成员 | token |', report)
        self.assertIn('| 甲 | 输出 5 |', report)
        self.assertNotIn('$', report)

    def test_low_cache_needs_a_positive_token_count(self):
        low = retro.render_report({'members': [{'name': '甲', 'cacheHit': 0.05, 'totalTokens': 100}]})
        self.assertIn('甲 的 cacheHit 是 0.05，同一处的 token 数是 100。', low)
        self.assertIn('低于本报告的参考线 0.2。参考线不是快照字段。这里不估算能省下的金额。', low)
        self.assertNotIn('$', low)
        borderline = retro.render_report({'members': [{'name': '甲', 'cacheHit': 0.2, 'totalTokens': 100}]})
        self.assertNotIn('参考线', borderline)
        quiet = retro.render_report({'members': [{'name': '甲', 'cacheHit': 0, 'totalTokens': 0}]})
        self.assertNotIn('参考线', quiet)
        missing = retro.render_report({'members': [{'name': '甲', 'totalTokens': 100}]})
        self.assertNotIn('缓存', missing)
        self.assertNotIn('参考线', missing)
        no_tokens = retro.render_report({'totals': {'allUsage': {'cacheHit': 0.1}}})
        self.assertNotIn('参考线', no_tokens)
        measured = retro.render_report({'totals': {'allUsage': {'cacheHit': 0.1, 'total': 80}}})
        self.assertIn('全队用量的 cacheHit 是 0.1，同一处的 token 合计是 80。', measured)

    def test_budget_over_uses_only_present_ratio(self):
        over = retro.render_report({
            'members': [{'name': '甲', 'status': 'completed'}],
            'budget': {
                'enabled': True,
                'status': 'over',
                'ratio': 1.5,
                'lowerBound': True,
                'spent': 0.2,
                'limit': 0.1,
                'suggestions': [
                    {'action': '甲下次把 model 设为 sonnet。'},
                    {'action': 'open /tmp/SECRET_PLAN'},
                ],
            },
        })
        self.assertIn('- 状态：超出预算', over)
        self.assertIn('- 使用率：1.5（下限，未定价未计入）', over)
        self.assertIn('- 已估算花费：$0.2', over)
        self.assertIn('- 上限：$0.1', over)
        self.assertIn('会话预算是超出。 budget.ratio 为 1.5。', over)
        self.assertIn('甲下次把 model 设为 sonnet。', over)
        self.assertNotIn('SECRET', over)
        self.assertNotIn('/tmp', over)
        no_ratio = retro.render_report({'budget': {'enabled': True, 'status': 'over'}})
        self.assertIn('快照里没有 budget.ratio，这里不估算比例。', no_ratio)
        self.assertNotIn('%', no_ratio)
        self.assertNotIn('1.', no_ratio)
        self.assertNotIn('$', no_ratio)
        disabled = retro.render_report({'budget': {'enabled': False, 'status': 'over', 'ratio': 2}})
        self.assertNotIn('预算', disabled)
        self.assertNotIn('超出', disabled)
        warn = retro.render_report({'budget': {'enabled': True, 'status': 'warn', 'ratio': 0.85}})
        self.assertIn('会话预算接近上限。 budget.ratio 为 0.85。', warn)
        self.assertNotIn('会话预算是超出', warn)

    def test_stall_does_not_invent_idle_seconds(self):
        report = retro.render_report({'members': [{'name': '甲', 'status': 'stalled'}]})
        self.assertIn('甲', report)
        self.assertIn('停滞', report)
        self.assertNotIn('秒', report)
        self.assertNotIn('分钟', report)
        self.assertNotIn('$', report)
        alerted = retro.render_report({
            'members': [{'id': 'm1', 'name': '甲', 'status': 'stalled'}],
            'stallAlerts': {
                'thresholdSec': 600,
                'candidates': [
                    {'id': 'm1', 'name': 'secret-alert-name', 'status': 'stalled', 'idleSeconds': 900},
                    {'id': 'other', 'name': '乙', 'status': 'idle', 'idleSeconds': 100},
                ],
            },
        })
        self.assertIn('stallAlerts 记下的安静时间是 900 秒。已达到阈值 600 秒。', alerted)
        self.assertNotIn('乙', alerted)
        self.assertNotIn('secret-alert-name', alerted)
        self.assertNotIn('100 秒', alerted)

    def test_idle_without_alert_and_closed_tasks_are_omitted(self):
        idle = retro.render_report({'members': [{'name': '甲', 'status': 'idle', 'startedAt': 1767261600}]})
        self.assertIn('| 甲 | 2026-01-01 10:00:00 UTC | — | — | — | 待命 |', idle)
        self.assertNotIn('## 阻塞与停滞', idle)
        self.assertNotIn('## 改进建议', idle)
        tasks = retro.render_report({'officialTeam': {'tasks': [
            {'subject': 'Ask the synthetic lead', 'blocked': False, 'blockedBy': ['2'], 'flags': ['blocked']},
            {'subject': 'Wait here', 'blocked': True, 'waitingOn': ['SECRET_TASK']},
        ]}})
        self.assertIn('任务「Wait here」的 blocked 为 true。waitingOn 有 1 项。', tasks)
        self.assertNotIn('Ask the synthetic lead', tasks)
        self.assertNotIn('SECRET_TASK', tasks)

    def test_private_error_reason_is_not_copied(self):
        report = retro.render_report({
            'members': [{'name': '甲', 'status': 'error', 'errorReason': '/home/SECRET/err'}],
        })
        self.assertIn('甲 · 出错。', report)
        self.assertIn('快照里没有可公开的原因，这里不补一句。', report)
        self.assertNotIn('SECRET', report)
        self.assertNotIn('/home', report)

    def test_private_fields_and_paths_are_omitted(self):
        snap = json.loads(json.dumps(self.demo))
        snap['session'] = {'id': 'SECRET_SESSION', 'cwd': '/home/SECRET_PATH/proj', 'project': 'C--Users-SECRET'}
        snap['sessions'] = [{'id': 'SECRET_SESSION', 'project': 'C--Users-SECRET', 'mtime': 1767261740}]
        snap['lead']['lastUserPrompt'] = 'SECRET_PROMPT do the thing'
        snap['lead']['name'] = 'lead@example.com'
        snap['members'][0]['prompt'] = 'SECRET_PROMPT'
        snap['members'][0]['description'] = 'SECRET_DESC'
        snap['members'][0]['lastText'] = 'SECRET_TEXT'
        snap['members'][0]['id'] = 'SECRET_SESSION'
        snap['members'][0]['name'] = 'ok-name'
        snap['members'][0]['timeline'] = [{'kind': 'tool', 'label': 'Bash · SECRET_ARG', 'name': 'Bash', 'error': True}]
        snap['officialTeam']['teamName'] = 'session-SECRETSESSION'
        snap['officialTeam']['tasks'][2]['description'] = 'SECRET_DESC pending'
        snap['comms'] = [{'text': 'SECRET_MESSAGE', 'file': '/tmp/SECRET_PATH'}]
        snap['feed'] = [{'label': 'Read · SECRET_ARG', 'name': 'SECRET_NAME'}]
        snap['members'][0]['errorReason'] = 'sk-SECRETKEY leaked'
        report = retro.render_report(snap)
        for secret in (
                'SECRET', 'example.com', 'SECRET_PATH', 'SECRET_PROMPT', 'SECRET_ARG',
                'SECRET_DESC', 'SECRET_TEXT', 'SECRET_MESSAGE', 'SECRET_SESSION',
                'synthetic-session', 'synthetic-member', 'session-syntheti', '/tmp/', '/home/',
                'sk-', 'C--Users'):
            self.assertNotIn(secret, report)
        self.assertIn('ok-name', report)
        self.assertIn('会话 01', report)
        self.assertIn('队长 · 指挥部', report)
        self.assertIn('Wait for the synthetic survey', report)

    def test_non_dict_snapshot_does_not_echo_the_value(self):
        with self.assertRaises(retro.ExportError) as raised:
            retro.render_report(['SECRET'])
        self.assertNotIn('SECRET', str(raised.exception))

    def test_missing_department_is_a_suggestion_without_a_cost(self):
        report = retro.render_report({'departments': [{'id': 'docs', 'name': '文档部', 'missing': True}]})
        self.assertIn('文档部 的 missing 为 true，看板会把它显示成建议补位。这不是一条错误。', report)
        self.assertNotIn('$', report)
        self.assertNotIn('## 成本与 token', report)


class RetroReportCliTest(unittest.TestCase):
    def test_demo_prints_timeline_and_cost(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch('sys.stdout', stdout), mock.patch('sys.stderr', stderr):
            code = retro.main(['--demo'])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), '')
        report = stdout.getvalue()
        self.assertIn(TIMELINE, report)
        self.assertIn('## 成本与 token', report)
        self.assertIn('全队估算成本：$2.0392', report)

    def test_output_file_and_same_file_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'retro.md'
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch('sys.stdout', stdout), mock.patch('sys.stderr', stderr):
                code = retro.main(['--demo', '--output', str(dest)])
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn('# 团队复盘', dest.read_text(encoding='utf-8'))
            stderr = io.StringIO()
            with mock.patch('sys.stderr', stderr):
                code = retro.main(['--input', str(dest), '--output', str(dest)])
            self.assertEqual(code, 1)
            self.assertIn('output must differ', stderr.getvalue())
            self.assertNotIn('SECRET', stderr.getvalue())

    def test_invalid_json_does_not_echo_the_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'snap.json'
            path.write_text('{"SECRET": NaN}', encoding='utf-8')
            stderr = io.StringIO()
            with mock.patch('sys.stderr', stderr):
                code = retro.main(['--input', str(path)])
            self.assertEqual(code, 1)
            self.assertIn('retro report failed', stderr.getvalue())
            self.assertNotIn('SECRET', stderr.getvalue())

    def test_selectors_require_port(self):
        with self.assertRaises(SystemExit) as raised:
            with mock.patch('sys.stderr', io.StringIO()):
                retro.main(['--demo', '--session', 'abc'])
        self.assertEqual(raised.exception.code, 2)
