"""合成 Agent Teams fixture 只包含文档里允许出现的结构，且没有个人数据。"""
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from test_board import BoardEnv
from test_claude_teams import adapter

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'examples'
FIXTURE = EXAMPLES / 'agent-teams.synthetic'
HOME = FIXTURE / 'claude-home'
TEAM = 'session-a1b2c3d4'
EDGE = 'session-c0ffee01'
DOC = ROOT / 'docs' / 'AGENT_TEAMS.md'
MAX_COMMITTED_BYTES = 64 * 1024
LIST_MTIME = 1700000000
GET_MTIME = 1700000100

EXPECTED = {
    'README.md',
    'claude-home/teams/session-a1b2c3d4/config.json',
    'claude-home/teams/session-a1b2c3d4/inboxes/synthetic-lead.json',
    'claude-home/teams/session-a1b2c3d4/inboxes/synthetic-researcher.json',
    'claude-home/teams/session-a1b2c3d4/inboxes/synthetic-writer.json',
    'claude-home/tasks/session-a1b2c3d4/tasks.json',
    'claude-home/tasks/session-a1b2c3d4/task-get.json',
    'claude-home/teams/session-c0ffee01/config.json',
    'claude-home/tasks/session-c0ffee01/task-list.json',
    'claude-home/tasks/session-c0ffee01/task-get.json',
}
TASK_STATUSES = {'pending', 'in_progress', 'completed', 'deleted', 'in progress'}
SECRET = re.compile(
    r'(/Users/|/home/|[A-Za-z]:[\\/]+Users[\\/]|'
    r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|'
    r'sk-ant-|sk-[A-Za-z0-9][A-Za-z0-9_-]{7,}|ghp_|github_pat_|xox[baprs]-|'
    r'AKIA[0-9A-Z]{8,}|BEGIN [A-Z ]*PRIVATE KEY|api[_-]?key\s*[:=]|'
    r'Bearer\s+[A-Za-z0-9._\-]+)',
    re.IGNORECASE)


def _files():
    return {path.relative_to(FIXTURE).as_posix() for path in FIXTURE.rglob('*') if path.is_file()}


def _materialize_edge(root):
    """复制合成团队，并固定两份 id 12 的修改时间。Git 不保存可复现的 mtime。"""
    for folder in ('teams', 'tasks'):
        dest = root / folder / EDGE
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(HOME / folder / EDGE, dest)
    tasks = root / 'tasks' / EDGE
    os.utime(tasks / 'task-list.json', (LIST_MTIME, LIST_MTIME))
    os.utime(tasks / 'task-get.json', (GET_MTIME, GET_MTIME))
    return tasks


