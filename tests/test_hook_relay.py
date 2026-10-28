"""钩子中继和看板接收端。只用合成字段，不读真实转录。"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELAY_PATH = ROOT / 'team-board' / 'hook_relay.py'
SECRET = 'SECRET_DO_NOT_FORWARD'
ALLOWED = {
    'v', 'source', 'session_id', 'event', 'ts', 'tool_name', 'tool_use_id',
    'agent_id', 'agent_type', 'notification_type', 'stop_hook_active', 'seq',
}


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay = load_module('hook_relay_under_test', RELAY_PATH)


def closed_port():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def synthetic_pretool():
    return {
        'session_id': 'sess-0001',
        'prompt_id': '550e8400-e29b-41d4-a716-446655440000',
        'transcript_path': '/tmp/synthetic/transcript.jsonl',
        'cwd': '/tmp/synthetic-project',
        'permission_mode': 'default',
        'hook_event_name': 'PreToolUse',
        'tool_name': 'Bash',
        'tool_input': {
            'command': 'echo ' + SECRET,
            'description': 'synthetic',
            'note': '{"tool_name": "Evil", "session_id": "nope"}',
        },
        'tool_response': {'stdout': SECRET + '-out'},
        'tool_use_id': 'toolu_synthetic_1',
        'agent_id': 'agent-a1',
        'agent_type': 'backend-dev',
        'last_assistant_message': SECRET + '-text',
    }


class SlimEventTest(unittest.TestCase):
    def test_documented_fields_only(self):
        event = relay.slim_bytes(json.dumps(synthetic_pretool()).encode('utf-8'))
        self.assertEqual(event['event'], 'PreToolUse')
        self.assertEqual(event['session_id'], 'sess-0001')
        self.assertEqual(event['tool_name'], 'Bash')
        self.assertEqual(event['tool_use_id'], 'toolu_synthetic_1')
        self.assertEqual(event['agent_id'], 'agent-a1')
        self.assertEqual(event['agent_type'], 'backend-dev')
        self.assertEqual(event['source'], 'claude-hook')
        self.assertEqual(event['v'], 1)
        blob = json.dumps(event)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn('transcript_path', blob)
        self.assertNotIn('Evil', blob)
        self.assertNotIn('nope', blob)
        self.assertNotIn('tool_input', event)
        self.assertTrue(set(event) <= ALLOWED)

    def test_nested_decoy_and_other_events(self):
        stop = {
            'session_id': 'sess-0001',
            'hook_event_name': 'Stop',
            'stop_hook_active': False,
            'last_assistant_message': SECRET,
            'background_tasks': [{'command': SECRET, 'description': 'synthetic'}],
        }
        event = relay.parse_hook_text(json.dumps(stop))
        self.assertEqual(event['event'], 'Stop')
        self.assertIs(event['stop_hook_active'], False)
        self.assertNotIn(SECRET, json.dumps(event))

        note = {
            'session_id': 'sess-0001',
            'hook_event_name': 'Notification',
            'notification_type': 'permission_prompt',
            'message': SECRET,
            'title': 'Permission needed',
        }
        event = relay.parse_hook_text(json.dumps(note))
        self.assertEqual(event['notification_type'], 'permission_prompt')
        self.assertNotIn('message', event)
        self.assertNotIn(SECRET, json.dumps(event))

        sub = {
            'session_id': 'sess-0001',
            'hook_event_name': 'SubagentStop',
            'agent_id': 'def456',
            'agent_type': 'Explore',
            'agent_transcript_path': '/tmp/synthetic/subagents/agent-def456.jsonl',
            'last_assistant_message': SECRET,
            'stop_hook_active': True,
        }
        event = relay.parse_hook_text(json.dumps(sub))
        self.assertEqual(event['agent_id'], 'def456')
        self.assertEqual(event['agent_type'], 'Explore')
        self.assertIs(event['stop_hook_active'], True)
        self.assertNotIn('agent_transcript_path', event)

    def test_unknown_or_truncated_input_is_not_an_event(self):
        self.assertIsNone(relay.slim_bytes(b''))
        self.assertIsNone(relay.slim_bytes(b'not-json'))
        self.assertIsNone(relay.parse_hook_text('{"hook_event_name":"UserPromptSubmit","session_id":"sess-0001","prompt":"%s"}' % SECRET))
        partial = '{"session_id":"sess-0001","hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{"command":"%s' % SECRET
        event = relay.parse_hook_text(partial)
        self.assertEqual(event['tool_name'], 'Read')
        self.assertNotIn(SECRET, json.dumps(event))
        self.assertNotIn('tool_use_id', event)

    def test_large_tool_input_is_not_forwarded(self):
        payload = {
            'session_id': 'sess-0001',
            'hook_event_name': 'PostToolUse',
            'tool_name': 'Write',
            'tool_input': {'content': SECRET + ('x' * 400000)},
            'tool_response': {'filePath': '/tmp/synthetic/out.txt'},
            'tool_use_id': 'toolu_after_bulk',
        }
        started = time.perf_counter()
        event = relay.slim_bytes(json.dumps(payload).encode('utf-8'))
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(event['event'], 'PostToolUse')
        self.assertEqual(event['tool_name'], 'Write')
        self.assertNotIn(SECRET, json.dumps(event))

    def test_client_event_drops_unknown_fields(self):
        event = relay.normalize_client_event({
            'source': 'claude-hook',
            'event': 'PreToolUse',
            'session_id': 'sess-0001',
            'tool_name': 'Bash',
            'tool_input': {'command': SECRET},
            'message': SECRET,
        }, now=1000)
        self.assertNotIn(SECRET, json.dumps(event))
        self.assertNotIn('tool_input', event)
        with self.assertRaises(ValueError):
            relay.normalize_client_event({'event': 'PreToolUse'}, now=1000)
        with self.assertRaises(ValueError):
            relay.normalize_client_event({'event': 'Nope', 'session_id': 'sess-0001'}, now=1000)


class RelayOfflineTest(unittest.TestCase):
    def test_opener_disables_proxies(self):
        # ProxyHandler({}) 不注册协议方法，build_opener 因此不会再装默认的环境代理。
        for handler in relay._OPENER.handlers:
            proxies = getattr(handler, 'proxies', None) or {}
            self.assertFalse(proxies)

    def test_closed_port_is_bounded(self):
        port = closed_port()
        raw = json.dumps(synthetic_pretool()).encode('utf-8')
        started = time.perf_counter()
        posted = relay.relay_from(raw, port)
        elapsed = time.perf_counter() - started
        self.assertFalse(posted)
        # 目标是连接失败 < 50ms。0.25s 是 CI 上限，仍然有界。
        self.assertLess(elapsed, 0.25, 'closed-port relay took %.3fs' % elapsed)

    def test_invalid_port_does_not_connect(self):
        old = os.environ.get('TEAM_BOARD_PORT')
        os.environ['TEAM_BOARD_PORT'] = 'nope'
        try:
            self.assertIsNone(relay.board_port())
            self.assertFalse(relay.relay_from(b'{}', relay.board_port()))
        finally:
            if old is None:
                os.environ.pop('TEAM_BOARD_PORT', None)
            else:
                os.environ['TEAM_BOARD_PORT'] = old

    def test_subprocess_stays_silent_when_board_is_down(self):
        port = closed_port()
        env = os.environ.copy()
        env.update({
            'TEAM_BOARD_PORT': str(port),
            'PYTHONIOENCODING': 'utf-8',
            'http_proxy': 'http://127.0.0.1:9',
            'HTTP_PROXY': 'http://127.0.0.1:9',
            'https_proxy': 'http://127.0.0.1:9',
            'HTTPS_PROXY': 'http://127.0.0.1:9',
            'all_proxy': 'http://127.0.0.1:9',
            'ALL_PROXY': 'http://127.0.0.1:9',
            'no_proxy': '',
            'NO_PROXY': '',
        })
        started = time.perf_counter()
        proc = subprocess.run([sys.executable, str(RELAY_PATH)], input=json.dumps(synthetic_pretool()).encode('utf-8'),
                              capture_output=True, env=env, timeout=3)
        elapsed = time.perf_counter() - started
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b'')
        self.assertEqual(proc.stderr, b'')
        self.assertLess(elapsed, 2.0, 'subprocess relay took %.3fs' % elapsed)
        bad = subprocess.run([sys.executable, str(RELAY_PATH)], input=b'{', capture_output=True, env=env, timeout=3)
        self.assertEqual(bad.returncode, 0)
        self.assertEqual(bad.stdout, b'')
        self.assertEqual(bad.stderr, b'')


class HookEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hook-board-')
        cls.home = Path(cls.tmp.name) / 'home'
        sub = cls.home / '.claude' / 'projects' / 'C--synthetic' / 'sess-0001' / 'subagents'
        sub.mkdir(parents=True)
        (sub / 'agent-a1.jsonl').write_text(
            json.dumps({'type': 'user', 'timestamp': '2026-01-01T00:00:00Z',
                        'message': {'role': 'user', 'content': 'synthetic hook fixture'}}) + '\n',
            encoding='utf-8')
        cls._env = {key: os.environ.get(key) for key in ('HOME', 'USERPROFILE')}
        os.environ['HOME'] = str(cls.home)
        os.environ['USERPROFILE'] = str(cls.home)
        cls.board = load_module('hook_board_under_test', ROOT / 'team-board' / 'team_board.py')
        cls.board.Handler.args = cls.board.argparse.Namespace(session=None, project=None, stale=600)
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), cls.board.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=3)
        cls.tmp.cleanup()
        for key, value in cls._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        self.board.HOOK_LOG.clear()
        self.board.Handler._cache = (0.0, None)

    def origin(self, port=None):
        return 'http://127.0.0.1:%d' % (self.port if port is None else port)

    def request(self, body, origin=None, include_origin=True):
        data = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        if include_origin:
            headers['Origin'] = self.origin() if origin is None else origin
        req = urllib.request.Request('http://127.0.0.1:%d/api/hooks/event' % self.port, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode('utf-8')
            return exc.code, json.loads(raw) if raw else {}

    def snapshot(self):
        with urllib.request.urlopen('http://127.0.0.1:%d/snapshot.json' % self.port, timeout=2) as resp:
            return json.loads(resp.read().decode('utf-8'))

    def test_origin_size_and_privacy(self):
        self.assertTrue(self.board.loopback_host('127.0.0.1'))
        self.assertTrue(self.board.loopback_host('::ffff:127.0.0.1'))
        self.assertTrue(self.board.loopback_host('::1'))
        self.assertFalse(self.board.loopback_host('8.8.8.8'))
        self.assertFalse(self.board.hook_origin_allowed('', self.port))
        self.assertFalse(self.board.hook_origin_allowed('http://evil.example', self.port))
        self.assertFalse(self.board.hook_origin_allowed('http://127.0.0.1:1', self.port))
        self.assertFalse(self.board.hook_origin_allowed('http://user:pw@127.0.0.1:%d' % self.port, self.port))
        self.assertTrue(self.board.hook_origin_allowed(self.origin(), self.port))

        missing = self.request({'session_id': 'sess-0001', 'event': 'Stop', 'source': 'claude-hook'}, include_origin=False)
        self.assertEqual(missing[0], 403)
        evil = self.request({'session_id': 'sess-0001', 'event': 'Stop', 'source': 'claude-hook',
                             'tool_input': {'command': SECRET}}, origin='http://evil.example')
        self.assertEqual(evil[0], 403)
        wrong_port = self.request({'session_id': 'sess-0001', 'event': 'Stop', 'source': 'claude-hook'},
                                  origin='http://127.0.0.1:1')
        self.assertEqual(wrong_port[0], 403)
        huge = b'{' * 5000
        over = self.request(huge)
        self.assertEqual(over[0], 413)
        self.assertEqual(self.request({'event': 'PreToolUse', 'session_id': 'sess-0001', 'tool_input': SECRET})[0], 200)
        self.assertEqual(self.request(['nope'])[0], 400)
        self.assertEqual(self.request({'event': 'UserPromptSubmit', 'session_id': 'sess-0001'})[0], 400)

        self.board.HOOK_LOG.clear()
        status, body = self.request({
            'source': 'claude-hook',
            'event': 'PreToolUse',
            'session_id': 'sess-0001',
            'tool_name': 'Bash',
            'tool_use_id': 'toolu_ok',
            'agent_id': 'a1',
            'agent_type': 'backend-dev',
            'tool_input': {'command': SECRET},
            'tool_response': SECRET,
            'transcript_path': '/tmp/synthetic/secret.jsonl',
            'message': SECRET,
        })
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertNotIn(SECRET, json.dumps(body))
        snap = self.snapshot()
        self.assertEqual(snap['session']['id'], 'sess-0001')
        self.assertFalse(snap['live']['merged'])
        self.assertEqual(snap['live']['correlate'], 'tool_use_id')
        self.assertTrue(snap['live']['fresh'])
        self.assertEqual(len(snap['live']['events']), 1)
        event = snap['live']['events'][0]
        self.assertTrue(set(event) <= ALLOWED)
        self.assertEqual(event['tool_name'], 'Bash')
        self.assertEqual(event['tool_use_id'], 'toolu_ok')
        self.assertNotIn(SECRET, json.dumps(snap['live']))
        again = self.snapshot()
        self.assertEqual(again['live']['events'][0]['seq'], event['seq'])

        self.request({'source': 'claude-hook', 'event': 'Stop', 'session_id': 'sess-other', 'last_assistant_message': SECRET})
        self.assertNotIn('sess-other', json.dumps(self.snapshot()['live']))
        self.assertNotIn(SECRET, json.dumps(self.snapshot()['live']))

    def test_memory_limit_and_stale_flag(self):
        now = time.time()
        for i in range(205):
            self.board.HOOK_LOG.add(relay.normalize_client_event({
                'source': 'claude-hook',
                'event': 'PreToolUse',
                'session_id': 'sess-0001',
                'tool_name': 'Read',
                'tool_use_id': 'toolu_%04d' % i,
                'ts': now,
            }, now=now))
        snap = self.snapshot()
        self.assertEqual(snap['live']['retained'], 200)
        self.assertEqual(snap['live']['limit'], 200)
        self.assertLessEqual(len(snap['live']['events']), 40)
        ids = [item['tool_use_id'] for item in snap['live']['events']]
        self.assertIn('toolu_0204', ids)
        self.assertNotIn('toolu_0000', ids)
        self.board.HOOK_LOG.clear()
        self.board.HOOK_LOG.add(relay.normalize_client_event({
            'source': 'claude-hook', 'event': 'Stop', 'session_id': 'sess-0001', 'ts': now - 100,
        }, now=now))
        self.assertFalse(self.snapshot()['live']['fresh'])

    def test_relay_posts_through_proxy_env(self):
        hits = []

        class Hang(BaseHTTPRequestHandler):
            def handle(self):
                hits.append(self.client_address)
                time.sleep(1.5)

            def log_message(self, *args):
                pass

        proxy = ThreadingHTTPServer(('127.0.0.1', 0), Hang)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(proxy.shutdown)
        self.addCleanup(proxy.server_close)
        env = os.environ.copy()
        proxy_url = 'http://127.0.0.1:%d' % proxy.server_address[1]
        env.update({
            'HOME': str(self.home),
            'USERPROFILE': str(self.home),
            'TEAM_BOARD_PORT': str(self.port),
            'PYTHONIOENCODING': 'utf-8',
            'http_proxy': proxy_url,
            'HTTP_PROXY': proxy_url,
            'https_proxy': proxy_url,
            'HTTPS_PROXY': proxy_url,
            'all_proxy': proxy_url,
            'ALL_PROXY': proxy_url,
            'no_proxy': '',
            'NO_PROXY': '',
        })
        proc = subprocess.run([sys.executable, str(RELAY_PATH)], input=json.dumps(synthetic_pretool()).encode('utf-8'),
                              capture_output=True, env=env, timeout=3)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))
        self.assertEqual(proc.stdout, b'')
        self.assertEqual(proc.stderr, b'')
        self.assertEqual(hits, [])
        snap = self.snapshot()
        event = snap['live']['events'][-1]
        self.assertEqual(event['tool_name'], 'Bash')
        self.assertEqual(event['tool_use_id'], 'toolu_synthetic_1')
        self.assertEqual(event['agent_id'], 'agent-a1')
        self.assertNotIn(SECRET, json.dumps(snap['live']))
        self.assertFalse(snap['live']['merged'])


class HookUiContractTest(unittest.TestCase):
    def test_page_and_synthetic_fixture_keep_live_separate(self):
        html = (ROOT / 'team-board' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="hook-live"', html)
        self.assertIn('尚未与转录合并', html)
        fixture = json.loads((ROOT / 'examples' / 'board-demo.synthetic.json').read_text(encoding='utf-8'))
        live = fixture['live']
        self.assertFalse(live['merged'])
        self.assertEqual(live['correlate'], 'tool_use_id')
        self.assertEqual(live['events'][0]['session_id'], 'synthetic-session')
        self.assertNotIn('tool_input', json.dumps(live))
        self.assertNotIn('tool_response', json.dumps(live))
        blob = json.dumps(fixture)
        self.assertNotIn('sk-', blob)
        self.assertNotIn(SECRET, blob)


if __name__ == '__main__':
    unittest.main()
