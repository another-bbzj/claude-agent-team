"""Startup failure reporting, without spawning a service or touching user settings."""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ensure_stability', ROOT / 'team-board' / 'ensure.py')
ensure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ensure)


class StartupFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ensure-stability-')
        self.addCleanup(self.tmp.cleanup)
        self.here = mock.patch.object(ensure, 'HERE', Path(self.tmp.name))
        self.here.start()
        self.addCleanup(self.here.stop)

    def test_exited_child_does_not_report_success_and_closes_log(self):
        with mock.patch.object(ensure.subprocess, 'Popen') as spawn, \
                mock.patch.object(ensure, 'listening', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            spawn.return_value.poll.return_value = 2
            with self.assertRaisesRegex(RuntimeError, '2'):
                ensure.start_server()
            self.assertNotIn('started', output.getvalue())
            self.assertTrue(spawn.call_args.kwargs['stdout'].closed)

    def test_timeout_reaps_child_and_does_not_report_success(self):
        with mock.patch.object(ensure.subprocess, 'Popen') as spawn, \
                mock.patch.object(ensure, 'listening', return_value=False), \
                mock.patch.object(ensure.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            spawn.return_value.poll.return_value = None
            with self.assertRaises(RuntimeError):
                ensure.start_server()
            spawn.return_value.terminate.assert_called_once_with()
            spawn.return_value.wait.assert_called_once_with(timeout=5)
            self.assertNotIn('started', output.getvalue())

    def test_ready_child_reports_success(self):
        with mock.patch.object(ensure.subprocess, 'Popen') as spawn, \
                mock.patch.object(ensure, 'listening', return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            spawn.return_value.poll.return_value = None
            ensure.start_server()
            self.assertIn('started', output.getvalue())
            spawn.return_value.terminate.assert_not_called()

    def test_main_failure_exits_nonzero_and_does_not_open_browser(self):
        with mock.patch.object(ensure.sys, 'argv', ['ensure.py', '--open']), \
                mock.patch.object(ensure, 'listening', return_value=False), \
                mock.patch.object(ensure, 'start_server', side_effect=RuntimeError('failed')), \
                mock.patch.object(ensure, 'open_board') as open_board, \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(ensure.main(), 1)
            self.assertIn('failed', error.getvalue())
            open_board.assert_not_called()
