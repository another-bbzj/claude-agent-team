"""Updater cleanup and restart result propagation with in-memory archives."""
import contextlib
import importlib.util
import io
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('update_stability', ROOT / 'team-board' / 'update.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


def archive(*names):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zipped:
        for name in names:
            zipped.writestr(name, '')
    return buffer.getvalue()


class UpdateFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='update-stability-')
        self.addCleanup(self.tmp.cleanup)
        temp_root = mock.patch.object(tempfile, 'tempdir', self.tmp.name)
        temp_root.start()
        self.addCleanup(temp_root.stop)
        factory = tempfile.TemporaryDirectory
        patch = mock.patch.object(update.tempfile, 'TemporaryDirectory',
                                  side_effect=lambda **kw: factory(dir=self.tmp.name, **kw))
        patch.start()
        self.addCleanup(patch.stop)

    def test_success_removes_extracted_archive(self):
        def install(args, **kwargs):
            self.assertTrue(Path(args[1]).is_file())
            return subprocess.CompletedProcess(args, 0, 'ok', '')
        with mock.patch.object(update, 'fetch', return_value=archive('project/install.py')), \
                mock.patch.object(update.subprocess, 'run', side_effect=install):
            self.assertEqual(update.apply(), 0)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_installer_failure_removes_extracted_archive(self):
        with mock.patch.object(update, 'fetch', return_value=archive('project/install.py')), \
                mock.patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 7, '', 'failed')):
            self.assertEqual(update.apply(), 7)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_ambiguous_archive_is_rejected_without_running_installer(self):
        with mock.patch.object(update, 'fetch', return_value=archive('first/install.py', 'second/install.py')), \
                mock.patch.object(update.subprocess, 'run') as run:
            self.assertEqual(update.apply(), 1)
            run.assert_not_called()
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_invalid_archive_is_cleaned_up(self):
        with mock.patch.object(update, 'fetch', return_value=b'incomplete zip'):
            with self.assertRaises(zipfile.BadZipFile):
                update.apply()
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_apply_propagates_restart_failure(self):
        with mock.patch.object(update, 'fetch', return_value=archive('project/install.py')), \
                mock.patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
                mock.patch.object(update, 'restart_board', return_value=5):
            self.assertEqual(update.apply(restart=True), 5)

    def test_restart_failure_does_not_report_success(self):
        with mock.patch.object(update.urllib.request, 'urlopen', side_effect=OSError('offline')), \
                mock.patch.object(update.time, 'sleep'), \
                mock.patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 4)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(update.restart_board(), 4)
            self.assertNotIn('已重启到新版本', output.getvalue())
