"""官方任务目录的只读看板：三列、负责人、阻塞，以及当前状态停留了多久。"""
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from test_board import BoardEnv
from test_claude_teams import adapter

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'examples' / 'agent-teams.synthetic' / 'claude-home'


def snapshot(board):
    return board.build_snapshot(board.argparse.Namespace(session=None, project=None, stale=600))


class FixtureRowsTest(unittest.TestCase):
    def test_shipped_task_files_fill_three_columns(self):
        env = BoardEnv()
        try:
            board = env.load()
            team = adapter.load_team(FIXTURE, 'session-a1b2c3d4')
            now = time.time() + 5
            rows = board.task_board_rows(team['tasks'], team['teamName'], now, clock={})
            by_id = {row['id']: row for row in rows}
            self.assertEqual(set(by_id), {'1', '2', '3', '4'})
            self.assertEqual(by_id['1']['normalizedStatus'], 'completed')
            self.assertEqual(by_id['1']['owner'], 'synthetic-researcher')
            self.assertFalse(by_id['1']['blocked'])
            self.assertEqual(by_id['2']['normalizedStatus'], 'in_progress')
            self.assertEqual(by_id['2']['owner'], 'synthetic-writer')
            self.assertEqual(by_id['2']['blockedBy'], ['1'])
            self.assertFalse(by_id['2']['blocked'])
            self.assertEqual(by_id['2']['waitingOn'], [])
            self.assertEqual(by_id['3']['normalizedStatus'], 'pending')
            self.assertIsNone(by_id['3']['owner'])
            self.assertTrue(by_id['3']['blocked'])
            self.assertEqual(by_id['3']['waitingOn'], ['2'])
            self.assertEqual(by_id['4']['normalizedStatus'], 'pending')
            self.assertEqual(by_id['4']['description'], 'Synthetic task stored in the TaskGet output shape.')
            self.assertEqual(by_id['4']['blocks'], [])
            self.assertTrue(by_id['4']['blocked'])
            self.assertEqual(by_id['1']['stateSinceKind'], 'file-shared')
            self.assertEqual(by_id['4']['stateSinceKind'], 'file')
            self.assertGreater(by_id['4']['stateSince'], 0)
            self.assertLessEqual(by_id['4']['stateSince'], now)
            blob = json.dumps(rows, ensure_ascii=False)
            self.assertNotIn('sourceMtime', blob)
            self.assertNotIn('raw', blob)
            self.assertNotIn('tasks/session-a1b2c3d4', blob)
        finally:
            env.cleanup()


