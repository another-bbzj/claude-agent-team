"""Static GitHub Pages demo: synthetic snapshot only, no personal data.

The examples privacy regex scans docs/site snapshot JSON, the over-budget
block, the boot script and the site README. It does not scan the copied board
shell: class names such as task-board contain the letters sk-, and a form
placeholder mentions a generic C:\\Users\\ path. Those strings are UI chrome,
not the fixture.
"""
import importlib.util
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from test_agent_teams_fixture import SECRET

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / 'docs' / 'site'


def _builder():
    spec = importlib.util.spec_from_file_location('pages_site_under_test', ROOT / 'tools' / 'build_pages_site.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _builder()


def _extract(html, script_id):
    marker = '<script id="%s"' % script_id
    start = html.find(marker)
    if start < 0:
        raise AssertionError('missing ' + script_id)
    open_end = html.find('>', start)
    close = html.find('</script>', open_end)
    if open_end < 0 or close < 0:
        raise AssertionError('unclosed ' + script_id)
    return html[open_end + 1:close].strip()


class PagesSiteTest(unittest.TestCase):
    def test_committed_site_matches_the_builder(self):
        problems = builder.check_site(SITE)
        self.assertEqual(problems, [], '\n'.join(problems))

    def test_render_rejects_a_board_that_lost_the_demo_flag(self):
        with self.assertRaises(SystemExit):
            builder.render_index('<body></body><script>const DEMO = true;</script>', {}, {}, '/* boot */')

    def test_site_is_a_frozen_synthetic_snapshot(self):
        fixture = json.loads((ROOT / 'examples' / 'board-demo.synthetic.json').read_text(encoding='utf-8'))
        snap = json.loads((SITE / 'snapshot.json').read_text(encoding='utf-8'))
        over = json.loads((SITE / 'budget-over.json').read_text(encoding='utf-8'))
        html = (SITE / 'index.html').read_text(encoding='utf-8')
        embedded = json.loads(_extract(html, 'pages-snapshot'))
        embedded_over = json.loads(_extract(html, 'pages-budget-over'))
        self.assertEqual(embedded, snap)
        self.assertEqual(embedded_over, over)
        self.assertEqual(snap['generatedAt'], fixture['generatedAt'])
        self.assertTrue(snap['demo'])
        self.assertNotIn('cacheTurns', snap)
        self.assertIn('cacheAdvice', snap)
        self.assertEqual(snap['budget']['status'], 'off')
        self.assertFalse(snap['budget']['enabled'])
        self.assertEqual(over['status'], 'over')
        self.assertTrue(over['enabled'])
        self.assertGreaterEqual(over['spent'], over['limit'])
        self.assertEqual(len(snap['members']), 6)
        self.assertEqual(len(snap['officialTeam']['tasks']), 5)
        stalled = next(member for member in snap['members'] if member['id'] == 'synthetic-member-4')
        self.assertEqual(stalled['name'], '演示文档')
        self.assertEqual(snap['generatedAt'] - stalled['lastActivityAt'], 900)
        for pet in ('aurelia-engineer', 'lyra-designer', 'nova-strategist', 'rune-analyst', 'mika-operator'):
            self.assertIn(pet, snap['pets'])
            self.assertTrue((SITE / 'sprites' / (pet + '.png')).is_file())
            self.assertEqual(
                (SITE / 'sprites' / (pet + '.png')).read_bytes(),
                (ROOT / 'team-board' / 'sprites' / (pet + '.png')).read_bytes(),
            )
            metadata = json.loads((ROOT / 'team-board' / 'sprites' / (pet + '.json')).read_text(encoding='utf-8'))
            self.assertEqual(snap['petInfo'][pet]['frameRects'], metadata['frameRects'])
            self.assertTrue(snap['petInfo'][pet]['shipped'])
        self.assertIn(builder.BANNER, html)
        self.assertIn(builder.DEMO_REPLACEMENT, html)
        self.assertNotIn(builder.DEMO_FLAG, html)
        self.assertNotIn('<script src=', html)
        self.assertNotIn('</script>', _extract(html, 'pages-boot'))
        boot = (ROOT / 'tools' / 'pages_boot.js').read_text(encoding='utf-8')
        self.assertIn(boot.strip(), html)

    def test_site_fixtures_use_the_examples_privacy_guard(self):
        """Same SECRET regex as tests/test_agent_teams_fixture.py, on the payload only."""
        targets = [
            SITE / 'snapshot.json',
            SITE / 'budget-over.json',
            SITE / 'README.md',
            ROOT / 'tools' / 'pages_boot.js',
            ROOT / 'tools' / 'build_pages_site.py',
        ]
        html = (SITE / 'index.html').read_text(encoding='utf-8')
        blobs = [(path.relative_to(ROOT).as_posix(), path.read_text(encoding='utf-8')) for path in targets]
        blobs.append(('pages-snapshot', _extract(html, 'pages-snapshot')))
        blobs.append(('pages-budget-over', _extract(html, 'pages-budget-over')))
        blobs.append(('pages-boot', _extract(html, 'pages-boot')))
        saw = False
        for label, text in blobs:
            saw = True
            match = SECRET.search(text)
            if match:
                self.fail('%s matched %r' % (label, match.group(0)))
        self.assertTrue(saw)
        self.assertIsNotNone(SECRET.search('sk-board-link'))
        self.assertIsNone(SECRET.search('synthetic-session'))

    def test_readme_and_workflow_document_the_one_time_pages_switch(self):
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        english = (ROOT / 'README.en.md').read_text(encoding='utf-8')
        demo = (ROOT / 'docs' / 'DEMO.md').read_text(encoding='utf-8')
        workflow = (ROOT / '.github' / 'workflows' / 'pages.yml').read_text(encoding='utf-8')
        site_readme = (SITE / 'README.md').read_text(encoding='utf-8')
        for text in (readme, english, demo, site_readme, workflow):
            self.assertIn('https://another-bbzj.github.io/claude-agent-team/', text)
            self.assertIn('GitHub Actions', text)
        self.assertIn('docs/site/index.html', readme)
        self.assertIn('docs/site/index.html', english)
        self.assertIn('actions/upload-pages-artifact@v5', workflow)
        self.assertIn('actions/deploy-pages@v5', workflow)
        self.assertIn('continue-on-error: true', workflow)
        self.assertIn('include-hidden-files: true', workflow)
        self.assertNotIn('pull_request', workflow)
        self.assertNotIn('secrets.', workflow)

    def test_node_boot_serves_the_snapshot_offline(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        completed = subprocess.run(
            [node, str(ROOT / 'tests' / 'check_pages_site.js')],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            check=False,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('"ok":true', completed.stdout)


if __name__ == '__main__':
    unittest.main()
