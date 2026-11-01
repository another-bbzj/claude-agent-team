"""Offline plan validation and preservation of existing workspaces."""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('team_workspace', ROOT / 'tools' / 'team_workspace.py')
workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace)


def plan():
    return {'name': 'Offline release', 'tasks': [
        {'id': '01-code', 'title': 'Implement', 'owner': 'backend-dev', 'files': ['src/app.py'],
         'depends_on': [], 'acceptance': ['CLI returns the expected summary']},
        {'id': '02-check', 'title': 'Verify', 'owner': 'qa-tester', 'files': ['tests/test_app.py'],
         'depends_on': ['01-code'], 'acceptance': ['Regression fails before and passes after']},
        {'id': '03-docs', 'title': 'Document', 'owner': 'docs-writer', 'files': ['README.md'],
         'depends_on': [], 'acceptance': ['Example is reproducible']}]}


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='team-workspace-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_creation_uses_dependency_waves_and_owned_files(self):
        output = self.root / 'repo'
        output.mkdir()
        existing = output / 'source.py'
        existing.write_text('user work', encoding='utf-8')
        result = workspace.create_workspace(plan(), output)
        self.assertEqual(result, output / '.team')
        self.assertIn('wave: 2', (result / 'tickets/02-check.md').read_text(encoding='utf-8'))
        self.assertIn('wave: 1', (result / 'tickets/03-docs.md').read_text(encoding='utf-8'))
        self.assertIn('- [ ] CLI returns', (result / 'tickets/01-code.md').read_text(encoding='utf-8'))
        self.assertEqual(len(list((result / 'inbox').glob('*.md'))), 3)
        self.assertTrue((result / 'handoffs').is_dir())
        self.assertEqual(existing.read_text(encoding='utf-8'), 'user work')
        self.assertEqual(list(output.glob('.team-stage-*')), [])

    def test_invalid_plans_make_no_files(self):
        invalid = []
        value = plan()
        value['tasks'][0]['depends_on'] = ['02-check']
        invalid.append(value)
        value = plan()
        value['tasks'][0]['depends_on'] = ['missing']
        invalid.append(value)
        value = plan()
        value['tasks'][1]['id'] = value['tasks'][0]['id']
        invalid.append(value)
        value = plan()
        value['tasks'][1]['files'] = ['SRC/App.py']
        invalid.append(value)
        value = plan()
        value['tasks'][1]['files'] = ['src']
        invalid.append(value)
        for key, bad in [('name', ''), ('name', '\ud800'), ('tasks', []), ('tasks', 'bad')]:
            value = plan()
            value[key] = bad
            invalid.append(value)
        for field in ('owner', 'title', 'acceptance', 'files', 'depends_on'):
            value = plan()
            value['tasks'][0][field] = None
            invalid.append(value)
        for value in invalid:
            with self.subTest(plan=value), self.assertRaises(workspace.PlanError):
                workspace.create_workspace(value, self.root / 'new')
            self.assertFalse((self.root / 'new').exists())

    def test_unsafe_paths_and_generated_names_are_rejected(self):
        for path in ('../escape', '/absolute', 'C:/drive', 'C:relative', '\\\\server\\share',
                     'src/../escape', 'src\\escape', './app.py', 'src//app.py', 'NUL.txt',
                     'src/COM1', 'app.py.', 'src/foo:bar', 'src/../app.py'):
            value = plan()
            value['tasks'][0]['files'] = [path]
            with self.subTest(path=path), self.assertRaises(workspace.PlanError):
                workspace.create_workspace(value, self.root / 'new')
        for field in ('id', 'owner'):
            for unsafe in ('../outside', 'NUL', 'con', 'a/b', 'a\\b'):
                value = plan()
                value['tasks'][0][field] = unsafe
                with self.subTest(field=field, value=unsafe), self.assertRaises(workspace.PlanError):
                    workspace.create_workspace(value, self.root / 'new')
        self.assertFalse((self.root / 'new').exists())

    def test_existing_workspace_is_preserved(self):
        output = self.root / 'repo'
        target = output / '.team'
        target.mkdir(parents=True)
        marker = target / 'user.md'
        marker.write_text('keep', encoding='utf-8')
        with self.assertRaises(workspace.PlanError):
            workspace.create_workspace(plan(), output)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')
        self.assertEqual(list(target.iterdir()), [marker])

    def test_write_failure_cleans_staging_without_publishing(self):
        output = self.root / 'repo'
        with mock.patch.object(workspace, '_write_tree', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                workspace.create_workspace(plan(), output)
        self.assertEqual(list(output.iterdir()), [])

    def test_symlink_output_and_dangling_team_are_refused(self):
        target = self.root / 'target'
        target.mkdir()
        link = self.root / 'link'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation is unavailable')
        with self.assertRaises(workspace.PlanError):
            workspace.create_workspace(plan(), link / 'child')
        (target / '.team').symlink_to(self.root / 'missing', target_is_directory=True)
        with self.assertRaises(workspace.PlanError):
            workspace.create_workspace(plan(), target)
        self.assertFalse((target / 'child').exists())

    def test_detected_link_is_refused_without_needing_symlink_privileges(self):
        output = self.root / 'linked' / 'repo'
        original = Path.is_symlink

        def is_link(path):
            return path == output.parent or original(path)

        with mock.patch.object(Path, 'is_symlink', is_link):
            with self.assertRaises(workspace.PlanError):
                workspace.create_workspace(plan(), output)
        self.assertFalse(output.parent.exists())

    def test_concurrent_workspace_is_preserved_and_private_stage_cleaned(self):
        output = self.root / 'repo'
        publish = workspace._publish

        def interfere(stage, target):
            target.mkdir()
            (target / 'user.md').write_text('keep concurrent content', encoding='utf-8')
            return publish(stage, target)

        with mock.patch.object(workspace, '_publish', side_effect=interfere):
            with self.assertRaises(OSError):
                workspace.create_workspace(plan(), output)
        self.assertEqual((output / '.team/user.md').read_text(encoding='utf-8'), 'keep concurrent content')
        self.assertEqual(list(output.glob('.team-stage-*')), [])

    def test_posix_publication_uses_exclusive_files(self):
        stage = self.root / 'stage'
        stage.mkdir()
        normalized, waves = workspace.validate_plan(plan())
        workspace._write_tree(stage, normalized, waves)
        target = self.root / '.team'
        with mock.patch.object(workspace, 'os', types.SimpleNamespace(name='posix')):
            workspace._publish(stage, target)
        self.assertEqual((target / 'SPEC.md').read_bytes(), (stage / 'SPEC.md').read_bytes())
        self.assertTrue((target / 'tickets/02-check.md').is_file())

    def test_posix_failure_does_not_delete_user_content(self):
        stage = self.root / 'stage'
        stage.mkdir()
        normalized, waves = workspace.validate_plan(plan())
        workspace._write_tree(stage, normalized, waves)
        target = self.root / '.team'
        original = Path.open

        def concurrent_write(path, mode='r', *args, **kwargs):
            if mode == 'xb':
                with original(target / 'user.md', 'w', encoding='utf-8') as handle:
                    handle.write('concurrent user work')
                raise OSError('disk full')
            return original(path, mode, *args, **kwargs)

        with mock.patch.object(workspace, 'os', types.SimpleNamespace(name='posix')):
            with mock.patch.object(Path, 'open', concurrent_write):
                with self.assertRaisesRegex(workspace.PlanError, 'partial .team'):
                    workspace._publish(stage, target)
        self.assertEqual((target / 'user.md').read_text(encoding='utf-8'), 'concurrent user work')

    def test_cli_success_and_safe_failures(self):
        source = self.root / 'plan.json'
        source.write_text(json.dumps(plan()), encoding='utf-8')
        output = self.root / 'new'
        cmd = [sys.executable, str(ROOT / 'tools/team_workspace.py'), '--plan', str(source), '--output', str(output)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / '.team/SPEC.md').exists())
        again = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertEqual(again.returncode, 1)
        self.assertIn('already exists', again.stderr)
        source.write_text('{bad JSON', encoding='utf-8')
        bad = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertEqual(bad.returncode, 1)
        self.assertNotIn('Traceback', bad.stderr)

    def test_input_order_does_not_change_waves(self):
        value = plan()
        _, expected = workspace.validate_plan(value)
        changed = copy.deepcopy(value)
        changed['tasks'].reverse()
        _, actual = workspace.validate_plan(changed)
        self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
