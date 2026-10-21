"""doctor.py：端口上的旧看板、缺失的进程工具，以及 ensure 会误报启动成功的组合。"""
import contextlib
import importlib.util
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('team_board_doctor', ROOT / 'team-board' / 'doctor.py')
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def write_board(root, version='1.4.2', team=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / 'team_board.py').write_text('# board\n', encoding='utf-8')
    (root / 'index.html').write_text('<html></html>\n', encoding='utf-8')
    (root / 'ensure.py').write_text('# ensure\n', encoding='utf-8')
    (root / 'VERSION').write_text(version + '\n', encoding='utf-8')
    if team is None:
        team = {'agents': {'lead': {}}, 'departments': []}
    if team is not False:
        (root / 'team.json').write_text(json.dumps(team), encoding='utf-8')
    return root


def by_id(report, check_id):
    return next(item for item in report['checks'] if item['id'] == check_id)


class SnapshotServer:
    def __init__(self, body, status=200):
        payload = body if isinstance(body, bytes) else body.encode('utf-8')

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.port = free_port()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


class ParseAndFilesTest(unittest.TestCase):
    def test_parse_port_accepts_edges_and_rejects_garbage(self):
        self.assertEqual(doctor.parse_port('7788'), (7788, ''))
        self.assertEqual(doctor.parse_port(' 80 '), (80, ''))
        self.assertEqual(doctor.parse_port(443), (443, ''))
        self.assertIsNone(doctor.parse_port('abc')[0])
        self.assertIsNone(doctor.parse_port('0')[0])
        self.assertIsNone(doctor.parse_port('65536')[0])
        self.assertIsNone(doctor.parse_port('')[0])

    def test_version_file_prefers_the_board_directory(self):
        with tempfile.TemporaryDirectory(prefix='doctor-ver-') as tmp:
            parent = Path(tmp)
            (parent / 'VERSION').write_text('1.0.0\n', encoding='utf-8')
            root = write_board(parent / 'team-board', version='   ')
            self.assertEqual(doctor.read_local_version(root), '1.0.0')
            (root / 'VERSION').write_text('9.9.9\n', encoding='utf-8')
            self.assertEqual(doctor.read_local_version(root), '9.9.9')

    def test_layout_rejects_empty_files_and_config_rejects_bad_shapes(self):
        with tempfile.TemporaryDirectory(prefix='doctor-files-') as tmp:
            root = write_board(Path(tmp) / 'board')
            (root / 'index.html').write_text('', encoding='utf-8')
            self.assertEqual(doctor.layout_check(root)['status'], 'fail')
            (root / 'index.html').write_text('<html></html>\n', encoding='utf-8')
            (root / 'team.json').write_text('{', encoding='utf-8')
            self.assertIn('合法 JSON', doctor.config_check(root)['summary'])
            (root / 'team.json').write_text('[]', encoding='utf-8')
            self.assertIn('对象', doctor.config_check(root)['summary'])
            (root / 'team.json').write_text(json.dumps({'agents': []}), encoding='utf-8')
            self.assertIn('departments', doctor.config_check(root)['summary'])
            (root / 'team.json').write_bytes(b'\xff')
            self.assertIn('UTF-8', doctor.config_check(root)['summary'])

    def test_unwritable_directory_fails(self):
        with tempfile.TemporaryDirectory(prefix='doctor-ro-') as tmp:
            root = Path(tmp)
            with mock.patch.object(doctor.tempfile, 'mkstemp', side_effect=OSError('read-only')):
                item = doctor.writable_check(root)
            self.assertEqual(item['status'], 'fail')
            self.assertIn('server.log', item['summary'])


