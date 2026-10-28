"""同一仓库的多路 git worktree 收成一个作战区，并且各自带着正确的分支名。

夹具是合成的：examples/worktrees.synthetic 只有相对 gitdir；测试在临时目录里
写转录。不读、不写贡献者自己的 ~/.claude。
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'examples' / 'worktrees.synthetic' / 'repo'


def jl(*entries):
    return '\n'.join(json.dumps(e, ensure_ascii=False) for e in entries) + '\n'


def load_board(home: Path):
    os.environ['HOME'] = str(home)
    os.environ['USERPROFILE'] = str(home)
    spec = importlib.util.spec_from_file_location('team_board_worktrees', ROOT / 'team-board' / 'team_board.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WorktreeTheaterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_home = os.environ.get('HOME')
        cls._old_profile = os.environ.get('USERPROFILE')
        cls.home = Path(tempfile.mkdtemp(prefix='worktree-home-'))
        try:
            cls.tb = load_board(cls.home)
        except Exception:
            cls._restore_env()
            shutil.rmtree(cls.home, ignore_errors=True)
            raise

    @classmethod
    def tearDownClass(cls):
        cls._restore_env()
        shutil.rmtree(cls.home, ignore_errors=True)

    @classmethod
    def _restore_env(cls):
        if cls._old_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = cls._old_home
        if cls._old_profile is None:
            os.environ.pop('USERPROFILE', None)
        else:
            os.environ['USERPROFILE'] = cls._old_profile

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='worktree-case-'))
        self.projects = self.tmp / 'projects'
        self.projects.mkdir()
        self.tb.PROJECTS = self.projects
        self.tb._lead_git_cache.clear()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def write_session(self, cwd, session, transcript_branch, mtime):
        proj = self.projects / self.tb.project_key(cwd)
        sub = proj / session / 'subagents'
        sub.mkdir(parents=True)
        lead = {
            'type': 'user', 'timestamp': '2026-01-01T10:00:00Z', 'cwd': cwd,
            'gitBranch': transcript_branch, 'isSidechain': False,
            'message': {'role': 'user', 'content': 'synthetic worktree session'},
        }
        agent_user = {
            'type': 'user', 'timestamp': '2026-01-01T10:00:10Z', 'cwd': cwd,
            'message': {'role': 'user', 'content': 'go'},
        }
        agent = {
            'type': 'assistant', 'timestamp': '2026-01-01T10:00:20Z', 'cwd': cwd,
            'gitBranch': transcript_branch,
            'message': {
                'role': 'assistant', 'model': 'claude-sonnet-5', 'stop_reason': 'end_turn',
                'content': [{'type': 'text', 'text': 'synthetic done'}],
                'usage': {'input_tokens': 1, 'output_tokens': 1},
            },
        }
        (proj / (session + '.jsonl')).write_text(jl(lead), encoding='utf-8')
        (sub / 'agent-a1.jsonl').write_text(jl(agent_user, agent), encoding='utf-8')
        (sub / 'agent-a1.meta.json').write_text(
            json.dumps({'agentType': 'backend-dev', 'description': 'synthetic worktree'}),
            encoding='utf-8')
        for path in (proj / (session + '.jsonl'), sub / 'agent-a1.jsonl'):
            os.utime(path, (mtime, mtime))

    def snapshot(self, session=None):
        return self.tb.build_snapshot(self.tb.argparse.Namespace(session=session, project=None, stale=600))

    def assert_theater_has_no_checkout_path(self, snap):
        blob = json.dumps(snap['theaters'], ensure_ascii=False)
        self.assertNotIn(str(self.tmp), blob)
        self.assertNotIn(str(self.home), blob)
        for theater in snap['theaters']:
            self.assertRegex(theater['id'], r'^wt-[0-9a-f]{12}$')
            self.assertNotIn('/', theater['label'])
            self.assertNotIn('\\', theater['label'])

    def test_claude_worktree_fixture_groups_sessions_and_prefers_head(self):
        gitfile = (FIXTURE / 'dot-claude' / 'worktrees' / 'feature-board' / 'gitfile').read_text(encoding='utf-8')
        pointer = gitfile.split(':', 1)[1].strip()
        self.assertTrue(gitfile.startswith('gitdir:'))
        self.assertFalse(pointer.startswith('/'))
        self.assertFalse(len(pointer) > 1 and pointer[1] == ':')

        repo = self.tmp / 'synthetic-repo'
        shutil.copytree(FIXTURE, repo)
        (repo / 'dot-git').rename(repo / '.git')
        (repo / 'dot-claude').rename(repo / '.claude')
        gitfile_path = repo / '.claude' / 'worktrees' / 'feature-board' / 'gitfile'
        gitfile_path.rename(gitfile_path.with_name('.git'))
        worktree = repo / '.claude' / 'worktrees' / 'feature-board'

        # 主工作区的转录只有占位符 HEAD；worktree 的转录仍写着 main（Claude Code 会冻住 gitBranch）。
        self.write_session(str(repo), 'sess-main', 'HEAD', 1_700_000_000)
        self.write_session(str(worktree), 'sess-feature', 'main', 1_700_000_100)
        snap = self.snapshot()
        again = self.snapshot()
        self.assertEqual(again['theaters'], snap['theaters'])

        self.assertEqual(len(snap['theaters']), 1)
        theater = snap['theaters'][0]
        self.assertEqual(theater['label'], 'synthetic-repo')
        self.assertEqual([s['id'] for s in theater['sessions']], ['sess-main', 'sess-feature'])
        self.assertEqual([s['branch'] for s in theater['sessions']], ['main', 'worktree-feature-board'])
        self.assertEqual([s['worktree'] for s in theater['sessions']], ['', 'feature-board'])
        self.assertTrue(theater['sessions'][0]['primary'])
        self.assertFalse(theater['sessions'][1]['primary'])
        self.assert_theater_has_no_checkout_path(snap)

        self.assertEqual(snap['session']['id'], 'sess-feature')
        self.assertEqual(snap['session']['branch'], 'worktree-feature-board')
        self.assertEqual(snap['session']['worktree'], 'feature-board')
        self.assertEqual(snap['session']['theaterId'], theater['id'])
        listed = {s['id']: s['branch'] for s in snap['sessions']}
        self.assertEqual(listed['sess-main'], 'main')
        self.assertEqual(listed['sess-feature'], 'worktree-feature-board')
        self.assertIn('officialTeam', snap)
        self.assertGreaterEqual(len(snap['members']), 1)

        main = self.snapshot('sess-main')
        self.assertEqual(main['session']['id'], 'sess-main')
        self.assertEqual(main['session']['branch'], 'main')
        self.assertEqual(main['session']['theaterId'], theater['id'])
        self.assertEqual(main['theaters'][0]['id'], theater['id'])

    def test_sibling_linked_worktree_joins_the_primary_checkout(self):
        repo = self.tmp / 'synthetic-repo'
        (repo / '.git').mkdir(parents=True)
        (repo / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
        admin = repo / '.git' / 'worktrees' / 'fix-board'
        admin.mkdir(parents=True)
        (admin / 'HEAD').write_text('ref: refs/heads/fix-board\n', encoding='utf-8')
        (admin / 'commondir').write_text('../..\n', encoding='utf-8')
        sibling = self.tmp / 'synthetic-repo-fix'
        sibling.mkdir()
        rel = os.path.relpath(str(admin), str(sibling)).replace(os.sep, '/')
        (sibling / '.git').write_text('gitdir: %s\n' % rel, encoding='utf-8')

        self.write_session(str(repo), 'sess-main', 'HEAD', 1_700_000_000)
        self.write_session(str(sibling), 'sess-fix', 'main', 1_700_000_100)
        snap = self.snapshot()
        self.assertEqual(len(snap['theaters']), 1)
        by_id = {s['id']: s for s in snap['theaters'][0]['sessions']}
        self.assertEqual(set(by_id), {'sess-main', 'sess-fix'})
        self.assertEqual(by_id['sess-main']['branch'], 'main')
        self.assertTrue(by_id['sess-main']['primary'])
        self.assertEqual(by_id['sess-fix']['branch'], 'fix-board')
        self.assertFalse(by_id['sess-fix']['primary'])
        self.assertEqual(snap['session']['branch'], 'fix-board')
        self.assert_theater_has_no_checkout_path(snap)

    def test_missing_git_metadata_still_groups_claude_worktree_paths(self):
        repo = self.tmp / 'synthetic-repo'
        worktree = repo / '.claude' / 'worktrees' / 'feature-board'
        worktree.mkdir(parents=True)
        other = self.tmp / 'other-repo'
        (other / '.git').mkdir(parents=True)
        (other / '.git' / 'HEAD').write_text('ref: refs/heads/other\n', encoding='utf-8')
        self.write_session(str(repo), 'sess-main', 'main', 1_700_000_000)
        self.write_session(str(worktree), 'sess-wt', 'feature/board', 1_700_000_100)
        self.write_session(str(other), 'sess-other', 'stale', 1_700_000_050)

        snap = self.snapshot()
        self.assertEqual([t['label'] for t in snap['theaters']], ['synthetic-repo'])
        got = {s['id']: s for s in snap['theaters'][0]['sessions']}
        self.assertEqual(set(got), {'sess-main', 'sess-wt'})
        self.assertEqual(got['sess-main']['branch'], 'main')
        self.assertEqual(got['sess-main']['worktree'], '')
        self.assertTrue(got['sess-main']['primary'])
        self.assertEqual(got['sess-wt']['branch'], 'feature/board')
        self.assertEqual(got['sess-wt']['worktree'], 'feature-board')
        other_row = next(s for s in snap['sessions'] if s['id'] == 'sess-other')
        self.assertEqual(other_row['branch'], 'other')
        self.assertEqual(other_row['theaterId'], '')
        self.assert_theater_has_no_checkout_path(snap)

    def test_same_checkout_does_not_form_a_theater(self):
        repo = self.tmp / 'synthetic-repo'
        (repo / '.git').mkdir(parents=True)
        (repo / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
        cwd = str(repo)
        self.write_session(cwd, 'sess-a', 'main', 1_700_000_000)
        self.write_session(cwd, 'sess-b', 'main', 1_700_000_100)
        snap = self.snapshot()
        self.assertEqual(snap['theaters'], [])
        self.assertEqual({s['id']: s['branch'] for s in snap['sessions']}, {'sess-a': 'main', 'sess-b': 'main'})
        self.assertEqual(snap['session']['theaterId'], '')
        self.assertEqual(snap['session']['branch'], 'main')

    def test_placeholder_and_detached_head_are_not_branch_names(self):
        self.assertEqual(self.tb._clean_branch('HEAD'), '')
        self.assertEqual(self.tb._clean_branch('feature/board'), 'feature/board')
        self.assertEqual(self.tb._clean_branch('feature/../main'), '')
        git = self.tmp / 'detached'
        git.mkdir()
        (git / 'HEAD').write_text('0123456789abcdef0123456789abcdef01234567\n', encoding='utf-8')
        self.assertEqual(self.tb._branch_from_head_file(git), '')
        (git / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
        self.assertEqual(self.tb._branch_from_head_file(git), 'main')


if __name__ == '__main__':
    unittest.main()
