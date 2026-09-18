"""
看板端到端测试：用合成的 Claude Code 转录文件跑 team_board（解析 → 快照 → 成员/部门 API → HTTP 服务）。
运行：python -m unittest discover -s tests -v   （仓库根目录，标准库即可）
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def webp_sheet(w, h):
    """最小 VP8X webp 头：只有画布尺寸，够看板校验网格用。"""
    return (b'RIFF' + (30).to_bytes(4, 'little') + b'WEBPVP8X' + (10).to_bytes(4, 'little') + bytes([0x10, 0, 0, 0])
            + (w - 1).to_bytes(3, 'little') + (h - 1).to_bytes(3, 'little') + bytes(8))


def png_sheet(w, h):
    return b'\x89PNG\r\n\x1a\n' + (13).to_bytes(4, 'big') + b'IHDR' + w.to_bytes(4, 'big') + h.to_bytes(4, 'big') + bytes(5) + bytes(4)


def pet_zip(files):
    """files: {路径: bytes} → zip 字节。"""
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def jl(*entries):
    return '\n'.join(json.dumps(e, ensure_ascii=False) for e in entries) + '\n'


def asst(ts, model, content, stop='tool_use', usage=None):
    return {'type': 'assistant', 'timestamp': ts, 'message': {'role': 'assistant', 'model': model, 'stop_reason': stop,
            'content': content, 'usage': usage or {'input_tokens': 10, 'output_tokens': 50, 'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 100}}}


def user(ts, content):
    return {'type': 'user', 'timestamp': ts, 'message': {'role': 'user', 'content': content}}


class BoardEnv:
    """临时 HOME：~/.claude/projects 里放一个会话（队长 + 两名成员），~/.claude/agents 放成员定义。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='teamboard-'))
        self.home = self.tmp / 'home'
        self.claude = self.home / '.claude'
        shutil.copytree(ROOT / 'team-board', self.claude / 'team-board')
        shutil.copytree(ROOT / 'agents', self.claude / 'agents')
        proj = self.claude / 'projects' / 'C--demo'
        sess = proj / 'sess-0001'
        sub = sess / 'subagents'
        sub.mkdir(parents=True)
        team = self.tmp / 'proj' / '.team' / 'inbox'
        team.mkdir(parents=True)
        inbox = str(team / 'frontend-dev.md')
        # 队长：派两名成员、收到一份回报、给成员收件箱留言
        (proj / 'sess-0001.jsonl').write_text(jl(
            user('2026-01-01T10:00:00Z', '用 agent team 做个演示'),
            asst('2026-01-01T10:00:05Z', 'claude-opus-5', [
                {'type': 'tool_use', 'id': 'tu-a', 'name': 'Agent', 'input': {'description': '实现数据层 store.js', 'subagent_type': 'backend-dev', 'prompt': '你是研发部·阿服。读 SPEC 后实现 store.js', 'run_in_background': True}},
                {'type': 'tool_use', 'id': 'tu-b', 'name': 'Agent', 'input': {'description': '实现界面 app.js', 'subagent_type': 'frontend-dev', 'prompt': '你是研发部·小界。阿服 已交付 store.js，导出 createStore', 'run_in_background': True}},
            ]),
            user('2026-01-01T10:00:06Z', [{'type': 'tool_result', 'tool_use_id': 'tu-a', 'content': 'launched'}, {'type': 'tool_result', 'tool_use_id': 'tu-b', 'content': 'launched'}]),
            asst('2026-01-01T10:00:07Z', 'claude-opus-5', [{'type': 'tool_use', 'id': 'tu-c', 'name': 'Write', 'input': {'file_path': inbox, 'content': '## 来自队长\n接口按 SPEC'}}]),
            user('2026-01-01T10:00:08Z', [{'type': 'tool_result', 'tool_use_id': 'tu-c', 'content': 'ok'}]),
        ), encoding='utf-8')
        # 成员 a：已完成（最后一条是 end_turn 的纯文本），写了 store.js，给小界留言
        (sub / 'agent-a1.jsonl').write_text(jl(
            user('2026-01-01T10:00:10Z', '你是研发部·阿服。读 SPEC 后实现 store.js'),
            asst('2026-01-01T10:00:20Z', 'claude-sonnet-5', [{'type': 'tool_use', 'id': 'x1', 'name': 'Write', 'input': {'file_path': str(self.tmp / 'proj' / 'src' / 'store.js'), 'content': 'export function createStore(){}'}}]),
            user('2026-01-01T10:00:21Z', [{'type': 'tool_result', 'tool_use_id': 'x1', 'content': 'ok'}]),
            asst('2026-01-01T10:00:22Z', 'claude-sonnet-5', [{'type': 'tool_use', 'id': 'x2', 'name': 'Edit', 'input': {'file_path': inbox, 'old_string': '', 'new_string': '## 来自 backend-dev\ncreateStore(storage) 已导出'}}]),
            user('2026-01-01T10:00:23Z', [{'type': 'tool_result', 'tool_use_id': 'x2', 'content': 'ok'}]),
            asst('2026-01-01T10:00:30Z', 'claude-sonnet-5', [{'type': 'text', 'text': '已完成 store.js，导出 createStore。'}], stop='end_turn'),
        ), encoding='utf-8')
        (sub / 'agent-a1.meta.json').write_text(json.dumps({'agentType': 'backend-dev', 'description': '实现数据层 store.js', 'toolUseId': 'tu-a'}), encoding='utf-8')
        # 成员 b：进行中（最后一条是 tool_use），读了 a 写的文件
        (sub / 'agent-b2.jsonl').write_text(jl(
            user('2026-01-01T10:00:12Z', '你是研发部·小界。'),
            asst('2026-01-01T10:00:40Z', 'claude-sonnet-5', [{'type': 'tool_use', 'id': 'y1', 'name': 'Read', 'input': {'file_path': str(self.tmp / 'proj' / 'src' / 'store.js')}}]),
            user('2026-01-01T10:00:41Z', [{'type': 'tool_result', 'tool_use_id': 'y1', 'content': 'export ...'}]),
            asst('2026-01-01T10:00:42Z', 'claude-sonnet-5', [{'type': 'tool_use', 'id': 'y2', 'name': 'Write', 'input': {'file_path': str(self.tmp / 'proj' / 'src' / 'app.js'), 'content': '...'}}]),
        ), encoding='utf-8')
        (sub / 'agent-b2.meta.json').write_text(json.dumps({'agentType': 'frontend-dev', 'description': '实现界面 app.js', 'toolUseId': 'tu-b'}), encoding='utf-8')
        # 让 b 的文件看起来"刚刚还在写"（mtime = now），a 的文件是旧的
        os.utime(sub / 'agent-b2.jsonl', None)
        old = 1735725600  # 2025-01-01
        os.utime(sub / 'agent-a1.jsonl', (old, old))
        os.utime(proj / 'sess-0001.jsonl', (old, old))

    def load(self):
        os.environ['HOME'] = str(self.home)
        os.environ['USERPROFILE'] = str(self.home)
        spec = importlib.util.spec_from_file_location('team_board_under_test', self.claude / 'team-board' / 'team_board.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.PROJECTS == self.claude / 'projects', mod.PROJECTS
        return mod

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class SnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = BoardEnv()
        cls.tb = cls.env.load()
        cls.snap = cls.tb.build_snapshot(cls.tb.argparse.Namespace(session=None, project=None, stale=600))

    @classmethod
    def tearDownClass(cls):
        cls.env.cleanup()

    def member(self, agent_type):
        return next(m for m in self.snap['members'] if m['agentType'] == agent_type)

    def test_session_and_members(self):
        self.assertEqual(self.snap['session']['id'], 'sess-0001')
        self.assertEqual(len(self.snap['members']), 2)
        a, b = self.member('backend-dev'), self.member('frontend-dev')
        self.assertEqual(a['status'], 'completed')
        self.assertEqual(b['status'], 'running')
        self.assertEqual(a['name'], '阿服')
        self.assertEqual(b['name'], '小界')
        self.assertEqual(a['dept'], 'dev')
        self.assertEqual(a['description'], '实现数据层 store.js')
        self.assertEqual(a['dispatchedAt'], self.tb.parse_iso('2026-01-01T10:00:05Z'))

    def test_model_effort_cost_tokens(self):
        a = self.member('backend-dev')
        self.assertEqual(a['modelFamily'], 'sonnet')
        self.assertEqual(a['definedModel'], 'sonnet')
        self.assertEqual(a['effort'], 'medium')
        self.assertGreater(a['cost'], 0)
        self.assertEqual(a['usage']['out'], 150)
        self.assertEqual(a['totalTokens'], 3 * (10 + 50 + 1000 + 100))
        self.assertAlmostEqual(a['cacheHit'], 1000 / 1110, places=3)
        t = self.snap['totals']
        self.assertIn('sonnet', t['modelMix'])
        self.assertGreater(t['allUsage']['total'], t['usage']['total'])
        self.assertEqual(self.snap['lead']['modelFamily'], 'opus')
        self.assertGreater(self.snap['lead']['cost'], 0)

    def test_comms(self):
        kinds = {(c['kind'], c['fromName'], c['toName']) for c in self.snap['comms']}
        self.assertIn(('assign', '队长', '阿服'), kinds)
        self.assertIn(('assign', '队长', '小界'), kinds)
        self.assertIn(('report', '阿服', '队长'), kinds)
        self.assertIn(('peer', '阿服', '小界'), kinds)      # 写对方收件箱 = 留言
        self.assertIn(('handoff', '阿服', '小界'), kinds)   # 读对方写的文件 = 交接
        self.assertIn(('relay', '阿服', '小界'), kinds)     # 派工指令里点名"阿服 已交付" = 转达
        self.assertIn(('direct', '队长', '小界'), kinds)    # 队长写收件箱 = 私信

    def test_departments_and_gate(self):
        deps = {d['id']: d for d in self.snap['departments']}
        self.assertEqual(deps['dev']['members'].__len__(), 2)
        self.assertEqual(deps['dev']['status'], 'running')
        self.assertEqual(deps['dev']['tools'], 4)
        self.assertGreater(deps['dev']['tokens'], 0)
        # 进行中：空部门只是“按需出场”，不算缺席；needed 只是标记
        self.assertTrue(deps['qa']['needed']); self.assertFalse(deps['qa']['missing'], '进行中不该报缺席')
        self.assertFalse(deps['hq']['missing']); self.assertEqual(deps['qa']['status'], 'empty')
        self.assertEqual(self.snap['lead']['phase'], 'coordinating')

    def test_gate_only_suggests_after_finish_and_small_team_ok(self):
        """两人都完成后才提示“建议补位”；1 人的小团队一切正常、没有任何缺席。"""
        sub = self.env.claude / 'projects' / 'C--demo' / 'sess-0001' / 'subagents'
        (sub / 'agent-b2.jsonl').write_text(jl(user('2026-01-01T10:00:20Z', 'go'),
            asst('2026-01-01T10:00:21Z', 'claude-sonnet-5', [{'type': 'text', 'text': '完成'}], stop='end_turn')), encoding='utf-8')
        os.utime(sub / 'agent-b2.jsonl', (1735725600, 1735725600))   # 不是“刚刚写完”，队长早已收到
        self.tb._agent_cache.clear()
        snap = self.tb.build_snapshot(self.tb.argparse.Namespace(session=None, project=None, stale=600))
        deps = {d['id']: d for d in snap['departments']}
        self.assertEqual(snap['lead']['phase'], 'finished')
        self.assertTrue(deps['qa']['missing']); self.assertTrue(deps['docs']['missing']); self.assertFalse(deps['rnd']['missing'])
        # 只有一个人：研发部只有 1 人 → 质量部不需要；一切正常
        (sub / 'agent-b2.jsonl').unlink(); (sub / 'agent-b2.meta.json').unlink()
        self.tb._agent_cache.clear()
        snap = self.tb.build_snapshot(self.tb.argparse.Namespace(session=None, project=None, stale=600))
        self.assertEqual(len(snap['members']), 1)
        self.assertFalse(any(d['missing'] for d in snap['departments']))
        self.assertEqual(snap['totals']['members'] if 'members' in snap['totals'] else len(snap['members']), 1)
        # 临时成员：标题「研究部·xxx」→ 归研究部；「[qa] xxx」→ 质量部
        (sub / 'agent-c9.jsonl').write_text(jl(user('2026-01-01T10:00:30Z', 'go'),
            asst('2026-01-01T10:00:31Z', 'claude-haiku-4-5', [{'type': 'text', 'text': 'ok'}], stop='end_turn')), encoding='utf-8')
        (sub / 'agent-c9.meta.json').write_text(json.dumps({'agentType': 'general-purpose', 'description': '研究部·对比三种排序算法'}), encoding='utf-8')
        (sub / 'agent-d9.jsonl').write_text(jl(user('2026-01-01T10:00:30Z', 'go'),
            asst('2026-01-01T10:00:31Z', 'claude-haiku-4-5', [{'type': 'text', 'text': 'ok'}], stop='end_turn')), encoding='utf-8')
        (sub / 'agent-d9.meta.json').write_text(json.dumps({'agentType': 'general-purpose', 'description': '[qa] 抽查登录流程'}), encoding='utf-8')
        self.tb._agent_cache.clear()
        snap = self.tb.build_snapshot(self.tb.argparse.Namespace(session=None, project=None, stale=600))
        by = {m['description']: m['dept'] for m in snap['members'] if m['agentType'] == 'general-purpose'}
        self.assertEqual(by['研究部·对比三种排序算法'], 'rnd'); self.assertEqual(by['[qa] 抽查登录流程'], 'qa')
        self.assertEqual(self.tb.dept_hint('质量部：复核'), 'qa'); self.assertIsNone(self.tb.dept_hint('实现登录页'))

    def test_roster_and_pets(self):
        names = {r['name'] for r in self.snap['roster']}
        self.assertTrue({'backend-dev', 'frontend-dev', 'qa-tester', 'code-reviewer', 'researcher', 'docs-writer', 'release-ops', 'team-architect'} <= names)
        self.assertGreaterEqual(len(self.snap['pets']), 13, '仓库应自带 13 只形象')
        self.assertTrue({'dada-code', 'pip', 'cubo', 'drip', 'mush', 'kit', 'spark', 'bolt', 'puff', 'tank'} <= set(self.snap['pets']))


class UsageAndPricingTest(unittest.TestCase):
    def setUp(self):
        self.env = BoardEnv(); self.tb = self.env.load()

    def tearDown(self):
        self.env.cleanup()

    def test_usage_deduped_by_message_id(self):
        """Claude Code 把一条 API 消息写成多行（每个 content block 一行，usage 是快照），只能计一次、取最后一份。"""
        sub = self.env.claude / 'projects' / 'C--demo' / 'sess-0001' / 'subagents'
        def a(ts, out, mid):
            e = asst(ts, 'claude-haiku-4-5', [{'type': 'text', 'text': 'x'}], stop='end_turn',
                     usage={'input_tokens': 10, 'output_tokens': out, 'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 0})
            e['message']['id'] = mid; return e
        (sub / 'agent-c3.jsonl').write_text(jl(user('2026-01-01T11:00:00Z', 'hi'),
            a('2026-01-01T11:00:01Z', 1, 'msg_1'), a('2026-01-01T11:00:01Z', 1, 'msg_1'), a('2026-01-01T11:00:02Z', 300, 'msg_1'),
            a('2026-01-01T11:00:05Z', 2, 'msg_2'), a('2026-01-01T11:00:06Z', 500, 'msg_2')), encoding='utf-8')
        (sub / 'agent-c3.meta.json').write_text(json.dumps({'agentType': 'researcher', 'description': 'dedupe'}), encoding='utf-8')
        snap = self.tb.build_snapshot(self.tb.argparse.Namespace(session=None, project=None, stale=600))
        m = next(x for x in snap['members'] if x['agentType'] == 'researcher')
        self.assertEqual(m['usage'], {'in': 20, 'out': 800, 'cache_read': 2000, 'cache_write': 0})
        self.assertEqual(m['totalTokens'], 2820)
        self.assertEqual(m['modelFamily'], 'haiku')

    def test_synthetic_model_does_not_override_real_one(self):
        """Claude Code 本地合成的消息 model 是 <synthetic>（中断/错误回执），不能覆盖成员真实模型，也不该算成“未定价”。"""
        sub = self.env.claude / 'projects' / 'C--demo' / 'sess-0001' / 'subagents'
        (sub / 'agent-e5.jsonl').write_text(jl(user('2026-01-01T11:00:00Z', 'hi'),
            asst('2026-01-01T11:00:01Z', 'claude-sonnet-5', [{'type': 'text', 'text': 'x'}]),
            asst('2026-01-01T11:00:02Z', '<synthetic>', [{'type': 'text', 'text': '[Request interrupted]'}], stop='end_turn',
                 usage={'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0})), encoding='utf-8')
        (sub / 'agent-e5.meta.json').write_text(json.dumps({'agentType': 'qa-tester', 'description': 'synthetic'}), encoding='utf-8')
        snap = self.tb.build_snapshot(self.tb.argparse.Namespace(session=None, project=None, stale=600))
        m = next(x for x in snap['members'] if x['agentType'] == 'qa-tester')
        self.assertEqual(m['model'], 'claude-sonnet-5'); self.assertIsNotNone(m['cost'])
        self.assertNotIn('<synthetic>', snap['totals']['unpricedModels'])

    def test_third_party_model_unpriced_until_configured(self):
        sub = self.env.claude / 'projects' / 'C--demo' / 'sess-0001' / 'subagents'
        (sub / 'agent-d4.jsonl').write_text(jl(user('2026-01-01T11:00:00Z', 'hi'),
            asst('2026-01-01T11:00:01Z', 'yi-lightning-2', [{'type': 'text', 'text': 'x'}], stop='end_turn')), encoding='utf-8')
        (sub / 'agent-d4.meta.json').write_text(json.dumps({'agentType': 'docs-writer', 'description': 'third party'}), encoding='utf-8')
        snap = self.tb.build_snapshot(self.tb.argparse.Namespace(session=None, project=None, stale=600))
        m = next(x for x in snap['members'] if x['agentType'] == 'docs-writer')
        self.assertIsNone(m['cost']); self.assertEqual(m['modelFamily'], 'yi')
        self.assertEqual(snap['totals']['unpriced'], 1); self.assertIn('yi-lightning-2', snap['totals']['unpricedModels'])
        # 内置了常见第三方价格：DeepSeek / OpenAI / Gemini / Qwen / GLM / Kimi / MiniMax
        for mid in ('deepseek-v4-flash-vision-exp', 'gpt-5.6-terra', 'gemini-2.5-flash', 'qwen3-max', 'glm-5', 'kimi-k2.5', 'minimax-m2.7',
                    'deepseek-chat', 'deepseek-reasoner', 'glm-4.6', 'kimi-k2-0905', 'grok-code-fast-1', 'openai/gpt-4.1-mini', 'meta-llama/llama-3.3-70b', 'MiniMax-M2'):
            self.assertIsNotNone(self.tb.price_for(mid), mid)
        self.assertEqual(self.tb.price_key('deepseek-v4-flash-vision-exp'), 'deepseek-v4-flash')
        # 配置价格后（按前缀匹配）即计价
        self.tb.TEAM_CFG['pricing_usd_per_mtok']['yi'] = {'in': 0.14, 'out': 0.28, 'cache_read': 0.014, 'cache_write': 0.14}
        self.tb._agent_cache.clear()
        snap = self.tb.build_snapshot(self.tb.argparse.Namespace(session=None, project=None, stale=600))
        m = next(x for x in snap['members'] if x['agentType'] == 'docs-writer')
        self.assertIsNotNone(m['cost']); self.assertEqual(snap['totals']['unpriced'], 0)
        # 更长的 key 优先
        self.tb.TEAM_CFG['pricing_usd_per_mtok']['deepseek-v4-flash'] = {'in': 1, 'out': 1, 'cache_read': 1, 'cache_write': 1}
        self.assertEqual(self.tb.price_for('deepseek-v4-flash-vision-exp')['in'], 1)
        self.assertEqual(self.tb.price_for('claude-opus-5-20260101')['in'], self.tb.TEAM_CFG['pricing_usd_per_mtok']['opus']['in'])


    def test_third_party_api_transcript(self):
        """第三方 API（ANTHROPIC_BASE_URL 转发）：模型 id 带 provider 前缀、消息没有 id 只有 requestId、没有缓存字段。"""
        sub = self.env.claude / 'projects' / 'C--demo' / 'sess-0001' / 'subagents'
        def a(ts, out, rid):
            e = asst(ts, 'deepseek/deepseek-chat', [{'type': 'text', 'text': 'x'}], stop='end_turn', usage={'input_tokens': 100, 'output_tokens': out})
            e['requestId'] = rid; return e
        (sub / 'agent-f6.jsonl').write_text(jl(user('2026-01-01T11:00:00Z', 'hi'),
            a('2026-01-01T11:00:01Z', 5, 'req_1'), a('2026-01-01T11:00:01Z', 40, 'req_1'), a('2026-01-01T11:00:03Z', 60, 'req_2')), encoding='utf-8')
        (sub / 'agent-f6.meta.json').write_text(json.dumps({'agentType': 'researcher', 'description': '第三方'}), encoding='utf-8')
        snap = self.tb.build_snapshot(self.tb.argparse.Namespace(session=None, project=None, stale=600))
        m = next(x for x in snap['members'] if x['agentType'] == 'researcher')
        self.assertEqual(m['usage'], {'in': 200, 'out': 100, 'cache_read': 0, 'cache_write': 0}, '同一 requestId 只计一次')
        self.assertEqual(m['modelFamily'], 'deepseek'); self.assertEqual(m['priceKey'], 'deepseek-chat'); self.assertIsNotNone(m['cost'])
        self.assertEqual(self.tb.model_family('openai/gpt-5'), 'gpt'); self.assertEqual(self.tb.model_family('anthropic/claude-sonnet-4-5'), 'sonnet')
        # 成员定义可以填第三方模型 id
        out = self.tb.write_agent({'name': 'ds-coder', 'description': '用 DeepSeek 写代码的成员，测试用', 'model': 'openai/gpt-5', 'dept': 'dev'})
        self.assertEqual(out['model'], 'openai/gpt-5')
        with self.assertRaises(ValueError): self.tb.write_agent({'name': 'ds-coder', 'description': '用 DeepSeek 写代码的成员，测试用', 'model': 'bad model!'})


class AgentApiTest(unittest.TestCase):
    def setUp(self):
        self.env = BoardEnv(); self.tb = self.env.load()

    def tearDown(self):
        self.env.cleanup()

    def test_write_read_delete_agent(self):
        out = self.tb.write_agent({'name': 'sec-reviewer', 'display': '小审', 'role': '安全审查员', 'dept': 'qa', 'model': 'opus', 'effort': 'high',
                                   'pet': 'kit', 'color': 'purple', 'description': '安全审查员，改动涉及登录权限时使用', 'skills': 'a, b', 'body': '你是小审。'})
        f = self.env.claude / 'agents' / 'sec-reviewer.md'
        self.assertTrue(f.exists())
        text = f.read_text(encoding='utf-8')
        for line in ('name: sec-reviewer', 'model: opus', 'effort: high', 'color: purple', '  - a', '  - b', '你是小审。'):
            self.assertIn(line, text)
        self.assertEqual(out['dept'], 'qa'); self.assertEqual(out['pet'], 'kit'); self.assertFalse(out['builtin'])
        self.assertEqual(self.tb.read_agent('sec-reviewer')['skills'], ['a', 'b'])
        snap = self.tb.build_snapshot(self.tb.argparse.Namespace(session=None, project=None, stale=600))
        r = next(x for x in snap['roster'] if x['name'] == 'sec-reviewer')
        self.assertTrue(r['custom']); self.assertEqual(r['display'], '小审'); self.assertEqual(r['pet'], 'kit')
        self.tb.delete_agent('sec-reviewer')
        self.assertFalse(f.exists()); self.assertNotIn('sec-reviewer', self.tb.TEAM_CFG['agents'])

    def test_validation(self):
        bad = [({'name': 'Bad Name', 'description': 'x' * 20}, '标识'), ({'name': 'ok-name', 'description': '短'}, '简介'),
               ({'name': 'ok-name', 'description': 'x' * 20, 'model': 'gpt-9!'}, '模型'), ({'name': 'ok-name', 'description': 'x' * 20, 'effort': 'ultra'}, '思考强度'),
               ({'name': 'ok-name', 'description': 'x' * 20, 'dept': 'nope'}, '部门')]
        for payload, word in bad:
            with self.assertRaises(ValueError, msg=str(payload)) as cm:
                self.tb.write_agent(payload)
            self.assertIn(word, str(cm.exception))
        with self.assertRaises(ValueError):
            self.tb.delete_agent('does-not-exist')

    def test_departments(self):
        self.tb.write_department({'id': 'design', 'name': '设计部', 'icon': '🎨', 'required': 'optional'})
        self.assertIn('design', [d['id'] for d in self.tb.TEAM_CFG['departments']])
        with self.assertRaises(ValueError):
            self.tb.delete_department('hq')
        with self.assertRaises(ValueError):
            self.tb.delete_department('qa')  # 有成员
        with self.assertRaises(ValueError):
            self.tb.write_department({'id': 'Bad', 'name': 'x'})
        with self.assertRaises(ValueError):
            self.tb.write_department({'id': 'x', 'name': 'x', 'required': 'sometimes'})
        self.tb.delete_department('design')
        self.assertNotIn('design', [d['id'] for d in self.tb.TEAM_CFG['departments']])
        cfg = json.loads((self.env.claude / 'team-board' / 'team.json').read_text(encoding='utf-8'))
        self.assertNotIn('design', [d['id'] for d in cfg['departments']])

    def test_edit_keeps_unmanaged_frontmatter(self):
        """改形象 / 改简介不能把 tools、permissionMode、maxTurns 这类表单不管理的字段弄丢。"""
        f = self.env.claude / 'agents' / 'backend-dev.md'
        text = f.read_text(encoding='utf-8')
        text = text.replace('\n---\n', '\nmaxTurns: 40\ntools:\n  - Read\n  - Bash\npermissionMode: default\nmemory: project\n---\n', 1)
        f.write_text(text, encoding='utf-8')
        a = self.tb.read_agent('backend-dev')
        self.tb.write_agent({**a, 'pet': 'kit', 'skills': ', '.join(a['skills'])})
        out = f.read_text(encoding='utf-8')
        for line in ('maxTurns: 40', 'tools:', '  - Read', '  - Bash', 'permissionMode: default', 'memory: project'):
            self.assertIn(line, out, line)
        self.assertEqual(out.count('memory:'), 1)
        self.assertEqual(self.tb.read_agent('backend-dev')['pet'], 'kit')
        self.assertIn(a['body'][:40], out)
        # 会话里正在出场的成员也立刻带上新形象（看板每次刷新按它换图）
        snap = self.tb.build_snapshot(self.tb.argparse.Namespace(session=None, project=None, stale=600))
        self.assertEqual(next(m for m in snap['members'] if m['agentType'] == 'backend-dev')['pet'], 'kit')


class PetApiTest(unittest.TestCase):
    """导入 / 删除桌宠形象：裸 webp、png、Codex/petdex zip 包、缩放过的雪碧图；坏尺寸与自带形象要拒绝。"""

    def setUp(self):
        self.env = BoardEnv(); self.tb = self.env.load()

    def tearDown(self):
        self.env.cleanup()

    def b64(self, data):
        import base64
        return base64.b64encode(data).decode()

    def test_import_variants(self):
        tb = self.tb
        r = tb.write_pet({'id': 'My Cat', 'filename': 'my-cat-spritesheet.webp', 'data': self.b64(webp_sheet(1536, 1872))})
        self.assertEqual((r['id'], r['file'], r['rows']), ('my-cat', 'my-cat.webp', 9))
        r = tb.write_pet({'filename': 'Dog Sprite.png', 'data': 'data:image/png;base64,' + self.b64(png_sheet(1536, 2288))})
        self.assertEqual((r['id'], r['file'], r['rows']), ('dog', 'dog.png', 11))
        # petdex / Codex zip：pet.json 大写 id、子目录、__MACOSX 垃圾、spritesheetPath 指定文件
        z = pet_zip({'Ovsyankin/pet.json': json.dumps({'id': 'Ovsyankin', 'displayName': 'Овсянкин', 'spriteVersionNumber': 2, 'spritesheetPath': 'spritesheet.webp'}),
                     'Ovsyankin/spritesheet.webp': webp_sheet(1536, 2288), 'Ovsyankin/preview.png': png_sheet(300, 300),
                     '__MACOSX/Ovsyankin/._spritesheet.webp': b'junk'})
        r = tb.write_pet({'filename': 'ovsyankin-c6e8.zip', 'data': self.b64(z)})
        self.assertEqual((r['id'], r['file'], r['rows'], r['displayName']), ('ovsyankin', 'ovsyankin.webp', 11, 'Овсянкин'))
        # pet.json 没有 id：用 displayName
        z = pet_zip({'pet.json': json.dumps({'displayName': 'ZhiZhi', 'spritesheetPath': 'spritesheet.png'}), 'spritesheet.png': png_sheet(1536, 1872)})
        r = tb.write_pet({'filename': 'pet-package-b7d8.zip', 'data': self.b64(z)})
        self.assertEqual((r['id'], r['file']), ('zhizhi', 'zhizhi.png'))
        # 等比缩放过的雪碧图也接受
        r = tb.write_pet({'id': 'small', 'data': self.b64(webp_sheet(768, 936))}); self.assertEqual(r['rows'], 9)
        info = tb.pet_info()
        self.assertEqual(info['small']['cw'], 96); self.assertEqual(info['small']['ch'], 104)
        self.assertEqual(info['ovsyankin']['displayName'], 'Овсянкин'); self.assertFalse(info['ovsyankin']['shipped']); self.assertTrue(info['pip']['shipped'])
        self.assertTrue((self.env.claude / 'team-board' / 'sprites' / 'ovsyankin.json').exists(), '边车记录名字与出处')
        # 覆盖：同名 png → webp 时旧 png 要清掉
        with self.assertRaises(ValueError): tb.write_pet({'id': 'dog', 'data': self.b64(webp_sheet(1536, 1872))})
        r = tb.write_pet({'id': 'dog', 'data': self.b64(webp_sheet(1536, 1872)), 'overwrite': True})
        self.assertEqual(r['file'], 'dog.webp'); self.assertFalse((self.env.claude / 'team-board' / 'sprites' / 'dog.png').exists())
        snap = tb.build_snapshot(tb.argparse.Namespace(session=None, project=None, stale=600))
        self.assertTrue({'my-cat', 'dog', 'ovsyankin', 'zhizhi', 'small'} <= set(snap['pets']))
        self.assertEqual(snap['petInfo']['dog']['file'], 'dog.webp')

    def test_reject_and_delete(self):
        tb = self.tb
        bad = [({'id': 'x', 'data': self.b64(webp_sheet(1000, 1000))}, '尺寸'), ({'id': 'x', 'data': self.b64(webp_sheet(1536, 1664))}, '尺寸'),
               ({'id': 'x', 'data': self.b64(b'GIF89a' + bytes(40))}, '只支持'), ({'id': 'pip', 'data': self.b64(webp_sheet(1536, 1872))}, '自带'),
               ({'id': 'x', 'data': self.b64(pet_zip({'readme.txt': b'hi'}))}, 'zip'), ({'id': 'x', 'data': '!!!'}, '')]
        for payload, word in bad:
            with self.assertRaises(ValueError, msg=str(payload)[:60]) as cm:
                tb.write_pet(payload)
            self.assertIn(word, str(cm.exception))
        tb.write_pet({'id': 'tmp-pet', 'data': self.b64(webp_sheet(1536, 1872))})
        tb.write_agent({'name': 'pet-user', 'description': '用 tmp-pet 形象的成员，测试用', 'pet': 'tmp-pet'})
        with self.assertRaises(ValueError) as cm: tb.delete_pet('tmp-pet')
        self.assertIn('pet-user', str(cm.exception))
        with self.assertRaises(ValueError): tb.delete_pet('pip')
        tb.write_agent({'name': 'pet-user', 'description': '用 tmp-pet 形象的成员，测试用', 'pet': ''})
        tb.delete_pet('tmp-pet')
        self.assertNotIn('tmp-pet', tb.pet_list())
        self.assertFalse((self.env.claude / 'team-board' / 'sprites' / 'tmp-pet.json').exists())

class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = BoardEnv(); cls.tb = cls.env.load()
        cls.tb.Handler.args = cls.tb.argparse.Namespace(session=None, project=None, stale=600)
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), cls.tb.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.env.cleanup()

    def get(self, path):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}') as r:
            return r.status, r.headers, r.read()

    def post(self, path, obj):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=json.dumps(obj).encode('utf-8'), headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_pages_and_json(self):
        st, hdr, body = self.get('/')
        self.assertEqual(st, 200); self.assertIn(b'Agent Team', body); self.assertIn('no-store', hdr['Cache-Control'])
        st, _, body = self.get('/snapshot.json'); self.assertEqual(st, 200)
        snap = json.loads(body); self.assertEqual(len(snap['members']), 2)
        st, _, body = self.get('/api/agents'); self.assertEqual(st, 200)
        self.assertEqual(len(json.loads(body)['agents']), 8)
        for f in ('/favicon.png', '/sprites/dada-code.webp', '/sprites/pip.webp'):
            self.assertEqual(self.get(f)[0], 200, f)

    def test_agent_roundtrip_over_http(self):
        st, r = self.post('/api/agents', {'name': 'http-agent', 'description': '通过 HTTP 创建的成员，用于测试', 'dept': 'rnd', 'model': 'haiku', 'effort': 'low'})
        self.assertEqual(st, 200); self.assertTrue(r['ok'])
        self.assertTrue((self.env.claude / 'agents' / 'http-agent.md').exists())
        st, r = self.post('/api/agents', {'name': 'bad name', 'description': 'xxxxxxxxxxxx'})
        self.assertEqual(st, 400); self.assertIn('error', r)
        st, r = self.post('/api/agents/delete', {'name': 'http-agent'}); self.assertEqual(st, 200)
        st, r = self.post('/api/departments/delete', {'id': 'hq'}); self.assertEqual(st, 400)
        st, _, body = self.get('/api/pets'); self.assertEqual(st, 200); self.assertIn('pip', json.loads(body)['pets'])
        st, r = self.post('/api/pets', {'id': 'http-pet', 'data': __import__('base64').b64encode(webp_sheet(1536, 1872)).decode()})
        self.assertEqual(st, 200, r); self.assertEqual(r['pet']['id'], 'http-pet'); self.assertIn('http-pet', r['pets'])
        self.assertEqual(self.get('/sprites/http-pet.webp')[0], 200)
        st, r = self.post('/api/pets', {'id': 'http-pet', 'data': 'AAAA'}); self.assertEqual(st, 400)
        st, r = self.post('/api/pets/delete', {'id': 'http-pet'}); self.assertEqual(st, 200); self.assertNotIn('http-pet', r['pets'])


class CustomPetImportTest(unittest.TestCase):
    """import_codex_pets.py 也要把用户在 Codex 里自制的宠物（~/.codex/pets/<id>/spritesheet.webp）搬进 sprites/。"""

    def test_custom_pets_from_codex_home(self):
        import subprocess, sys
        tmp = Path(tempfile.mkdtemp(prefix='petimport-'))
        try:
            sheet = webp_sheet
            good = tmp / 'codex' / 'pets' / 'my-pet'; good.mkdir(parents=True)
            (good / 'pet.json').write_text(json.dumps({'id': 'my-pet', 'displayName': 'My Pet'}), encoding='utf-8')
            (good / 'spritesheet.webp').write_bytes(sheet(1536, 1872))
            bad = tmp / 'codex' / 'pets' / 'odd'; bad.mkdir()
            (bad / 'spritesheet.webp').write_bytes(sheet(1000, 1000))
            out = tmp / 'out'
            r = subprocess.run([sys.executable, str(ROOT / 'team-board' / 'import_codex_pets.py'), '/nonexistent/app.asar', '--out', str(out)],
                               env={**os.environ, 'CODEX_HOME': str(tmp / 'codex'), 'PYTHONIOENCODING': 'utf-8'},
                               capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((out / 'my-pet.webp').exists())
            self.assertFalse((out / 'odd.webp').exists(), '尺寸不对的雪碧图要跳过')
            self.assertIn('my-pet', r.stdout); self.assertIn('My Pet', r.stdout)
            # ~/.petdex/pets/<slug>/ 与直接传 zip 路径
            pd = tmp / 'petdex' / 'pets' / 'boba'; pd.mkdir(parents=True)
            (pd / 'pet.json').write_text(json.dumps({'id': 'boba', 'displayName': 'Boba', 'spritesheetPath': 'spritesheet.webp'}), encoding='utf-8')
            (pd / 'spritesheet.webp').write_bytes(webp_sheet(1536, 2288))
            zp = tmp / 'zhizhi.zip'; zp.write_bytes(pet_zip({'pet.json': json.dumps({'displayName': 'ZhiZhi', 'spritesheetPath': 'spritesheet.png'}), 'spritesheet.png': png_sheet(1536, 1872)}))
            env = {**os.environ, 'CODEX_HOME': str(tmp / 'codex'), 'PETDEX_HOME': str(tmp / 'petdex'), 'PYTHONIOENCODING': 'utf-8'}
            r = subprocess.run([sys.executable, str(ROOT / 'team-board' / 'import_codex_pets.py'), '/nonexistent/app.asar', '--out', str(out)], env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertTrue((out / 'boba.webp').exists()); self.assertTrue((out / 'boba.json').exists())
            r = subprocess.run([sys.executable, str(ROOT / 'team-board' / 'import_codex_pets.py'), str(zp), '--out', str(out)], env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertTrue((out / 'zhizhi.png').exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class UpdateTest(unittest.TestCase):
    def test_check_and_apply_from_local_zip(self):
        import subprocess, sys, zipfile
        tmp = Path(tempfile.mkdtemp(prefix='teamupd-'))
        try:
            (tmp / 'VERSION').write_text('9.9.9\n', encoding='utf-8')
            env = {**os.environ, 'TEAM_UPDATE_VERSION_URL': (tmp / 'VERSION').as_uri(), 'PYTHONIOENCODING': 'utf-8'}
            r = subprocess.run([sys.executable, str(ROOT / 'team-board' / 'update.py'), '--json'], env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(r.returncode, 0, r.stderr); info = json.loads(r.stdout)
            self.assertEqual(info['remote'], '9.9.9'); self.assertTrue(info['hasUpdate'])
            (tmp / 'VERSION').write_text('0.0.1\n', encoding='utf-8')
            r = subprocess.run([sys.executable, str(ROOT / 'team-board' / 'update.py'), '--json'], env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertFalse(json.loads(r.stdout)['hasUpdate'])
            # --apply：把当前仓库打成 zip 当“GitHub 最新版”，装进临时 HOME
            zp = tmp / 'main.zip'
            with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as z:
                for f in ROOT.rglob('*'):
                    if f.is_file() and '.git' not in f.parts and '__pycache__' not in f.parts:
                        z.write(f, 'claude-agent-team-main/' + f.relative_to(ROOT).as_posix())
            home = tmp / 'home'; (home / '.claude').mkdir(parents=True)
            env.update({'HOME': str(home), 'USERPROFILE': str(home), 'TEAM_UPDATE_ZIP_URL': zp.as_uri()})
            r = subprocess.run([sys.executable, str(ROOT / 'team-board' / 'update.py'), '--apply'], env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=str(tmp))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((home / '.claude' / 'team-board' / 'team_board.py').exists())
            self.assertEqual((home / '.claude' / 'team-board' / 'VERSION').read_text(encoding='utf-8').strip(), (ROOT / 'VERSION').read_text(encoding='utf-8').strip())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FitPetTest(unittest.TestCase):
    """fit_pet.py：识别主体、按行归一倍率；有 Pillow 时再验证真的改图 + 备份 + 还原。"""

    def setUp(self):
        spec = importlib.util.spec_from_file_location('fit_pet', ROOT / 'team-board' / 'fit_pet.py')
        self.fp = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.fp)

    def test_finish_rows_rules(self):
        fp = self.fp
        # 各行质量一样、站满格子 → 全 1（Codex 自带角色的情况）
        rows = [{'h': 170, 'w': 140, 'mass': 18000, 'cropped': False} for _ in range(9)]
        self.assertEqual([r['k'] for r in fp.finish_rows(rows, 8, 208)['rows']], [1.0] * 9)
        # idle 只有跑动行 70% 的质量 → 按 sqrt 归一 ≈ 1.2；被裁的行只用全局倍率（这里为 1）；趴下但质量相同的行不拉高
        rows = [{'h': 158, 'w': 186, 'mass': 14000, 'cropped': False}, {'h': 190, 'w': 155, 'mass': 20000, 'cropped': False},
                {'h': 136, 'w': 123, 'mass': 12000, 'cropped': True}, {'h': 96, 'w': 150, 'mass': 20000, 'cropped': False}]
        ks = [r['k'] for r in fp.finish_rows(rows, 8, 208)['rows']]
        self.assertAlmostEqual(ks[0], (20000 / 14000) ** 0.5, places=2); self.assertEqual(ks[1], 1.0); self.assertEqual(ks[2], 1.0); self.assertEqual(ks[3], 1.0)
        # 整体偏小（最壮的行只有 100px 高）→ 全局倍率 = 0.82*208/100，且不超过 KMAX
        rows = [{'h': 100, 'w': 80, 'mass': 5000, 'cropped': False}, {'h': 100, 'w': 80, 'mass': 5000, 'cropped': True}]
        ks = [r['k'] for r in fp.finish_rows(rows, 8, 208)['rows']]
        self.assertAlmostEqual(ks[0], 0.82 * 208 / 100, places=2); self.assertEqual(ks[0], ks[1])
        # 行间放大封顶 KROW
        rows = [{'h': 60, 'w': 60, 'mass': 2000, 'cropped': False}, {'h': 190, 'w': 150, 'mass': 20000, 'cropped': False}]
        self.assertEqual(fp.finish_rows(rows, 8, 208)['rows'][0]['k'], fp.KROW)

    def test_fit_file_with_pillow(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('没有 Pillow')
        fp = self.fp
        tmp = Path(tempfile.mkdtemp(prefix='fitpet-'))
        try:
            # 合成一张 8×9 雪碧图：每格一个 40×60 的小方块角色（远小于格子）→ 应被放大到约 82% 格高
            sheet = Image.new('RGBA', (1536, 1872), (0, 0, 0, 0))
            for r in range(9):
                for c in range(8):
                    box = Image.new('RGBA', (40, 60), (200, 80, 80, 255))
                    sheet.alpha_composite(box, (c * 192 + 76, r * 208 + 140))
            f = tmp / 'tiny.png'; sheet.save(f)
            res = fp.fit_file(f)
            self.assertTrue(res['changed']); self.assertTrue((tmp / 'tiny.orig.png').exists())
            self.assertTrue(all(k > 1 for k in res['inImage']))
            info = fp.analyze(Image.open(f))
            self.assertGreaterEqual(info['rows'][0]['h'], int(0.82 * 208) - 2, '放大后主体高度接近 82% 格高')
            self.assertEqual(info['rows'][0]['boxes'][0][1][3], 199, '脚底基线不动（原来离底 8px）')
            self.assertTrue(all(r['k'] == 1.0 for r in info['rows']), '再跑一遍不需要再放大')
            side = json.loads((tmp / 'tiny.json').read_text(encoding='utf-8'))
            self.assertNotIn('fitRows', side, '图内已放大到位，不需要屏幕再放大')
            self.assertTrue(fp.restore_file(f)); self.assertFalse((tmp / 'tiny.orig.png').exists())
            self.assertEqual(fp.analyze(Image.open(f))['rows'][0]['h'], 60)
            # 翅膀太宽放不下：图不改、写 fitRows 让看板放大
            wide = Image.new('RGBA', (1536, 1872), (0, 0, 0, 0))
            for r in range(9):
                for c in range(8):
                    wide.alpha_composite(Image.new('RGBA', (186, 100), (80, 80, 200, 255)), (c * 192 + 3, r * 208 + 100))
            g = tmp / 'wide.png'; wide.save(g)
            res = fp.fit_file(g)
            self.assertFalse(res['changed']); self.assertTrue(all(k > 1 for k in res['onScreen']))
            self.assertIn('fitRows', json.loads((tmp / 'wide.json').read_text(encoding='utf-8')))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
