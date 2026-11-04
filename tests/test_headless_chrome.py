"""Port parsing and startup retry for the shared headless Chrome helper."""
import os
import shutil
import signal
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import headless_chrome


class FakeProc(object):
    def __init__(self, returncode=None):
        self.pid = None
        self.returncode = returncode
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls += 1
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class Clock(object):
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _profile_from(cmd):
    for arg in cmd:
        if arg.startswith('--user-data-dir='):
            return arg.split('=', 1)[1]
    raise AssertionError('missing user-data-dir')


class ParseDevtoolsPortTest(unittest.TestCase):
    def test_parse_devtools_port_reads_the_active_file_or_a_listening_line(self):
        cases = [
            ('36445\n/devtools/browser/abc\n', 36445),
            ('36445\r\n/devtools/browser/abc\r\n', 36445),
            ('36445\n/devtools/browser/abc', 36445),
            ('  36445 \n /devtools/browser/abc \n', 36445),
            ('65535\n/devtools/browser/abc\n', 65535),
            ('12', None),
            ('36445\n', None),
            ('36445\n/tmp/not-devtools\n', None),
            ('0\n/devtools/browser/abc\n', None),
            ('65536\n/devtools/browser/abc\n', None),
            ('not-a-port\n/devtools/browser/abc\n', None),
            ('DevTools listening on ws://127.0.0.1:36445/devtools/browser/abc\n', 36445),
            ('noise\nDevTools listening on ws://[::1]:9222/devtools/browser/abc\n', 9222),
            (
                '[1:2:ERROR:dbus/bus.cc:405] Failed to connect\n\n'
                'DevTools listening on ws://127.0.0.1:36445/devtools/browser/abc\n',
                36445,
            ),
            ('DevTools listening on ws://127.0.0.1:36445', None),
            ('DevTools listening on ws://127.0.0.1:0/devtools/browser/abc\n', None),
            (
                'DevTools listening on ws://127.0.0.1:22222/devtools/browser/abc\n'
                '11111\n/devtools/browser/abc\n',
                22222,
            ),
            ('', None),
            (None, None),
            (b'36445\n/devtools/browser/abc\n', 36445),
            (3, None),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(headless_chrome.parse_devtools_port(text), expected)

    def test_read_devtools_port_prefers_a_complete_active_file(self):
        root = tempfile.mkdtemp(prefix='devtools-port-')
        try:
            profile = os.path.join(root, 'profile')
            os.makedirs(profile)
            stderr_path = os.path.join(root, 'stderr.txt')
            active = os.path.join(profile, 'DevToolsActivePort')
            with open(active, 'w', encoding='utf-8') as handle:
                handle.write('11111\n/devtools/browser/from-file\n')
            with open(stderr_path, 'w', encoding='utf-8') as handle:
                handle.write('DevTools listening on ws://127.0.0.1:22222/devtools/browser/from-stderr\n')
            self.assertEqual(headless_chrome.read_devtools_port(profile, stderr_path), 11111)
            with open(active, 'w', encoding='utf-8') as handle:
                handle.write('111')
            self.assertEqual(headless_chrome.read_devtools_port(profile, stderr_path), 22222)
            os.remove(active)
            self.assertEqual(headless_chrome.read_devtools_port(profile, ''), None)
        finally:
            shutil.rmtree(root)

    def test_chrome_binary_returns_the_first_name_on_path(self):
        def which(name):
            if name == 'google-chrome':
                return '/bin/google-chrome'
            return ''

        with mock.patch('headless_chrome.shutil.which', side_effect=which):
            self.assertEqual(headless_chrome.chrome_binary(), '/bin/google-chrome')

        with mock.patch('headless_chrome.shutil.which', return_value=''):
            self.assertEqual(headless_chrome.chrome_binary(), '')


class _LaunchHarness(object):
    def __init__(self):
        self.created = []
        self.commands = []
        self.procs = []
        self.spawn_error = None

    def mkdtemp(self, prefix='headless-chrome-', dir=None):
        path = tempfile.mkdtemp(prefix=prefix, dir=dir)
        self.created.append(path)
        return path

    def popen(self, cmd, **kwargs):
        self.commands.append(list(cmd))
        if self.spawn_error is not None and len(self.procs) == 0:
            self.procs.append(None)
            raise self.spawn_error
        proc = self.build_proc(cmd, kwargs.get('stderr'))
        self.procs.append(proc)
        return proc

    def build_proc(self, cmd, stderr):
        raise NotImplementedError

    def terminate(self, process):
        process.killed = True
        if process.returncode is None:
            process.returncode = -9

    def kwargs(self, **extra):
        params = {
            'popen': self.popen,
            'sleep': Clock().sleep,
            'clock': None,
            'mkdtemp': self.mkdtemp,
            'terminate': self.terminate,
        }
        params.update(extra)
        clock = params.pop('clock')
        if clock is None:
            clock = Clock()
        params['clock'] = clock.time
        params['sleep'] = clock.sleep
        params['_clock'] = clock
        return params


class LaunchRetryTest(unittest.TestCase):
    def _launch(self, harness, **extra):
        params = harness.kwargs(**extra)
        self.clock = params.pop('_clock')
        return headless_chrome.launch_headless_chrome('/bin/chrome', **params)

    def test_launch_returns_without_retry_when_the_first_port_is_ready(self):
        harness = _LaunchHarness()

        def build_proc(cmd, stderr):
            profile = _profile_from(cmd)
            with open(os.path.join(profile, 'DevToolsActivePort'), 'w', encoding='utf-8') as handle:
                handle.write('43210\n/devtools/browser/abc\n')
            stderr.write(b'noise before the port\n')
            return FakeProc()

        harness.build_proc = build_proc
        with self._launch(harness, ready=lambda port: port == 43210, timeout=5) as chrome:
            self.assertEqual(chrome.port, 43210)
            self.assertEqual(len(harness.procs), 1)
            self.assertFalse(harness.procs[0].killed)
            self.assertTrue(os.path.exists(harness.created[0]))
        self.assertTrue(harness.procs[0].killed)
        self.assertFalse(os.path.exists(harness.created[0]))
        self.assertEqual(self.clock.now, 0)
        cmd = harness.commands[0]
        self.assertEqual(cmd[0], '/bin/chrome')
        self.assertIn('--remote-debugging-port=0', cmd)
        self.assertNotIn('--remote-debugging-port=43210', cmd)
        self.assertEqual(cmd[-1], 'about:blank')

    def test_launch_waits_until_the_parsed_port_accepts_connections(self):
        harness = _LaunchHarness()
        state = {'polls': 0}

        def build_proc(cmd, stderr):
            profile = _profile_from(cmd)
            with open(os.path.join(profile, 'DevToolsActivePort'), 'w', encoding='utf-8') as handle:
                handle.write('43210\n/devtools/browser/abc\n')
            return FakeProc()

        def ready(port):
            state['polls'] += 1
            return state['polls'] >= 3

        harness.build_proc = build_proc
        with self._launch(harness, ready=ready, timeout=5) as chrome:
            self.assertEqual(chrome.port, 43210)
        self.assertGreaterEqual(state['polls'], 3)
        self.assertGreater(self.clock.now, 0)
        self.assertEqual(len(harness.procs), 1)

    def test_launch_retries_once_after_an_early_exit(self):
        harness = _LaunchHarness()

        def build_proc(cmd, stderr):
            if len(harness.procs) == 0:
                stderr.write(b'first-crash: sandbox failed\n')
                return FakeProc(returncode=1)
            profile = _profile_from(cmd)
            with open(os.path.join(profile, 'DevToolsActivePort'), 'w', encoding='utf-8') as handle:
                handle.write('43210\n/devtools/browser/abc\n')
            stderr.write(b'DevTools listening on ws://127.0.0.1:43210/devtools/browser/abc\n')
            return FakeProc()

        harness.build_proc = build_proc
        with self._launch(harness, ready=lambda port: True, timeout=15) as chrome:
            self.assertEqual(chrome.port, 43210)
        self.assertEqual(len(harness.commands), 2)
        self.assertNotEqual(
            _profile_from(harness.commands[0]),
            _profile_from(harness.commands[1]),
        )
        self.assertTrue(harness.procs[0].killed)
        self.assertEqual(self.clock.now, 0)
        for cmd in harness.commands:
            self.assertIn('--remote-debugging-port=0', cmd)
            self.assertFalse(any(
                arg.startswith('--remote-debugging-port=') and arg != '--remote-debugging-port=0'
                for arg in cmd
            ))
        self.assertFalse(os.path.exists(harness.created[0]))

    def test_launch_retries_once_after_a_timeout_and_reads_stderr(self):
        harness = _LaunchHarness()

        def build_proc(cmd, stderr):
            if len(harness.procs) == 0:
                stderr.write(b'dbus: Failed to connect to the bus\n')
                return FakeProc()
            profile = _profile_from(cmd)
            stderr.write(b'DevTools listening on ws://127.0.0.1:23232/devtools/browser/abc\n')
            # No active-port file: the listening line is the only source.
            self.assertFalse(os.path.exists(os.path.join(profile, 'DevToolsActivePort')))
            return FakeProc()

        harness.build_proc = build_proc
        with self._launch(harness, ready=lambda port: port == 23232, timeout=0.2) as chrome:
            self.assertEqual(chrome.port, 23232)
        self.assertEqual(len(harness.procs), 2)
        self.assertTrue(harness.procs[0].killed)
        self.assertGreaterEqual(self.clock.now, 0.2)

    def test_launch_failure_includes_both_stderr_tails_and_cleans_up(self):
        harness = _LaunchHarness()

        def build_proc(cmd, stderr):
            stderr.write(('crash-%d: address already in use\n' % (len(harness.procs) + 1)).encode('utf-8'))
            return FakeProc(returncode=1)

        harness.build_proc = build_proc
        with self.assertRaises(headless_chrome.ChromeLaunchError) as caught:
            self._launch(harness, timeout=5)
        message = str(caught.exception)
        self.assertIn('after 2 attempt', message)
        self.assertIn('crash-1: address already in use', message)
        self.assertIn('crash-2: address already in use', message)
        self.assertIn('exited with status 1', message)
        self.assertEqual(len(harness.procs), 2)
        self.assertEqual(self.clock.now, 0)
        for path in harness.created:
            self.assertFalse(os.path.exists(path))

    def test_launch_retries_a_spawn_error_then_uses_stderr(self):
        harness = _LaunchHarness()
        harness.spawn_error = OSError('exec failed')

        def build_proc(cmd, stderr):
            stderr.write(b'DevTools listening on ws://127.0.0.1:24000/devtools/browser/abc\n')
            return FakeProc()

        harness.build_proc = build_proc
        with self._launch(harness, ready=lambda port: True, timeout=1) as chrome:
            self.assertEqual(chrome.port, 24000)
        self.assertEqual(len(harness.commands), 2)

    def test_launch_reports_a_missing_binary_without_spawning(self):
        with self.assertRaises(headless_chrome.ChromeLaunchError) as caught:
            headless_chrome.launch_headless_chrome('')
        self.assertIn('missing', str(caught.exception))

    def test_launch_rejects_zero_attempts(self):
        with self.assertRaises(ValueError):
            headless_chrome.launch_headless_chrome('/bin/chrome', attempts=0)


class TerminateAndReadyTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, 'killpg') and hasattr(signal, 'SIGKILL'), 'POSIX process groups')
    def test_terminate_process_signals_the_process_group(self):
        proc = FakeProc()
        proc.pid = 424242
        with mock.patch('headless_chrome.os.killpg') as killpg:
            headless_chrome._terminate_process(proc)
        killpg.assert_called_once_with(424242, signal.SIGKILL)
        self.assertGreaterEqual(proc.wait_calls, 1)

    @unittest.skipUnless(hasattr(os, 'killpg') and hasattr(signal, 'SIGKILL'), 'POSIX process groups')
    def test_terminate_process_falls_back_to_kill(self):
        proc = FakeProc()
        proc.pid = 424242
        with mock.patch('headless_chrome.os.killpg', side_effect=OSError('gone')):
            headless_chrome._terminate_process(proc)
        self.assertTrue(proc.killed)

    def test_terminate_process_uses_taskkill_on_windows(self):
        proc = FakeProc()
        proc.pid = 424242
        with mock.patch('headless_chrome.os.name', 'nt'), \
                mock.patch('headless_chrome.subprocess.call') as call:
            headless_chrome._terminate_process(proc)
        args = call.call_args[0][0]
        self.assertEqual(args[:4], ['taskkill', '/F', '/T', '/PID'])
        self.assertEqual(args[4], '424242')

    def test_terminate_process_ignores_a_missing_process(self):
        headless_chrome._terminate_process(None)

    def test_devtools_ready_accepts_json_version(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/json/version':
                    body = b'{"webSocketDebuggerUrl":"ws://127.0.0.1/devtools/browser/x"}'
                    status = 200
                else:
                    body = b'no'
                    status = 404
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            self.assertTrue(headless_chrome.devtools_ready(port, timeout=1))
            closed = socket_port()
            self.assertFalse(headless_chrome.devtools_ready(closed, timeout=0.2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_devtools_ready_rejects_a_page_without_the_websocket(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"Browser":"Chrome"}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertFalse(headless_chrome.devtools_ready(server.server_address[1], timeout=1))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def socket_port():
    import socket
    probe = socket.socket()
    try:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


class RemoveChromeProfileTest(unittest.TestCase):
    def test_remove_chrome_profile_drops_a_tree_and_a_late_default_dir(self):
        root = Path(tempfile.mkdtemp(prefix='task-deps-cleanup-'))
        default = root / 'Default'
        default.mkdir()
        (default / 'Cookies').write_text('x', encoding='utf-8')
        headless_chrome.remove_chrome_profile(root, timeout=1)
        self.assertFalse(root.exists())

        raced = Path(tempfile.mkdtemp(prefix='task-deps-cleanup-'))
        (raced / 'Default').mkdir()
        real_rmtree = shutil.rmtree
        calls = {'n': 0}

        def flaky(path, ignore_errors=False, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1 and not ignore_errors:
                os.makedirs(os.path.join(path, 'Default'), exist_ok=True)
                raise OSError(39, 'Directory not empty', 'Default')
            return real_rmtree(path, ignore_errors=ignore_errors)

        with mock.patch('headless_chrome.shutil.rmtree', side_effect=flaky):
            headless_chrome.remove_chrome_profile(raced, timeout=1)
        self.assertGreaterEqual(calls['n'], 2)
        self.assertFalse(raced.exists())

    def test_remove_chrome_profile_gives_up_after_the_deadline(self):
        raced = Path(tempfile.mkdtemp(prefix='task-deps-cleanup-'))
        (raced / 'Default').mkdir()
        real_rmtree = shutil.rmtree
        calls = {'ignore': 0}

        def flaky(path, ignore_errors=False, **kwargs):
            if not ignore_errors:
                raise OSError(39, 'Directory not empty', 'Default')
            calls['ignore'] += 1
            return real_rmtree(path, ignore_errors=True)

        with mock.patch('headless_chrome.time.time', side_effect=[0, 100]), \
                mock.patch('headless_chrome.shutil.rmtree', side_effect=flaky):
            headless_chrome.remove_chrome_profile(raced, timeout=1)
        self.assertEqual(calls['ignore'], 1)
        self.assertFalse(raced.exists())


class WindowsUserDataDirTest(unittest.TestCase):
    def test_get_short_path_name_is_empty_off_windows(self):
        if os.name == 'nt':
            self.skipTest('windows resolves 8.3 names')
        self.assertEqual(headless_chrome._get_short_path_name('/tmp/headless-chrome'), '')

    def test_windows_temp_candidates_lead_with_the_drive_temp(self):
        with mock.patch.dict(os.environ, {
            'SystemDrive': 'C:',
            'RUNNER_TEMP': 'D:\\a\\_temp',
            'TEMP': 'C:\\Users\\runneradmin\\AppData\\Local\\Temp',
            'TMP': '',
        }), mock.patch('headless_chrome.tempfile.gettempdir', return_value='C:\\Windows\\Temp'):
            found = headless_chrome._windows_temp_candidates()
        self.assertEqual(found[0], 'C:\\Temp')
        self.assertEqual(found[1], 'D:\\a\\_temp')
        self.assertIn('C:\\Users\\runneradmin\\AppData\\Local\\Temp', found)
        self.assertIn('C:\\Windows\\Temp', found)
        self.assertNotIn('', found)

    def test_windows_short_path_keeps_a_real_directory_and_ignores_a_missing_one(self):
        path = tempfile.mkdtemp(prefix='profile-')
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        with mock.patch('headless_chrome._get_short_path_name', return_value=path):
            self.assertEqual(headless_chrome._windows_short_path(path), path)
        with mock.patch('headless_chrome._get_short_path_name', return_value=path + '-missing'):
            self.assertEqual(headless_chrome._windows_short_path(path), path)

    def test_windows_short_temp_base_prefers_the_8_3_name(self):
        base = tempfile.mkdtemp(prefix='longtemp-')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        long_name = os.path.join(base, 'Users', 'runneradmin', 'AppData', 'Local', 'Temp')
        os.makedirs(long_name)
        with mock.patch('headless_chrome._windows_temp_candidates', return_value=[long_name]), \
                mock.patch('headless_chrome._get_short_path_name', return_value=base):
            found = headless_chrome._windows_short_temp_base()
        self.assertEqual(os.path.normcase(found), os.path.normcase(base))

    def test_windows_short_temp_base_skips_an_unwritable_candidate(self):
        usable = tempfile.mkdtemp(prefix='fallback-temp-')
        self.addCleanup(shutil.rmtree, usable, ignore_errors=True)
        blocked = os.path.join(usable, 'blocked')
        with open(blocked, 'w', encoding='utf-8') as handle:
            handle.write('x')
        with mock.patch('headless_chrome._windows_temp_candidates', return_value=[blocked, usable]), \
                mock.patch('headless_chrome._get_short_path_name', return_value=''):
            found = headless_chrome._windows_short_temp_base()
        self.assertEqual(os.path.normcase(os.path.abspath(found)), os.path.normcase(os.path.abspath(usable)))

    def test_writable_dir_removes_its_probe_and_rejects_a_file(self):
        base = tempfile.mkdtemp(prefix='probe-')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        before = set(os.listdir(base))
        self.assertTrue(headless_chrome._writable_dir(base))
        self.assertEqual(set(os.listdir(base)), before)
        path = os.path.join(base, 'file')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('x')
        self.assertFalse(headless_chrome._writable_dir(path))

    def test_windows_launch_passes_a_short_user_data_dir_and_reads_the_active_port(self):
        harness = _LaunchHarness()
        base = tempfile.mkdtemp(prefix='win-temp-')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        short_profile = os.path.join(base, 'HC~1')
        os.makedirs(short_profile)
        seen = []

        def build_proc(cmd, stderr):
            profile = _profile_from(cmd)
            self.assertEqual(os.path.normcase(profile), os.path.normcase(short_profile))
            with open(os.path.join(profile, 'DevToolsActivePort'), 'wb') as handle:
                handle.write(b'43210\r\n/devtools/browser/abc\r\n')
            return FakeProc()

        def short_name(path):
            seen.append(path)
            if os.path.basename(path) == 'profile':
                return short_profile
            return ''

        harness.build_proc = build_proc
        env = {'SystemDrive': '', 'RUNNER_TEMP': '', 'TEMP': base, 'TMP': base}
        params = harness.kwargs(ready=lambda port: port == 43210, timeout=5)
        params.pop('_clock')
        with mock.patch('headless_chrome.os.name', 'nt'), \
                mock.patch.dict(os.environ, env), \
                mock.patch('headless_chrome._get_short_path_name', side_effect=short_name):
            with headless_chrome.launch_headless_chrome('/bin/chrome', **params) as chrome:
                self.assertEqual(chrome.port, 43210)
        cmd = harness.commands[0]
        self.assertIn('--remote-debugging-port=0', cmd)
        self.assertFalse(any(
            arg.startswith('--remote-debugging-port=') and arg != '--remote-debugging-port=0'
            for arg in cmd
        ))
        self.assertEqual(os.path.normcase(_profile_from(cmd)), os.path.normcase(short_profile))
        self.assertTrue(os.path.basename(harness.created[0]).startswith('hc'))
        self.assertTrue(any(os.path.basename(path) == 'profile' for path in seen))
        self.assertFalse(os.path.exists(harness.created[0]))


if __name__ == '__main__':
    unittest.main()
