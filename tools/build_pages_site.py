#!/usr/bin/env python3
"""Write the static GitHub Pages demo under docs/site/.

The page is the board UI plus one frozen synthetic snapshot. It does not start
a server, read a home directory, or import team_board. Open docs/site/index.html
in a browser. ``python tools/build_pages_site.py --check`` exits 1 when the
committed files drift from this script.
"""
import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / 'docs' / 'site'
BOOT = Path(__file__).resolve().parent / 'pages_boot.js'
DEMO_FLAG = "const DEMO = new URLSearchParams(location.search).has('demo');"
DEMO_REPLACEMENT = 'const DEMO = false; /* static pages: ?demo=1 would duplicate the synthetic cast */'
BANNER = (
    '<aside id="synthetic-demo-notice" role="status" '
    'style="position:fixed;bottom:0;left:0;right:0;z-index:1000;text-align:center;'
    'padding:7px;background:#153d50;color:#fff;font:14px sans-serif">'
    '只读静态演示 · 全部为合成数据 · Static synthetic demo</aside>'
)
GENERATED = (
    'index.html',
    'snapshot.json',
    'budget-over.json',
    'favicon.png',
    'favicon.ico',
    'sprites/aurelia-engineer.png',
    'sprites/lyra-designer.png',
    '.nojekyll',
)
HAND_WRITTEN = ('README.md',)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _demo():
    return _load('pages_build_demo', ROOT / 'tools' / 'demo_board.py')


def _budget(demo):
    return demo._budget_module()


def prepare_snapshots(demo=None, budget=None):
    """Frozen snapshot at the fixture clock, plus the opt-in over-budget block."""
    demo = demo or _demo()
    budget = budget or _budget(demo)
    source = demo.load_fixture_snapshot()
    base = demo.snapshot_at(source, source['generatedAt'])
    budget.attach_budget(base, None, None)
    over = demo.snapshot_at(source, source['generatedAt'])
    budget.attach_budget(over, None, budget.over_budget_demo(over))
    return base, over['budget']


def _embed(value):
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return text.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def _pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


def _write(path, text):
    path.write_bytes(text.encode('utf-8'))


def render_index(board_html, snapshot, budget_over, boot_js):
    """Inject the banner, the snapshot, and the offline fetch shim."""
    if board_html.count('<body>') != 1:
        raise SystemExit('expected one <body> in team-board/index.html')
    if board_html.count(DEMO_FLAG) != 1:
        raise SystemExit('expected the DEMO query flag once in team-board/index.html')
    html = board_html.replace(DEMO_FLAG, DEMO_REPLACEMENT, 1)
    block = (
        '\n' + BANNER + '\n'
        '<script id="pages-snapshot" type="application/json">\n' + _embed(snapshot) + '\n</script>\n'
        '<script id="pages-budget-over" type="application/json">\n' + _embed(budget_over) + '\n</script>\n'
        '<script id="pages-boot">\n' + boot_js.strip() + '\n</script>\n'
    )
    return html.replace('<body>', '<body>' + block, 1)


def build_site(dest, demo=None):
    """Write generated files into dest. Does not remove hand-written files."""
    demo = demo or _demo()
    dest = Path(dest)
    (dest / 'sprites').mkdir(parents=True, exist_ok=True)
    base, budget_over = prepare_snapshots(demo)
    board_html = (demo.BOARD / 'index.html').read_text(encoding='utf-8')
    boot_js = BOOT.read_text(encoding='utf-8')
    _write(dest / 'index.html', render_index(board_html, base, budget_over, boot_js))
    _write(dest / 'snapshot.json', _pretty(base))
    _write(dest / 'budget-over.json', _pretty(budget_over))
    for name in ('favicon.png', 'favicon.ico'):
        shutil.copyfile(str(demo.BOARD / name), str(dest / name))
    for pet in demo.ORIGINAL_PETS:
        source = demo.BOARD / 'sprites' / (pet + '.png')
        if not source.is_file():
            raise SystemExit('missing shipped sprite: ' + pet)
        shutil.copyfile(str(source), str(dest / 'sprites' / (pet + '.png')))
    (dest / '.nojekyll').write_bytes(b'')
    return base, budget_over


def _rel_files(root):
    files = []
    for path in Path(root).rglob('*'):
        if path.is_file():
            files.append(path.relative_to(root).as_posix())
    return sorted(files)


def check_site(committed=None):
    """Return a list of problems. Empty means the committed site matches."""
    committed = Path(committed or SITE)
    problems = []
    if not committed.is_dir():
        return ['missing ' + str(committed)]
    parent = Path(tempfile.mkdtemp(prefix='pages-site-'))
    try:
        fresh = parent / 'site'
        build_site(fresh)
        for rel in GENERATED:
            got = fresh / rel
            have = committed / rel
            if not have.is_file():
                problems.append('missing ' + rel)
                continue
            if got.read_bytes() != have.read_bytes():
                problems.append('drift ' + rel)
        expected = set(GENERATED) | set(HAND_WRITTEN)
        present = set(_rel_files(committed))
        for rel in sorted(present - expected):
            problems.append('unexpected ' + rel)
        for rel in sorted(expected - present):
            problems.append('missing ' + rel)
    finally:
        shutil.rmtree(str(parent))
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='exit 1 when docs/site drifts from this builder')
    parser.add_argument('--dest', default='', help='write somewhere other than docs/site (ignored with --check)')
    args = parser.parse_args(argv)
    if args.check:
        problems = check_site()
        if problems:
            print('docs/site is out of date:\n' + '\n'.join(problems), file=sys.stderr)
            print('Run: python tools/build_pages_site.py', file=sys.stderr)
            return 1
        print('docs/site matches the synthetic pages builder')
        return 0
    dest = Path(args.dest) if args.dest else SITE
    build_site(dest)
    print('Wrote synthetic static demo: ' + str(dest))
    return 0


if __name__ == '__main__':
    sys.exit(main())
