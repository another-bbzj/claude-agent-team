"""官方任务目录的只读看板：三列、负责人、阻塞，以及当前状态停留了多久。"""
import io
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from email.message import Message
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
            board._TASK_STATE_CLOCK.clear()
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
            board._TASK_STATE_CLOCK.clear()
        finally:
            env.cleanup()


class OwnerMatchTest(unittest.TestCase):
    def test_exact_string_only(self):
        env = BoardEnv()
        try:
            board = env.load()
            names = ['合成研究员', '合成写手', '阿服']
            self.assertEqual(board.exact_owner_name('合成研究员', names), '合成研究员')
            self.assertEqual(board.exact_owner_name('阿服', names), '阿服')
            self.assertIsNone(board.exact_owner_name('合成研究员 ', names))
            self.assertIsNone(board.exact_owner_name('合成研究', names))
            self.assertIsNone(board.exact_owner_name(' 合成研究员', names))
            self.assertIsNone(board.exact_owner_name('Synthetic-Lead', ['synthetic-lead']))
            self.assertIsNone(board.exact_owner_name('', names))
            self.assertIsNone(board.exact_owner_name(' ', names))
            self.assertIsNone(board.exact_owner_name(None, names))
            self.assertIsNone(board.exact_owner_name(1, names))
            tasks = [
                {'id': '1', 'owner': '合成研究员'},
                {'id': '2', 'owner': 'synthetic-lead'},
                {'id': '3', 'owner': None},
                {'id': '4', 'owner': ''},
                {'id': '5', 'owner': '合成研究员 '},
                {'id': '6', 'owner': '不是成员'},
                {'id': '7', 'owner': '队长 · Claude'},
                {'id': '8', 'owner': '阿服'},
            ]
            board.annotate_owner_matches(tasks, names, 'synthetic-lead')
            self.assertEqual(
                [task['ownerMatch'] for task in tasks],
                ['合成研究员', 'synthetic-lead', None, None, None, None, None, '阿服'],
            )
            self.assertEqual(tasks[4]['owner'], '合成研究员 ')
            self.assertEqual(tasks[6]['owner'], '队长 · Claude')
            idle, subject = board._official_status('合成研究员', [
                {'owner': '合成研究员 ', 'normalizedStatus': 'in_progress', 'subject': 'near miss'},
                {'owner': '合成研究', 'normalizedStatus': 'in_progress', 'subject': 'prefix'},
            ])
            self.assertEqual(idle, 'idle')
            self.assertEqual(subject, '')
            running, subject = board._official_status('合成研究员', [
                {'owner': '合成研究员', 'normalizedStatus': 'in_progress', 'subject': 'Survey'},
            ])
            self.assertEqual((running, subject), ('running', 'Survey'))
        finally:
            env.cleanup()

    def test_snapshot_marks_member_and_lead_and_leaves_near_misses(self):
        env = BoardEnv()
        try:
            team = env.claude / 'teams' / 'session-sess-000'
            (team / 'inboxes').mkdir(parents=True)
            tasks = env.claude / 'tasks' / 'session-sess-000'
            tasks.mkdir(parents=True)
            (team / 'config.json').write_text(json.dumps({
                'members': [
                    {'name': 'synthetic-lead', 'agentType': 'team-lead', 'agentId': 'synthetic-agent-lead'},
                    {'name': '合成研究员', 'agentType': 'researcher', 'agentId': 'synthetic-agent-researcher'},
                    {'name': '合成写手'},
                ],
            }, ensure_ascii=False), encoding='utf-8')
            payload = json.dumps({
                'tasks': [
                    {'id': '1', 'subject': 'Owned by teammate', 'status': 'in_progress', 'owner': '合成研究员'},
                    {'id': '2', 'subject': 'Owned by lead', 'status': 'pending', 'owner': 'synthetic-lead'},
                    {'id': '3', 'subject': 'Unowned', 'status': 'pending'},
                    {'id': '4', 'subject': 'Trailing space', 'status': 'pending', 'owner': '合成研究员 '},
                    {'id': '5', 'subject': 'Owned by writer', 'status': 'completed', 'owner': '合成写手'},
                    {'id': '6', 'subject': 'Stage label is not the lead name', 'status': 'pending', 'owner': '队长 · Claude'},
                    {'id': '7', 'subject': 'Case differs', 'status': 'pending', 'owner': 'Synthetic-Lead'},
                    {'id': '8', 'subject': 'Owned by a subagent display name', 'status': 'pending', 'owner': '阿服'},
                ],
            }, ensure_ascii=False)
            (tasks / 'tasks.json').write_text(payload, encoding='utf-8')
            original = (tasks / 'tasks.json').read_bytes()
            board = env.load()
            board._TASK_STATE_CLOCK.clear()
            official = snapshot(board)['officialTeam']
            self.assertTrue(official['readOnly'])
            self.assertEqual(official['lead'], 'synthetic-lead')
            rows = {row['id']: row for row in official['tasks']}
            self.assertEqual(rows['1']['ownerMatch'], '合成研究员')
            self.assertEqual(rows['2']['ownerMatch'], 'synthetic-lead')
            self.assertIsNone(rows['3']['owner'])
            self.assertIsNone(rows['3']['ownerMatch'])
            self.assertEqual(rows['4']['owner'], '合成研究员 ')
            self.assertIsNone(rows['4']['ownerMatch'])
            self.assertEqual(rows['5']['ownerMatch'], '合成写手')
            self.assertIsNone(rows['6']['ownerMatch'])
            self.assertIsNone(rows['7']['ownerMatch'])
            self.assertEqual(rows['8']['ownerMatch'], '阿服')
            self.assertEqual((tasks / 'tasks.json').read_bytes(), original)
            self.assertNotIn('ownerMatch', original.decode('utf-8'))
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
                board._TASK_STATE_CLOCK.clear()
        finally:
            env.cleanup()


