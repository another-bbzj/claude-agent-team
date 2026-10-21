"""Installer regressions: preserve unrelated hooks in shared SessionStart groups."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install_stability', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class SettingsPreservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='install-stability-')
        self.addCleanup(self.tmp.cleanup)
        self.claude = Path(self.tmp.name)
        self.patch = mock.patch.object(installer, 'CLAUDE', self.claude)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.path = self.claude / 'settings.json'

    def write(self, entries):
        self.original = {'model': 'sonnet', 'hooks': {
            'SessionStart': entries,
            'Stop': [{'hooks': [{'type': 'command', 'command': 'echo stopped'}]}],
        }}
        self.path.write_text(json.dumps(self.original), encoding='utf-8')

    def read(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def test_uninstall_preserves_other_hooks_and_group_metadata(self):
        other = {'type': 'command', 'command': 'echo user-hook', 'timeout': 30}
        self.write([{'matcher': 'startup', 'hooks': [
            {'type': 'command', 'command': 'python',
             'args': ['/tmp/.claude/team-board/ensure.py']}, other,
        ]}])
        installer.merge_settings(False)
        data = self.read()
        self.assertEqual(data['hooks']['SessionStart'],
                         [{'matcher': 'startup', 'hooks': [other]}])
        self.assertEqual(data['model'], 'sonnet')
        self.assertEqual(data['hooks']['Stop'], self.original['hooks']['Stop'])

    def test_reinstall_is_idempotent_for_mixed_hook_groups(self):
        other = {'type': 'command', 'command': 'echo keep'}
        self.write([{'matcher': 'startup', 'hooks': [
            other, {'type': 'command', 'command': 'python /tmp/team-board/ensure.py --open'},
        ]}])
        installer.merge_settings(True)
        once = self.read()
        installer.merge_settings(True)
        self.assertEqual(self.read(), once)
        entries = once['hooks']['SessionStart']
        self.assertEqual(entries[0], {'matcher': 'startup', 'hooks': [other]})
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(entries[1]['hooks']), 1)

    def test_windows_hook_path_is_removed_without_deleting_siblings(self):
        other = {'type': 'command', 'command': 'echo keep'}
        self.write([{'hooks': [
            {'type': 'command', 'command': 'python',
             'args': [r'C:\Users\example\.claude\team-board\ensure.py']}, other,
        ]}])
        installer.merge_settings(False)
        self.assertEqual(self.read()['hooks']['SessionStart'], [{'hooks': [other]}])

    def test_group_metadata_mentioning_script_does_not_delete_user_hooks(self):
        entries = [{'description': 'Run after team-board/ensure.py',
                    'hooks': [{'type': 'command', 'command': 'echo keep'}]}]
        self.write(entries)
        installer.merge_settings(False)
        self.assertEqual(self.read()['hooks']['SessionStart'], entries)


if __name__ == '__main__':
    unittest.main()
