"""Dependency arcs on the read-only official task board.

Geometry and edge selection run through the functions embedded in index.html.
A headless Chrome pass, when the browser is installed, checks hover, focus,
click, Escape, pin, themes and a 390px column stack.
"""
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'team-board' / 'index.html'
BROWSER = Path(__file__).resolve().parent / 'check_task_deps_browser.js'
FUNCTIONS = (
    'relationFromIds',
    'noticesFromWarnings',
    'taskDetailView',
    'depEdgeList',
    'depNum',
    'depArcPath',
)


def script_text():
    html = HTML.read_text(encoding='utf-8')
    start = html.index('<script>') + len('<script>')
    end = html.rindex('</script>')
    return html[start:end]


def extract_function(source, name):
    token = 'function %s(' % name
    start = source.index(token)
    opening = source.index('{', start)
    depth = 0
    quote = ''
    escape = False
    for index in range(opening, len(source)):
        char = source[index]
        if quote:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == quote:
                quote = ''
            continue
        if char in ('"', "'", '`'):
            quote = char
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError('unclosed function ' + name)


def run_node(code):
    binary = shutil.which('node')
    if not binary:
        raise unittest.SkipTest('node is not installed')
    with tempfile.TemporaryDirectory(prefix='task-deps-') as directory:
        program = Path(directory) / 'dep-check.js'
        program.write_text(code, encoding='utf-8')
        completed = subprocess.run(
            [binary, str(program)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


class DepEdgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = '\n'.join(extract_function(script_text(), name) for name in FUNCTIONS)
        cls.prelude = source

    def run_case(self, body):
        return run_node(self.prelude + '\nprocess.stdout.write(JSON.stringify(' + body + '));\n')

    def test_visible_edges_keep_unfinished_dependencies_emphatic(self):
        tasks = [
            {
                'id': '1', 'subject': 'Survey', 'normalizedStatus': 'in_progress',
                'blockedByDetail': [], 'blocksDetail': [
                    {'id': '3', 'subject': 'Wait', 'resolved': True, 'normalizedStatus': 'pending'},
                ],
                'flags': [], 'notices': [],
            },
            {
                'id': '2', 'subject': 'Draft', 'normalizedStatus': 'completed',
                'blockedByDetail': [], 'blocksDetail': [
                    {'id': '4', 'subject': 'Ask', 'resolved': True, 'normalizedStatus': 'pending'},
                ],
                'flags': [], 'notices': [],
            },
            {
                'id': '3', 'subject': 'Wait', 'normalizedStatus': 'pending',
                'blockedByDetail': [
                    {'id': '1', 'subject': 'Survey', 'resolved': True, 'normalizedStatus': 'in_progress'},
                    {'id': '9', 'subject': None, 'resolved': False, 'normalizedStatus': None},
                ],
                'blocksDetail': [], 'flags': ['blocked'], 'notices': [],
            },
            {
                'id': '4', 'subject': 'Ask', 'normalizedStatus': 'pending',
                'blockedByDetail': [
                    {'id': '2', 'subject': 'Draft', 'resolved': True, 'normalizedStatus': 'completed'},
                    {'id': '8', 'subject': 'Hidden', 'resolved': True, 'normalizedStatus': 'pending'},
                ],
                'blocksDetail': [], 'flags': [], 'notices': [],
            },
        ]
        visible = ['1', '2', '3', '4']
        report = self.run_case(
            '({waiting: depEdgeList(%s, %s, %s), done: depEdgeList(%s, %s, %s),'
            ' blocker: depEdgeList(%s, %s, %s), satisfied: depEdgeList(%s, %s, %s)})'
            % (
                json.dumps(tasks[2]), json.dumps(tasks), json.dumps(visible),
                json.dumps(tasks[3]), json.dumps(tasks), json.dumps(visible),
                json.dumps(tasks[0]), json.dumps(tasks), json.dumps(visible),
                json.dumps(tasks[1]), json.dumps(tasks), json.dumps(visible),
            )
        )
        self.assertEqual(report['waiting'], [{
            'id': '1', 'key': 'id:1', 'roles': ['blockedBy'], 'open': True,
            'subject': 'Survey', 'normalizedStatus': 'in_progress',
        }])
        self.assertEqual(report['done'], [{
            'id': '2', 'key': 'id:2', 'roles': ['blockedBy'], 'open': False,
            'subject': 'Draft', 'normalizedStatus': 'completed',
        }])
        self.assertEqual(report['blocker'][0]['id'], '3')
        self.assertTrue(report['blocker'][0]['open'])
        self.assertEqual(report['blocker'][0]['roles'], ['blocks'])
        self.assertFalse(report['satisfied'][0]['open'])
        self.assertEqual(report['satisfied'][0]['id'], '4')

    def test_fallback_ids_skip_missing_self_and_offboard_rows(self):
        tasks = [
            {'id': '1', 'subject': 'Survey', 'normalizedStatus': 'in_progress', 'blockedBy': [], 'blocks': ['1', '3']},
            {'id': '3', 'subject': 'Wait', 'normalizedStatus': 'pending', 'blockedBy': ['1', '9'], 'blocks': []},
        ]
        report = self.run_case(
            '({held: depEdgeList(%s, %s, ["1", "3", "9"]), self: depEdgeList(%s, %s, ["1"])})'
            % (json.dumps(tasks[1]), json.dumps(tasks), json.dumps(tasks[0]), json.dumps(tasks))
        )
        self.assertEqual([edge['id'] for edge in report['held']], ['1'])
        self.assertTrue(report['held'][0]['open'])
        self.assertEqual(report['held'][0]['subject'], 'Survey')
        self.assertEqual(report['self'], [])

    def test_arc_path_uses_the_gap_the_gutter_or_the_top_rail(self):
        report = self.run_case(
            '({right: depArcPath(%s, %s), left: depArcPath(%s, %s), stack: depArcPath(%s, %s), rail: depArcPath(%s, %s)})'
            % (
                json.dumps({'left': 0, 'top': 40, 'right': 100, 'bottom': 100}),
                json.dumps({'left': 130, 'top': 40, 'right': 230, 'bottom': 100}),
                json.dumps({'left': 130, 'top': 40, 'right': 230, 'bottom': 100}),
                json.dumps({'left': 0, 'top': 40, 'right': 100, 'bottom': 100}),
                json.dumps({'left': 20, 'top': 40, 'right': 200, 'bottom': 90}),
                json.dumps({'left': 20, 'top': 110, 'right': 200, 'bottom': 160}),
                json.dumps({'left': 0, 'top': 80, 'right': 100, 'bottom': 140}),
                json.dumps({'left': 400, 'top': 80, 'right': 500, 'bottom': 140}),
            )
        )
        self.assertEqual(report['right'], {
            'd': 'M 100.0 70.0 C 118.0 70.0, 112.0 70.0, 130.0 70.0',
            'x': 130, 'y': 70, 'c2x': 112, 'c2y': 70, 'side': 'right',
        })
        self.assertEqual(report['left']['side'], 'left')
        self.assertEqual(report['left']['x'], 100)
        self.assertEqual(report['stack'], {
            'd': 'M 32.0 90.0 L 14.0 90.0 L 14.0 110.0 L 32.0 110.0',
            'x': 32, 'y': 110, 'c2x': 14, 'c2y': 110, 'side': 'stack',
        })
        self.assertEqual(report['rail']['side'], 'rail')
        self.assertIn(' L 108.0 5.0 L 392.0 5.0 ', report['rail']['d'])
        self.assertLess(report['stack']['c2x'], 20)


def chrome_binary():
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
        found = shutil.which(name)
        if found:
            return found
    return ''


def free_port():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class DepBrowserTest(unittest.TestCase):
    def test_headless_board_draws_arcs_without_breaking_pin_or_escape(self):
        if not shutil.which('node'):
            self.skipTest('node is not installed')
        binary = chrome_binary()
        if not binary:
            self.skipTest('chrome is not installed')
        import test_demo_board
        server = test_demo_board.demo.DemoServer(0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='task-deps-chrome-') as directory:
                shots = Path(directory) / 'shots'
                shots.mkdir()
                completed = subprocess.run(
                    [
                        shutil.which('node'), str(BROWSER),
                        'http://127.0.0.1:%d/' % server.server_port,
                        binary, str(free_port()), directory, str(shots),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if completed.returncode != 0:
                    self.fail(completed.stderr or completed.stdout)
                report = json.loads(completed.stdout)
                self._keep_shots(shots)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        wide = report['wide']
        self.assertEqual(wide['idle'], 0)
        self.assertTrue(wide['alt']['pinned'])
        self.assertFalse(wide['alt']['detail']['open'])
        self.assertEqual(wide['openEdge']['for'], 'id:3')
        self.assertEqual(wide['openEdge']['arcs'], [{
            'target': 'id:1', 'open': '1', 'side': 'right',
            'label': wide['openEdge']['arcs'][0]['label'],
        }])
        self.assertIn('尚未完成', wide['openEdge']['arcs'][0]['label'])
        self.assertNotIn('id:9', json.dumps(wide['openEdge']))
        self.assertTrue(wide['openHit']['centerInCard'])
        self.assertTrue(wide['openHit']['endInArc'])
        self.assertEqual(wide['afterArc']['detail']['title'], 'Survey the synthetic module')
        self.assertTrue(wide['afterArc']['detail']['open'])
        self.assertTrue(wide['afterArc']['pinned'])
        self.assertEqual(wide['afterArc']['for'], 'id:1')
        self.assertFalse(wide['afterEsc']['detail']['open'])
        self.assertTrue(wide['afterEsc']['pinned'])
        self.assertFalse(wide['afterUnpin']['pinned'])
        self.assertFalse(wide['afterUnpin']['detail']['open'])
        self.assertEqual(wide['cleared'], 0)
        self.assertEqual(wide['doneEdge']['for'], 'id:4')
        self.assertEqual(wide['doneEdge']['arcs'][0]['target'], 'id:2')
        self.assertEqual(wide['doneEdge']['arcs'][0]['open'], '0')
        self.assertEqual(wide['doneEdge']['arcs'][0]['side'], 'rail')
        self.assertIn('已经完成', wide['doneEdge']['arcs'][0]['label'])
        self.assertEqual(wide['afterKey']['detail']['title'], 'Draft synthetic notes')
        self.assertFalse(wide['afterKey']['pinned'])
        self.assertEqual(wide['doneFromBlocker'][0]['target'], 'id:4')
        self.assertEqual(wide['doneFromBlocker'][0]['open'], '0')
        self.assertEqual(wide['openFromBlocker'][0]['target'], 'id:3')
        self.assertEqual(wide['openFromBlocker'][0]['open'], '1')
        self.assertNotEqual(wide['light'], wide['dark'])
        self.assertNotIn('none', wide['doneStroke'].split()[-1:])
        self.assertNotEqual(wide['light'].split()[0], wide['doneStroke'].split()[0])
        narrow = report['narrow']
        self.assertEqual(narrow['side'], 'stack')
        self.assertEqual(narrow['target'], 'id:1')
        self.assertEqual(narrow['open'], '1')
        self.assertEqual(narrow['count'], 1)
        self.assertGreaterEqual(narrow['box']['x'], -1)
        self.assertLessEqual(narrow['box']['x'] + narrow['box']['w'], narrow['svgWidth'] + 12)
        self.assertGreater(narrow['box']['h'], 8)
        self.assertTrue(narrow['centerInCard'])
        self.assertTrue(narrow['endInArc'])
        self.assertNotEqual(narrow['light'], report['narrowDark'])

    def _keep_shots(self, shots):
        destination = Path('/opt/cursor/artifacts/task-deps')
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        for shot in shots.glob('*.png'):
            target = destination / shot.name
            target.write_bytes(shot.read_bytes())


if __name__ == '__main__':
    unittest.main()