class LimitAndDedupeTest(unittest.TestCase):
    def test_oversized_task_file_is_skipped_and_a_small_sibling_remains(self):
        env = BoardEnv()
        try:
            tasks = env.claude / 'tasks' / 'session-limit01'
            tasks.mkdir(parents=True)
            huge = tasks / 'huge.json'
            huge.write_bytes(b'{')
            os.truncate(huge, adapter.MAX_TASK_FILE_BYTES + 1)
            self.assertEqual(adapter.MAX_TASK_FILE_BYTES, 2 * 1024 * 1024)
            (tasks / 'ok.json').write_text(json.dumps({'tasks': [
                {'id': '1', 'subject': 'Keep the small synthetic task', 'status': 'pending'},
            ]}), encoding='utf-8')
            team = adapter.load_team(env.claude, 'session-limit01')
            self.assertEqual([task['id'] for task in team['tasks']], ['1'])
            self.assertTrue(all('huge.json' not in (task.get('source') or '') for task in team['tasks']))
            self.assertTrue(any('huge.json' in warning and '超过 2MB' in warning for warning in team['warnings']))
            self.assertNotIn(str(env.home), json.dumps(team['warnings'], ensure_ascii=False))
        finally:
            env.cleanup()

    def test_board_stops_at_five_hundred_rows(self):
        env = BoardEnv()
        try:
            board = env.load()
            self.assertEqual(board.MAX_TASK_ROWS, 500)
            tasks = [{
                'id': str(index),
                'subject': 'Synthetic task %s' % index,
                'status': 'pending',
                'normalizedStatus': 'pending',
                'source': 'tasks.json',
                'sourceMtime': 10,
            } for index in range(501)]
            warnings = []
            clock = {}
            rows = board.task_board_rows(tasks, 'session-cap', 20, clock=clock, warnings=warnings)
            self.assertEqual(len(rows), 500)
            self.assertEqual(rows[0]['id'], '0')
            self.assertEqual(rows[-1]['id'], '499')
            self.assertTrue(any('任务超过 500 条，看板只显示前 500 条' in warning for warning in warnings))
            self.assertEqual(len(clock), 501)
        finally:
            env.cleanup()

    def test_duplicate_id_keeps_newer_copy_and_warns_without_paths(self):
        env = BoardEnv()
        try:
            tasks = env.claude / 'tasks' / 'session-sess-000'
            tasks.mkdir(parents=True)
            shared = tasks / 'tasks.json'
            shared.write_text(json.dumps({'tasks': [
                {'id': '1', 'subject': 'Older completed copy', 'status': 'completed', 'owner': '合成写手'},
                {'id': '2', 'subject': 'Wait on one', 'status': 'pending', 'blockedBy': ['1']},
            ]}), encoding='utf-8')
            older = time.time() - 400
            os.utime(shared, (older, older))
            newer_path = tasks / 'task-get.json'
            newer_path.write_text(json.dumps({'task': {
                'id': '1', 'subject': 'Newer pending copy', 'status': 'pending', 'owner': '合成研究员',
            }}), encoding='utf-8')
            newer = time.time() - 30
            os.utime(newer_path, (newer, newer))
            board = env.load()
            board._TASK_STATE_CLOCK.clear()
            official = snapshot(board)['officialTeam']
            by_id = {row['id']: row for row in official['tasks']}
            self.assertEqual(set(by_id), {'1', '2'})
            self.assertEqual(by_id['1']['subject'], 'Newer pending copy')
            self.assertEqual(by_id['1']['normalizedStatus'], 'pending')
            self.assertEqual(by_id['1']['stateSinceKind'], 'file')
            self.assertTrue(by_id['2']['blocked'])
            self.assertEqual(by_id['2']['waitingOn'], ['1'])
            self.assertEqual(by_id['2']['stateSinceKind'], 'file-shared')
            warning = next(item for item in official['warnings'] if '重复' in item)
            self.assertIn('1', warning)
            self.assertNotIn('tasks.json', warning)
            self.assertNotIn('task-get.json', warning)
            self.assertNotIn('/', warning)
            blob = json.dumps(official, ensure_ascii=False)
            self.assertNotIn(str(env.home), blob)
            self.assertNotIn('sourceMtime', blob)
            board._TASK_STATE_CLOCK.clear()
        finally:
            env.cleanup()

    def test_equal_or_missing_mtime_keeps_the_later_copy_only_when_neither_is_newer(self):
        env = BoardEnv()
        try:
            board = env.load()
            warnings = []
            tied = board.task_board_rows([
                {'id': '7', 'subject': 'first', 'normalizedStatus': 'pending', 'status': 'pending', 'source': 'a.json', 'sourceMtime': 50},
                {'id': '7', 'subject': 'second', 'normalizedStatus': 'completed', 'status': 'completed', 'source': 'b.json', 'sourceMtime': 50},
            ], 'session-tie', 80, clock={}, warnings=warnings)
            self.assertEqual(len(tied), 1)
            self.assertEqual(tied[0]['subject'], 'second')
            self.assertEqual(tied[0]['normalizedStatus'], 'completed')
            missing = board.task_board_rows([
                {'id': '8', 'subject': 'has-mtime', 'normalizedStatus': 'pending', 'status': 'pending', 'source': 'a.json', 'sourceMtime': 10},
                {'id': '8', 'subject': 'no-mtime', 'normalizedStatus': 'completed', 'status': 'completed', 'source': 'b.json'},
            ], 'session-tie', 80, clock={}, warnings=[])
            self.assertEqual(missing[0]['subject'], 'has-mtime')
            self.assertEqual(missing[0]['normalizedStatus'], 'pending')
        finally:
            env.cleanup()


