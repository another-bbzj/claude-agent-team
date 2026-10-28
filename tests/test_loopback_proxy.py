"""Startup and update control requests must reach the board, not an HTTP proxy."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location('loopback_' + name, ROOT / 'team-board' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoopbackProxyTest(unittest.TestCase):
    def serve(self, respond):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                requests.append((self.command, self.path, body))
                status, payload = respond(self.command)
                self.send_response(status)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = handle_request
            do_POST = handle_request

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.addCleanup(close)
        return server.server_port, requests

    def setUp(self):
        self.stopped = False

        def board_response(method):
            if method == 'POST':
                self.stopped = True
                return 200, b'{}'
            if self.stopped:
                return 503, b'{}'
            return 200, json.dumps({'version': '9.9.9'}).encode('utf-8')

        self.port, self.board_requests = self.serve(board_response)
        proxy_port, self.proxy_requests = self.serve(lambda method: (200, b'proxy-version'))
        proxy_url = 'http://127.0.0.1:%s' % proxy_port
        env = {key: proxy_url for key in ('http_proxy', 'HTTP_PROXY', 'https_proxy', 'HTTPS_PROXY',
                                         'all_proxy', 'ALL_PROXY')}
        env.update({'no_proxy': '', 'NO_PROXY': ''})
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        # Windows registry bypass rules and a cached default opener must not mask the bug.
        for patch in (mock.patch.object(urllib.request, 'proxy_bypass', return_value=False),
                      mock.patch.object(urllib.request, '_opener', None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.ensure = load_script('ensure')
        self.update = load_script('update')
        self.ensure.PORT = self.update.PORT = self.port

    def test_running_version_bypasses_proxy(self):
        self.assertEqual(self.ensure.running_version(), '9.9.9')
        self.assertEqual(self.board_requests, [('GET', '/snapshot.json', b'')])
        self.assertEqual(self.proxy_requests, [])

    def test_current_server_is_not_restarted_with_proxy(self):
        with mock.patch.object(self.ensure.sys, 'argv', ['ensure.py']), \
                mock.patch.object(self.ensure, 'listening', return_value=True), \
                mock.patch.object(self.ensure, 'local_version', return_value='9.9.9'), \
                mock.patch.object(self.ensure, 'stop_server') as stop, \
                mock.patch.object(self.ensure, 'start_server') as start:
            self.ensure.main()
        stop.assert_not_called()
        start.assert_not_called()
        self.assertEqual(self.proxy_requests, [])

    def test_shutdown_avoids_force_kill_with_proxy(self):
        with mock.patch.object(self.ensure, 'listening', side_effect=lambda: not self.stopped), \
                mock.patch.object(self.ensure, 'pids_on_port', return_value=set()) as pids, \
                mock.patch.object(self.ensure.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.ensure.stop_server('test')
        self.assertEqual(self.board_requests, [('POST', '/api/shutdown', b'{}')])
        pids.assert_not_called()
        self.assertEqual(self.proxy_requests, [])

    def test_update_restart_reaches_local_shutdown_and_poll(self):
        with mock.patch.object(self.update.time, 'sleep'), \
                mock.patch.object(self.update.subprocess, 'run',
                                  return_value=subprocess.CompletedProcess([], 0)) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.update.restart_board(), 0)
        self.assertEqual(self.board_requests, [('POST', '/api/shutdown', b'{}'),
                                              ('GET', '/snapshot.json', b'')])
        run.assert_called_once()
        self.assertEqual(self.proxy_requests, [])

    def test_remote_download_keeps_environment_proxy(self):
        url = 'http://updates.invalid/VERSION'
        self.assertEqual(self.update.fetch(url), b'proxy-version')
        self.assertEqual(self.proxy_requests, [('GET', url, b'')])
        self.assertEqual(self.board_requests, [])


if __name__ == '__main__':
    unittest.main()
