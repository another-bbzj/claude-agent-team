"""
消息总线（bus）+ 背景板（backdrop）+ msg.py CLI 测试。
运行：python -m unittest discover -s tests -v   （仓库根目录，标准库即可）

v1.4.1：内置立绘预设已移除，不再联网；背景板换图走 data/path/wallpaper/desktop。
"""
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机服务：不走环境 / 系统代理
PNG_1PX = b'\x89PNG\r\n\x1a\n' + (13).to_bytes(4, 'big') + b'IHDR' + (2).to_bytes(4, 'big') + (2).to_bytes(4, 'big') + bytes(5) + bytes(4)


def jl(*entries):
    return '\n'.join(json.dumps(e, ensure_ascii=False) for e in entries) + '\n'


def asst(ts, content, cwd=None, model='claude-sonnet-5'):
    e = {'type': 'assistant', 'timestamp': ts, 'message': {'role': 'assistant', 'model': model, 'stop_reason': 'tool_use',
         'content': content, 'usage': {'input_tokens': 5, 'output_tokens': 5}}}
    if cwd:
        e['cwd'] = cwd
    return e


def user(ts, content, cwd=None):
    e = {'type': 'user', 'timestamp': ts, 'message': {'role': 'user', 'content': content}}
    if cwd:
        e['cwd'] = cwd
    return e