class AgentTeamsFixtureTest(unittest.TestCase):
    def test_tree_is_exactly_the_synthetic_files(self):
        self.assertEqual(_files(), EXPECTED)
        readme = (FIXTURE / 'README.md').read_text(encoding='utf-8')
        for needle in ('假数据', TEAM, EDGE, 'synthetic-observer', 'task-list.json', 'task-get.json', '超过 2MB', '64KB'):
            self.assertIn(needle, readme)
        parsed = 0
        for path in FIXTURE.rglob('*.json'):
            json.loads(path.read_text(encoding='utf-8'))
            parsed += 1
        self.assertEqual(parsed, len(EXPECTED) - 1)

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
        self.assertIn('tasks', listed)
        self.assertNotIn('task', listed)
        self.assertIn('task', single)
        self.assertNotIn('tasks', single)
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

    def test_edge_fixture_has_chain_duplicate_shapes_and_unmatched_owner(self):
        listed_path = HOME / 'tasks' / EDGE / 'task-list.json'
        single_path = HOME / 'tasks' / EDGE / 'task-get.json'
        listed = json.loads(listed_path.read_text(encoding='utf-8'))
        single = json.loads(single_path.read_text(encoding='utf-8'))
        self.assertIsInstance(listed['tasks'], list)
        self.assertNotIn('task', listed)
        self.assertIsInstance(single['task'], dict)
        self.assertNotIn('tasks', single)
        self.assertEqual(single['task']['id'], '12')
        self.assertEqual(single['task']['blocks'], ['13'])
        team = adapter.load_team(HOME, EDGE)
        self.assertEqual(team['warnings'], [])
        self.assertEqual(
            [member['name'] for member in team['members']],
            ['synthetic-lead', 'synthetic-researcher', 'synthetic-writer', 'synthetic-observer'])
        observer = team['members'][3]
        self.assertEqual(observer['role'], 'teammate')
        self.assertEqual(observer['agentType'], 'observer')
        names = {member['name'] for member in team['members']}
        owners = {task['owner'] for task in team['tasks']}
        self.assertNotIn('synthetic-observer', owners)
        self.assertIn('synthetic-not-on-team', owners)
        self.assertNotIn('synthetic-not-on-team', names)
        copies = [task for task in team['tasks'] if task['id'] == '12']
        self.assertEqual(
            sorted(task['source'] for task in copies),
            ['tasks/%s/task-get.json' % EDGE, 'tasks/%s/task-list.json' % EDGE])
        by_source = {task['source']: task for task in copies}
        self.assertEqual(by_source['tasks/%s/task-list.json' % EDGE]['status'], 'completed')
        self.assertEqual(by_source['tasks/%s/task-get.json' % EDGE]['normalizedStatus'], 'in_progress')
        listed_tasks = {
            task['id']: task for task in team['tasks']
            if task['source'].endswith('task-list.json')}
        self.assertEqual(listed_tasks['10']['blockedBy'], [])
        self.assertEqual(listed_tasks['11']['blockedBy'], ['10'])
        self.assertEqual(listed_tasks['12']['blockedBy'], ['11'])
        self.assertEqual(listed_tasks['13']['blockedBy'], ['12'])
        self.assertEqual(listed_tasks['14']['blockedBy'], ['13'])
        self.assertEqual(listed_tasks['15']['blockedBy'], ['10', '11'])
        self.assertEqual(listed_tasks['10']['normalizedStatus'], 'completed')
        self.assertEqual(listed_tasks['11']['normalizedStatus'], 'completed')
        self.assertEqual(listed_tasks['14']['normalizedStatus'], 'pending')
        self.assertIsNone(listed_tasks['14']['owner'])
        self.assertEqual(listed_tasks['16']['owner'], 'synthetic-not-on-team')

    def test_edge_fixture_board_keeps_newer_duplicate_and_blocks_the_chain(self):
        env = BoardEnv()
        try:
            tmp = tempfile.TemporaryDirectory(prefix='agent-teams-edge-')
            self.addCleanup(tmp.cleanup)
            root = Path(tmp.name)
            _materialize_edge(root)
            team = adapter.load_team(root, EDGE)
            self.assertEqual(team['warnings'], [])
            board = env.load()
            warnings = []
            now = GET_MTIME + 30
            rows = board.task_board_rows(team['tasks'], team['teamName'], now, clock={}, warnings=warnings)
            by_id = {row['id']: row for row in rows}
            self.assertEqual(set(by_id), {'10', '11', '12', '13', '14', '15', '16'})
            self.assertEqual(by_id['12']['subject'], 'Revise the synthetic chain note')
            self.assertEqual(by_id['12']['normalizedStatus'], 'in_progress')
            self.assertEqual(by_id['12']['owner'], 'synthetic-writer')
            self.assertEqual(by_id['12']['blocks'], ['13'])
            self.assertEqual(by_id['12']['blockedBy'], ['11'])
            self.assertEqual(by_id['12']['waitingOn'], [])
            self.assertFalse(by_id['12']['blocked'])
            self.assertEqual(by_id['12']['stateSinceKind'], 'file')
            self.assertAlmostEqual(by_id['12']['stateSince'], GET_MTIME, delta=1)
            self.assertTrue(by_id['13']['blocked'])
            self.assertEqual(by_id['13']['waitingOn'], ['12'])
            self.assertEqual(by_id['13']['stateSinceKind'], 'file-shared')
            self.assertAlmostEqual(by_id['13']['stateSince'], LIST_MTIME, delta=1)
            self.assertTrue(by_id['14']['blocked'])
            self.assertEqual(by_id['14']['waitingOn'], ['13'])
            self.assertIsNone(by_id['14']['owner'])
            self.assertFalse(by_id['15']['blocked'])
            self.assertEqual(by_id['15']['waitingOn'], [])
            self.assertEqual(by_id['15']['owner'], 'synthetic-writer')
            self.assertFalse(by_id['10']['blocked'])
            self.assertFalse(by_id['11']['blocked'])
            self.assertEqual(by_id['16']['owner'], 'synthetic-not-on-team')
            self.assertFalse(by_id['16']['blocked'])
            self.assertEqual(warnings, ['任务 id 重复，已保留较新的一条：12'])
            self.assertNotIn('/', warnings[0])
            self.assertNotIn('\\', warnings[0])
            blob = json.dumps(rows, ensure_ascii=False)
            self.assertNotIn('sourceMtime', blob)
            self.assertNotIn('raw', blob)
            self.assertNotIn(str(root), blob)
            self.assertEqual(
                board._official_status('synthetic-writer', team['tasks']),
                ('running', 'Revise the synthetic chain note'))
            self.assertEqual(
                board._official_status('synthetic-researcher', team['tasks']),
                ('idle', 'Review the synthetic chain'))
            self.assertEqual(board._official_status('synthetic-observer', team['tasks']), ('idle', ''))
            observer = next(member for member in team['members'] if member['name'] == 'synthetic-observer')
            card = board._official_card(observer, team['tasks'])
            self.assertEqual(card['status'], 'idle')
            self.assertEqual(card['description'], '官方 Agent Team 成员')
            self.assertTrue(card['officialTeam'])
        finally:
            env.cleanup()

    def test_committed_fixtures_stay_small_and_generated_oversize_file_is_skipped(self):
        saw_example = False
        for path in EXAMPLES.rglob('*'):
            if not path.is_file():
                continue
            saw_example = True
            self.assertLess(path.stat().st_size, adapter.MAX_TASK_FILE_BYTES, path)
            if FIXTURE in path.parents:
                self.assertLessEqual(path.stat().st_size, MAX_COMMITTED_BYTES, path)
        self.assertTrue(saw_example)
        tmp = tempfile.TemporaryDirectory(prefix='agent-teams-oversize-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _materialize_edge(root)
        huge = root / 'tasks' / EDGE / 'huge.json'
        huge.write_bytes(b'{')
        os.truncate(huge, adapter.MAX_TASK_FILE_BYTES + 1)
        self.assertGreater(huge.stat().st_size, adapter.MAX_TASK_FILE_BYTES)
        self.assertEqual(adapter.MAX_TASK_FILE_BYTES, 2 * 1024 * 1024)
        team = adapter.load_team(root, EDGE)
        self.assertEqual(sorted({task['id'] for task in team['tasks']}), ['10', '11', '12', '13', '14', '15', '16'])
        self.assertEqual(len(team['tasks']), 8)
        self.assertTrue(all('huge.json' not in (task.get('source') or '') for task in team['tasks']))
        self.assertTrue(any('huge.json' in warning and '超过 2MB' in warning for warning in team['warnings']))
        blob = json.dumps(team['warnings'], ensure_ascii=False)
        self.assertNotIn(str(root), blob)
        self.assertNotIn(str(ROOT), blob)

    def test_fixture_has_no_personal_data(self):
        saw_file = False
        for path in EXAMPLES.rglob('*'):
            if not path.is_file():
                continue
            saw_file = True
            text = path.relative_to(EXAMPLES).as_posix() + '\n' + path.read_text(encoding='utf-8')
            match = SECRET.search(text)
            if match:
                self.fail('%s matched %r' % (path.relative_to(ROOT).as_posix(), match.group(0)))
        self.assertTrue(saw_file)

    def test_privacy_guard_matches_each_forbidden_category(self):
        probes = {
            'home': (
                '/home/synthetic/notes',
                '/Users/synthetic/notes',
                'C:\\Users\\synthetic\\notes',
                'D:/Users/synthetic/notes',
            ),
            'email': ('synthetic.person@example.com',),
            'sk': ('sk-ant-api03-synthetic', 'sk-proj-synthetickey', 'sk-12345678'),
            'api': ('api_key=synthetic', 'api-key: synthetic', 'API_KEY=synthetic', 'Bearer synthetic.token'),
            'other': ('ghp_synthetictoken', 'github_pat_synthetic', 'xoxb-synthetic', 'AKIA12345678',
                      '-----BEGIN PRIVATE KEY-----'),
        }
        for category, samples in probes.items():
            for sample in samples:
                self.assertIsNotNone(SECRET.search(sample), '%s: %s' % (category, sample))
        for sample in (
            'synthetic-researcher',
            'synthetic-observer',
            'session-a1b2c3d4',
            'session-c0ffee01',
            '~/.claude/tasks/{team-name}/',
            'blockedBy',
            'team-lead',
            'in_progress',
            'owner',
            'TaskList',
            'TaskGet',
        ):
            self.assertIsNone(SECRET.search(sample), sample)

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
            EDGE,
            'synthetic-observer',
            '超过 2MB',
        ):
            self.assertIn(needle, text)
