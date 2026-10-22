#!/usr/bin/env python3
"""Serve the existing board UI with synthetic data and no user-home access."""
import argparse
import copy
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / 'team-board'
FIXTURE = ROOT / 'examples' / 'board-demo.synthetic.json'
SHIPPED_PETS = (
    'huhu-plan', 'dada-code', 'bubu-fix', 'huihui-build', 'pip', 'cubo',
    'drip', 'mush', 'kit', 'spark', 'bolt', 'puff', 'tank',
)
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/index.html': ('index.html', 'text/html; charset=utf-8'),
          '/favicon.png': ('favicon.png', 'image/png'),
          '/favicon.ico': ('favicon.ico', 'image/x-icon')}
STATIC.update({'/sprites/' + pet + '.webp': ('sprites/' + pet + '.webp', 'image/webp')
               for pet in SHIPPED_PETS})
TIMESTAMPS = {'generatedAt', 'startedAt', 'endedAt', 'lastActivityAt', 'lastActivity', 'mtime', 'ts', 'stateSince'}
BANNER = ('<aside id="synthetic-demo-notice" role="status" '
          'style="position:fixed;bottom:0;left:0;right:0;z-index:1000;text-align:center;'
          'padding:7px;background:#153d50;color:#fff;font:14px sans-serif">'
          '只读演示 · 全部为合成数据 · Read-only demo / synthetic data</aside>')


def focus_demo_theater(snapshot, session_id):
    """Point the synthetic theater label at a known session. Member cards stay the one example team."""
    if not session_id or not isinstance(snapshot.get('session'), dict):
        return snapshot
    for theater in snapshot.get('theaters') or []:
        if not isinstance(theater, dict):
            continue
        for item in theater.get('sessions') or []:
            if not isinstance(item, dict) or item.get('id') != session_id:
                continue
            session = snapshot['session']
            session['id'] = item['id']
            if item.get('project'):
                session['project'] = item['project']
            session['branch'] = item.get('branch') or ''
            session['worktree'] = item.get('worktree') or ''
            session['theaterId'] = theater.get('id') or ''
            if 'mtime' in item:
                session['lastActivity'] = item['mtime']
            return snapshot
    return snapshot


def snapshot_at(source, now=None):
    """Refresh display times in memory; never read transcripts or write fixtures."""
    snapshot = copy.deepcopy(source)
    delta = (time.time() if now is None else now) - snapshot['generatedAt']

    def shift(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in TIMESTAMPS and type(item) in (int, float):
                    value[key] = item + delta
                else:
                    shift(item)
        elif isinstance(value, list):
            for item in value:
                shift(item)
    shift(snapshot)
    return snapshot


class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, body, content_type='application/json; charset=utf-8', status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                         "object-src 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        try:
            request = urlsplit(self.path)
        except ValueError:
            return self.respond(b'{"error":"not found"}', status=404)
        path = request.path
        if path in ('/', '/index.html') and 'demo' in parse_qs(request.query, keep_blank_values=True):
            # The normal board's ?demo overlay would duplicate synthetic members.
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path == '/snapshot.json':
            snapshot = snapshot_at(self.server.snapshot)
            session_id = parse_qs(request.query).get('session', [''])[0]
            body = json.dumps(focus_demo_theater(snapshot, session_id), ensure_ascii=False, allow_nan=False).encode('utf-8')
            return self.respond(body)
        item = STATIC.get(path)
        if item is None:
            return self.respond(b'{"error":"read-only demo: route unavailable"}', status=404)
        relative, content_type = item
        try:
            candidate = (BOARD / relative).resolve()
            # Exact mapping denies encoded traversal, listings and custom pets.
            if BOARD.resolve() not in candidate.parents or not candidate.is_file():
                return self.respond(b'{"error":"not found"}', status=404)
            body = candidate.read_bytes()
            if relative == 'index.html':
                body = body.replace(b'<body>', ('<body>' + BANNER).encode('utf-8'), 1)
        except (OSError, RuntimeError):
            return self.respond(b'{"error":"demo asset unavailable"}', status=404)
        self.respond(body, content_type)

    do_HEAD = do_GET

    def read_only(self):
        self.close_connection = True
        self.respond(b'{"error":"read-only demo: changes are disabled"}', status=403)

    do_POST = read_only
    do_PUT = read_only
    do_PATCH = read_only
    do_DELETE = read_only


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=7790):
        self.snapshot = json.loads(FIXTURE.read_text(encoding='utf-8'))
        super().__init__(('127.0.0.1', port), DemoHandler)

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]


def _port(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('port must be an integer from 0 to 65535')
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError('port must be an integer from 0 to 65535')
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=_port, default=7790, help='loopback port; 0 chooses an available port')
    args = parser.parse_args(argv)
    try:
        with DemoServer(args.port) as server:
            print('Read-only synthetic demo: http://127.0.0.1:{}/ (Ctrl+C to stop)'.format(server.server_port), flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError):
        print('Cannot start demo; check the port and repository assets.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