class BusEnv:
    """临时 HOME + 临时项目目录：一个会话（队长 + backend-dev + frontend-dev），项目目录下有 .team/inbox/。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='teambus-'))
        self.home = self.tmp / 'home'
        self.claude = self.home / '.claude'
        shutil.copytree(ROOT / 'team-board', self.claude / 'team-board', ignore=shutil.ignore_patterns('bus', 'backdrops'))   # 本机数据不带进测试
        shutil.copytree(ROOT / 'agents', self.claude / 'agents')
        self.proj_dir = self.tmp / 'proj'
        (self.proj_dir / '.team' / 'inbox').mkdir(parents=True)
        # 没有 .team/inbox 的“另一个项目”，用于测试离线退化找不到目录的分支
        self.no_team_dir = self.tmp / 'proj-no-team'
        self.no_team_dir.mkdir(parents=True)

        spec = importlib.util.spec_from_file_location('tb_key_probe', ROOT / 'team-board' / 'team_board.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        self.project_key_fn = probe.project_key
        self.proj_key = probe.project_key(str(self.proj_dir))

        proj = self.claude / 'projects' / self.proj_key
        sess = proj / 'sess-0001'
        sub = sess / 'subagents'
        sub.mkdir(parents=True)
        cwd = str(self.proj_dir)
        (proj / 'sess-0001.jsonl').write_text(jl(
            user('2026-01-01T10:00:00Z', '用 agent team 做个演示', cwd=cwd),
            asst('2026-01-01T10:00:05Z', [
                {'type': 'tool_use', 'id': 'tu-a', 'name': 'Agent', 'input': {'description': '实现数据层', 'subagent_type': 'backend-dev', 'prompt': '实现', 'run_in_background': True}},
                {'type': 'tool_use', 'id': 'tu-b', 'name': 'Agent', 'input': {'description': '实现界面', 'subagent_type': 'frontend-dev', 'prompt': '实现', 'run_in_background': True}},
            ], model='claude-opus-5'),
            user('2026-01-01T10:00:06Z', [{'type': 'tool_result', 'tool_use_id': 'tu-a', 'content': 'launched'}, {'type': 'tool_result', 'tool_use_id': 'tu-b', 'content': 'launched'}]),
        ), encoding='utf-8')
        (sub / 'agent-a1.jsonl').write_text(jl(
            user('2026-01-01T10:00:10Z', '实现'),
            asst('2026-01-01T10:00:30Z', [{'type': 'text', 'text': '完成。'}]),
        ), encoding='utf-8')
        (sub / 'agent-a1.meta.json').write_text(json.dumps({'agentType': 'backend-dev', 'description': '实现数据层', 'toolUseId': 'tu-a'}), encoding='utf-8')
        (sub / 'agent-b2.jsonl').write_text(jl(
            user('2026-01-01T10:00:12Z', '实现'),
            asst('2026-01-01T10:00:40Z', [{'type': 'tool_use', 'id': 'y1', 'name': 'Read', 'input': {'file_path': 'x'}}]),
        ), encoding='utf-8')
        (sub / 'agent-b2.meta.json').write_text(json.dumps({'agentType': 'frontend-dev', 'description': '实现界面', 'toolUseId': 'tu-b'}), encoding='utf-8')
        os.utime(sub / 'agent-b2.jsonl', None)

    def load(self):
        os.environ['HOME'] = str(self.home)
        os.environ['USERPROFILE'] = str(self.home)
        spec = importlib.util.spec_from_file_location('team_board_bus_test', self.claude / 'team-board' / 'team_board.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.PROJECTS == self.claude / 'projects', mod.PROJECTS
        return mod

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class BusCoreTest(unittest.TestCase):
    """不起 HTTP 服务，直接调用模块函数：同义词规范化、send/inbox 游标、peek/all、400 校验。"""

    @classmethod
    def setUpClass(cls):
        cls.env = BusEnv()
        cls.tb = cls.env.load()

    @classmethod
    def tearDownClass(cls):
        cls.env.cleanup()

    def setUp(self):
        # 每个测试用独立的 bus/ 目录 + 清空内存缓存，测试之间不互相污染游标 / 消息
        self.tb.BUS = self.env.tmp / f'bus-{self._testMethodName}'
        self.tb._bus_cache = {'key': None, 'msgs': []}

    def test_bus_party_synonyms(self):
        tb = self.tb
        for s in ('lead', '__lead', '队长', 'main'):
            self.assertEqual(tb.bus_party(s), 'lead')
        for s in ('user', '__user', '你'):
            self.assertEqual(tb.bus_party(s), 'user')
        for s in ('all', '*', '__all', '全员'):
            self.assertEqual(tb.bus_party(s), 'all')
        self.assertEqual(tb.bus_party('backend-dev'), 'backend-dev')
        self.assertEqual(tb.bus_party('  spaced\tname  '), 'spaced name')

    def test_send_validation_errors(self):
        tb = self.tb
        with self.assertRaises(ValueError):
            tb.bus_send({'to': 'frontend-dev', 'text': ''})
        with self.assertRaises(ValueError):
            tb.bus_send({'to': '', 'text': 'hi'})
        with self.assertRaises(ValueError):
            tb.bus_send({'to': 'frontend-dev', 'text': 'x' * 4001})

    def test_send_inbox_cursor_peek_all(self):
        tb = self.tb
        out = tb.bus_send({'from': 'backend-dev', 'to': 'frontend-dev', 'text': '第一条'})
        self.assertTrue(out['ok'])
        self.assertTrue(out['message']['id'].startswith('m-'))
        tb.bus_send({'from': 'backend-dev', 'to': 'frontend-dev', 'text': '第二条'})

        peek = tb.bus_inbox('frontend-dev', peek=True)
        self.assertEqual(peek['unread'], 2)
        peek_again = tb.bus_inbox('frontend-dev', peek=True)
        self.assertEqual(peek_again['unread'], 2, 'peek 不推进游标')

        real = tb.bus_inbox('frontend-dev')
        self.assertEqual(real['unread'], 2)
        drained = tb.bus_inbox('frontend-dev')
        self.assertEqual(drained['unread'], 0, '游标已推进，没有新消息')

        allmsgs = tb.bus_inbox('frontend-dev', everything=True)
        self.assertEqual(len(allmsgs['messages']), 2, 'all=1 返回全部历史，不受游标影响')

    def test_broadcast_all(self):
        tb = self.tb
        tb.bus_send({'from': 'lead', 'to': 'all', 'text': '全员公告'})
        a = tb.bus_inbox('backend-dev', peek=True)
        b = tb.bus_inbox('frontend-dev', peek=True)
        self.assertTrue(any(m['text'] == '全员公告' for m in a['messages']))
        self.assertTrue(any(m['text'] == '全员公告' for m in b['messages']))
        # 发件人自己不应该收到自己发的广播
        lead_inbox = tb.bus_inbox('lead', peek=True)
        self.assertFalse(any(m['text'] == '全员公告' and m['from'] == 'lead' for m in lead_inbox['messages']))

    def test_inbox_requires_to(self):
        with self.assertRaises(ValueError):
            self.tb.bus_inbox('')


class BusHttpTest(unittest.TestCase):
    """起真实 HTTP 服务：/api/bus/send 同步写收件箱、snapshot 里的 bus comms + unread、backdrop 上传/设置/清除。"""

    @classmethod
    def setUpClass(cls):
        cls.env = BusEnv()
        cls.tb = cls.env.load()
        cls.tb.Handler.args = cls.tb.argparse.Namespace(session=None, project=None, stale=600)
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), cls.tb.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.env.cleanup()

    def get(self, path):
        with NOPROXY.open(f'http://127.0.0.1:{self.port}{path}') as r:
            return r.status, json.loads(r.read())

    def post(self, path, obj):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                      data=json.dumps(obj).encode('utf-8'), headers={'Content-Type': 'application/json'})
        try:
            with NOPROXY.open(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_send_syncs_inbox_file(self):
        st, out = self.post('/api/bus/send', {'from': 'backend-dev', 'to': 'frontend-dev', 'text': '接口已就绪', 'cwd': str(self.env.proj_dir)})
        self.assertEqual(st, 200, out)
        self.assertTrue(out['ok'])
        self.assertIn('inbox_file', out)
        f = Path(out['inbox_file'])
        self.assertTrue(f.is_file())
        self.assertIn('接口已就绪', f.read_text(encoding='utf-8'))
        self.assertIn('backend-dev', f.read_text(encoding='utf-8'))

    def test_send_400_errors(self):
        st, out = self.post('/api/bus/send', {'from': 'backend-dev', 'to': 'frontend-dev', 'text': ''})
        self.assertEqual(st, 400); self.assertIn('error', out)
        st, out = self.post('/api/bus/send', {'from': 'backend-dev', 'to': '', 'text': 'hi'})
        self.assertEqual(st, 400); self.assertIn('error', out)
        st, out = self.post('/api/bus/send', {'from': 'backend-dev', 'to': 'frontend-dev', 'text': 'x' * 4001})
        self.assertEqual(st, 400); self.assertIn('error', out)

    def test_bus_log_and_inbox_endpoints(self):
        self.post('/api/bus/send', {'from': 'user', 'to': 'lead', 'text': '来自用户的问候', 'cwd': str(self.env.proj_dir)})
        st, out = self.get(f'/api/bus?project={self.env.proj_key}&limit=200')
        self.assertEqual(st, 200)
        self.assertTrue(any(m['text'] == '来自用户的问候' for m in out['messages']))
        st, out = self.get(f'/api/bus/inbox?to=lead&project={self.env.proj_key}&peek=1')
        self.assertEqual(st, 200)
        self.assertTrue(any(m['text'] == '来自用户的问候' for m in out['messages']))

    def test_snapshot_bus_comms_and_unread(self):
        self.post('/api/bus/send', {'from': 'backend-dev', 'to': 'frontend-dev', 'text': '快照可见的消息', 'session': 'sess-0001'})
        st, snap = self.get('/snapshot.json?session=sess-0001')
        self.assertEqual(st, 200)
        bus_comms = [c for c in snap['comms'] if c['kind'] == 'bus']
        self.assertTrue(any(c['text'] == '快照可见的消息' for c in bus_comms))
        self.assertIn('bus', snap)
        self.assertGreaterEqual(snap['bus']['count'], 1)
        fe = next(m for m in snap['members'] if m['agentType'] == 'frontend-dev')
        self.assertGreaterEqual(fe['unread'], 1)
        self.assertEqual(snap['session']['cwd'], str(self.env.proj_dir))

    def test_backdrop_upload_settings_clear(self):
        st, out = self.get('/api/backdrop')
        self.assertEqual(st, 200)
        self.assertFalse(out['enabled'])
        self.assertEqual(out['presets'], [], 'v1.4.1：内置预设已移除，presets 恒为空数组')

        data_b64 = base64.b64encode(PNG_1PX).decode()
        st, out = self.post('/api/backdrop', {'data': data_b64, 'name': '本机测试图'})
        self.assertEqual(st, 200, out)
        self.assertTrue(out['enabled'])
        self.assertTrue(out['url'])

        st, out = self.post('/api/backdrop', {'opacity': 0.4, 'side': 'left'})
        self.assertEqual(st, 200, out)
        self.assertAlmostEqual(out['opacity'], 0.4, places=2)
        self.assertEqual(out['side'], 'left')

        # 透明度边界：0 = 完全透明但保留配置（原下限 0.1 放开到 0）
        st, out = self.post('/api/backdrop', {'opacity': 0})
        self.assertEqual(st, 200, out)
        self.assertAlmostEqual(out['opacity'], 0, places=2)
        self.assertTrue(out['enabled'], '透明度 0 不等于关闭背景')

        # panelAlpha 边界：0.2（原下限 0.3 放开到 0.2）
        st, out = self.post('/api/backdrop', {'panelAlpha': 0.2})
        self.assertEqual(st, 200, out)
        self.assertAlmostEqual(out['panelAlpha'], 0.2, places=2)
        st, out = self.post('/api/backdrop', {'panelAlpha': 0.1})
        self.assertEqual(st, 200, out)
        self.assertAlmostEqual(out['panelAlpha'], 0.2, places=2, msg='低于下限被夹到 0.2')

        st, out = self.post('/api/backdrop', {'clear': True})
        self.assertEqual(st, 200, out)
        self.assertFalse(out['enabled'])
        self.assertEqual(out['url'], '')

    def test_backdrop_preset_removed_400(self):
        st, out = self.post('/api/backdrop', {'preset': 'shu'})
        self.assertEqual(st, 400, out)
        self.assertIn('error', out)
        self.assertIn('预设已移除', out['error'])
        # 未知预设名也是同一条 400，不联网也不区分名字
        st, out = self.post('/api/backdrop', {'preset': 'nope'})
        self.assertEqual(st, 400, out)
        self.assertIn('预设已移除', out['error'])

    def test_backdrop_legacy_preset_source_compat(self):
        """旧 backdrop.json 里 source:'preset' 的配置照常显示（图片来源换成本机图，字段本身不受影响）。"""
        tb = self.tb
        bs = tb._bs()
        img_cfg = bs.save_image(PNG_1PX, 'legacy', tb.BACKDROPS, credit='旧立绘')
        raw = tb._read_backdrop_raw(tb.BACKDROPS)
        raw.update({'source': 'preset', 'title': '黍（明日方舟）', 'kind': 'image', 'wallpaperId': ''})
        tb._write_backdrop_raw({**img_cfg, **raw}, tb.BACKDROPS)
        st, out = self.get('/api/backdrop')
        self.assertEqual(st, 200, out)
        self.assertTrue(out['enabled'])
        self.assertTrue(out['url'])
        self.assertEqual(out['source'], 'preset')
        self.post('/api/backdrop', {'clear': True})

    def test_backdrop_bad_side_400(self):
        st, out = self.post('/api/backdrop', {'side': 'up'})
        self.assertEqual(st, 400, out)


class MsgCliTest(unittest.TestCase):
    """msg.py 子进程测试：在线经服务端收发，离线退化到 .team/inbox/*.md。"""

    @classmethod
    def setUpClass(cls):
        cls.env = BusEnv()
        cls.tb = cls.env.load()
        cls.tb.Handler.args = cls.tb.argparse.Namespace(session=None, project=None, stale=600)
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), cls.tb.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.msg_py = str(ROOT / 'team-board' / 'msg.py')

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.env.cleanup()

    def run_msg(self, args, cwd, port, agent='backend-dev'):
        env = dict(os.environ)
        env['TEAM_AGENT_NAME'] = agent
        env['TEAM_BOARD_PORT'] = str(port)
        r = subprocess.run([sys.executable, self.msg_py] + args, cwd=str(cwd), env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
        return r.returncode, r.stdout, r.stderr

    def test_send_and_inbox_online(self):
        rc, out, err = self.run_msg(['send', 'frontend-dev', '来自 CLI 的消息'], self.env.proj_dir, self.port)
        self.assertEqual(rc, 0, err)
        self.assertIn('已发送', out)

        rc, out, err = self.run_msg(['inbox', 'frontend-dev', '--peek'], self.env.proj_dir, self.port, agent='frontend-dev')
        self.assertEqual(rc, 0, err)
        self.assertIn('来自 CLI 的消息', out)

    def test_who_and_log_online(self):
        rc, out, err = self.run_msg(['who'], self.env.proj_dir, self.port)
        self.assertEqual(rc, 0, err)
        self.assertIn('backend-dev', out)
        self.assertIn('frontend-dev', out)

        rc, out, err = self.run_msg(['log', '-n', '10'], self.env.proj_dir, self.port)
        self.assertEqual(rc, 0, err)

    def test_send_and_inbox_offline_degrade(self):
        dead_port = 1  # 没有服务监听在这个端口上（特权端口，本机跑不起来）
        rc, out, err = self.run_msg(['send', 'frontend-dev', '离线消息'], self.env.proj_dir, dead_port)
        self.assertEqual(rc, 0, err)
        self.assertIn('看板未运行', out)
        f = self.env.proj_dir / '.team' / 'inbox' / 'frontend-dev.md'
        self.assertTrue(f.is_file())
        self.assertIn('离线消息', f.read_text(encoding='utf-8'))

        rc, out, err = self.run_msg(['inbox', 'frontend-dev'], self.env.proj_dir, dead_port)
        self.assertEqual(rc, 0, err)
        self.assertIn('看板未运行', out)
        self.assertIn('离线消息', out)

    def test_send_offline_no_team_dir(self):
        dead_port = 1
        rc, out, err = self.run_msg(['send', 'frontend-dev', '没有收件箱目录'], self.env.no_team_dir, dead_port)
        self.assertEqual(rc, 1)
        self.assertIn('未送达', err)



class ReviewFixesTest(unittest.TestCase):
    """工单 04 收口：session.cwd 取启动目录、cwd→项目 key 往上找、msg.py 按项目过滤 / stdin / 绕代理 / who 含队长、
    bus 原文件不可静态访问、Origin 校验、backdrop 非图片与路径穿越。"""

    @classmethod
    def setUpClass(cls):
        cls.env = BusEnv()
        env = cls.env
        # 队长后来 cd 进 <proj>/.team，又 cd 进别的会话目录：session.cwd 仍应是启动目录
        cls.other_sess_dir = env.claude / 'projects' / env.proj_key / 'sess-9999' / 'subagents'
        cls.other_sess_dir.mkdir(parents=True)
        (env.proj_dir / '.team' / 'tickets').mkdir(parents=True)
        lead = env.claude / 'projects' / env.proj_key / 'sess-0001.jsonl'
        with open(lead, 'a', encoding='utf-8') as fh:
            fh.write(jl(user('2026-01-01T10:01:00Z', '继续', cwd=str(env.proj_dir / '.team')),
                        user('2026-01-01T10:02:00Z', '继续', cwd=str(cls.other_sess_dir))))
        cls.tb = env.load()
        cls.tb.Handler.args = cls.tb.argparse.Namespace(session=None, project=None, stale=600)
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), cls.tb.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.msg_py = str(ROOT / 'team-board' / 'msg.py')

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.env.cleanup()

    def raw(self, method, path, body=None, headers=None):
        """http.client 直发：路径原样送出（urllib 会规范化 /./ 与 %2F），便于测静态路径绕过。"""
        import http.client
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        c.putrequest(method, path, skip_accept_encoding=True)
        data = json.dumps(body).encode('utf-8') if body is not None else b''
        hs = {'Content-Type': 'application/json', 'Content-Length': str(len(data)), **(headers or {})}
        for k, v in hs.items():
            c.putheader(k, v)
        c.endheaders()
        if data:
            c.send(data)
        r = c.getresponse()
        out = r.status, r.read()
        c.close()
        return out

    def run_msg(self, args, cwd, agent='backend-dev', stdin=None, proxy=False, port=None):
        env = {k: v for k, v in os.environ.items() if k.lower() not in ('no_proxy', 'http_proxy', 'https_proxy', 'all_proxy')}
        env.update({'TEAM_AGENT_NAME': agent, 'TEAM_BOARD_PORT': str(port or self.port)})
        if proxy:   # 不可达的 HTTP 代理：msg.py 必须绕过代理直连本机看板
            env['HTTP_PROXY'] = 'http://127.0.0.1:9'
        r = subprocess.run([sys.executable, self.msg_py] + args, cwd=str(cwd), env=env, input=stdin,
                           capture_output=True, text=True, encoding='utf-8', timeout=20)
        return r.returncode, r.stdout, r.stderr

    def test_session_cwd_is_launch_dir(self):
        snap = json.loads(self.raw('GET', '/snapshot.json?session=sess-0001')[1])
        self.assertEqual(snap['session']['cwd'], str(self.env.proj_dir), '不能取到队长后来 cd 去的别的会话目录')

    def test_resolve_project_walks_up(self):
        self.assertEqual(self.tb.resolve_project(str(self.env.proj_dir / '.team' / 'tickets')), self.env.proj_key)
        self.assertEqual(self.tb.resolve_project(str(self.env.no_team_dir)), self.env.project_key_fn(str(self.env.no_team_dir)))

    def test_board_send_uses_recent_lead_cwd_for_inbox(self):
        st, body = self.raw('POST', '/api/bus/send', {'from': 'user', 'to': 'backend-dev', 'text': '看板发的', 'session': 'sess-0001'})
        out = json.loads(body)
        self.assertEqual(st, 200, out)
        self.assertEqual(Path(out['inbox_file']), self.env.proj_dir / '.team' / 'inbox' / 'backend-dev.md')
        self.assertEqual(out['message']['project'], self.env.proj_key)

    def test_msg_cli_from_subdir_project_filter_who_stdin_proxy(self):
        sub = self.env.proj_dir / '.team' / 'tickets'
        # 别的项目发给 frontend-dev 的消息：本项目的 inbox / log 不应看到
        self.raw('POST', '/api/bus/send', {'from': 'x', 'to': 'frontend-dev', 'text': '别的项目的消息', 'project': 'OTHER-proj'})
        text = '含 "双引号" 与 \'单引号\' 的中文\n第二行'
        rc, out, err = self.run_msg(['send', 'frontend-dev', '-'], sub, stdin=text, proxy=True)
        self.assertEqual(rc, 0, out + err)
        self.assertIn('frontend-dev.md', out, '子目录里发也要写进上级 .team/inbox/')
        rc, out, err = self.run_msg(['inbox', 'frontend-dev', '--peek'], sub, agent='frontend-dev', proxy=True)
        self.assertEqual(rc, 0, err)
        self.assertIn('含 "双引号" 与 \'单引号\' 的中文', out)
        self.assertNotIn('别的项目的消息', out)
        rc, out, err = self.run_msg(['log', '-n', '50'], sub, proxy=True)
        self.assertEqual(rc, 0, err)
        self.assertIn('第二行', out)
        self.assertNotIn('别的项目的消息', out)
        st, body = self.raw('GET', f'/api/bus?project={self.env.proj_key}')
        m = next(x for x in json.loads(body)['messages'] if '双引号' in x['text'])
        self.assertEqual(m['project'], self.env.proj_key)
        self.assertEqual(m['via'], 'cli')
        rc, out, err = self.run_msg(['who'], sub, proxy=True)
        self.assertEqual(rc, 0, err)
        self.assertIn('队长', out)
        self.assertIn('frontend-dev', out)

    def test_msg_offline_from_subdir(self):
        sub = self.env.proj_dir / '.team' / 'tickets'
        rc, out, err = self.run_msg(['send', 'qa-tester', '离线从子目录发'], sub, port=1)
        self.assertEqual(rc, 0, err)
        f = self.env.proj_dir / '.team' / 'inbox' / 'qa-tester.md'
        self.assertIn('离线从子目录发', f.read_text(encoding='utf-8'))
        rc, out, err = self.run_msg(['inbox', 'qa-tester'], sub, port=1)
        self.assertEqual(rc, 0, err)
        self.assertIn('离线从子目录发', out)

    def test_bus_files_not_served(self):
        self.raw('POST', '/api/bus/send', {'from': 'a', 'to': 'b', 'text': '隐私'})
        self.assertTrue((self.tb.HERE / 'bus' / 'messages.jsonl').is_file())
        paths = ['/bus/messages.jsonl', '/./bus/messages.jsonl', '/bus%2Fmessages.jsonl', '/x/../bus/cursors.json', '/bus/']
        if os.name == 'nt':
            paths.append('/BUS/messages.jsonl')   # Windows 文件系统不分大小写
        for path in paths:
            st, body = self.raw('GET', path)
            self.assertEqual(st, 404, path)
            self.assertNotIn('隐私'.encode('utf-8'), body)
        self.assertEqual(self.raw('HEAD', '/bus/messages.jsonl')[0], 404)

    def test_origin_check(self):
        body = {'from': 'user', 'to': 'lead', 'text': 'origin'}
        self.assertEqual(self.raw('POST', '/api/bus/send', body)[0], 200, 'msg.py 没有 Origin，放行')
        self.assertEqual(self.raw('POST', '/api/bus/send', body, {'Origin': f'http://127.0.0.1:{self.port}'})[0], 200)
        self.assertEqual(self.raw('POST', '/api/bus/send', body, {'Origin': f'http://localhost:{self.port}'})[0], 200)
        for o in ('http://evil.example', f'http://evil.example:{self.port}', 'http://localhost:3000', 'null'):
            self.assertEqual(self.raw('POST', '/api/bus/send', body, {'Origin': o})[0], 403, o)

    def test_negative_content_length(self):
        self.assertEqual(self.raw('POST', '/api/bus/send', None, {'Content-Length': '-1'})[0], 400)

    def test_backdrop_rejects_non_image_and_traversal(self):
        st, body = self.raw('POST', '/api/backdrop', {'data': base64.b64encode(b'<script>alert(1)</script>').decode()})
        self.assertEqual(st, 400, body)
        st, body = self.raw('POST', '/api/backdrop', {'path': str(self.env.tmp / 'nope.png')})
        self.assertEqual(st, 400, body)
        bd = self.tb.BACKDROPS
        bd.mkdir(parents=True, exist_ok=True)
        victim = bd.parent / 'victim.png'
        victim.write_bytes(PNG_1PX)
        (bd / 'backdrop.json').write_text(json.dumps({'file': '../victim.png', 'enabled': True}), encoding='utf-8')
        self.assertEqual(json.loads(self.raw('GET', '/api/backdrop')[1])['url'], '', '配置里的 ../ 文件名不能变成 url')
        st, body = self.raw('POST', '/api/backdrop', {'clear': True})
        self.assertEqual(st, 200, body)
        self.assertTrue(victim.is_file(), 'clear 不能删 backdrops/ 以外的文件')

if __name__ == '__main__':
    unittest.main()
