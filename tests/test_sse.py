"""Loopback SSE notices, and the page's poll fallback when the stream is absent.

Synthetic snapshots only. The HTTP cases read the first event and close; they do
not sleep waiting for a later change. Revision changes are checked by calling
the same generator the handler writes to the socket.
"""
import http.client
import importlib.util
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path

import headless_chrome

from test_board import BoardEnv, jl, asst
from test_task_deps_ui import extract_function, script_text

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'team-board' / 'index.html'


def sample_view():
    return {
        'generatedAt': 1000,
        'source': 'claude',
        'session': {'id': 'sess-1', 'cwd': '/tmp/secret-project'},
        'lead': {'status': 'running', 'cost': 0.2, 'prompt': 'lead secret prompt'},
        'members': [{
            'id': 'm1', 'status': 'running', 'cost': 0.4, 'toolCount': 2,
            'prompt': 'member secret prompt', 'lastActivityAt': 990,
        }],
        'totals': {'cost': 0.6, 'tokens': 10},
        'officialTeam': {'tasks': [{
            'id': 't1', 'subject': 'Write the notes', 'normalizedStatus': 'pending',
            'status': 'pending', 'stateSeconds': 3, 'description': 'secret task body',
        }]},
        'stallAlerts': {
            'thresholdSec': 600,
            'candidates': [{
                'id': 'm1', 'name': '阿服', 'status': 'idle', 'idleSeconds': 100,
                'basis': 'lastActivityAt', 'anchor': 900, 'errorReason': '',
            }],
        },
        'live': {'fresh': False, 'retained': 0, 'merged': False, 'events': []},
        'feed': [{'ts': 1, 'label': 'Read SPEC'}],
        'conversation': [{'role': 'user', 'text': 'secret conversation'}],
    }


def parse_sse(blob):
    event, retry, data = None, None, []
    for line in blob.decode('utf-8').splitlines():
        if line.startswith('event:'):
            event = line.split(':', 1)[1].strip()
        elif line.startswith('retry:'):
            retry = int(line.split(':', 1)[1].strip())
        elif line.startswith('data:'):
            data.append(line.split(':', 1)[1].lstrip())
    payload = json.loads('\n'.join(data)) if data else None
    return event, payload, retry


def run_js(source, expression):
    binary = shutil.which('node')
    if not binary:
        raise unittest.SkipTest('node is not installed')
    with tempfile.TemporaryDirectory(prefix='sse-ui-') as directory:
        program = Path(directory) / 'sse-check.js'
        program.write_text(source + '\nprocess.stdout.write(JSON.stringify(' + expression + '));\n', encoding='utf-8')
        completed = subprocess.run(
            [binary, str(program)], check=False, capture_output=True, text=True, timeout=20,
        )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


class SseBoardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = BoardEnv()
        cls.tb = cls.env.load()
        cls.tb.Handler.args = cls.tb.argparse.Namespace(
            session=None, project=None, stale=600, source='all')
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), cls.tb.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=3)
        cls.env.cleanup()

    def revision(self, view):
        return self.tb.snapshot_revision(view)

    def test_clock_fields_keep_the_same_revision(self):
        original = sample_view()
        moved = deepcopy(original)
        moved['generatedAt'] = 1099
        moved['officialTeam']['tasks'][0]['stateSeconds'] = 99
        moved['stallAlerts']['candidates'][0]['idleSeconds'] = 140
        self.assertEqual(self.revision(original), self.revision(moved))
        self.revision(original)
        self.assertEqual(original['generatedAt'], 1000)
        self.assertNotIn('alerting', original['stallAlerts'])
        self.assertEqual(original['stallAlerts']['candidates'][0]['idleSeconds'], 100)

    def test_meaningful_changes_move_the_revision(self):
        base = sample_view()
        base_rev = self.revision(base)

        status = deepcopy(base)
        status['members'][0]['status'] = 'completed'
        cost = deepcopy(base)
        cost['totals']['cost'] = 1.25
        cost['members'][0]['cost'] = 1.05
        task = deepcopy(base)
        task['officialTeam']['tasks'][0]['normalizedStatus'] = 'in_progress'
        feed = deepcopy(base)
        feed['feed'] = list(base['feed']) + [{'ts': 2, 'label': 'Write app.js'}]
        live = deepcopy(base)
        live['live'] = dict(base['live'], fresh=True, retained=1, events=[{'seq': 1, 'event': 'PostToolUse'}])
        quiet = deepcopy(base)
        quiet['stallAlerts']['candidates'][0]['idleSeconds'] = 599
        alerting = deepcopy(base)
        alerting['stallAlerts']['candidates'][0]['idleSeconds'] = 600

        changed = {
            'status': status, 'cost': cost, 'task': task, 'feed': feed,
            'live': live, 'stall': alerting,
        }
        for name, view in changed.items():
            self.assertNotEqual(base_rev, self.revision(view), name)
        self.assertEqual(self.revision(base), self.revision(quiet))
        self.assertNotEqual(self.revision(quiet), self.revision(alerting))

    def test_notice_keeps_transcript_text_off_the_wire(self):
        view = sample_view()
        notice = self.tb.sse_notice(view, self.revision(view))
        self.assertEqual(set(notice), {'rev', 'source', 'session', 'generatedAt'})
        blob = self.tb.format_sse_event('refresh', notice, self.tb.SSE_RETRY_MS)
        text = blob.decode('utf-8')
        for private in ('secret-project', 'secret prompt', 'secret task', 'secret conversation', 'Read SPEC'):
            self.assertNotIn(private, text)
        self.assertIn('retry: %d' % self.tb.SSE_RETRY_MS, text)
        event, payload, retry = parse_sse(blob)
        self.assertEqual((event, retry, payload['session']), ('refresh', 3000, 'sess-1'))

    def test_stream_emits_refresh_when_the_snapshot_changes(self):
        mod = self.tb
        args = mod.argparse.Namespace(**vars(mod.Handler.args))
        mod.Handler._cache = (0.0, None)
        before = mod.public_snapshot(args, True)
        path = (self.env.claude / 'projects' / 'C--demo' / 'sess-0001' / 'subagents' / 'agent-b2.jsonl')
        with path.open('a', encoding='utf-8') as handle:
            handle.write(jl(asst(
                '2026-01-01T10:05:00Z', 'claude-sonnet-5',
                [{'type': 'text', 'text': '界面 app.js 已接上 createStore。'}],
                stop='end_turn',
            )))
        mod.Handler._cache = (0.0, None)
        after = mod.public_snapshot(args, True)
        self.assertNotEqual(self.revision(before), self.revision(after))
        prompt = before['members'][0].get('prompt') or '你是研发部'
        self.assertTrue(prompt)

        seq = [before, before, after]

        def load():
            return seq.pop(0)

        writes = []
        mod.write_sse_stream(writes.append, load, lambda _seconds: False, lambda: 0, None, rounds=3)
        text = b''.join(writes).decode('utf-8')
        self.assertEqual(text.count('event: hello'), 1)
        self.assertEqual(text.count('event: refresh'), 1)
        self.assertIn('\n: ping\n', text)
        self.assertNotIn(prompt, text)
        self.assertNotIn('createStore', text)
        parts = text.split('\n\n')
        hello = parse_sse(parts[0].encode('utf-8'))
        refresh = parse_sse(parts[2].encode('utf-8'))
        self.assertEqual(hello[0], 'hello')
        self.assertEqual(hello[1]['rev'], self.revision(before))
        self.assertEqual(refresh[0], 'refresh')
        self.assertEqual(refresh[1]['rev'], self.revision(after))

    def test_heartbeat_refreshes_without_a_content_change(self):
        view = sample_view()
        times = [0, 1, 12]

        def clock():
            return times.pop(0)

        kinds = []
        for kind, payload in self.tb.iter_sse_notices(lambda: view, lambda _seconds: False, clock, 12, rounds=3):
            kinds.append((kind, None if payload is None else payload['rev']))
        self.assertEqual([item[0] for item in kinds], ['hello', 'ping', 'refresh'])
        self.assertEqual(kinds[0][1], kinds[2][1])

    def test_poll_returning_true_stops_before_another_event(self):
        view = sample_view()
        events = list(self.tb.iter_sse_notices(lambda: view, lambda _seconds: True, lambda: 0, None))
        self.assertEqual([item[0] for item in events], ['hello'])

    def test_subscription_stays_on_loopback(self):
        allowed = self.tb.sse_request_allowed
        self.assertTrue(allowed('127.0.0.1', '', 7788))
        self.assertTrue(allowed('::1', None, 7788))
        self.assertTrue(allowed('::ffff:127.0.0.1', 'http://127.0.0.1:7788', 7788))
        self.assertFalse(allowed('192.0.2.10', '', 7788))
        self.assertFalse(allowed('127.0.0.1', 'https://evil.example', 7788))
        self.assertFalse(allowed('127.0.0.1', 'http://127.0.0.1:9', 7788))
        self.assertFalse(allowed('127.0.0.1', 'http://127.0.0.1:7788/other', 7788))

    def _open(self, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            conn.request('GET', path, headers=headers or {})
            resp = conn.getresponse()
            status = resp.status
            ctype = resp.getheader('Content-Type') or ''
            if status != 200:
                return status, ctype, resp.read()
            data = b''
            while b'\n\n' not in data and len(data) < 8192:
                piece = resp.read(256)
                if not piece:
                    break
                data += piece
            return status, ctype, data
        finally:
            conn.close()

    def test_polling_snapshot_still_works(self):
        with urllib.request.urlopen('http://127.0.0.1:%s/snapshot.json' % self.port, timeout=3) as response:
            self.assertEqual(response.status, 200)
            snap = json.loads(response.read().decode('utf-8'))
        self.assertEqual(len(snap['members']), 2)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen('http://127.0.0.1:%s/snapshot.json?source=nope' % self.port, timeout=3)
        self.assertEqual(caught.exception.code, 400)

    def test_events_hello_matches_snapshot_revision(self):
        with urllib.request.urlopen('http://127.0.0.1:%s/snapshot.json' % self.port, timeout=3) as response:
            snap = json.loads(response.read().decode('utf-8'))
        status, ctype, body = self._open('/events')
        self.assertEqual(status, 200)
        self.assertIn('text/event-stream', ctype)
        event, payload, retry = parse_sse(body.split(b'\n\n', 1)[0] + b'\n\n')
        self.assertEqual(event, 'hello')
        self.assertEqual(retry, 3000)
        self.assertEqual(payload['rev'], self.revision(snap))
        self.assertEqual(payload['session'], snap['session']['id'])
        self.assertNotIn('你是研发部', body.decode('utf-8'))
        with urllib.request.urlopen('http://127.0.0.1:%s/snapshot.json' % self.port, timeout=3) as response:
            self.assertEqual(response.status, 200)
        status, ctype, body = self._open('/api/events?source=claude')
        self.assertEqual(status, 200, body)
        self.assertIn('text/event-stream', ctype)
        event, payload, _retry = parse_sse(body.split(b'\n\n', 1)[0] + b'\n\n')
        self.assertEqual(event, 'hello')
        self.assertEqual(payload['source'], 'claude')

    def test_events_reject_bad_source_and_cross_origin(self):
        status, ctype, body = self._open('/events?source=nope')
        self.assertEqual(status, 400)
        self.assertIn('application/json', ctype)
        self.assertIn('error', json.loads(body.decode('utf-8')))
        status, ctype, body = self._open('/events', {'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)
        self.assertIn('application/json', ctype)
        self.assertIn('拒绝', json.loads(body.decode('utf-8'))['error'])

    def test_browser_applies_sse_and_demo_falls_back_to_polling(self):
        binary = headless_chrome.chrome_binary()
        node = shutil.which('node')
        if not binary or not node:
            self.skipTest('node or a headless browser is not installed')
        script = str(ROOT / 'tests' / 'check_sse_browser.js')
        with headless_chrome.launch_headless_chrome(binary) as browser:
            live = subprocess.run(
                [node, script, 'http://127.0.0.1:%s/' % self.port, str(browser.port), 'live'],
                cwd=str(ROOT), capture_output=True, text=True, encoding='utf-8', errors='replace',
                check=False, timeout=30,
            )
            self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
            report = json.loads(live.stdout)
            self.assertTrue(report['pollStopped'], report)
            self.assertIn('/events?', report['opened']['url'])
            self.assertTrue(report['opened']['hello'])
            self.assertIn('session=sess-0001', report['switched']['url'])
            self.assertFalse(report['switched']['poll'])

            spec = importlib.util.spec_from_file_location('sse_demo_board', ROOT / 'tools' / 'demo_board.py')
            demo = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(demo)
            server = demo.DemoServer(0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                fallback = subprocess.run(
                    [node, script, 'http://127.0.0.1:%s/' % server.server_port, str(browser.port), 'fallback'],
                    cwd=str(ROOT), capture_output=True, text=True, encoding='utf-8', errors='replace',
                    check=False, timeout=30,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
            self.assertEqual(fallback.returncode, 0, fallback.stdout + fallback.stderr)
            state = json.loads(fallback.stdout)['state']
            self.assertGreater(state['persons'], 0)
            self.assertTrue(state['poll'])
            self.assertTrue(state['closed'])
            self.assertFalse(state['hello'])
            self.assertGreater(state['fetches'], 0)


class SseClientFallbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which('node') is None:
            raise unittest.SkipTest('node is not installed')
        source = script_text()
        cls.prelude = '\n'.join(extract_function(source, name) for name in (
            'normalizeSource', 'eventsUrl', 'sseWatchDecision'))

    def decide(self, state, signal):
        return run_js(
            self.prelude,
            'sseWatchDecision(%s, %s)' % (json.dumps(state), json.dumps(signal)),
        )

    def test_events_url_matches_snapshot_selectors(self):
        got = run_js(self.prelude, (
            "(() => { const raw = eventsUrl('codex', 'codex:thread?&id');"
            " const url = new URL(raw, 'http://127.0.0.1');"
            " return {path: url.pathname, source: url.searchParams.get('source'),"
            " session: url.searchParams.get('session'), plain: eventsUrl('nope', '')}; })()"
        ))
        self.assertEqual(got['path'], '/events')
        self.assertEqual(got['source'], 'codex')
        self.assertEqual(got['session'], 'codex:thread?&id')
        self.assertEqual(got['plain'], '/events?source=all')

    def test_fallback_closes_a_stream_that_never_connected(self):
        unsupported = self.decide({'mode': 'poll', 'hadHello': False}, 'unsupported')
        self.assertEqual(unsupported, {'mode': 'poll', 'hadHello': False, 'refresh': False, 'close': True})
        failed = self.decide({'mode': 'poll', 'hadHello': False}, 'error')
        self.assertEqual(failed['mode'], 'poll')
        self.assertTrue(failed['close'])
        self.assertFalse(failed['refresh'])
        self.assertFalse(failed['hadHello'])

    def test_hello_refreshes_and_a_later_drop_keeps_the_stream_for_reconnect(self):
        hello = self.decide({'mode': 'poll', 'hadHello': False}, 'hello')
        self.assertEqual(hello, {'mode': 'sse', 'hadHello': True, 'refresh': True, 'close': False})
        refresh = self.decide({'mode': 'sse', 'hadHello': True}, 'refresh')
        self.assertTrue(refresh['refresh'])
        self.assertEqual(refresh['mode'], 'sse')
        dropped = self.decide({'mode': 'sse', 'hadHello': True}, 'error')
        self.assertEqual(dropped['mode'], 'poll')
        self.assertTrue(dropped['hadHello'])
        self.assertFalse(dropped['close'])
        self.assertFalse(dropped['refresh'])
        again = self.decide({'mode': 'poll', 'hadHello': True}, 'hello')
        self.assertEqual(again['mode'], 'sse')
        self.assertTrue(again['refresh'])

    def test_page_wires_event_source_and_keeps_the_poll_interval(self):
        html = HTML.read_text(encoding='utf-8')
        self.assertIn('new EventSource(eventsUrl(selectedSource, selectedSession))', html)
        self.assertIn('SNAPSHOT_POLL_MS = 1500', html)
        self.assertIn("es.addEventListener('hello'", html)
        self.assertIn("es.addEventListener('refresh'", html)
        self.assertIn('es.onerror', html)
        self.assertIn('openLiveEvents()', html)
