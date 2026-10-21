"""Installer option regressions, using a temporary install and mocked helpers."""
import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install_options', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallOptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='install-options-')
        self.addCleanup(self.tmp.cleanup)
        self.claude = Path(self.tmp.name) / 'home' / '.claude'
        self.claude_patch = mock.patch.object(installer, 'CLAUDE', self.claude)
        self.manifest_patch = mock.patch.object(installer, 'MANIFEST', self.claude / 'team-board' / '.installed.json')
        self.claude_patch.start()
        self.manifest_patch.start()
        self.addCleanup(self.claude_patch.stop)
        self.addCleanup(self.manifest_patch.stop)

    def install(self, *options, no_restart_env=False, helper_result=None):
        output = io.StringIO()
        result = helper_result or subprocess.CompletedProcess([], 0, 'helper complete\n', '')
        with mock.patch.dict(os.environ):
            os.environ.pop('TEAM_BOARD_NO_RESTART', None)
            if no_restart_env:
                os.environ['TEAM_BOARD_NO_RESTART'] = '1'
            with mock.patch.object(installer.sys, 'argv', ['install.py', *options]), \
                    mock.patch('subprocess.run', return_value=result) as run, \
                    contextlib.redirect_stdout(output):
                installer.main()
        helpers = [Path(call.args[0][1]).name for call in run.call_args_list]
        self.assertTrue((self.claude / 'team-board' / '.installed.json').is_file())
        for call in run.call_args_list:
            self.assertEqual(Path(call.args[0][1]).parent, self.claude / 'team-board')
        return helpers, output.getvalue()

    def test_no_restart_flag_keeps_local_and_explicit_petdex_imports(self):
        helpers, _ = self.install('--no-restart', '--petdex')
        self.assertEqual(helpers, ['import_codex_pets.py', 'fetch_petdex.py'])

    def test_no_restart_environment_keeps_independent_imports(self):
        helpers, _ = self.install('--petdex', no_restart_env=True)
        self.assertEqual(helpers, ['import_codex_pets.py', 'fetch_petdex.py'])

    def test_no_pets_skips_local_import_but_keeps_explicit_petdex(self):
        helpers, _ = self.install('--no-pets', '--no-restart', '--petdex')
        self.assertEqual(helpers, ['fetch_petdex.py'])

    def test_no_pets_without_petdex_restarts_only(self):
        helpers, _ = self.install('--no-pets')
        self.assertEqual(helpers, ['ensure.py'])

    def test_normal_install_restarts_before_optional_imports(self):
        helpers, _ = self.install('--petdex')
        self.assertEqual(helpers, ['ensure.py', 'import_codex_pets.py', 'fetch_petdex.py'])

    def test_restart_nonzero_warns_without_preventing_install_or_import(self):
        failed = subprocess.CompletedProcess([], 7, '', 'restart error')
        helpers, output = self.install(helper_result=failed)
        self.assertEqual(helpers, ['ensure.py', 'import_codex_pets.py'])
        self.assertIn('看板重启失败（不影响安装）', output)
        self.assertIn('7', output)
        self.assertIn('安装完成', output)


if __name__ == '__main__':
    unittest.main()