class SessionClockTest(unittest.TestCase):
    def test_switching_team_drops_the_previous_clock(self):
        env = BoardEnv()
        try:
            board = env.load()
            clock = {}
            now = 1_000_000.0
            first = {
                'id': '1', 'subject': 'Team A task', 'status': 'pending', 'normalizedStatus': 'pending',
                'source': 'a.json', 'sourceMtime': now - 100,
            }
            changed = dict(first, status='completed', normalizedStatus='completed')
            board.task_board_rows([first], 'session-aaaa', now, clock=clock)
            observed = board.task_board_rows([changed], 'session-aaaa', now + 10, clock=clock)
            self.assertEqual(observed[0]['stateSinceKind'], 'observed')
            self.assertIn(('session-aaaa', '1'), clock)
            other = {
                'id': '9', 'subject': 'Team B task', 'status': 'pending', 'normalizedStatus': 'pending',
                'source': 'b.json', 'sourceMtime': now - 50,
            }
            rows_b = board.task_board_rows([other], 'session-bbbb', now + 20, clock=clock)
            self.assertNotIn(('session-aaaa', '1'), clock)
            self.assertIn(('session-bbbb', '9'), clock)
            self.assertEqual(rows_b[0]['stateSinceKind'], 'file')
            again = board.task_board_rows([changed], 'session-aaaa', now + 30, clock=clock)
            self.assertEqual(again[0]['stateSinceKind'], 'file')
            self.assertAlmostEqual(again[0]['stateSince'], now - 100, delta=0.01)
            self.assertNotIn(('session-bbbb', '9'), clock)
            board.task_board_rows([], '', now + 40, clock=clock)
            self.assertEqual(clock, {})
        finally:
            env.cleanup()