class StateClockTest(unittest.TestCase):
    def test_shared_file_keeps_siblings_and_single_file_uses_mtime(self):
        env = BoardEnv()
        try:
            tasks = env.claude / 'tasks' / 'session-sess-000'
            tasks.mkdir(parents=True)
            shared = tasks / 'tasks.json'
            shared.write_text(json.dumps({'tasks': [
                {'id': '1', 'subject': 'Survey the synthetic module', 'status': 'pending', 'owner': '合成研究员', 'blockedBy': []},
                {'id': '2', 'subject': 'Draft synthetic notes', 'status': 'in progress', 'owner': '合成写手', 'blockedBy': ['1']},
                {'id': '9', 'subject': 'Drop the synthetic row', 'status': 'deleted'},
            ]}, ensure_ascii=False), encoding='utf-8')
            past = time.time() - 500
            os.utime(shared, (past, past))
            alone = tasks / 'task-get.json'
            alone.write_text(json.dumps({'task': {
                'id': '4', 'subject': 'Hold the synthetic checklist', 'description': 'Synthetic task stored in the TaskGet output shape.',
                'status': 'pending', 'blocks': ['2'], 'blockedBy': ['1'],
            }}), encoding='utf-8')
            alone_past = time.time() - 800
            os.utime(alone, (alone_past, alone_past))
            (tasks / 'weird.json').write_text(json.dumps({'status': 'not-a-real-status', 'id': 'x', 'subject': 'Ignore me'}), encoding='utf-8')
            board = env.load()
            snap = snapshot(board)
            official = snap['officialTeam']
            self.assertTrue(official['available'])
            self.assertTrue(official['readOnly'])
            self.assertEqual(official['teamName'], 'session-sess-000')
            by_id = {row['id']: row for row in official['tasks']}
            self.assertEqual(set(by_id), {'1', '2', '4'})
            self.assertEqual(by_id['2']['normalizedStatus'], 'in_progress')
            self.assertEqual(by_id['2']['status'], 'in progress')
            self.assertFalse(by_id['2']['blocked'])
            self.assertEqual(by_id['2']['waitingOn'], ['1'])
            self.assertEqual(by_id['2']['owner'], '合成写手')
            self.assertEqual(by_id['1']['stateSinceKind'], 'file-shared')
            self.assertAlmostEqual(by_id['1']['stateSince'], past, delta=1)
            self.assertGreaterEqual(by_id['1']['stateSeconds'], 499)
            self.assertEqual(by_id['4']['stateSinceKind'], 'file')
            self.assertAlmostEqual(by_id['4']['stateSince'], alone_past, delta=1)
            self.assertEqual(by_id['4']['blocks'], ['2'])
            self.assertTrue(by_id['4']['blocked'])
            blob = json.dumps(official, ensure_ascii=False)
            self.assertNotIn('sourceMtime', blob)
            self.assertNotIn(str(env.home), blob)
            self.assertNotIn('tasks.json', blob)

            shared.write_text(json.dumps({'tasks': [
                {'id': '1', 'subject': 'Survey the synthetic module', 'status': 'pending', 'owner': '合成研究员', 'blockedBy': [], 'description': 'Still pending.'},
                {'id': '2', 'subject': 'Draft synthetic notes', 'status': 'completed', 'owner': '合成写手', 'blockedBy': ['1']},
            ]}, ensure_ascii=False), encoding='utf-8')
            os.utime(shared, None)
            before = time.time()
            snap = snapshot(board)
            after = time.time()
            by_id = {row['id']: row for row in snap['officialTeam']['tasks']}
            self.assertAlmostEqual(by_id['1']['stateSince'], past, delta=1)
            self.assertEqual(by_id['1']['stateSinceKind'], 'file-shared')
            self.assertEqual(by_id['2']['normalizedStatus'], 'completed')
            self.assertEqual(by_id['2']['stateSinceKind'], 'observed')
            self.assertGreaterEqual(by_id['2']['stateSince'], before - 1)
            self.assertLessEqual(by_id['2']['stateSince'], after + 1)
            self.assertFalse(by_id['2']['blocked'])
            self.assertEqual(by_id['2']['waitingOn'], ['1'])
            self.assertTrue(by_id['4']['blocked'])
            self.assertEqual(by_id['4']['waitingOn'], ['1'])
        finally:
            env.cleanup()


class ReadOnlyHttpTest(unittest.TestCase):
    def test_post_does_not_change_task_files(self):
        env = BoardEnv()
        try:
            tasks = env.claude / 'tasks' / 'session-sess-000'
            tasks.mkdir(parents=True)
            path = tasks / 'tasks.json'
            payload = {'tasks': [{'id': '1', 'subject': 'Survey the synthetic module', 'status': 'pending', 'owner': '合成研究员'}]}
            path.write_text(json.dumps(payload), encoding='utf-8')
            original = path.read_bytes()
            board = env.load()
            board.Handler.args = board.argparse.Namespace(session=None, project=None, stale=600)
            server = ThreadingHTTPServer(('127.0.0.1', 0), board.Handler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps({'id': '1', 'status': 'completed'}).encode('utf-8')
                request = urllib.request.Request(
                    'http://127.0.0.1:%s/api/tasks' % port, data=body,
                    headers={'Content-Type': 'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request)
                self.assertEqual(raised.exception.code, 405)
                self.assertIn('只读', json.loads(raised.exception.read().decode('utf-8'))['error'])
                self.assertEqual(path.read_bytes(), original)
                with urllib.request.urlopen('http://127.0.0.1:%s/snapshot.json' % port) as response:
                    official = json.loads(response.read().decode('utf-8'))['officialTeam']
                self.assertEqual(official['tasks'][0]['normalizedStatus'], 'pending')
                self.assertTrue(official['readOnly'])
                self.assertEqual(path.read_bytes(), original)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        finally:
            env.cleanup()
