"""team-lead 模板：frontmatter、安装清单、协议里的模式判定，以及看板的 lead 类型。"""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / 'agents' / 'team-lead.md'
MODES = ROOT / 'skills' / 'agent-team' / 'modes.md'


def _frontmatter(path):
    text = path.read_text(encoding='utf-8')
    if not text.startswith('---\n'):
        raise AssertionError(str(path) + ' 缺少 frontmatter')
    end = text.find('\n---\n', 4)
    if end < 0:
        raise AssertionError(str(path) + ' 的 frontmatter 没有结束')
    meta = {}
    for line in text[4:end].splitlines():
        if not line or line[0].isspace() or ':' not in line:
            continue
        key, value = line.split(':', 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[end + 5:]


def _tuple_literal(source, name):
    needle = name + ' = ('
    start = source.index(needle) + len(needle)
    end = source.index(')', start)
    return tuple(part.strip().strip("'") for part in source[start:end].split(',') if part.strip())


def _load_teams():
    spec = importlib.util.spec_from_file_location(
        'claude_teams_for_lead_template', ROOT / 'team-board' / 'adapters' / 'claude_teams.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_doctor():
    spec = importlib.util.spec_from_file_location(
        'doctor_for_lead_template', ROOT / 'team-board' / 'doctor.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TeamLeadTemplateTest(unittest.TestCase):
    def test_frontmatter_matches_board_agent_rules(self):
        board = (ROOT / 'team-board' / 'team_board.py').read_text(encoding='utf-8')
        models = _tuple_literal(board, 'MODELS')
        efforts = _tuple_literal(board, 'EFFORTS')
        colors = _tuple_literal(board, 'COLORS')
        meta, body = _frontmatter(AGENT)
        self.assertEqual(meta['name'], AGENT.stem)
        self.assertEqual(meta['name'], 'team-lead')
        self.assertGreaterEqual(len(meta['description']), 8)
        self.assertIn(meta['model'], models)
        self.assertEqual(meta['model'], 'inherit')
        self.assertIn(meta['effort'], efforts)
        self.assertEqual(meta['effort'], 'high')
        self.assertEqual(meta['memory'], 'user')
        self.assertIn(meta['color'], colors)
        self.assertNotIn('disallowedTools: Agent', AGENT.read_text(encoding='utf-8'))
        for phrase in (
            'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS',
            'subagent',
            'metadata.acceptance',
            'owner',
            'estimate',
            'diff',
            'WORKTREES.md',
            'in_progress',
        ):
            self.assertIn(phrase, body)

    def test_protocol_documents_mode_decision_and_board_fields(self):
        skill = (ROOT / 'skills' / 'agent-team' / 'SKILL.md').read_text(encoding='utf-8')
        modes = MODES.read_text(encoding='utf-8')
        roles = (ROOT / 'skills' / 'agent-team' / 'roles.md').read_text(encoding='utf-8')
        office = (ROOT / 'skills' / 'agent-team' / 'office.md').read_text(encoding='utf-8')
        self.assertIn('modes.md', skill)
        self.assertIn('官方 Team', skill)
        self.assertIn('subagent', skill)
        self.assertIn('team-lead', roles)
        self.assertIn('modes.md', roles)
        self.assertIn('## Owned files', office)
        self.assertIn('modes.md', office)
        for phrase in (
            '## 判定',
            '官方 Team 模式',
            'subagent 模式',
            'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS',
            '只有一个团队',
            '嵌套',
            'docs/WORKTREES.md',
            'metadata.acceptance',
            'acceptance.items',
            'acceptance.checklist',
            'ownerMatch',
            'estimate',
            'diff',
            '## Owned files',
            'https://code.claude.com/docs/en/agent-teams',
            'https://code.claude.com/docs/en/agents',
        ):
            self.assertIn(phrase, modes)
        self.assertIn('|官方Team模式|subagent模式|', modes.replace(' ', ''))

    def test_lead_type_registration_matches_the_board(self):
        adapter = _load_teams()
        meta, _body = _frontmatter(AGENT)
        self.assertEqual(adapter.LEAD_TYPE, 'team-lead')
        self.assertEqual(meta['name'], adapter.LEAD_TYPE)
        cfg = json.loads((ROOT / 'team-board' / 'team.json').read_text(encoding='utf-8'))
        entry = cfg['agents']['team-lead']
        self.assertEqual(entry['dept'], 'hq')
        self.assertEqual(entry['avatar'], 'team-lead')
        self.assertEqual(entry['name'], '队长')
        shipped = {path.stem for path in (ROOT / 'agents').glob('*.md')}
        self.assertEqual(shipped, set(cfg['agents']))
        doctor = _load_doctor()
        item = doctor.config_check(ROOT / 'team-board')
        self.assertEqual(item['status'], 'ok')
        doctor_src = (ROOT / 'team-board' / 'doctor.py').read_text(encoding='utf-8')
        self.assertNotIn('team-architect', doctor_src)
        self.assertNotIn('team-lead.md', doctor_src)

    def test_docs_mention_the_switch_and_the_template(self):
        teams = (ROOT / 'docs' / 'AGENT_TEAMS.md').read_text(encoding='utf-8')
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        readme_en = (ROOT / 'README.en.md').read_text(encoding='utf-8')
        self.assertIn('agents/team-lead.md', teams)
        self.assertIn('CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1', teams)
        self.assertIn('metadata.acceptance', teams)
        self.assertIn('agents/team-lead.md', readme)
        self.assertIn('CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1', readme)
        self.assertIn('agents/team-lead.md', readme_en)
        self.assertIn('CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1', readme_en)
        install = (ROOT / 'install.py').read_text(encoding='utf-8')
        self.assertIn("(HERE / 'agents').glob('*.md')", install)
        update = (ROOT / 'team-board' / 'update.py').read_text(encoding='utf-8')
        self.assertIn('install.py', update)


if __name__ == '__main__':
    unittest.main()
