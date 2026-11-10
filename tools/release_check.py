#!/usr/bin/env python3
"""发版前核对版本、变更记录、README 脚本、静态站和测试发现。

不修改 VERSION，不打 tag，也不创建 GitHub Release。任一检查失败时退出码为 1。

    python tools/release_check.py
    python tools/release_check.py --json
"""
import argparse
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LABELS = {
    'version': '版本号',
    'unreleased': '未发布',
    'readme_commands': 'README 脚本',
    'pages_site': '静态站',
    'tests_discoverable': '测试发现',
}
README_NAMES = ('README.md', 'README.en.md')
_VERSION_TEXT = re.compile(r'\d+(?:\.\d+){1,3}')
_H2 = re.compile(r'^##\s+(.+?)\s*$')
_VERSION_TITLE = re.compile(r'^(\d+(?:\.\d+){1,3})\b')
_INTERPRETER = re.compile(r'(?<![\w./])python3?(?:\.\d+)?(?![\w.])')
_HTML_COMMENT = re.compile(r'<!--.*?-->', re.S)
_DETAILS_TAG = re.compile(r'</?(?:details|summary)\b[^>]*>', re.I)
_RULE = re.compile(r'[-*_]{3,}')
_DISCOVER = """\
import contextlib
import io
import json
import sys
import unittest
buf = io.StringIO()
loader = unittest.TestLoader()
try:
    with contextlib.redirect_stdout(buf):
        suite = loader.discover('tests', pattern='test*.py')
except Exception as exc:
    payload = {'count': 0, 'errors': ['discovery failed: ' + str(exc)]}
else:
    payload = {'count': suite.countTestCases(), 'errors': list(loader.errors)}
json.dump(payload, sys.stdout)
"""


class Check(object):
    def __init__(self, name, ok, detail):
        self.name = name
        self.ok = bool(ok)
        self.detail = ' '.join(str(detail).split())


def _lines(text):
    return text.replace('\r\n', '\n').replace('\r', '\n').split('\n')


def _read(path):
    return path.read_text(encoding='utf-8', errors='replace').lstrip('\ufeff')


def _short(text, limit=180):
    text = ' '.join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:limit - 3] + '...'


def read_version(root):
    """Return the VERSION text, '' when the file is missing, or None when it is not a version."""
    path = Path(root) / 'VERSION'
    if not path.is_file():
        return ''
    text = _read(path).strip()
    if _VERSION_TEXT.fullmatch(text):
        return text
    return None


def latest_changelog_version(text):
    """First version heading in file order. This changelog keeps the newest release above older ones."""
    for line in _lines(text):
        heading = _H2.match(line.strip())
        if not heading:
            continue
        title = heading.group(1).strip()
        if title == '未发布' or title.lower() == 'unreleased':
            continue
        match = _VERSION_TITLE.match(title)
        if match:
            return match.group(1)
    return None


def unreleased_body(text):
    """Text between the 未发布 heading and the next level-2 heading. None when the heading is absent."""
    lines = _lines(text)
    start = None
    for index, line in enumerate(lines):
        heading = _H2.match(line.strip())
        if heading and heading.group(1).strip() == '未发布':
            start = index + 1
            break
    if start is None:
        return None
    body = []
    for line in lines[start:]:
        if _H2.match(line.strip()):
            break
        body.append(line)
    return '\n'.join(body)


def unreleased_content_lines(body):
    """Lines a reader would call release notes. Headings, blanks and empty disclosure tags do not count."""
    body = _HTML_COMMENT.sub('', body)
    kept = []
    for line in _lines(body):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        text = _DETAILS_TAG.sub('', stripped).strip()
        if not text or _RULE.fullmatch(text):
            continue
        kept.append(text)
    return kept


