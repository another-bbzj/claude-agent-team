"""官方 teammate 和 subagent 同时出现在快照里，lead 不重复占卡。"""
import json
import unittest

from test_board import BoardEnv


def snapshot(board):
    return board.build_snapshot(board.argparse.Namespace(session=None, project=None, stale=600))


class OfficialTeamBoardTest(unittest.TestCase):
    def test_missing_directory_adds_no_members_or_warnings(self):
        env = BoardEnv()
        try:
            board = env.load()
            snap = snapshot(board)
            self.assertEqual(len(snap['members']), 2)
            self.assertFalse(any(member.get('officialTeam') for member in snap['members']))
            self.assertFalse(snap['officialTeam']['available'])
            self.assertEqual(snap['officialTeam']['warnings'], [])
        finally:
            env.cleanup()

    def test_subagents_and_official_teammates_share_one_snapshot(self):
        env = BoardEnv()
        try:
            team = env.claude / 'teams' / 'session-sess-000'
            (team / 'inboxes').mkdir(parents=True)
            tasks = env.claude / 'tasks' / 'session-sess-000'
            tasks.mkdir(parents=True)
            (team / 'config.json').write_text(json.dumps({
                'members': [
                    {'name': 'synthetic-lead', 'agentType': 'team-lead', 'agentId': 'synthetic-agent-lead',
                     'unspecified': 'runtime field names are unpublished'},
                    {'name': '合成研究员', 'agentType': 'researcher', 'agentId': 'synthetic-agent-researcher'},
                    {'name': '合成写手'},
                ],
            }, ensure_ascii=False), encoding='utf-8')
            (tasks / 'tasks.json').write_text(json.dumps({
                'tasks': [
                    {'id': '1', 'subject': 'Survey the synthetic module', 'status': 'in_progress',
                     'owner': '合成研究员', 'blockedBy': []},
                    {'id': '2', 'subject': 'Draft synthetic notes', 'status': 'completed',
                     'owner': '合成写手', 'blockedBy': ['1']},
                ],
            }, ensure_ascii=False), encoding='utf-8')
            board = env.load()
            snap = snapshot(board)
            subagents = [member for member in snap['members'] if not member.get('officialTeam')]
            official = [member for member in snap['members'] if member.get('officialTeam')]
            self.assertEqual(len(subagents), 2)
            by_name = {member['name']: member for member in official}
            self.assertEqual(set(by_name), {'合成研究员', '合成写手'})
            self.assertEqual(by_name['合成研究员']['status'], 'running')
            self.assertEqual(by_name['合成研究员']['dept'], 'rnd')
            self.assertEqual(by_name['合成研究员']['description'], 'Survey the synthetic module')
            self.assertEqual(by_name['合成写手']['status'], 'completed')
            self.assertEqual(by_name['合成写手']['agentType'], '')
            self.assertNotIn('team-lead', [member.get('agentType') for member in official])
            self.assertTrue(snap['officialTeam']['available'])
            self.assertEqual(snap['officialTeam']['teamName'], 'session-sess-000')
            self.assertEqual(snap['officialTeam']['lead'], 'synthetic-lead')
            self.assertNotIn('unspecified', json.dumps(snap['officialTeam'], ensure_ascii=False))
            self.assertNotIn('raw', snap['officialTeam'])
        finally:
            env.cleanup()