class TaskApiStatusTest(unittest.TestCase):
    def _handler(self, board, method, path, body=b'', origin=None, length=None):
        events = []

        class RecordingReader(io.BytesIO):
            def read(self, n=-1):
                chunk = super().read(n)
                events.append(('read', n, len(chunk)))
                return chunk

        class RecordingWriter(io.BytesIO):
            def write(self, data):
                events.append(('write', len(data)))
                return super().write(data)

            def flush(self):
                events.append(('flush',))
                return super().flush()

        handler = board.Handler.__new__(board.Handler)
        handler.path = path
        handler.command = method
        handler.headers = Message()
        handler.headers['Content-Length'] = str(len(body) if length is None else length)
        if origin:
            handler.headers['Origin'] = origin
        handler.rfile = RecordingReader(body)
        handler.wfile = RecordingWriter()
        handler.request_version = 'HTTP/1.1'
        handler.requestline = '%s %s HTTP/1.1' % (method, path)
        return handler, events

    def test_any_method_returns_405_before_parsing_and_drains_the_body(self):
        env = BoardEnv()
        try:
            board = env.load()
            body = b'not-json'
            for method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'):
                with self.subTest(method=method):
                    handler, events = self._handler(
                        board, method, '/api/tasks?ignored=1', body,
                        origin='http://evil.example')
                    getattr(handler, 'do_' + method)()
                    raw = handler.wfile.getvalue()
                    self.assertIn(b' 405 ', raw)
                    self.assertIn(b'Allow: GET', raw)
                    self.assertNotIn(b' 400 ', raw)
                    self.assertNotIn(b' 403 ', raw)
                    kinds = [event[0] for event in events]
                    self.assertLess(kinds.index('read'), kinds.index('write'))
                    if method == 'HEAD':
                        self.assertNotIn('只读'.encode('utf-8'), raw)
                    else:
                        self.assertIn('只读'.encode('utf-8'), raw)
                    self.assertEqual(handler.rfile.read(), b'')
            handler, events = self._handler(board, 'POST', '/api/tasks/1', b'not-json')
            handler.do_POST()
            self.assertIn(b' 405 ', handler.wfile.getvalue())
            self.assertIn(b'Allow: GET', handler.wfile.getvalue())
        finally:
            env.cleanup()

    def test_live_server_rejects_task_writes_and_leaves_the_file(self):
        env = BoardEnv()
        try:
            tasks = env.claude / 'tasks' / 'session-sess-000'
            tasks.mkdir(parents=True)
            path = tasks / 'tasks.json'
            payload = {'tasks': [{'id': '1', 'subject': 'Survey the synthetic module', 'status': 'pending'}]}
            path.write_text(json.dumps(payload), encoding='utf-8')
            original = path.read_bytes()
            board = env.load()
            board._TASK_STATE_CLOCK.clear()
            board.Handler.args = board.argparse.Namespace(session=None, project=None, stale=600)
            server = ThreadingHTTPServer(('127.0.0.1', 0), board.Handler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for method, body in (
                    ('POST', b'not-json'),
                    ('GET', None),
                    ('HEAD', None),
                    ('PUT', b'not-json'),
                    ('PATCH', b'{"id":"1"}'),
                    ('DELETE', b''),
                ):
                    with self.subTest(method=method):
                        request = urllib.request.Request(
                            'http://127.0.0.1:%s/api/tasks' % port, data=body, method=method)
                        with self.assertRaises(urllib.error.HTTPError) as raised:
                            urllib.request.urlopen(request)
                        self.assertEqual(raised.exception.code, 405)
                        self.assertEqual(raised.exception.headers.get('Allow'), 'GET')
                        if method != 'HEAD':
                            self.assertIn('只读', json.loads(raised.exception.read().decode('utf-8'))['error'])
                        self.assertEqual(path.read_bytes(), original)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                board._TASK_STATE_CLOCK.clear()
        finally:
            env.cleanup()
