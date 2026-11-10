"""Pre-release evidence: version notes, README scripts, static site, and test discovery."""
import contextlib
import importlib.util
import io
import json
import re
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_check_under_test', ROOT / 'tools' / 'release_check.py')
release_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_check)


def _run(argv, root):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = release_check.main(list(argv), root=root)
    return code, buf.getvalue()


class ReleaseCheckFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='release-check-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip('\n'), encoding='utf-8')
        return path

    def passing_tree(self):
        self.write('VERSION', '1.2.0\n')
        self.write('CHANGELOG.md', """
            # 变更记录

            ## 未发布

            - 一条尚未发版的说明

            ## 1.2.0 — 2026-01-01

            - 已发布

            ## 9.9.9 — 2020-01-01

            - 更早的版本，不能盖过上面的 1.2.0
            """)
        self.write('README.md', """
            # 示例

            ```bash
            python tools/demo_board.py --port 1
            python install.py
            python -m unittest discover -s tests -v
            ```

            可选 `python install.py --hooks`。
            本机路径 `python ~/.claude/team-board/ensure.py` 不要求仓库里有这个文件。
            通配 `tests/test_*.py` 也不是一个文件。
            """)
        self.write('README.en.md', """
            Use `export_snapshot.py` and `msg.py`.
            `python3.12 tools/demo_board.py` is the same script.
            """)
        self.write('tools/demo_board.py', 'print("demo")\n')
        self.write('install.py', 'print("install")\n')
        self.write('tools/export_snapshot.py', 'print("export")\n')
        self.write('team-board/msg.py', 'print("msg")\n')
        self.write('tests/test_sample.py', """
            import unittest

            class SampleTest(unittest.TestCase):
                def test_ok(self):
                    self.assertTrue(True)
            """)
        self.write('tests/helper.py', 'import this_helper_must_not_be_discovered\n')

    def test_version_uses_the_first_heading_and_ignores_older_ones(self):
        self.passing_tree()
        result = release_check.check_version(self.root)
        self.assertTrue(result.ok, result.detail)
        self.assertIn('1.2.0', result.detail)
        self.assertNotIn('9.9.9', result.detail)

    def test_version_mismatch_blank_and_missing_changelog_fail(self):
        self.write('VERSION', '1.2.1\n')
        self.write('CHANGELOG.md', '## 未发布\n\n- 笔记\n\n## 1.2.0 — 2026-01-01\n')
        mismatch = release_check.check_version(self.root)
        self.assertFalse(mismatch.ok)
        self.assertIn('1.2.1', mismatch.detail)
        self.assertIn('1.2.0', mismatch.detail)

        self.write('VERSION', '1.2.0 extra\n')
        invalid = release_check.check_version(self.root)
        self.assertFalse(invalid.ok)
        self.assertIn('不是', invalid.detail)

        self.write('VERSION', '\n')
        blank = release_check.check_version(self.root)
        self.assertFalse(blank.ok)

        (self.root / 'VERSION').unlink()
        missing_version = release_check.check_version(self.root)
        self.assertFalse(missing_version.ok)
        self.assertIn('没有 VERSION', missing_version.detail)

        (self.root / 'CHANGELOG.md').unlink()
        self.write('VERSION', '1.2.0\n')
        missing_log = release_check.check_version(self.root)
        self.assertFalse(missing_log.ok)
        self.assertIn('CHANGELOG.md', missing_log.detail)

    def test_crlf_changelog_and_version_still_match(self):
        (self.root / 'VERSION').write_bytes(b'\xef\xbb\xbf1.2.0\r\n')
        (self.root / 'CHANGELOG.md').write_bytes(
            '## 未发布\r\n\r\n- 一条\r\n\r\n## 1.2.0 — 2026-01-01\r\n'.encode('utf-8'))
        self.assertTrue(release_check.check_version(self.root).ok)
        unreleased = release_check.check_unreleased(self.root)
        self.assertTrue(unreleased.ok, unreleased.detail)
        self.assertIn('1 行', unreleased.detail)

    def test_unreleased_requires_prose_not_just_headings(self):
        self.write('CHANGELOG.md', """
            ## 未发布

            ### 新增

            <details>
            </details>

            <!-- 隐藏的说明不算 -->

            ## 1.2.0 — 2026-01-01

            - 已发布
            """)
        empty = release_check.check_unreleased(self.root)
        self.assertFalse(empty.ok)
        self.assertIn('没有正文', empty.detail)

        self.write('CHANGELOG.md', """
            ## 未发布

            <details>
            <summary>有一条说明</summary>
            </details>

            ## 1.2.0 — 2026-01-01
            """)
        present = release_check.check_unreleased(self.root)
        self.assertTrue(present.ok, present.detail)

        self.write('CHANGELOG.md', '## 1.2.0 — 2026-01-01\n\n- 已发布\n')
        missing = release_check.check_unreleased(self.root)
        self.assertFalse(missing.ok)
        self.assertIn('没有「未发布」', missing.detail)

    def test_readme_scripts_must_exist_and_home_paths_are_ignored(self):
        self.passing_tree()
        result = release_check.check_readme(self.root)
        self.assertTrue(result.ok, result.detail)
        self.assertIn('4 个脚本都在', result.detail)
        tokens = release_check.script_tokens((self.root / 'README.md').read_text(encoding='utf-8'))
        self.assertEqual(sorted(tokens), ['install.py', 'tools/demo_board.py'])
        english = release_check.script_tokens((self.root / 'README.en.md').read_text(encoding='utf-8'))
        self.assertEqual(english, ['export_snapshot.py', 'msg.py', 'tools/demo_board.py'])

    def test_missing_relative_path_is_not_satisfied_by_another_copy(self):
        self.write('README.md', 'python tools/demo_board.py\n')
        self.write('demo_board.py', 'print(1)\n')
        result = release_check.check_readme(self.root)
        self.assertFalse(result.ok)
        self.assertIn('tools/demo_board.py', result.detail)
        self.assertIn('README.md', result.detail)

    def test_english_readme_missing_script_fails(self):
        self.write('README.md', 'python install.py\n')
        self.write('install.py', 'print(1)\n')
        self.write('README.en.md', 'python tools/missing.py\n')
        result = release_check.check_readme(self.root)
        self.assertFalse(result.ok)
        self.assertIn('tools/missing.py', result.detail)
        self.assertIn('README.en.md', result.detail)

    def test_readme_without_a_repo_script_fails(self):
        self.write('README.md', '只运行 `python ~/.claude/team-board/ensure.py`。\n')
        result = release_check.check_readme(self.root)
        self.assertFalse(result.ok)
        self.assertIn('没有引用', result.detail)
        self.root.joinpath('README.md').unlink()
        missing = release_check.check_readme(self.root)
        self.assertFalse(missing.ok)
        self.assertIn('没有 README.md', missing.detail)

    def test_pages_check_is_invoked_and_skips_when_absent(self):
        self.write('tools/build_pages_site.py', """
            import pathlib
            import sys
            pathlib.Path('called.txt').write_text(' '.join(sys.argv[1:]), encoding='utf-8')
            if '--check' not in sys.argv:
                raise SystemExit(2)
            print('pages ok')
            """)
        result = release_check.check_pages(self.root)
        self.assertTrue(result.ok, result.detail)
        self.assertIn('已运行', result.detail)
        self.assertEqual((self.root / 'called.txt').read_text(encoding='utf-8'), '--check')

        self.write('tools/build_pages_site.py', 'print("build only")\n')
        skipped = release_check.check_pages(self.root)
        self.assertTrue(skipped.ok, skipped.detail)
        self.assertIn('跳过', skipped.detail)
        self.assertIn('没有 --check', skipped.detail)

        (self.root / 'tools' / 'build_pages_site.py').unlink()
        absent = release_check.check_pages(self.root)
        self.assertTrue(absent.ok, absent.detail)
        self.assertIn('没有 tools/build_pages_site.py', absent.detail)

    def test_pages_nonzero_exit_fails(self):
        self.write('tools/build_pages_site.py', """
            import sys
            sys.stderr.write('drift snapshot.json\\n')
            # --check
            raise SystemExit(1)
            """)
        result = release_check.check_pages(self.root)
        self.assertFalse(result.ok)
        self.assertIn('退出码 1', result.detail)
        self.assertIn('drift snapshot.json', result.detail)

    def test_tests_are_discovered_without_running_them_or_loading_helpers(self):
        self.write('tests/test_sample.py', """
            import unittest

            class SampleTest(unittest.TestCase):
                def test_one(self):
                    raise AssertionError('discovery must not run this')

                def test_two(self):
                    pass
            """)
        self.write('tests/helper.py', 'import this_helper_must_not_be_discovered\n')
        result = release_check.check_tests(self.root)
        self.assertTrue(result.ok, result.detail)
        self.assertIn('发现 2 个测试', result.detail)
        self.assertNotIn('this_helper_must_not_be_discovered', result.detail)

    def test_import_errors_and_empty_or_missing_tests_fail(self):
        self.write('tests/test_broken.py', 'import definitely_missing_release_check_mod\n')
        self.write('tests/test_also_broken.py', 'import another_missing_release_check_mod\n')
        broken = release_check.check_tests(self.root)
        self.assertFalse(broken.ok)
        self.assertIn('无法加载测试', broken.detail)
        self.assertIn('另有 1 个', broken.detail)

        for path in (self.root / 'tests').glob('test_*.py'):
            path.unlink()
        self.write('tests/test_empty.py', 'VALUE = 1\n')
        self.write('tests/helper.py', 'import this_helper_must_not_be_discovered\n')
        empty = release_check.check_tests(self.root)
        self.assertFalse(empty.ok)
        self.assertIn('没有可发现的测试', empty.detail)
        self.assertNotIn('this_helper_must_not_be_discovered', empty.detail)

        shutil.rmtree(str(self.root / 'tests'))
        missing = release_check.check_tests(self.root)
        self.assertFalse(missing.ok)
        self.assertIn('没有 tests/', missing.detail)

    def test_table_and_json_use_the_exit_code(self):
        self.passing_tree()
        code, out = _run([], self.root)
        self.assertEqual(code, 0, out)
        self.assertIn('5 项通过，0 项失败', out)
        self.assertIn('版本号', out)
        self.assertIn('通过', out)
        self.assertNotIn('"ok"', out)

        self.write('VERSION', '9.9.9\n')
        code, out = _run(['--json'], self.root)
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload['ok'])
        self.assertEqual(
            [item['name'] for item in payload['checks']],
            ['version', 'unreleased', 'readme_commands', 'pages_site', 'tests_discoverable'],
        )
        self.assertFalse(payload['checks'][0]['ok'])
        self.assertTrue(all(item['ok'] for item in payload['checks'][1:]))
        self.assertIn('跳过', payload['checks'][3]['detail'])
        self.assertNotIn('项通过', out)

    def test_report_marks_a_failed_row(self):
        text = release_check.format_report([
            release_check.Check('version', True, '一致'),
            release_check.Check('unreleased', False, '没有正文'),
        ])
        self.assertIn('1 项通过，1 项失败', text)
        self.assertIn('失败', text)
        self.assertIn('未发布', text)


