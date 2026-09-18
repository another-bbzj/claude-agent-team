"""ensure.py：发现端口上跑的是旧版本看板时，必须把它换成本机当前版本（否则页面新、服务旧 → 导入形象 not found）。"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OLD_SERVER = """
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps({'version': '0.9.0', 'members': []}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        self.send_response(404); self.end_headers()
    def log_message(self, *a):
        pass
HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
"""


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_json(port, timeout=8):
    for _ in range(int(timeout * 10)):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/snapshot.json', timeout=1) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception:
            time.sleep(0.1)
    return None


class EnsureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='teamensure-'))
        self.board = self.tmp / 'team-board'
        shutil.copytree(ROOT / 'team-board', self.board)
        (self.board / 'VERSION').write_text('9.9.9', encoding='utf-8')   # install.py 会把 VERSION 放进 team-board/
        self.port = free_port()
        self.env = {**os.environ, 'TEAM_BOARD_PORT': str(self.port), 'PYTHONIOENCODING': 'utf-8', 'HOME': str(self.tmp), 'USERPROFILE': str(self.tmp)}
        self.procs = []

    def tearDown(self):
        try:
            urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{self.port}/api/shutdown', data=b'{}', headers={'Content-Type': 'application/json'}), timeout=3).read()
        except Exception:
            pass
        for p in self.procs:
            try:
                p.kill()
            except Exception:
                pass
        time.sleep(0.3)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stale_server_is_replaced(self):
        old = subprocess.Popen([sys.executable, '-c', OLD_SERVER, str(self.port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(old)
        self.assertEqual(wait_json(self.port)['version'], '0.9.0')
        r = subprocess.run([sys.executable, str(self.board / 'ensure.py')], env=self.env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
        out = r.stdout + r.stderr
        self.assertIn('重启看板服务', out, out)
        self.assertIn('0.9.0', out)
        time.sleep(0.5)
        self.assertIsNotNone(old.poll(), '旧进程应已被结束')
        snap = wait_json(self.port, 15)
        self.assertIsNotNone(snap, '新服务应已在同一端口上')
        self.assertEqual(snap.get('version'), '9.9.9')

    def test_current_server_is_left_alone(self):
        subprocess.run([sys.executable, str(self.board / 'ensure.py')], env=self.env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
        snap = wait_json(self.port, 15)
        self.assertEqual(snap.get('version'), '9.9.9')
        r = subprocess.run([sys.executable, str(self.board / 'ensure.py')], env=self.env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
        self.assertNotIn('重启看板服务', r.stdout + r.stderr, '版本一致就不该重启')


if __name__ == '__main__':
    unittest.main()
