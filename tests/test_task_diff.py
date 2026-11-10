"""任务和工单的 git 改动量只来自只读 git，失败时不写成 0。"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from test_agent_teams_fixture import SECRET
from test_board import BoardEnv, asst, jl

ROOT = Path(__file__).resolve().parents[1]


def load_task_diff():
    spec = importlib.util.spec_from_file_location(
        'task_diff_under_test', ROOT / 'team-board' / 'task_diff.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git_env(date=None):
    env = os.environ.copy()
    for key in (
        'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
        'GIT_NAMESPACE', 'GIT_PREFIX', 'GIT_EXTERNAL_DIFF',
    ):
        env.pop(key, None)
    env['GIT_CONFIG_NOSYSTEM'] = '1'
    env['GIT_CONFIG_GLOBAL'] = os.devnull
    env['GIT_AUTHOR_NAME'] = 'Synthetic'
    env['GIT_AUTHOR_EMAIL'] = 'synthetic@example.com'
    env['GIT_COMMITTER_NAME'] = 'Synthetic'
    env['GIT_COMMITTER_EMAIL'] = 'synthetic@example.com'
    if date is not None:
        stamp = '%d +0000' % int(date)
        env['GIT_AUTHOR_DATE'] = stamp
        env['GIT_COMMITTER_DATE'] = stamp
    return env


def git(repo, *args, date=None):
    binary = shutil.which('git')
    if not binary:
        raise unittest.SkipTest('git is not installed')
    proc = subprocess.run(
        [binary, '-C', str(repo)] + list(args),
        check=False, env=git_env(date),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise AssertionError('%s\n%s' % (args, proc.stderr.decode('utf-8', 'replace')))
    return proc.stdout


def epoch(text):
    return datetime.fromisoformat(text).timestamp()


class TaskDiffRulesTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_task_diff()

    def test_numstat_and_commit_log_keep_text_and_mark_binary(self):
        files, binary, truncated = self.mod.parse_numstat('2\t1\ta.py\n-\t-\tb.bin\n')
        self.assertEqual(files['a.py'], [2, 1])
        self.assertEqual(files['b.bin'], [0, 0])
        self.assertTrue(binary)
        self.assertFalse(truncated)
        commits, cut = self.mod.parse_commit_log('---100\n2\t0\ta.py\n\n---200\n1\t1\tb.py\n')
        self.assertEqual([item['ts'] for item in commits], [100, 200])
        self.assertEqual(commits[0]['files']['a.py'], [2, 0])
        self.assertFalse(cut)
        self.assertEqual(self.mod._owned_paths(
            '# note\n\n## Owned files\n\n- beta.txt\n- ../outside.txt\n- /tmp/x\n- C:\\x\n'),
            ['beta.txt'])

    def test_missing_directory_git_and_timeout_stay_unavailable(self):
        missing = {'id': '1', 'normalizedStatus': 'in_progress'}
        self.assertEqual(self.mod.annotate([missing], [], 10, {'cwd': ''}), [])
        self.assertEqual(missing['diff']['status'], 'unavailable')
        self.assertIsNone(missing['diff']['files'])

        directory = tempfile.mkdtemp(prefix='task-diff-empty-')
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        plain = {'id': '1', 'normalizedStatus': 'in_progress'}
        tickets = self.mod.annotate([plain], [], 10, {'cwd': directory})
        self.assertEqual(plain['diff']['status'], 'unavailable')
        self.assertEqual(tickets, [])
        self.assertFalse((Path(directory) / '.git').exists())

        def explode(cwd, args):
            raise FileNotFoundError('git')

        broken = {'id': '1', 'normalizedStatus': 'completed'}
        self.assertEqual(self.mod.annotate([broken], [], 10, {'cwd': directory}, run=explode), [])
        self.assertEqual(broken['diff']['status'], 'unavailable')

        def timed_out(cwd, args):
            raise subprocess.TimeoutExpired(['git'], 4)

        slow = {'id': '1', 'normalizedStatus': 'in_progress'}
        self.mod.annotate([slow], [], 10, {'cwd': directory}, run=timed_out)
        self.assertEqual(slow['diff']['status'], 'unavailable')
        self.assertIsNone(slow['diff']['insertions'])


class TaskDiffGitTest(unittest.TestCase):
    def setUp(self):
        if not shutil.which('git'):
            self.skipTest('git is not installed')
        self.mod = load_task_diff()
        self.mod.clear_cache()
        self.tmp = Path(tempfile.mkdtemp(prefix='task-diff-git-'))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def init_repo(self, name='repo'):
        repo = self.tmp / name
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        (repo / 'README').write_text('synthetic\n', encoding='utf-8')
        git(repo, 'add', 'README')
        git(repo, 'commit', '-m', 'init', date=epoch('2026-01-01T09:00:00+00:00'))
        return repo

    def test_window_keeps_one_commit_and_ignores_later_dirty_edits(self):
        repo = self.init_repo()
        (repo / 'beta.txt').write_text('a\nb\nc\n', encoding='utf-8')
        git(repo, 'add', 'beta.txt')
        git(repo, 'commit', '-m', 'beta', date=epoch('2026-01-01T10:05:00+00:00'))
        index = (repo / '.git' / 'index').read_bytes()
        head = git(repo, 'rev-parse', 'HEAD')
        (repo / 'beta.txt').write_text('a\nb\nc\nd\n', encoding='utf-8')
        task = {
            'id': '1', 'normalizedStatus': 'completed', 'stateSinceKind': 'file',
            'stateSince': epoch('2026-01-01T10:10:00+00:00'),
        }
        other = {'id': '2', 'normalizedStatus': 'pending'}
        marks = [
            {'ts': epoch('2026-01-01T10:00:00+00:00'), 'id': '1', 'status': 'in_progress'},
            {'ts': epoch('2026-01-01T10:10:00+00:00'), 'id': '1', 'status': 'completed'},
        ]
        self.mod.clear_cache()
        tickets = self.mod.annotate(
            [task, other], marks, epoch('2026-01-01T12:00:00+00:00'), {'cwd': str(repo)})
        self.assertEqual(tickets, [])
        self.assertEqual(task['diff'], {
            'files': 1, 'insertions': 3, 'deletions': 0,
            'status': 'known', 'partial': False, 'basis': 'window',
        })
        self.assertEqual(other['diff']['status'], 'unavailable')
        self.assertIsNone(other['diff']['files'])
        self.assertNotIn('beta.txt', json.dumps(task['diff']))
        self.assertEqual((repo / '.git' / 'index').read_bytes(), index)
        self.assertEqual(git(repo, 'rev-parse', 'HEAD'), head)
        self.assertFalse((repo / '.git' / 'index.lock').exists())

    def test_overlap_is_not_split_and_open_window_includes_dirty_lines(self):
        repo = self.init_repo()
        (repo / 'early.txt').write_text('a\nb\n', encoding='utf-8')
        git(repo, 'add', 'early.txt')
        git(repo, 'commit', '-m', 'early', date=epoch('2026-01-01T10:02:00+00:00'))
        (repo / 'late.txt').write_text('only\n', encoding='utf-8')
        git(repo, 'add', 'late.txt')
        git(repo, 'commit', '-m', 'late', date=epoch('2026-01-01T10:08:00+00:00'))
        left = {'id': 'a', 'normalizedStatus': 'completed'}
        right = {'id': 'b', 'normalizedStatus': 'completed'}
        marks = [
            {'ts': epoch('2026-01-01T10:00:00+00:00'), 'id': 'a', 'status': 'in_progress'},
            {'ts': epoch('2026-01-01T10:10:00+00:00'), 'id': 'a', 'status': 'completed'},
            {'ts': epoch('2026-01-01T10:06:00+00:00'), 'id': 'b', 'status': 'in_progress'},
            {'ts': epoch('2026-01-01T10:10:00+00:00'), 'id': 'b', 'status': 'completed'},
        ]
        self.mod.annotate([left, right], marks, epoch('2026-01-01T12:00:00+00:00'), {'cwd': str(repo)})
        self.assertEqual(left['diff']['insertions'], 2)
        self.assertEqual(left['diff']['files'], 1)
        self.assertTrue(left['diff']['partial'])
        self.assertEqual(right['diff']['insertions'], 0)
        self.assertEqual(right['diff']['files'], 0)
        self.assertTrue(right['diff']['partial'])
        self.assertEqual(right['diff']['basis'], 'window')

        (repo / 'early.txt').write_text('a\nb\nc\n', encoding='utf-8')
        open_task = {'id': 'c', 'normalizedStatus': 'in_progress'}
        open_marks = [
            {'ts': epoch('2026-01-01T10:00:00+00:00'), 'id': 'c', 'status': 'in_progress'},
        ]
        self.mod.clear_cache()
        self.mod.annotate(
            [open_task], open_marks, epoch('2026-01-01T12:00:00+00:00'), {'cwd': str(repo)})
        self.assertEqual(open_task['diff']['basis'], 'window')
        self.assertGreaterEqual(open_task['diff']['insertions'], 2 + 1)
        self.assertFalse(open_task['diff']['partial'])

    def test_branch_worktree_and_ticket_files_and_a_clean_default_branch(self):
        repo = self.init_repo()
        idle = {'id': '1', 'normalizedStatus': 'in_progress'}
        self.mod.annotate([idle], [], epoch('2026-01-01T12:00:00+00:00'), {'cwd': str(repo)})
        self.assertEqual(idle['diff']['status'], 'unavailable')

        git(repo, 'checkout', '-b', 'feature')
        (repo / 'beta.txt').write_text('a\nb\nc\n', encoding='utf-8')
        git(repo, 'add', 'beta.txt')
        git(repo, 'commit', '-m', 'beta', date=epoch('2026-01-01T11:00:00+00:00'))
        feature = {'id': '1', 'normalizedStatus': 'in_progress'}
        self.mod.clear_cache()
        self.mod.annotate([feature], [], epoch('2026-01-01T12:00:00+00:00'), {'cwd': str(repo)})
        self.assertEqual(feature['diff']['basis'], 'branch')
        self.assertTrue(feature['diff']['partial'])
        self.assertEqual(feature['diff']['files'], 1)
        self.assertEqual(feature['diff']['insertions'], 3)
        self.assertEqual(feature['diff']['deletions'], 0)

        tickets_dir = repo / '.team' / 'tickets'
        tickets_dir.mkdir(parents=True)
        (tickets_dir / '01-note.md').write_text(
            '# 01-note: Synthetic note\n\nstatus: doing\n\n## Owned files\n\n- beta.txt\n- ../outside.txt\n',
            encoding='utf-8')
        (tickets_dir / '02-share.md').write_text(
            '# 02-share\n\nstatus: doing\n\n## Owned files\n\n- beta.txt\n',
            encoding='utf-8')
        (tickets_dir / '03-empty.md').write_text(
            '# 03-empty\n\nstatus: todo\n\n## Owned files\n\n- ../nope\n',
            encoding='utf-8')
        (tickets_dir / 'skip me.md').write_text('nope\n', encoding='utf-8')
        self.mod.clear_cache()
        rows = self.mod.annotate([], [], epoch('2026-01-01T12:00:00+00:00'), {'cwd': str(repo)})
        by_id = {row['id']: row for row in rows}
        self.assertEqual(set(by_id), {'01-note', '02-share', '03-empty'})
        self.assertEqual(by_id['01-note']['title'], '01-note: Synthetic note')
        self.assertEqual(by_id['01-note']['status'], 'doing')
        self.assertEqual(by_id['01-note']['diff']['files'], 1)
        self.assertEqual(by_id['01-note']['diff']['insertions'], 3)
        self.assertEqual(by_id['01-note']['diff']['basis'], 'ticket')
        self.assertTrue(by_id['01-note']['diff']['partial'])
        self.assertTrue(by_id['02-share']['diff']['partial'])
        self.assertEqual(by_id['03-empty']['diff']['status'], 'unavailable')
        blob = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn('outside', blob)
        self.assertNotIn(str(repo), blob)
        self.assertIsNone(SECRET.search(blob))

        git(repo, 'checkout', 'main')
        wt = self.tmp / 'wt'
        git(repo, 'worktree', 'add', '-b', 'topic', str(wt))
        (wt / 'gamma.txt').write_text('x\ny\n', encoding='utf-8')
        git(wt, 'add', 'gamma.txt')
        git(wt, 'commit', '-m', 'gamma', date=epoch('2026-01-01T11:30:00+00:00'))
        linked = {'id': '9', 'normalizedStatus': 'in_progress'}
        self.mod.clear_cache()
        self.mod.annotate(
            [linked], [], epoch('2026-01-01T12:00:00+00:00'),
            {'cwd': str(wt), 'worktree': 'topic'})
        self.assertEqual(linked['diff']['basis'], 'worktree')
        self.assertTrue(linked['diff']['partial'])
        self.assertEqual(linked['diff']['files'], 1)
        self.assertEqual(linked['diff']['insertions'], 2)

    def test_external_diff_and_fsmonitor_are_not_required(self):
        repo = self.init_repo()
        git(repo, 'config', 'diff.external', 'team-board-missing-diff-driver')
        git(repo, 'config', 'core.fsmonitor', 'team-board-missing-fsmonitor')
        (repo / 'README').write_text('synthetic\nmore\n', encoding='utf-8')
        task = {'id': '1', 'normalizedStatus': 'in_progress'}
        marks = [{'ts': epoch('2026-01-01T09:30:00+00:00'), 'id': '1', 'status': 'in_progress'}]
        self.mod.clear_cache()
        self.mod.annotate([task], marks, epoch('2026-01-01T12:00:00+00:00'), {'cwd': str(repo)})
        self.assertEqual(task['diff']['status'], 'known')
        self.assertEqual(task['diff']['insertions'], 1)
        self.assertGreaterEqual(task['diff']['files'], 1)


class TaskDiffSnapshotTest(unittest.TestCase):
    def test_snapshot_and_sse_use_the_task_window_without_writing_the_repo(self):
        if not shutil.which('git'):
            self.skipTest('git is not installed')
        env = BoardEnv()
        old_home, old_profile = os.environ.get('HOME'), os.environ.get('USERPROFILE')
        try:
            repo = env.tmp / 'repo'
            repo.mkdir()
            git(repo, 'init', '-b', 'main')
            (repo / 'README').write_text('synthetic\n', encoding='utf-8')
            git(repo, 'add', 'README')
            git(repo, 'commit', '-m', 'init', date=epoch('2026-01-01T09:00:00+00:00'))
            git(repo, 'checkout', '-b', 'feature')
            (repo / 'beta.txt').write_text('a\nb\nc\n', encoding='utf-8')
            git(repo, 'add', 'beta.txt')
            git(repo, 'commit', '-m', 'beta', date=epoch('2026-01-01T10:05:00+00:00'))
            tickets = repo / '.team' / 'tickets'
            tickets.mkdir(parents=True)
            (tickets / '01-note.md').write_text(
                '# 01-note: Synthetic note\n\nstatus: doing\n\n## Owned files\n\n- beta.txt\n',
                encoding='utf-8')
            index = (repo / '.git' / 'index').read_bytes()
            head = git(repo, 'rev-parse', 'HEAD')

            team = env.claude / 'teams' / 'session-sess-000'
            (team / 'inboxes').mkdir(parents=True)
            task_dir = env.claude / 'tasks' / 'session-sess-000'
            task_dir.mkdir(parents=True)
            (team / 'config.json').write_text(json.dumps({
                'members': [
                    {'name': 'synthetic-lead', 'agentType': 'team-lead', 'agentId': 'synthetic-agent-lead'},
                ],
            }), encoding='utf-8')
            task_file = task_dir / 'tasks.json'
            task_file.write_text(json.dumps({'tasks': [
                {'id': '1', 'subject': 'Synthetic window', 'status': 'completed', 'owner': '阿服'},
                {'id': '2', 'subject': 'Synthetic pending', 'status': 'pending'},
            ]}), encoding='utf-8')
            original = task_file.read_bytes()
            lead = env.claude / 'projects' / 'C--demo' / 'sess-0001.jsonl'
            lead.write_text(lead.read_text(encoding='utf-8') + jl({
                'type': 'user', 'timestamp': '2026-01-01T09:30:00Z', 'cwd': str(repo),
                'message': {'role': 'user', 'content': 'synthetic cwd'},
            }), encoding='utf-8')
            sub = env.claude / 'projects' / 'C--demo' / 'sess-0001' / 'subagents' / 'agent-a1.jsonl'
            sub.write_text(sub.read_text(encoding='utf-8') + jl(
                asst('2026-01-01T10:00:00Z', 'claude-sonnet-5', [
                    {'type': 'tool_use', 'id': 'tu-start', 'name': 'TaskUpdate',
                     'input': {'taskId': '1', 'status': 'in_progress', 'owner': '阿服'}},
                ]),
                asst('2026-01-01T10:10:00Z', 'claude-sonnet-5', [
                    {'type': 'tool_use', 'id': 'tu-done', 'name': 'TaskUpdate',
                     'input': {'taskId': '1', 'status': 'completed', 'owner': '阿服'}},
                ]),
            ), encoding='utf-8')
            board = env.load()
            board._task_diff.clear_cache()
            snap = board.build_snapshot(board.argparse.Namespace(
                session=None, project=None, stale=600, source='all'))
            rows = {row['id']: row for row in snap['officialTeam']['tasks']}
            owned = rows['1']['diff']
            self.assertEqual(owned['status'], 'known')
            self.assertEqual(owned['basis'], 'window')
            self.assertFalse(owned['partial'])
            self.assertEqual(owned['files'], 1)
            self.assertEqual(owned['insertions'], 3)
            self.assertEqual(owned['deletions'], 0)
            self.assertEqual(rows['2']['diff']['status'], 'unavailable')
            self.assertIsNone(rows['2']['diff']['files'])
            ticket = snap['tickets'][0]
            self.assertEqual(ticket['id'], '01-note')
            self.assertEqual(ticket['diff']['basis'], 'ticket')
            self.assertEqual(ticket['diff']['insertions'], 3)
            self.assertFalse(ticket['diff']['partial'])
            notice = board.sse_notice(snap, board.snapshot_revision(snap))
            kinds = {(item['kind'], item['id'], item['insertions']) for item in notice['taskDiff']}
            self.assertIn(('task', '1', 3), kinds)
            self.assertIn(('ticket', '01-note', 3), kinds)
            blob = json.dumps(notice, ensure_ascii=False)
            official = json.dumps(snap['officialTeam'], ensure_ascii=False)
            self.assertNotIn(str(repo), blob)
            self.assertNotIn('beta.txt', blob)
            self.assertNotIn(str(repo), official)
            self.assertNotIn('beta.txt', json.dumps(rows['1']['diff']))
            self.assertIsNone(SECRET.search(blob))
            self.assertEqual(task_file.read_bytes(), original)
            self.assertEqual((repo / '.git' / 'index').read_bytes(), index)
            self.assertEqual(git(repo, 'rev-parse', 'HEAD'), head)
            self.assertFalse((repo / '.git' / 'index.lock').exists())
            board._task_diff.clear_cache()
            quiet = board.build_snapshot(board.argparse.Namespace(
                session=None, project=None, stale=600, source='all'))
            quiet_rows = {row['id']: row for row in quiet['officialTeam']['tasks']}
            self.assertEqual(quiet_rows['1']['diff'], owned)
            moved = json.loads(json.dumps(quiet))
            moved['officialTeam']['tasks'][0]['diff']['insertions'] = 99
            self.assertNotEqual(board.snapshot_revision(quiet), board.snapshot_revision(moved))
        finally:
            env.cleanup()
            if old_home is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = old_home
            if old_profile is None:
                os.environ.pop('USERPROFILE', None)
            else:
                os.environ['USERPROFILE'] = old_profile


class TaskDiffBrowserTest(unittest.TestCase):
    def test_demo_cards_show_diff_counts_in_light_and_narrow_dark(self):
        if not shutil.which('node'):
            self.skipTest('node is not installed')
        import headless_chrome
        import test_demo_board
        binary = headless_chrome.chrome_binary()
        if not binary:
            self.skipTest('chrome is not installed')
        server = test_demo_board.demo.DemoServer(0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        shots = Path('/opt/cursor/artifacts')
        if not shots.is_dir():
            shots = Path(tempfile.mkdtemp(prefix='task-diff-shots-'))
        try:
            with headless_chrome.launch_headless_chrome(binary) as chrome:
                completed = subprocess.run(
                    [shutil.which('node'), str(Path(__file__).resolve().parent / 'check_task_diff_browser.js'),
                     'http://127.0.0.1:%d/' % server.server_port,
                     str(chrome.port), str(shots)],
                    check=False, capture_output=True, text=True, timeout=60)
                if completed.returncode != 0:
                    detail = (completed.stdout or '') + (completed.stderr or '')
                    self.fail(detail[-4000:])
                report = json.loads(completed.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertIn('2 文件 · +18 -3', report['card1'])
        self.assertNotIn('部分', report['card1'])
        self.assertIn('1 文件 · +4 -1 · 部分', report['card2'])
        self.assertNotIn('文件', report['card3'])
        self.assertIn('改动', report['drawer1'])
        self.assertIn('2 文件 · +18 -3', report['drawer1'])
        self.assertIn('只读 git', report['drawer1'])
        self.assertIn('改动未知', report['drawer3'])
        self.assertNotIn('0 文件', report['drawer3'])
        self.assertIn('Synthetic ticket', report['tickets'])
        self.assertIn('1 文件 · +6 -0', report['tickets'])
        self.assertIn('2 文件 · +18 -3', report['narrow'])
        self.assertIn('1 文件 · +6 -0', report['narrowTickets'])
        self.assertNotIn('sk-', report['card1'] + report['tickets'])