class RepositoryReleaseCheckTest(unittest.TestCase):
    def test_readmes_cite_scripts_that_exist(self):
        tokens = []
        for name in ('README.md', 'README.en.md'):
            tokens.extend(release_check.script_tokens((ROOT / name).read_text(encoding='utf-8')))
        for token in (
            'tools/demo_board.py',
            'install.py',
            'team-board/team_board.py',
            'tools/export_replay.py',
            'tools/battle_card.py',
            'tools/retro_report.py',
            'export_snapshot.py',
            'msg.py',
            'tools/release_check.py',
        ):
            self.assertIn(token, tokens)
        result = release_check.check_readme(ROOT)
        self.assertTrue(result.ok, result.detail)

    def test_version_matches_changelog_and_unreleased_has_notes(self):
        version = release_check.check_version(ROOT)
        unreleased = release_check.check_unreleased(ROOT)
        self.assertTrue(version.ok, version.detail)
        self.assertTrue(unreleased.ok, unreleased.detail)
        self.assertIn('1.4.3', version.detail)

    def test_checklist_and_ci_job_match_the_command(self):
        doc = (ROOT / 'docs' / 'RELEASE_CHECKLIST.md').read_text(encoding='utf-8')
        self.assertIn('python tools/release_check.py', doc)
        self.assertIn('未发布', doc)
        self.assertIn('build_pages_site.py --check', doc)
        self.assertIn('ubuntu-24.04', doc)
        self.assertIn('3.8', doc)
        self.assertIn('用户配置', doc)
        self.assertIn('不打 tag', doc)
        workflow = (ROOT / '.github' / 'workflows' / 'test.yml').read_text(encoding='utf-8')
        self.assertIn('\n  release-check:\n', workflow)
        self.assertNotIn('needs: release-check', workflow)
        start = workflow.index('  release-check:\n') + len('  release-check:\n')
        rest = workflow[start:]
        nxt = re.search(r'\n  [A-Za-z0-9-]+:\n', rest)
        block = rest[:nxt.start()] if nxt else rest
        self.assertNotIn('needs:', block)
        self.assertIn('python tools/release_check.py', block)
        self.assertNotIn('\n    needs:', block)

    def test_this_checkout_exits_zero(self):
        code, out = _run(['--json'], ROOT)
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertTrue(payload['ok'])
        by_name = {item['name']: item for item in payload['checks']}
        self.assertEqual(list(by_name), [
            'version', 'unreleased', 'readme_commands', 'pages_site', 'tests_discoverable',
        ])
        for item in payload['checks']:
            self.assertTrue(item['ok'], item)
        self.assertIn('已运行', by_name['pages_site']['detail'])
        self.assertNotIn('跳过', by_name['pages_site']['detail'])
        match = re.search(r'发现 (\d+) 个测试', by_name['tests_discoverable']['detail'])
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 20)


if __name__ == '__main__':
    unittest.main()
