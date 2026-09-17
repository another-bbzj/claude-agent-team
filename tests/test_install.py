"""install.py 安装 / 卸载测试：在临时 HOME 上跑一遍，不碰真实 ~/.claude。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='teaminstall-'))
        self.home = self.tmp / 'home'
        (self.home / '.claude').mkdir(parents=True)
        (self.home / '.claude' / 'settings.json').write_text(json.dumps({'model': 'sonnet', 'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo hi'}]}]}}), encoding='utf-8')
        (self.home / '.claude' / 'CLAUDE.md').write_text('# 我的旧约定\n', encoding='utf-8')
        self.env = {**os.environ, 'HOME': str(self.home), 'USERPROFILE': str(self.home), 'PYTHONIOENCODING': 'utf-8'}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_install(self, *args):
        r = subprocess.run([sys.executable, str(ROOT / 'install.py'), *args], env=self.env, capture_output=True, text=True, encoding='utf-8', cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_install_then_uninstall(self):
        self.run_install('--no-pets')
        c = self.home / '.claude'
        self.assertEqual(len(list((c / 'agents').glob('*.md'))), 8)
        self.assertTrue((c / 'skills' / 'agent-team' / 'SKILL.md').exists())
        self.assertTrue((c / 'team-board' / 'team_board.py').exists())
        self.assertGreaterEqual(len(list((c / 'team-board' / 'sprites').glob('*.webp'))), 13)
        s = json.loads((c / 'settings.json').read_text(encoding='utf-8'))
        hooks = s['hooks']['SessionStart']
        self.assertEqual(hooks[0]['hooks'][0]['command'], 'echo hi', '原有钩子必须保留')
        self.assertTrue(hooks[1]['hooks'][0]['args'][0].endswith('team-board/ensure.py'))
        self.assertTrue(hooks[1]['hooks'][0]['args'][0].startswith(self.home.as_posix()), '钩子路径按本机生成')
        md = (c / 'CLAUDE.md').read_text(encoding='utf-8')
        self.assertIn('# 我的旧约定', md); self.assertIn('claude-agent-team:begin', md); self.assertIn('agent-team', md)
        # 重复安装不重复追加
        self.run_install('--no-pets')
        s = json.loads((c / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(len(s['hooks']['SessionStart']), 2)
        self.assertEqual((c / 'CLAUDE.md').read_text(encoding='utf-8').count('claude-agent-team:begin'), 1)
        # 卸载只移除自己加的
        self.run_install('--uninstall')
        s = json.loads((c / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(len(s['hooks']['SessionStart']), 1)
        self.assertEqual((c / 'CLAUDE.md').read_text(encoding='utf-8').strip(), '# 我的旧约定')
        self.assertFalse((c / 'team-board').exists())
        self.assertFalse((c / 'agents' / 'backend-dev.md').exists())


if __name__ == '__main__':
    unittest.main()
