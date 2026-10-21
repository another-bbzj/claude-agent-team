"""合成 Agent Teams fixture 只包含文档里允许出现的结构，且没有个人数据。"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'examples' / 'agent-teams.synthetic'
HOME = FIXTURE / 'claude-home'
TEAM = 'session-a1b2c3d4'
DOC = ROOT / 'docs' / 'AGENT_TEAMS.md'

EXPECTED = {
    'README.md',
    'claude-home/teams/session-a1b2c3d4/config.json',
    'claude-home/teams/session-a1b2c3d4/inboxes/synthetic-lead.json',
    'claude-home/teams/session-a1b2c3d4/inboxes/synthetic-researcher.json',
    'claude-home/teams/session-a1b2c3d4/inboxes/synthetic-writer.json',
    'claude-home/tasks/session-a1b2c3d4/tasks.json',
    'claude-home/tasks/session-a1b2c3d4/task-get.json',
}
TASK_STATUSES = {'pending', 'in_progress', 'completed', 'deleted', 'in progress'}
SECRET = re.compile(
    r'(/Users/|/home/|[A-Za-z]:\\Users\\|'
    r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|'
    r'sk-ant-|sk-[A-Za-z0-9]{8,}|ghp_|xox[baprs]-|AKIA[0-9A-Z]{8,}|'
    r'BEGIN [A-Z ]*PRIVATE KEY|api[_-]?key\s*[:=]|Bearer\s+[A-Za-z0-9])',
    re.IGNORECASE)


def _files():
    return {path.relative_to(FIXTURE).as_posix() for path in FIXTURE.rglob('*') if path.is_file()}


class AgentTeamsFixtureTest(unittest.TestCase):
    def test_tree_is_exactly_the_synthetic_files(self):
        self.assertEqual(_files(), EXPECTED)
        readme = (FIXTURE / 'README.md').read_text(encoding='utf-8')
        self.assertIn('假数据', readme)
        self.assertIn(TEAM, readme)

    def test_config_has_lead_typed_teammate_and_omitted_type(self):
        config = json.loads((HOME / 'teams' / TEAM / 'config.json').read_text(encoding='utf-8'))
        members = config['members']
        self.assertEqual(len(members), 3)
        self.assertEqual(members[0]['agentType'], 'team-lead')
        self.assertEqual(members[1]['agent_type'], 'researcher')
        self.assertNotIn('agentType', members[1])
        self.assertNotIn('agentType', members[2])
        self.assertNotIn('agent_type', members[2])
        self.assertIn('unspecified', members[0])
        self.assertIn('unspecifiedRuntime', config)

    def test_tasks_use_published_tool_shapes_and_known_statuses(self):
        listed = json.loads((HOME / 'tasks' / TEAM / 'tasks.json').read_text(encoding='utf-8'))
        single = json.loads((HOME / 'tasks' / TEAM / 'task-get.json').read_text(encoding='utf-8'))
        self.assertEqual([item['status'] for item in listed['tasks']], ['completed', 'in_progress', 'pending'])
        for item in listed['tasks']:
            self.assertIn(item['status'], TASK_STATUSES)
            self.assertIn('id', item)
            self.assertIn('subject', item)
            self.assertIsInstance(item['blockedBy'], list)
        self.assertEqual(single['task']['id'], '4')
        self.assertIn(single['task']['status'], TASK_STATUSES)
        self.assertIn('blocks', single['task'])
        self.assertIn('description', single['task'])

    def test_mailboxes_are_json_arrays_without_assumed_fields(self):
        inbox = HOME / 'teams' / TEAM / 'inboxes'
        lead = json.loads((inbox / 'synthetic-lead.json').read_text(encoding='utf-8'))
        researcher = json.loads((inbox / 'synthetic-researcher.json').read_text(encoding='utf-8'))
        writer = json.loads((inbox / 'synthetic-writer.json').read_text(encoding='utf-8'))
        self.assertIsInstance(lead, list)
        self.assertEqual(researcher, [])
        self.assertIsInstance(writer[0], str)
        self.assertNotIn('from', lead[0])
        self.assertNotIn('text', lead[0])
        self.assertNotIn('timestamp', lead[0])

    def test_fixture_has_no_personal_data(self):
        for path in FIXTURE.rglob('*'):
            if not path.is_file():
                continue
            text = path.read_text(encoding='utf-8')
            self.assertIsNone(SECRET.search(text), path)

    def test_doc_cites_version_paths_and_unknown_fields(self):
        text = DOC.read_text(encoding='utf-8')
        for needle in (
            '2.1.304',
            'https://code.claude.com/docs/en/agent-teams',
            'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS',
            '~/.claude/teams/{team-name}/config.json',
            '~/.claude/tasks/{team-name}/',
            'inboxes/{agent-name}.json',
            '`members`',
            'team-lead',
            'cleanupPeriodDays',
            '未公布',
            'agentId',
            'agent_id',
        ):
            self.assertIn(needle, text)