def check_version(root):
    version = read_version(root)
    changelog = Path(root) / 'CHANGELOG.md'
    if version == '':
        return Check('version', False, '没有 VERSION 文件')
    if version is None:
        return Check('version', False, 'VERSION 不是 x.y 或 x.y.z 这样的版本号')
    if not changelog.is_file():
        return Check('version', False, '没有 CHANGELOG.md')
    latest = latest_changelog_version(_read(changelog))
    if not latest:
        return Check('version', False, 'CHANGELOG 没有版本段（## x.y.z）')
    if version != latest:
        return Check('version', False, 'VERSION %s 与 CHANGELOG 最新版本段 %s 不一致' % (version, latest))
    return Check('version', True, 'VERSION %s 与 CHANGELOG 最新版本段一致' % version)


def check_unreleased(root):
    changelog = Path(root) / 'CHANGELOG.md'
    if not changelog.is_file():
        return Check('unreleased', False, '没有 CHANGELOG.md')
    body = unreleased_body(_read(changelog))
    if body is None:
        return Check('unreleased', False, 'CHANGELOG 没有「未发布」段')
    lines = unreleased_content_lines(body)
    if not lines:
        return Check('unreleased', False, '「未发布」没有正文')
    return Check('unreleased', True, '「未发布」有 %s 行正文' % len(lines))


def _normalize_token(token):
    token = token.strip().strip('\'"').replace('\\', '/')
    while token.startswith('./'):
        token = token[2:]
    return token


def _is_script_path(token):
    if not token.endswith('.py') or '://' in token or token.startswith(('~', '/')):
        return False
    if re.match(r'^[A-Za-z]:', token):
        return False
    if any(char in token for char in '*?$<>|`'):
        return False
    parts = token.split('/')
    if not parts or any(part in ('', '.', '..') for part in parts):
        return False
    return all(re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in parts)


def _python_scripts(text):
    found = []
    for match in _INTERPRETER.finditer(text):
        rest = text[match.end():].split('\n', 1)[0]
        for token in rest.split():
            token = token.strip('\'"')
            if token in ('-m', '-c'):
                break
            if token.startswith('-'):
                continue
            if token.endswith('.py'):
                found.append(token)
            break
    return found


def script_tokens(text):
    """Repo-relative scripts cited by a python command or a backtick path. Home paths and globs are ignored."""
    text = _HTML_COMMENT.sub('', text)
    found = []
    seen = set()

    def add(token):
        token = _normalize_token(token)
        if token in seen or not _is_script_path(token):
            return
        seen.add(token)
        found.append(token)

    for span in re.findall(r'`([^`\n]*)`', text):
        span = span.strip()
        pieces = span.split()
        first = pieces[0] if pieces else ''
        if _is_script_path(_normalize_token(first)):
            add(first)
        for token in _python_scripts(span):
            add(token)
    for line in _lines(text):
        for token in _python_scripts(line):
            add(token)
    return found


def readme_citations(root):
    """(readme name, token) pairs. README.md is required; README.en.md is included when present."""
    root = Path(root)
    if not (root / 'README.md').is_file():
        return None
    citations = []
    for name in README_NAMES:
        path = root / name
        if not path.is_file():
            continue
        for token in script_tokens(_read(path)):
            citations.append((name, token))
    return citations


def script_exists(root, token):
    root = Path(root)
    if '/' in token:
        return (root / token).is_file()
    return any((base / token).is_file() for base in (root, root / 'tools', root / 'team-board'))


def check_readme(root):
    citations = readme_citations(root)
    if citations is None:
        return Check('readme_commands', False, '没有 README.md')
    if not any(name == 'README.md' for name, _token in citations):
        return Check('readme_commands', False, 'README.md 没有引用仓库内的脚本')
    missing = {}
    for name, token in citations:
        if not script_exists(root, token):
            missing.setdefault(token, [])
            if name not in missing[token]:
                missing[token].append(name)
    if missing:
        parts = []
        for token in sorted(missing):
            parts.append('%s（%s）' % (token, '、'.join(missing[token])))
        shown = parts[:4]
        if len(parts) > 4:
            shown.append('等 %s 个' % len(parts))
        return Check('readme_commands', False, '缺少 ' + '、'.join(shown))
    unique = sorted({token for _name, token in citations})
    return Check('readme_commands', True, '%s 个脚本都在' % len(unique))