class EnvironmentCheckTest(unittest.TestCase):
    def test_old_python_and_non_utf8_console(self):
        with mock.patch.object(doctor.sys, 'version_info', (3, 7, 9, 'final', 0)):
            self.assertEqual(doctor.python_check()['status'], 'fail')
        self.assertEqual(doctor.python_check()['status'], 'ok')

        class Stream:
            encoding = 'cp936'

        with mock.patch.object(doctor.sys, 'stdout', Stream()), mock.patch.object(doctor.sys, 'stderr', Stream()):
            item = doctor.encoding_check()
        self.assertEqual(item['status'], 'warn')
        self.assertIn('PYTHONIOENCODING', item['hint'])

    def test_process_tool_follows_the_platform_ensure_uses(self):
        with mock.patch.object(doctor, '_is_windows', return_value=True), \
                mock.patch.object(doctor.shutil, 'which', side_effect=lambda name: name == 'netstat' and 'netstat.exe'):
            item = doctor.tool_check()
        self.assertEqual(item['status'], 'ok')
        self.assertIn('netstat', item['summary'])
        self.assertNotIn('lsof', item['summary'])

        with mock.patch.object(doctor, '_is_windows', return_value=False), \
                mock.patch.object(doctor.shutil, 'which', return_value=None):
            item = doctor.tool_check()
        self.assertEqual(item['status'], 'warn')
        self.assertIn('lsof', item['summary'])
        self.assertIn('ss', item['summary'])
        self.assertIn('fuser', item['summary'])
        self.assertIn('iproute2', item['hint'])
        self.assertIn('psmisc', item['hint'])
        self.assertIn('lsof', item['hint'])

    def test_only_ss_or_only_fuser_is_enough(self):
        def only(name):
            return '/usr/bin/' + name

        with mock.patch.object(doctor, '_is_windows', return_value=False), \
                mock.patch.object(doctor.shutil, 'which', side_effect=lambda name: only('ss') if name == 'ss' else None):
            ss_only = doctor.tool_check()
        self.assertEqual(ss_only['status'], 'ok')
        self.assertIn('ss', ss_only['summary'])
        self.assertNotIn('lsof', ss_only['summary'])
        self.assertNotIn('fuser', ss_only['summary'])

        with mock.patch.object(doctor, '_is_windows', return_value=False), \
                mock.patch.object(doctor.shutil, 'which', side_effect=lambda name: only('fuser') if name == 'fuser' else None):
            fuser_only = doctor.tool_check()
        self.assertEqual(fuser_only['status'], 'ok')
        self.assertIn('fuser', fuser_only['summary'])
        self.assertNotIn('lsof', fuser_only['summary'])
        self.assertNotIn('ss（', fuser_only['summary'])

    def test_pillow_status_matches_whether_it_imports(self):
        item = doctor.pillow_check()
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.assertEqual(item['status'], 'warn')
        else:
            self.assertEqual(item['status'], 'ok')


class ProbeTest(unittest.TestCase):
    def test_snapshot_proxy_and_non_board(self):
        server = SnapshotServer(json.dumps({'version': '0.9.0', 'members': []}))
        self.addCleanup(server.close)
        env = {
            'http_proxy': 'http://127.0.0.1:9',
            'HTTP_PROXY': 'http://127.0.0.1:9',
            'https_proxy': 'http://127.0.0.1:9',
            'HTTPS_PROXY': 'http://127.0.0.1:9',
        }
        with mock.patch.dict(os.environ, env):
            probed = doctor.probe_port(server.port)
        self.assertTrue(probed['board'])
        self.assertEqual(probed['version'], '0.9.0')

        other = SnapshotServer('not-json')
        self.addCleanup(other.close)
        probed = doctor.probe_port(other.port)
        self.assertTrue(probed['listening'])
        self.assertFalse(probed['board'])
        self.assertEqual(probed['error'], 'JSONDecodeError')

        closed = doctor.probe_port(free_port())
        self.assertFalse(closed['listening'])


class DiagnosisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='doctor-diag-')
        self.addCleanup(self.tmp.cleanup)
        self.root = write_board(Path(self.tmp.name) / 'team-board')

    def diagnose(self, probed, tools=('lsof',)):
        wanted = set(tools)

        def which(name):
            return '/usr/bin/' + name if name in wanted else None

        with mock.patch.object(doctor, '_is_windows', return_value=False), \
                mock.patch.object(doctor, 'probe_port', return_value=probed), \
                mock.patch.object(doctor.shutil, 'which', side_effect=which):
            return doctor.diagnose(self.root, 7788)

    def test_free_port_with_tool_is_ok(self):
        report = self.diagnose({'listening': False, 'board': False, 'version': '', 'error': ''})
        self.assertTrue(report['ok'])
        self.assertEqual(by_id(report, 'restart')['status'], 'ok')
        self.assertEqual(by_id(report, 'port')['status'], 'ok')

    def test_free_port_without_tool_warns_but_does_not_fail(self):
        report = self.diagnose({'listening': False, 'board': False, 'version': '', 'error': ''}, tools=())
        self.assertTrue(report['ok'])
        self.assertEqual(by_id(report, 'process-tool')['status'], 'warn')
        self.assertEqual(by_id(report, 'restart')['status'], 'warn')
        self.assertIn('误判', by_id(report, 'restart')['summary'])

    def test_stale_board_without_process_tool_is_a_failure(self):
        probed = {'listening': True, 'board': True, 'version': '0.9.0', 'error': ''}
        report = self.diagnose(probed, tools=())
        self.assertFalse(report['ok'])
        restart = by_id(report, 'restart')
        self.assertEqual(restart['status'], 'fail')
        self.assertIn('v0.9.0', restart['summary'])
        self.assertIn('v1.4.2', restart['summary'])
        self.assertIn('当成启动成功', restart['summary'])
        self.assertIn('iproute2', restart['hint'])
        self.assertIn('psmisc', restart['hint'])

    def test_stale_board_with_only_ss_or_only_fuser_can_be_replaced(self):
        probed = {'listening': True, 'board': True, 'version': '0.9.0', 'error': ''}
        for tools in (('ss',), ('fuser',)):
            report = self.diagnose(probed, tools=tools)
            self.assertTrue(report['ok'], tools)
            self.assertEqual(by_id(report, 'process-tool')['status'], 'ok')
            self.assertIn(tools[0], by_id(report, 'process-tool')['summary'])
            self.assertEqual(by_id(report, 'restart')['status'], 'warn')

    def test_stale_board_with_tool_can_be_replaced(self):
        probed = {'listening': True, 'board': True, 'version': '0.9.0', 'error': ''}
        report = self.diagnose(probed, tools=('lsof',))
        self.assertTrue(report['ok'])
        restart = by_id(report, 'restart')
        self.assertEqual(restart['status'], 'warn')
        self.assertIn('ensure.py', restart['summary'])

    def test_current_version_is_left_alone(self):
        probed = {'listening': True, 'board': True, 'version': '1.4.2', 'error': ''}
        report = self.diagnose(probed)
        self.assertEqual(by_id(report, 'port')['status'], 'ok')
        self.assertEqual(by_id(report, 'restart')['status'], 'ok')

    def test_current_version_without_tool_warns_about_the_next_upgrade(self):
        probed = {'listening': True, 'board': True, 'version': '1.4.2', 'error': ''}
        report = self.diagnose(probed, tools=())
        self.assertTrue(report['ok'])
        self.assertEqual(by_id(report, 'restart')['status'], 'warn')
        self.assertIn('下次升级', by_id(report, 'restart')['summary'])

    def test_missing_local_version_is_not_called_a_failed_restart(self):
        (self.root / 'VERSION').write_text(' \n', encoding='utf-8')
        probed = {'listening': True, 'board': True, 'version': '0.9.0', 'error': ''}
        report = self.diagnose(probed, tools=())
        self.assertTrue(report['ok'])
        self.assertEqual(by_id(report, 'version')['status'], 'warn')
        self.assertEqual(by_id(report, 'restart')['status'], 'warn')
        self.assertIn('没有 VERSION', by_id(report, 'restart')['summary'])

    def test_foreign_listener_is_not_killed(self):
        probed = {'listening': True, 'board': False, 'version': '', 'error': 'JSONDecodeError'}
        report = self.diagnose(probed)
        self.assertFalse(report['ok'])
        self.assertEqual(by_id(report, 'port')['status'], 'fail')
        self.assertEqual(by_id(report, 'restart')['status'], 'fail')
        self.assertIn('未知进程', by_id(report, 'port')['hint'])
        self.assertIn('TEAM_BOARD_PORT', by_id(report, 'restart')['hint'])


