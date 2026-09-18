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
        self.env = {**os.environ, 'HOME': str(self.home), 'USERPROFILE': str(self.home), 'PYTHONIOENCODING': 'utf-8', 'TEAM_BOARD_NO_RESTART': '1'}

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
        # 用户改动：自建部门 / 成员 / 形象 / 价格 / 改过的成员 .md / 导入的形象 → 重复安装（= 更新）全部保留
        tj = c / 'team-board' / 'team.json'
        cfg = json.loads(tj.read_text(encoding='utf-8'))
        cfg['departments'].append({'id': 'design', 'name': '设计部', 'icon': '🎨', 'required': 'optional'})
        cfg['agents']['my-designer'] = {'dept': 'design', 'avatar': 'ui-designer', 'name': '小设', 'role': '设计师', 'pet': 'kit'}
        cfg['agents']['backend-dev']['pet'] = 'cubo'
        cfg['member_pets'] = {'abc123': 'pip'}; cfg['lead_pet'] = 'tank'
        cfg['pricing_usd_per_mtok']['my-model'] = {'in': 1, 'out': 2, 'cache_read': 0.1, 'cache_write': 1}
        cfg['pricing_usd_per_mtok']['sonnet'] = {'in': 9, 'out': 9, 'cache_read': 9, 'cache_write': 9}
        tj.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        md = c / 'agents' / 'qa-tester.md'
        md.write_text(md.read_text(encoding='utf-8') + '\n- 我自己加的一条规则\n', encoding='utf-8')
        (c / 'team-board' / 'sprites' / 'my-pet.webp').write_bytes(b'RIFF0000WEBPVP8X')
        (c / 'team-board' / 'sprites' / 'pip.webp').write_bytes(b'RIFF0000WEBPVP8Xmine')   # 与自带同名但换了图
        # 重复安装不重复追加
        self.run_install('--no-pets')
        cfg2 = json.loads(tj.read_text(encoding='utf-8'))
        self.assertIn('design', [d['id'] for d in cfg2['departments']]); self.assertEqual(cfg2['departments'][0]['id'], 'hq')
        self.assertEqual(cfg2['agents']['my-designer']['name'], '小设'); self.assertEqual(cfg2['agents']['backend-dev']['pet'], 'cubo')
        self.assertEqual(cfg2['member_pets'], {'abc123': 'pip'}); self.assertEqual(cfg2['lead_pet'], 'tank')
        self.assertEqual(cfg2['pricing_usd_per_mtok']['my-model']['in'], 1); self.assertEqual(cfg2['pricing_usd_per_mtok']['sonnet']['in'], 9, '用户改过的价格要保留')
        self.assertIn('deepseek', cfg2['pricing_usd_per_mtok'], '新默认价格行要补上')
        self.assertIn('我自己加的一条规则', md.read_text(encoding='utf-8')); self.assertTrue(md.with_name('qa-tester.md.new').exists(), '新版本另存为 .new')
        self.assertTrue((c / 'team-board' / 'sprites' / 'my-pet.webp').exists())
        self.assertEqual((c / 'team-board' / 'sprites' / 'pip.webp').read_bytes(), b'RIFF0000WEBPVP8Xmine', '用户换过的同名形象不覆盖')
        self.assertTrue((c / 'team-board' / 'VERSION').exists()); self.assertTrue((c / 'team-board' / '.installed.json').exists())
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
