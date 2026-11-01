"""Browser-independent checks for provider selection and visible dialogue."""
import os
import shutil
import subprocess
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import headless_chrome

ROOT = Path(__file__).resolve().parents[1]


class CodexUiTest(unittest.TestCase):
    def test_source_dialogue_and_upload_boundaries(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        result = subprocess.run(
            [node, str(ROOT / 'tests' / 'check_codex_ui.js')],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', check=False, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_browser_sources_and_appearance_viewports(self):
        node = shutil.which('node')
        binary = headless_chrome.chrome_binary()
        if not binary:
            binary = next((str(path) for path in (
                Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),
                Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
            ) if path.is_file()), '')
        if not node or not binary:
            self.skipTest('node or a headless browser is not installed')

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        handler = partial(QuietHandler, directory=str(ROOT / 'team-board'))
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with headless_chrome.launch_headless_chrome(binary) as browser:
                result = subprocess.run(
                    [node, str(ROOT / 'tests/check_codex_ui.js'),
                     'http://127.0.0.1:{}/'.format(server.server_port), str(browser.port),
                     os.environ.get('TEAM_BOARD_UI_SHOTS', '')],
                    cwd=str(ROOT), capture_output=True, text=True,
                    encoding='utf-8', errors='replace', check=False, timeout=45,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('"browser":"passed"', result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
