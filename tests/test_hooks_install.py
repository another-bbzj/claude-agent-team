"""install.py --hooks 合并 / 幂等 / 卸载。临时家目录，不碰真实 ~/.claude。"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LIVE_EVENTS = ('PreToolUse', 'PostToolUse', 'SubagentStop', 'Notification', 'Stop')


def load_installer():
    spec = importlib.util.spec_from_file_location('install_hooks', ROOT / 'install.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def relay_handlers(data, event):
    found = []
    for entry in (data.get('hooks') or {}).get(event) or []:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get('hooks') or []:
            if not isinstance(hook, dict):
                continue
            blob = ' '.join([str(hook.get('command') or '')] + [str(v) for v in (hook.get('args') or [])])
            if 'hook_relay.py' in blob.replace('\\', '/'):
                found.append((entry, hook))
    return found


class HooksInstallCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='teamhooks-'))
        self.home = self.tmp / 'home'
        claude = self.home / '.claude'
        claude.mkdir(parents=True)
        self.settings = claude / 'settings.json'
        self.original = {
            'model': 'sonnet',
            'hooks': {
                'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo hi'}]}],
                'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo keep-pre'}]}],
                'Stop': [{'hooks': [{'type': 'command', 'command': 'echo keep-stop'}]}],
            },
        }
        self.settings.write_text(json.dumps(self.original), encoding='utf-8')
        self.env = {**os.environ, 'HOME': str(self.home), 'USERPROFILE': str(self.home),
                    'PYTHONIOENCODING': 'utf-8', 'TEAM_BOARD_NO_RESTART': '1'}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_install(self, *args):
        proc = subprocess.run([sys.executable, str(ROOT / 'install.py'), *args], env=self.env,
                              capture_output=True, text=True, encoding='utf-8', cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def read(self):
        return json.loads(self.settings.read_text(encoding='utf-8'))

    def assert_one_relay(self, data, event):
        found = relay_handlers(data, event)
        self.assertEqual(len(found), 1, event)
        entry, hook = found[0]
        self.assertEqual(hook['type'], 'command')
        self.assertEqual(hook['command'], 'python')
        self.assertIsInstance(hook['args'], list)
        self.assertEqual(len(hook['args']), 1)
        self.assertTrue(hook['args'][0].endswith('team-board/hook_relay.py'))
        self.assertTrue(hook['args'][0].startswith(self.home.as_posix()))
        self.assertEqual(hook['timeout'], 2)
        self.assertNotIn('statusMessage', hook)
        self.assertNotIn('async', hook)
        self.assertNotIn('onFailure', hook)
        self.assertNotIn('matcher', entry)

    def test_hooks_merge_are_idempotent_and_uninstall(self):
        plain_out = self.run_install('--no-pets')
        self.assertNotIn('实时钩子', plain_out)
        plain = self.read()
        self.assertEqual(plain['model'], 'sonnet')
        self.assertEqual(plain['hooks']['PreToolUse'], self.original['hooks']['PreToolUse'])
        self.assertEqual(plain['hooks']['Stop'], self.original['hooks']['Stop'])
        self.assertNotIn('Notification', plain['hooks'])
        self.assertNotIn('PostToolUse', plain['hooks'])
        self.assertEqual(plain['hooks']['SessionStart'][0]['hooks'][0]['command'], 'echo hi')
        self.assertEqual(len(relay_handlers(plain, 'PreToolUse')), 0)

        first = self.run_install('--hooks', '--no-pets')
        self.assertIn('实时钩子', first)
        hooked = self.read()
        for event in LIVE_EVENTS:
            self.assert_one_relay(hooked, event)
        self.assertEqual(hooked['hooks']['PreToolUse'][0]['hooks'][0]['command'], 'echo keep-pre')
        self.assertEqual(hooked['hooks']['Stop'][0]['hooks'][0]['command'], 'echo keep-stop')
        self.assertEqual(hooked['model'], 'sonnet')
        ensures = [h for entry in hooked['hooks']['SessionStart'] for h in entry.get('hooks', [])
                   if 'ensure.py' in json.dumps(h)]
        self.assertEqual(len(ensures), 1)

        self.run_install('--hooks', '--no-pets')
        self.assertEqual(self.read(), hooked)

        self.run_install('--no-pets')
        self.assertEqual(self.read(), hooked, '不加 --hooks 的更新不能卸掉已经装上的实时钩子')

        self.run_install('--uninstall')
        gone = self.read()
        self.assertEqual(gone['model'], 'sonnet')
        self.assertEqual(gone['hooks']['SessionStart'], self.original['hooks']['SessionStart'])
        self.assertEqual(gone['hooks']['PreToolUse'], self.original['hooks']['PreToolUse'])
        self.assertEqual(gone['hooks']['Stop'], self.original['hooks']['Stop'])
        for event in ('PostToolUse', 'SubagentStop', 'Notification'):
            self.assertNotIn(event, gone['hooks'])
        self.assertFalse((self.home / '.claude' / 'team-board').exists())


class HooksInstallMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hooks-merge-')
        self.addCleanup(self.tmp.cleanup)
        self.claude = Path(self.tmp.name)
        self.installer = load_installer()
        patch_claude = mock.patch.object(self.installer, 'CLAUDE', self.claude)
        patch_manifest = mock.patch.object(self.installer, 'MANIFEST', self.claude / 'team-board' / '.installed.json')
        patch_claude.start()
        patch_manifest.start()
        self.addCleanup(patch_claude.stop)
        self.addCleanup(patch_manifest.stop)

    def test_windows_path_and_shared_group_round_trip(self):
        other = {'type': 'command', 'command': 'echo keep'}
        self.claude.joinpath('settings.json').write_text(json.dumps({'model': 'sonnet', 'hooks': {
            'PreToolUse': [{'matcher': 'Bash', 'hooks': [
                other,
                {'type': 'command', 'command': 'python',
                 'args': [r'C:\Users\example\.claude\team-board\hook_relay.py'], 'timeout': 2},
            ]}],
            'Stop': [{'hooks': [{'type': 'command', 'command': 'echo stopped'}]}],
        }}), encoding='utf-8')
        self.installer.merge_settings(True, live=True)
        once = json.loads(self.claude.joinpath('settings.json').read_text(encoding='utf-8'))
        self.installer.merge_settings(True, live=True)
        self.assertEqual(json.loads(self.claude.joinpath('settings.json').read_text(encoding='utf-8')), once)
        groups = once['hooks']['PreToolUse']
        self.assertEqual(groups[0], {'matcher': 'Bash', 'hooks': [other]})
        self.assertEqual(len(relay_handlers(once, 'PreToolUse')), 1)
        self.assertEqual(once['hooks']['Stop'][0]['hooks'][0]['command'], 'echo stopped')
        self.installer.merge_settings(False, live=False)
        left = json.loads(self.claude.joinpath('settings.json').read_text(encoding='utf-8'))
        self.assertEqual(left['hooks']['PreToolUse'], [{'matcher': 'Bash', 'hooks': [other]}])
        self.assertEqual(left['hooks']['Stop'], [{'hooks': [{'type': 'command', 'command': 'echo stopped'}]}])
        self.assertNotIn('PostToolUse', left['hooks'])

    def test_plain_merge_does_not_create_live_events(self):
        self.installer.merge_settings(True, live=None)
        data = json.loads(self.claude.joinpath('settings.json').read_text(encoding='utf-8'))
        self.assertIn('SessionStart', data['hooks'])
        for event in LIVE_EVENTS:
            self.assertNotIn(event, data['hooks'])


if __name__ == '__main__':
    unittest.main()
