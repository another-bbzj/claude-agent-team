"""The standalone synthetic demo has no dependency on a personal installation."""
import builtins
import http.client
import importlib.util
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_demo():
    spec = importlib.util.spec_from_file_location('readonly_demo_test', ROOT / 'tools' / 'demo_board.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = load_demo()


class FixtureTest(unittest.TestCase):
    def test_aggregates_and_department_membership_match_four_synthetic_members(self):
        source = json.loads(demo.FIXTURE.read_text(encoding='utf-8'))
        members = source['members']
        self.assertTrue(source['demo'])
        self.assertEqual(len(members), 6)
        self.assertEqual(sum(1 for member in members if member.get('officialTeam')), 2)
        self.assertEqual(sum(1 for member in members if not member.get('officialTeam')), 4)
        self.assertEqual(source['lead']['total'], 6)
        self.assertEqual(source['lead']['running'], sum(m['status'] == 'running' for m in members))
        self.assertEqual(source['lead']['done'], sum(m['status'] == 'completed' for m in members))
        self.assertAlmostEqual(source['totals']['membersCost'], sum(m['cost'] for m in members))
        self.assertAlmostEqual(source['totals']['cost'], source['totals']['membersCost'] + source['lead']['cost'])
        assigned = []
        for department in source['departments']:
            selected = [m for m in members if m['dept'] == department['id']]
            self.assertEqual(department['members'], [m['id'] for m in selected])
            self.assertEqual(department['tools'], sum(m['toolCount'] for m in selected))
            assigned += department['members']
        self.assertCountEqual(assigned, [m['id'] for m in members])
        for key in ('in', 'out', 'cache_read', 'cache_write'):
            self.assertEqual(source['totals']['usage'][key], sum(m['usage'][key] for m in members))
            self.assertEqual(source['totals']['allUsage'][key], source['totals']['usage'][key] + source['lead']['usage'][key])
        for group in ('usage', 'leadUsage', 'allUsage'):
            usage = source['totals'][group]
            self.assertEqual(usage['total'], sum(usage[k] for k in ('in', 'out', 'cache_read', 'cache_write')))
        board = source['officialTeam']
        self.assertTrue(board['available'])
        self.assertTrue(board['readOnly'])
        self.assertEqual([task['normalizedStatus'] for task in board['tasks']], ['in_progress', 'completed', 'pending', 'pending', 'pending'])
        self.assertEqual(board['tasks'][0]['owner'], '合成研究员')
        self.assertEqual(
            [task['ownerMatch'] for task in board['tasks']],
            ['合成研究员', '合成写手', None, '演示队长', None],
        )
        self.assertIsNone(board['tasks'][2]['owner'])
        self.assertEqual(board['tasks'][4]['owner'], '合成研究员 ')
        self.assertTrue(board['tasks'][2]['blocked'])
        self.assertEqual(board['tasks'][2]['waitingOn'], ['1', '9'])
        self.assertTrue(any(isinstance(task.get('description'), str) and task['description'].strip() for task in board['tasks']))
        self.assertIn('完整说明', board['tasks'][2]['description'])
        self.assertEqual(board['tasks'][2]['flags'], ['blocked', 'approximate'])
        self.assertEqual(board['tasks'][2]['blockedByDetail'][0]['subject'], board['tasks'][0]['subject'])
        self.assertTrue(board['tasks'][2]['blockedByDetail'][0]['resolved'])
        self.assertFalse(board['tasks'][2]['blockedByDetail'][1]['resolved'])
        self.assertIsNone(board['tasks'][2]['blockedByDetail'][1]['subject'])

    def test_time_refresh_is_in_memory_and_preserves_durations(self):
        source = json.loads(demo.FIXTURE.read_text(encoding='utf-8'))
        original = json.dumps(source, sort_keys=True)
        snapshot = demo.snapshot_at(source, source['generatedAt'] + 600)
        self.assertEqual(snapshot['generatedAt'], source['generatedAt'] + 600)
        for old, new in zip(source['members'], snapshot['members']):
            self.assertEqual(new['lastActivityAt'] - new['startedAt'], old['lastActivityAt'] - old['startedAt'])
        self.assertEqual(snapshot['officialTeam']['tasks'][0]['stateSince'], source['officialTeam']['tasks'][0]['stateSince'] + 600)
        self.assertEqual(json.dumps(source, sort_keys=True), original)

    def test_stalled_member_crosses_the_default_alert_threshold(self):
        source = json.loads(demo.FIXTURE.read_text(encoding='utf-8'))
        stalled = next(member for member in source['members'] if member['id'] == 'synthetic-member-4')
        self.assertEqual(stalled['status'], 'stalled')
        self.assertEqual(source['generatedAt'] - stalled['lastActivityAt'], 900)
        self.assertEqual(stalled['lastActivityAt'] - stalled['startedAt'], 130)
        snapshot = demo.snapshot_at(source, source['generatedAt'] + 50)
        alerts = snapshot['stallAlerts']
        self.assertEqual(alerts['thresholdSec'], 600)
        self.assertEqual(alerts['defaultThresholdSec'], 600)
        active = [item for item in alerts['candidates'] if item['idleSeconds'] >= alerts['thresholdSec']]
        self.assertEqual([(item['id'], item['name'], item['idleSeconds'], item['basis']) for item in active], [
            ('synthetic-member-4', '演示文档', 900, 'lastActivityAt'),
        ])
        self.assertTrue(all(item['status'] in ('idle', 'error', 'stalled') for item in alerts['candidates']))

    def test_stall_annotation_does_not_import_the_live_backend(self):
        source = json.loads(demo.FIXTURE.read_text(encoding='utf-8'))
        original = builtins.__import__

        def restricted(name, *args, **kwargs):
            if name == 'team_board' or (isinstance(name, str) and name.startswith('team_board.')):
                raise AssertionError('live backend imported')
            return original(name, *args, **kwargs)

        with mock.patch('builtins.__import__', side_effect=restricted):
            snapshot = demo.snapshot_at(source, source['generatedAt'])
        self.assertEqual(snapshot['stallAlerts']['candidates'][0]['id'], 'synthetic-member-4')


class DemoHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = demo.DemoServer(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_actual_ui_snapshot_head_and_shipped_assets(self):
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'synthetic-demo-notice', body)
        self.assertIn('只读演示'.encode('utf-8'), body)
        self.assertIn(b'function renderHeader', body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        status, head_headers, head_body = self.request('HEAD', '/')
        self.assertEqual(status, 200)
        self.assertEqual(head_body, b'')
        self.assertEqual(head_headers['Content-Length'], headers['Content-Length'])
        for asset in ('/sprites/dada-code.webp', '/favicon.png', '/favicon.ico'):
            self.assertEqual(self.request('GET', asset)[0], 200)
        self.assertIn('官方 Team'.encode('utf-8'), body)
        self.assertIn(b'id="task-detail"', body)
        self.assertIn(b'function openTaskDetail', body)
        self.assertIn(b'function closeTaskDetail', body)
        self.assertNotIn(b'/api/tasks', body)
        self.assertIn('当前列表里没有这条'.encode('utf-8'), body)
        self.assertIn('没有说明'.encode('utf-8'), body)
        self.assertIn('计时大约'.encode('utf-8'), body)
        served = json.loads(self.request('GET', '/snapshot.json')[2])
        pending = next(task for task in served['officialTeam']['tasks'] if task['id'] == '3')
        self.assertIn('完整说明', pending['description'])
        self.assertEqual(pending['flags'], ['blocked', 'approximate'])
        self.assertFalse(pending['blockedByDetail'][1]['resolved'])
        snapshot = served
        self.assertEqual(len(snapshot['members']), 6)
        self.assertEqual(sum(1 for member in snapshot['members'] if member.get('officialTeam')), 2)
        self.assertTrue(snapshot['demo'])
        active = [item['id'] for item in snapshot['stallAlerts']['candidates']
                  if item['idleSeconds'] >= snapshot['stallAlerts']['thresholdSec']]
        self.assertEqual(active, ['synthetic-member-4'])
        self.assertEqual(self.request('GET', '/?demo=1')[0], 303)

    def test_all_mutations_including_shutdown_are_forbidden(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            for path in ('/api/shutdown', '/api/agents', '/api/update/apply', '/snapshot.json'):
                with self.subTest(method=method, path=path):
                    status, _, body = self.request(method, path)
                    self.assertEqual(status, 403)
                    self.assertIn('read-only', json.loads(body)['error'])
        self.assertEqual(self.request('GET', '/snapshot.json')[0], 200)

    def test_routes_do_not_expose_private_files_or_directory_listings(self):
        for path in ('/team.json', '/server.log', '/bus/', '/sprites/', '/sprites/custom.webp',
                     '/api/agents', '/api/backdrop', '/api/update/check', '/../install.py',
                     '/%2e%2e/install.py', '/sprites/../team.json', '/sprites%2Fdada-code.webp'):
            with self.subTest(path=path):
                status, _, body = self.request('GET', path)
                self.assertEqual(status, 404)
                self.assertIn('error', json.loads(body))

    def test_startup_and_requests_never_read_home_or_import_live_backend(self):
        original_import = builtins.__import__

        def restricted_import(name, *args, **kwargs):
            if name == 'team_board' or name.startswith('team_board.'):
                raise AssertionError('live backend imported')
            return original_import(name, *args, **kwargs)

        with mock.patch('builtins.__import__', side_effect=restricted_import), \
                mock.patch.object(Path, 'home', side_effect=AssertionError('home read')), \
                mock.patch.object(Path, 'write_text', side_effect=AssertionError('unexpected write')), \
                mock.patch.object(Path, 'write_bytes', side_effect=AssertionError('unexpected write')), \
                mock.patch.object(socket, 'getfqdn', side_effect=AssertionError('reverse DNS')):
            isolated = load_demo()
            with isolated.DemoServer(0) as server:
                self.assertEqual(server.server_address[0], '127.0.0.1')
                self.assertEqual(server.server_name, 'localhost')
            self.assertEqual(self.request('GET', '/snapshot.json')[0], 200)

    def test_allowlisted_asset_symlink_cannot_escape_board_directory(self):
        with tempfile.TemporaryDirectory(prefix='demo-static-') as directory:
            root = Path(directory)
            board = root / 'board'
            board.mkdir()
            outside = root / 'outside.txt'
            outside.write_text('PRIVATE_SENTINEL', encoding='utf-8')
            try:
                (board / 'favicon.png').symlink_to(outside)
            except OSError:
                self.skipTest('symlink creation unavailable')
            with mock.patch.object(demo, 'BOARD', board):
                status, _, body = self.request('GET', '/favicon.png')
                self.assertEqual(status, 404)
                self.assertNotIn(b'PRIVATE_SENTINEL', body)

    def test_asset_resolution_errors_return_safe_not_found(self):
        original_resolve = Path.resolve

        for failure in (OSError('PRIVATE_PERMISSION'), RuntimeError('PRIVATE_SYMLINK_LOOP')):
            def resolve(path, *args, failure=failure, **kwargs):
                if path == demo.BOARD / 'favicon.png':
                    raise failure
                return original_resolve(path, *args, **kwargs)
            with self.subTest(failure=type(failure).__name__), mock.patch.object(Path, 'resolve', resolve):
                status, _, body = self.request('GET', '/favicon.png')
                self.assertEqual(status, 404)
                self.assertNotIn(b'PRIVATE_', body)
                self.assertEqual(json.loads(body)['error'], 'demo asset unavailable')


if __name__ == '__main__':
    unittest.main()