class LiveServerTest(unittest.TestCase):
    def test_real_stale_server_is_described_without_touching_it(self):
        server = SnapshotServer(json.dumps({'version': '0.9.0', 'members': []}))
        self.addCleanup(server.close)
        with tempfile.TemporaryDirectory(prefix='doctor-live-') as tmp:
            root = write_board(Path(tmp) / 'team-board', version='9.9.9')
            before = doctor._OPENER.open('http://127.0.0.1:%s/snapshot.json' % server.port, timeout=2).read()
            with mock.patch.object(doctor, '_is_windows', return_value=False), \
                    mock.patch.object(doctor.shutil, 'which', return_value=None):
                report = doctor.diagnose(root, server.port)
            after = doctor._OPENER.open('http://127.0.0.1:%s/snapshot.json' % server.port, timeout=2).read()
        self.assertEqual(before, after)
        self.assertFalse(report['ok'])
        restart = by_id(report, 'restart')
        self.assertEqual(restart['status'], 'fail')
        self.assertIn('0.9.0', restart['summary'])
        self.assertIn('9.9.9', restart['summary'])


class CliTest(unittest.TestCase):
    def test_json_strict_and_invalid_port(self):
        with tempfile.TemporaryDirectory(prefix='doctor-cli-') as tmp:
            root = write_board(Path(tmp) / 'team-board')
            probed = {'listening': False, 'board': False, 'version': '', 'error': ''}
            with mock.patch.object(doctor, '_is_windows', return_value=False), \
                    mock.patch.object(doctor, 'probe_port', return_value=probed), \
                    mock.patch.object(doctor.shutil, 'which', side_effect=lambda name: '/usr/bin/lsof' if name == 'lsof' else None), \
                    mock.patch.object(doctor, 'pillow_check', return_value=doctor.check('pillow', 'warn', '未安装 Pillow', 'pip install pillow')):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = doctor.main(['--json', '--root', str(root), '--port', '7788'])
                report = json.loads(buf.getvalue())
                self.assertEqual(code, 0)
                self.assertTrue(report['ok'])
                self.assertEqual(report['counts']['warn'], 1)
                self.assertIn('pillow', [item['id'] for item in report['checks']])
                with contextlib.redirect_stdout(io.StringIO()):
                    strict = doctor.main(['--json', '--strict', '--root', str(root), '--port', '7788'])
                self.assertEqual(strict, 1)

                text = io.StringIO()
                with contextlib.redirect_stdout(text):
                    failed = doctor.main(['--root', str(root), '--port', 'nope'])
                self.assertEqual(failed, 1)
                self.assertIn('端口无效', text.getvalue())

            with mock.patch.dict(os.environ, {'TEAM_BOARD_PORT': '65536'}):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = doctor.main(['--json', '--root', str(root)])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(buf.getvalue())['ok'])


if __name__ == '__main__':
    unittest.main()