def check_pages(root):
    script = Path(root) / 'tools' / 'build_pages_site.py'
    if not script.is_file():
        return Check('pages_site', True, '跳过：没有 tools/build_pages_site.py')
    source = _read(script)
    if '--check' not in source:
        return Check('pages_site', True, '跳过：build_pages_site.py 没有 --check')
    try:
        proc = subprocess.run(
            [sys.executable, str(script), '--check'],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Check('pages_site', False, '无法运行 build_pages_site.py --check')
    if proc.returncode != 0:
        message = _short(proc.stderr or proc.stdout or '无输出')
        return Check('pages_site', False, 'build_pages_site.py --check 退出码 %s：%s' % (proc.returncode, message))
    last = ''
    for line in (proc.stdout or '').splitlines():
        if line.strip():
            last = line.strip()
    if last:
        return Check('pages_site', True, '已运行 build_pages_site.py --check：%s' % last)
    return Check('pages_site', True, '已运行 build_pages_site.py --check')


def _json_object(text):
    for line in reversed(str(text or '').splitlines()):
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _error_summary(message):
    lines = [line.strip() for line in str(message).splitlines() if line.strip()]
    for line in lines:
        if line.startswith(('ImportError', 'ModuleNotFoundError', 'SyntaxError', 'discovery failed')):
            return _short(line)
    if lines:
        return _short(lines[-1])
    return '导入失败'


def check_tests(root):
    if not (Path(root) / 'tests').is_dir():
        return Check('tests_discoverable', False, '没有 tests/ 目录')
    try:
        proc = subprocess.run(
            [sys.executable, '-c', _DISCOVER],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Check('tests_discoverable', False, '无法发现测试')
    payload = _json_object(proc.stdout)
    if not isinstance(payload, dict):
        return Check('tests_discoverable', False, '无法发现测试')
    errors = payload.get('errors') or []
    if not isinstance(errors, list):
        errors = [str(errors)]
    try:
        count = int(payload.get('count') or 0)
    except (TypeError, ValueError):
        count = 0
    if errors:
        extra = '（另有 %s 个）' % (len(errors) - 1) if len(errors) > 1 else ''
        return Check('tests_discoverable', False, '无法加载测试：%s%s' % (_error_summary(errors[0]), extra))
    if count <= 0:
        return Check('tests_discoverable', False, 'tests/ 里没有可发现的测试')
    return Check('tests_discoverable', True, '发现 %s 个测试' % count)


def run_checks(root):
    root = Path(root)
    return [
        check_version(root),
        check_unreleased(root),
        check_readme(root),
        check_pages(root),
        check_tests(root),
    ]


def _display_width(text):
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
    return width


def _pad(text, width):
    gap = width - _display_width(text)
    if gap <= 0:
        return text
    return text + (' ' * gap)


def format_report(checks):
    rows = []
    for item in checks:
        rows.append((LABELS.get(item.name, item.name), '通过' if item.ok else '失败', item.detail))
    header = ('检查', '结果', '说明')
    widths = [max(_display_width(cell) for cell in column) for column in zip(header, *rows)]
    lines = ['  '.join(_pad(cell, width) for cell, width in zip(header, widths))]
    for row in rows:
        lines.append('  '.join(_pad(cell, width) for cell, width in zip(row, widths)))
    passed = sum(1 for item in checks if item.ok)
    failed = len(checks) - passed
    lines.append('')
    lines.append('%s 项通过，%s 项失败' % (passed, failed))
    return '\n'.join(lines)


def result_payload(checks):
    return {
        'ok': all(item.ok for item in checks),
        'checks': [{'name': item.name, 'ok': item.ok, 'detail': item.detail} for item in checks],
    }


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(description='核对发版证据。不打 tag，也不发布。')
    parser.add_argument('--json', action='store_true', help='把同一结果写成一个 JSON 对象')
    args = parser.parse_args(argv)
    checks = run_checks(ROOT if root is None else root)
    payload = result_payload(checks)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_report(checks))
    return 0 if payload['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
