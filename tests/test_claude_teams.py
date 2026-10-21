"""官方 Agent Teams 目录解析：合成 fixture 能得到 lead、teammate 和任务状态。"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'examples' / 'agent-teams.synthetic' / 'claude-home'


def load_adapter():
    spec = importlib.util.spec_from_file_location(
        'claude_teams_adapter', ROOT / 'team-board' / 'adapters' / 'claude_teams.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter = load_adapter()


class FixtureParseTest(unittest.TestCase):
    def test_synthetic_fixture_yields_lead_teammates_and_task_status(self):
        team = adapter.load_team(FIXTURE, 'session-a1b2c3d4')
        self.assertEqual(team['warnings'], [])
        lead = next(member for member in team['members'] if member['role'] == 'lead')
        teammates = [member for member in team['members'] if member['role'] == 'teammate']
        self.assertEqual(lead['name'], 'synthetic-lead')
        self.assertEqual(lead['agentId'], 'synthetic-agent-lead')
        self.assertEqual(lead['agentType'], 'team-lead')
        self.assertEqual(lead['raw']['unspecified'], 'runtime field names are unpublished')
        self.assertEqual([member['name'] for member in teammates], ['synthetic-researcher', 'synthetic-writer'])
        researcher, writer = teammates
        self.assertEqual(researcher['agentId'], 'synthetic-agent-researcher')
        self.assertEqual(researcher['agentType'], 'researcher')
        self.assertIsNone(writer['agentType'])
        self.assertIsNone(writer['agentId'])
        self.assertEqual(team['rawConfig']['unspecifiedRuntime'], 'session ids and tmux pane ids have no published key names')
        by_status = {task['id']: task['normalizedStatus'] for task in team['tasks']}
        self.assertEqual(by_status, {'1': 'completed', '2': 'in_progress', '3': 'pending', '4': 'pending'})
        held = next(task for task in team['tasks'] if task['id'] == '4')
        self.assertEqual(held['description'], 'Synthetic task stored in the TaskGet output shape.')
        self.assertEqual(held['blocks'], [])
        self.assertEqual(held['blockedBy'], ['2'])
        by_name = {box['agentName']: box['entries'] for box in team['mailboxes']}
        self.assertEqual(by_name['synthetic-lead'][0]['opaque'], 'synthetic mailbox entry; official entry keys are not published')
        self.assertEqual(by_name['synthetic-researcher'], [])
        self.assertEqual(by_name['synthetic-writer'][0], 'synthetic plain-text entry')


class ToleranceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='claude-teams-')
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _write(self, relative, payload):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, str):
            path.write_text(payload, encoding='utf-8')
        else:
            path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_session_name_and_discovery_keep_tasks_after_config_is_gone(self):
        self.assertEqual(adapter.team_name_for_session('a1b2c3d4-0000-4000-8000-000000000001'), 'session-a1b2c3d4')
        self.assertEqual(adapter.team_name_for_session('sess-0001'), 'session-sess-000')
        self.assertEqual(adapter.team_name_for_session('abcd'), 'session-abcd')
        self.assertEqual(adapter.team_name_for_session(''), '')
        self.assertEqual(adapter.team_name_for_session(None), '')
        self._write('tasks/session-aaaaaaaa/only.json', {'tasks': [{'id': '9', 'subject': 'Left behind', 'status': 'completed'}]})
        self.assertEqual(adapter.discover_team_names(self.root), ['session-aaaaaaaa'])
        team = adapter.load_team(self.root, 'session-aaaaaaaa')
        self.assertEqual(team['members'], [])
        self.assertEqual(team['tasks'][0]['normalizedStatus'], 'completed')
        self.assertTrue(any('config.json' in warning for warning in team['warnings']))

    def test_prose_status_deleted_and_single_object_are_accepted(self):
        self._write('teams/session-bbbbbbbb/config.json', {'members': [
            {'teammate_name': 'synthetic-lead', 'agent_id': 'lead-1', 'agent_type': 'team-lead'},
        ]})
        self._write('tasks/session-bbbbbbbb/prose.json', {'id': 'p', 'subject': 'Still going', 'status': 'in progress'})
        self._write('tasks/session-bbbbbbbb/gone.json', [{'taskId': 'd', 'status': 'deleted', 'active_form': 'Removing'}])
        team = adapter.load_team(self.root, 'session-bbbbbbbb')
        self.assertEqual(team['members'][0]['role'], 'lead')
        self.assertEqual(team['members'][0]['name'], 'synthetic-lead')
        by_id = {task['id']: task for task in team['tasks']}
        self.assertEqual(by_id['p']['status'], 'in progress')
        self.assertEqual(by_id['p']['normalizedStatus'], 'in_progress')
        self.assertEqual(by_id['d']['normalizedStatus'], 'deleted')
        self.assertEqual(by_id['d']['activeForm'], 'Removing')

    def test_bad_files_unknown_keys_and_conflicting_aliases_do_not_raise(self):
        self._write('teams/session-cccccccc/config.json', {
            'members': [
                {'name': 'synthetic-lead', 'agentType': 'team-lead', 'agentId': 'keep', 'agent_id': 'drop', 'extra': 1},
                'not-an-object',
                {'agentType': 'team-lead'},
            ],
            'futureField': {'keep': True},
        })
        self._write('teams/session-cccccccc/inboxes/synthetic-lead.json', {'not': 'an array'})
        self._write('teams/session-cccccccc/inboxes/broken.json', '{')
        self._write('tasks/session-cccccccc/weird.json', {'hello': 'not a task'})
        self._write('tasks/session-cccccccc/bad.json', '{')
        (self.root / 'tasks' / 'session-cccccccc' / '.lock').write_text('secret-looking', encoding='utf-8')
        team = adapter.load_team(self.root, 'session-cccccccc')
        self.assertEqual(team['members'][0]['agentId'], 'keep')
        self.assertEqual(team['members'][0]['raw']['extra'], 1)
        self.assertEqual(team['rawConfig']['futureField'], {'keep': True})
        self.assertEqual(len(team['members']), 2)
        self.assertTrue(any('多名 team-lead' in warning for warning in team['warnings']))
        self.assertTrue(any('候选键不一致' in warning for warning in team['warnings']))
        self.assertEqual(team['tasks'], [])
        lead_box = next(box for box in team['mailboxes'] if box['agentName'] == 'synthetic-lead')
        self.assertEqual(lead_box['entries'], [{'not': 'an array'}])
        self.assertTrue(any('不是合法 JSON' in warning for warning in team['warnings']))
        self.assertNotIn('secret-looking', json.dumps(team))

    def test_symlink_outside_the_root_is_skipped(self):
        outside = self.root.parent / ('outside-' + self.root.name + '.json')
        outside.write_text('{"id":"nope","status":"completed","secret":"OUTSIDE"}', encoding='utf-8')
        self.addCleanup(outside.unlink)
        tasks = self.root / 'tasks' / 'session-dddddddd'
        tasks.mkdir(parents=True)
        link = tasks / 'escape.json'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('symlink creation unavailable')
        self._write('teams/session-dddddddd/config.json', {'members': [
            {'name': 'synthetic-lead', 'agentType': 'team-lead'},
        ]})
        team = adapter.load_team(self.root, 'session-dddddddd')
        self.assertEqual(team['tasks'], [])
        self.assertNotIn('OUTSIDE', json.dumps(team))
        self.assertTrue(any('跳过任务文件' in warning for warning in team['warnings']))

    def test_default_root_honors_config_dir_without_reading_home(self):
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.root)}, clear=False):
            with mock.patch.object(Path, 'home', side_effect=AssertionError('home read')):
                self.assertEqual(adapter.default_root(), self.root)
        env = os.environ.copy()
        env.pop('CLAUDE_CONFIG_DIR', None)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(Path, 'home', return_value=self.root):
                self.assertEqual(adapter.default_root(), self.root / '.claude')

    def test_invalid_team_name_does_not_escape(self):
        self._write('teams/session-eeeeeeee/config.json', {'members': []})
        team = adapter.load_team(self.root, '../session-eeeeeeee')
        self.assertEqual(team['members'], [])
        self.assertIn('团队名无效', team['warnings'])
