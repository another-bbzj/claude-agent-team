"""把官方任务和 `.team` 工单对上本地 git 的改动量。

只跑只读的 git（rev-parse、log、merge-base、diff），关掉分页、可选锁、钩子目录、
外部 diff 和 fsmonitor。不写索引，也不改分支。git 不在、目录不是仓库、命令失败或超时时，
对应条目的 `diff.status` 是 `unavailable`，看板照常刷新，不把缺失写成 0。

任务（按这个顺序，命中一种就不再用下一种）：

1. 时间窗。状态机和估算成本一样，只认 TaskUpdate / TaskCreate 里的状态和时间，
   不按 owner 把区间截短：改动属于这条任务，不属于某一个人。
   提交的提交者时间落在「进行中」区间里才计入。区间仍开着时，再加上相对 HEAD 的已跟踪未提交改动。
   同一提交落在多条任务上时，这几条都不计入这次提交，并标成部分。
   没有这类调用、当前是进行中、并且这条任务独占一个文件时，用文件修改时间到现在，标成部分。
2. 没有时间窗、且任务正在进行时，若当前检出是链接的 worktree，或分支不是默认分支，
   用默认分支与工作区的差异（已跟踪，含未提交）。多条任务共用这一份，不拆开，标成部分。
   人就在默认分支上、也没有未提交改动时，不把整支历史算进来。

工单读会话仓库根上的 `.team/tickets/*.md`，只取「## Owned files」里的相对路径。
统计这些路径相对默认分支的已跟踪差异（含未提交）。路径越界的条目丢掉。
两条工单写了同一个路径时，两边都保留行数，并标成部分。

未跟踪的新文件没有行数，不计入。输出里没有路径、提交说明或本机目录。
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path


_STATUSES = ('pending', 'in_progress', 'completed', 'deleted')
_BASIS = ('window', 'branch', 'worktree', 'ticket', 'none')
_TTL = 2.0
_MAX_OUTPUT = 2000000
_MAX_COMMITS = 400
_MAX_NUMSTAT = 5000
_MAX_TICKETS = 40
_MAX_PATHS = 40
_TICKET_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.md$')
_STATUS_LINE = re.compile(r'(?m)^status:\s*([A-Za-z0-9_-]{1,40})\s*$')
_LOCK = threading.Lock()
_CACHE = {}
_HOOKS = ''


class GitError(Exception):
    pass


def clear_cache():
    """丢掉只读 git 的短缓存。测试在同一秒里改完仓库再读时用。"""
    with _LOCK:
        _CACHE.clear()


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value == float('inf') or value == float('-inf'):
        return None
    return float(value)


def _unavailable():
    return {
        'files': None,
        'insertions': None,
        'deletions': None,
        'status': 'unavailable',
        'partial': False,
        'basis': 'none',
    }


def _stat(files, insertions, deletions, basis, partial):
    return {
        'files': int(files),
        'insertions': int(insertions),
        'deletions': int(deletions),
        'status': 'partial' if partial else 'known',
        'partial': True if partial else False,
        'basis': basis if basis in _BASIS else 'none',
    }


def _hooks_dir():
    global _HOOKS
    if _HOOKS and os.path.isdir(_HOOKS):
        return _HOOKS
    _HOOKS = tempfile.mkdtemp(prefix='team-board-git-hooks-')
    return _HOOKS


def _git_binary():
    return shutil.which('git') or ''


def run_git(cwd, args, timeout=4):
    """只读 git。失败抛 GitError，不把 stderr 带出去。"""
    git = _git_binary()
    if not git:
        raise GitError('missing')
    cmd = [
        git, '--no-pager', '--no-optional-locks',
        '-c', 'core.hooksPath=' + _hooks_dir(),
        '-c', 'core.fsmonitor=',
        '-C', cwd,
    ]
    cmd.extend(args)
    env = os.environ.copy()
    env['GIT_OPTIONAL_LOCKS'] = '0'
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_PAGER'] = ''
    env['GIT_CONFIG_NOSYSTEM'] = '1'
    env['GIT_CONFIG_GLOBAL'] = os.devnull
    for key in (
        'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
        'GIT_NAMESPACE', 'GIT_PREFIX', 'GIT_EXTERNAL_DIFF',
        'GIT_ALTERNATE_OBJECT_DIRECTORIES',
    ):
        env.pop(key, None)
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GitError('failed')
    if proc.returncode != 0:
        raise GitError('failed')
    return proc.stdout.decode('utf-8', 'replace')


def _repo_token(cwd):
    try:
        base = os.path.normcase(os.path.abspath(cwd))
    except (OSError, ValueError):
        base = str(cwd)
    return base + '|' + _head_token(cwd) + '|' + _index_token(cwd)


def _git_dir_from(cwd):
    git_path = os.path.join(cwd, '.git')
    if os.path.isfile(git_path):
        try:
            with open(git_path, 'r', encoding='utf-8', errors='replace') as handle:
                line = handle.readline().strip()
        except OSError:
            return ''
        if not line.startswith('gitdir:'):
            return ''
        raw = line.split(':', 1)[1].strip()
        if not raw or '\n' in raw or '..' in raw.replace('\\', '/').split('/'):
            return ''
        if not os.path.isabs(raw):
            raw = os.path.join(os.path.dirname(git_path), raw)
        return raw
    if os.path.isdir(git_path):
        return git_path
    return ''


def _ref_file(git_dir, ref_name):
    direct = os.path.join(git_dir, ref_name)
    if os.path.isfile(direct):
        return direct
    common_path = os.path.join(git_dir, 'commondir')
    try:
        with open(common_path, 'r', encoding='utf-8', errors='replace') as handle:
            common = handle.read(500).strip()
    except OSError:
        return ''
    if not common or '\n' in common or '\x00' in common:
        return ''
    if not os.path.isabs(common):
        common = os.path.join(git_dir, common)
    candidate = os.path.normpath(os.path.join(common, ref_name))
    if os.path.isfile(candidate):
        return candidate
    return ''


def _head_token(cwd):
    git_dir = _git_dir_from(cwd)
    if not git_dir:
        return ''
    try:
        with open(os.path.join(git_dir, 'HEAD'), 'r', encoding='utf-8', errors='replace') as handle:
            ref = handle.read(300).strip()
    except OSError:
        return ''
    if ref.startswith('ref:'):
        name = ref.split(':', 1)[1].strip()
        path = _ref_file(git_dir, name)
        try:
            st = os.stat(path) if path else None
        except OSError:
            st = None
        if st is not None:
            return '%s:%s:%s' % (name, st.st_mtime_ns, st.st_size)
    return ref[:200]


def _index_token(cwd):
    git_dir = _git_dir_from(cwd)
    if not git_dir:
        return '0'
    try:
        st = os.stat(os.path.join(git_dir, 'index'))
    except OSError:
        return '0'
    return '%s:%s' % (st.st_mtime_ns, st.st_size)


def _cached_call(key, compute):
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _TTL:
            kind, payload = hit[1]
            if kind == 'err':
                raise GitError('failed')
            return payload
    try:
        value = compute()
    except GitError:
        with _LOCK:
            _CACHE[key] = (time.monotonic(), ('err', 'failed'))
        raise
    with _LOCK:
        _CACHE[key] = (time.monotonic(), ('ok', value))
        if len(_CACHE) > 64:
            oldest = sorted(_CACHE, key=lambda item: _CACHE[item][0])[:16]
            for item in oldest:
                _CACHE.pop(item, None)
    return value


def _executor(run):
    if run is not None:
        return run

    def cached(cwd, args):
        key = (_repo_token(cwd), int(time.monotonic() // _TTL), tuple(args))
        return _cached_call(key, lambda: run_git(cwd, list(args)))

    return cached


def _marks_for(marks, task_id):
    if not isinstance(task_id, str) or not task_id or not isinstance(marks, list):
        return []
    found = []
    for index, mark in enumerate(marks):
        if not isinstance(mark, dict) or mark.get('id') != task_id:
            continue
        ts = _finite(mark.get('ts'))
        if ts is None:
            continue
        status = mark.get('status')
        if status == 'in progress':
            status = 'in_progress'
        if status not in _STATUSES:
            status = None
        found.append((ts, index, status))
    found.sort()
    return found


def _file_window(task, now):
    if task.get('normalizedStatus') != 'in_progress' or task.get('stateSinceKind') != 'file':
        return None
    since = _finite(task.get('stateSince'))
    if since is None or since <= 0 or now is None:
        return None
    start = min(since, now)
    return {'segments': [(start, now)], 'partial': True, 'open': True}


def _status_window(task, marks, now):
    """进行中的时间段。忽略 owner：改动按任务计，不按负责人截短。"""
    rows = _marks_for(marks, task.get('id'))
    status = None
    open_start = None
    segments = []
    dropped_tail = False
    for ts, _index, mark_status in rows:
        if mark_status:
            status = mark_status
        active = status == 'in_progress'
        if active and open_start is None:
            open_start = ts
        elif (not active) and open_start is not None:
            if ts >= open_start:
                segments.append((open_start, ts))
            open_start = None
    if open_start is not None:
        norm = task.get('normalizedStatus')
        since = _finite(task.get('stateSince'))
        if norm == 'in_progress' and now is not None and now >= open_start:
            segments.append((open_start, now))
        elif (norm == 'completed' and task.get('stateSinceKind') == 'file'
              and since is not None and since >= open_start):
            segments.append((open_start, since))
        else:
            dropped_tail = True
    if segments:
        end = segments[-1][1]
        opened = (task.get('normalizedStatus') == 'in_progress' and now is not None
                  and abs(end - now) < 1e-6)
        return {'segments': segments, 'partial': dropped_tail, 'open': opened}
    if dropped_tail:
        return None
    return _file_window(task, now)


def _covers(segments, ts):
    for start, end in segments:
        if start <= ts <= end:
            return True
    return False


def parse_numstat(text):
    """返回 (path -> [added, deleted], binary, truncated)。二进制行数按 0，文件仍计数。"""
    files = {}
    binary = False
    truncated = False
    count = 0
    if not isinstance(text, str):
        return files, False, False
    if len(text) > _MAX_OUTPUT:
        text = text[:_MAX_OUTPUT]
        truncated = True
    for line in text.splitlines():
        if '\t' not in line:
            continue
        added, deleted, path = line.split('\t', 2)
        if not path:
            continue
        count += 1
        if count > _MAX_NUMSTAT:
            truncated = True
            break
        if added == '-' or deleted == '-':
            binary = True
            files.setdefault(path, [0, 0])
            continue
        if not added.isdigit() or not deleted.isdigit():
            continue
        pair = files.setdefault(path, [0, 0])
        pair[0] += int(added)
        pair[1] += int(deleted)
    return files, binary, truncated


def parse_commit_log(text):
    """`git log --numstat --pretty=format:---%ct`。最多留下 400 个提交。"""
    commits = []
    current = None
    truncated = False
    if not isinstance(text, str):
        return commits, False
    if len(text) > _MAX_OUTPUT:
        text = text[:_MAX_OUTPUT]
        truncated = True
    for line in text.splitlines():
        if line.startswith('---') and line[3:].isdigit():
            if len(commits) >= _MAX_COMMITS:
                truncated = True
                current = None
                break
            current = {'ts': int(line[3:]), 'files': {}, 'binary': False}
            commits.append(current)
            continue
        if current is None or '\t' not in line:
            continue
        files, binary, cut = parse_numstat(line)
        if cut:
            truncated = True
        if binary:
            current['binary'] = True
        for path, pair in files.items():
            got = current['files'].setdefault(path, [0, 0])
            got[0] += pair[0]
            got[1] += pair[1]
    if len(commits) >= _MAX_COMMITS:
        truncated = True
    return commits, truncated


def _merge(dest, src):
    for path, pair in src.items():
        got = dest.setdefault(path, [0, 0])
        got[0] += pair[0]
        got[1] += pair[1]


def _sum(files):
    insertions = 0
    deletions = 0
    for pair in files.values():
        insertions += pair[0]
        deletions += pair[1]
    return len(files), insertions, deletions


def _diff_args(extra):
    return ['diff', '--numstat', '--no-ext-diff', '--no-textconv'] + list(extra)


def _numstat_of(run, cwd, extra):
    text = run(cwd, _diff_args(extra))
    return parse_numstat(text)


def _one_line(text):
    if not isinstance(text, str):
        return ''
    line = text.strip().splitlines()
    if len(line) != 1:
        return ''
    value = line[0].strip()
    if not value or len(value) > 200 or any(ch in value for ch in '\n\r\x00'):
        return ''
    return value


def _default_branch(run, cwd):
    for args in (
        ['symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD'],
        ['rev-parse', '--abbrev-ref', 'origin/HEAD'],
    ):
        try:
            name = _one_line(run(cwd, args))
        except GitError:
            name = ''
        if name.startswith('origin/'):
            name = name[len('origin/'):]
        if name and name != 'HEAD' and '..' not in name.split('/'):
            return name
    for candidate in ('main', 'master', 'trunk'):
        try:
            run(cwd, ['show-ref', '--verify', '--quiet', 'refs/heads/' + candidate])
        except GitError:
            continue
        return candidate
    return ''


def _branch_name(run, cwd):
    try:
        name = _one_line(run(cwd, ['rev-parse', '--abbrev-ref', 'HEAD']))
    except GitError:
        return ''
    if not name or name == 'HEAD' or '..' in name.split('/'):
        return ''
    return name


def _linked(run, cwd, worktree_name):
    if worktree_name:
        return True
    try:
        git_dir = _one_line(run(cwd, ['rev-parse', '--absolute-git-dir']))
    except GitError:
        git_dir = ''
    if not git_dir:
        return False
    return 'worktrees' in Path(git_dir).parts


def _merge_base(run, cwd, default):
    if not default:
        return ''
    try:
        return _one_line(run(cwd, ['merge-base', default, 'HEAD']))
    except GitError:
        return ''


def _apply_window(run, cwd, prepared, head_ok):
    windowed = [item for item in prepared if item['window']]
    if not windowed or not head_ok:
        for item in windowed:
            item['broken'] = True
        return
    start = min(seg[0] for item in windowed for seg in item['window']['segments'])
    end = max(seg[1] for item in windowed for seg in item['window']['segments'])
    since = max(0, int(start) - 1)
    until = int(end) + 2
    try:
        text = run(cwd, [
            'log', '-n', str(_MAX_COMMITS), '--numstat', '--pretty=format:---%ct',
            '--since=@%d' % since, '--until=@%d' % until,
        ])
        commits, truncated = parse_commit_log(text)
    except GitError:
        commits, truncated = [], True
    for commit in commits:
        hits = [item for item in windowed if _covers(item['window']['segments'], commit['ts'])]
        if len(hits) == 1:
            _merge(hits[0]['files'], commit['files'])
            if commit['binary']:
                hits[0]['binary'] = True
        elif len(hits) > 1:
            for item in hits:
                item['overlap'] = True
    if truncated:
        for item in windowed:
            item['truncated'] = True
    opened = [item for item in windowed if item['window'].get('open')]
    if len(opened) == 1:
        try:
            dirty, binary, cut = _numstat_of(run, cwd, ['HEAD'])
        except GitError:
            opened[0]['truncated'] = True
        else:
            _merge(opened[0]['files'], dirty)
            opened[0]['binary'] = opened[0]['binary'] or binary
            opened[0]['truncated'] = opened[0]['truncated'] or cut
    elif len(opened) > 1:
        for item in opened:
            item['overlap'] = True
    for item in windowed:
        files, insertions, deletions = _sum(item['files'])
        partial = bool(item['overlap'] or item['binary'] or item['truncated']
                       or item['broken'] or item['window'].get('partial'))
        item['task']['diff'] = _stat(files, insertions, deletions, 'window', partial)


def _apply_branch(run, cwd, prepared, worktree_name, head_ok):
    pending = [item for item in prepared
               if item['window'] is None and item['task'].get('normalizedStatus') == 'in_progress']
    if not pending:
        return
    if not head_ok:
        return
    linked = _linked(run, cwd, worktree_name)
    branch = _branch_name(run, cwd)
    default = _default_branch(run, cwd)
    on_feature = bool(linked or (branch and default and branch != default))
    basis = 'worktree' if linked else 'branch'
    base = _merge_base(run, cwd, default) if on_feature else ''
    files = {}
    binary = False
    truncated = False
    measured = False
    if base:
        try:
            files, binary, truncated = _numstat_of(run, cwd, [base])
            measured = True
        except GitError:
            measured = False
    if not measured:
        # 没有 merge-base 时，只在工作区真有未提交改动才报数字，避免把空 diff 说成整支分支没改过。
        try:
            files, binary, truncated = _numstat_of(run, cwd, ['HEAD'])
        except GitError:
            return
        if not files:
            return
    elif not on_feature:
        return
    count, insertions, deletions = _sum(files)
    if not on_feature and count == 0:
        return
    stat = _stat(count, insertions, deletions, basis, True)
    if binary or truncated:
        stat['partial'] = True
        stat['status'] = 'partial'
    for item in pending:
        item['task']['diff'] = dict(stat)


def _safe_rel(path):
    if not isinstance(path, str) or not path or len(path) > 180:
        return False
    if path.startswith(('/', '\\')) or ':' in path:
        return False
    if any(ch in path for ch in '\x00\n\r'):
        return False
    parts = path.replace('\\', '/').split('/')
    if any(part in ('', '.', '..') for part in parts):
        return False
    return True


def _owned_paths(text):
    capture = False
    found = []
    for line in text.splitlines():
        if line.startswith('##'):
            heading = ' '.join(line[2:].strip().lower().split())
            capture = heading == 'owned files'
            continue
        if not capture:
            continue
        if line.startswith('- ') or line.startswith('* '):
            raw = line[2:].strip()
            if not _safe_rel(raw):
                token = raw.split()[0] if raw.split() else ''
                raw = token if _safe_rel(token) else ''
            if raw and raw not in found:
                found.append(raw)
            if len(found) >= _MAX_PATHS:
                break
            continue
        if line.strip():
            break
    return found


def _title_of(text, fallback):
    for line in text.splitlines():
        if line.startswith('# '):
            title = ''.join(ch for ch in line[2:].strip() if ch >= ' ' and ch != '\x7f')
            title = title[:80]
            return title or fallback
        if line.strip():
            break
    return fallback


def _status_of(text):
    match = _STATUS_LINE.search(text or '')
    if not match:
        return ''
    return match.group(1)


def _inside(root, path):
    try:
        root_real = os.path.realpath(root)
        path_real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    root_key = os.path.normcase(root_real)
    path_key = os.path.normcase(path_real)
    if path_key == root_key:
        return True
    prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
    return path_key.startswith(prefix)


def _read_tickets(root):
    directory = Path(root) / '.team' / 'tickets'
    try:
        if directory.is_symlink() or not directory.is_dir():
            return []
    except OSError:
        return []
    if not _inside(root, str(directory)):
        return []
    found = []
    try:
        names = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    for path in names:
        if len(found) >= _MAX_TICKETS:
            break
        if not _TICKET_NAME.match(path.name):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > 65536:
                continue
        except OSError:
            continue
        if not _inside(root, str(path)):
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        found.append({
            'id': path.name[:-3],
            'title': _title_of(text, path.name[:-3]),
            'status': _status_of(text),
            'paths': _owned_paths(text),
        })
    return found


def _ticket_rows(run, cwd, tickets, head_ok):
    rows = []
    shared = {}
    for ticket in tickets:
        for path in ticket['paths']:
            shared.setdefault(path, set()).add(ticket['id'])
    default = ''
    base = ''
    if head_ok:
        default = _default_branch(run, cwd)
        base = _merge_base(run, cwd, default)
    for ticket in tickets:
        paths = ticket['paths']
        item = {'id': ticket['id'], 'title': ticket['title'], 'status': ticket['status']}
        if not paths or not head_ok:
            item['diff'] = _unavailable()
            rows.append(item)
            continue
        overlap = any(len(shared.get(path) or ()) > 1 for path in paths)
        try:
            if base:
                files, binary, truncated = _numstat_of(run, cwd, [base, '--'] + paths)
                partial = overlap or binary or truncated
            else:
                files, binary, truncated = _numstat_of(run, cwd, ['HEAD', '--'] + paths)
                partial = True
                if not files and not binary:
                    item['diff'] = _unavailable()
                    rows.append(item)
                    continue
        except GitError:
            item['diff'] = _unavailable()
            rows.append(item)
            continue
        count, insertions, deletions = _sum(files)
        item['diff'] = _stat(count, insertions, deletions, 'ticket', partial)
        rows.append(item)
    return rows


def _blank(tasks):
    for task in tasks:
        task['diff'] = _unavailable()


def _cwd_of(session):
    if not isinstance(session, dict):
        return ''
    cwd = session.get('cwd')
    if not isinstance(cwd, str) or not cwd or '\x00' in cwd or len(cwd) > 2000:
        return ''
    return cwd


def _worktree_of(session):
    if not isinstance(session, dict):
        return ''
    name = session.get('worktree')
    if not isinstance(name, str):
        return ''
    name = name.strip()
    if not name or len(name) > 200 or any(ch in name for ch in '\n\r\x00\\'):
        return ''
    if any(part in ('', '.', '..') for part in name.split('/')):
        return ''
    return name


def annotate(tasks, marks, now, session, run=None):
    """就地写上每条任务的 diff，并返回工单列表。失败时任务为 unavailable，工单为 []。"""
    rows = [task for task in (tasks or []) if isinstance(task, dict)]
    try:
        return _annotate(rows, marks, now, session, run)
    except Exception:
        _blank(rows)
        return []


def _annotate(rows, marks, now, session, run):
    now = _finite(now)
    cwd = _cwd_of(session)
    if not cwd or not os.path.isdir(cwd):
        _blank(rows)
        return []
    run = _executor(run)
    try:
        top = _one_line(run(cwd, ['rev-parse', '--show-toplevel']))
    except GitError:
        top = ''
    if not top or not os.path.isdir(top) or not _inside(top, top):
        _blank(rows)
        tickets = _read_tickets(cwd)
        for ticket in tickets:
            ticket['diff'] = _unavailable()
            ticket.pop('paths', None)
        return tickets
    head_ok = True
    try:
        if not _one_line(run(top, ['rev-parse', '--verify', 'HEAD'])):
            head_ok = False
    except GitError:
        head_ok = False
    prepared = []
    for task in rows:
        prepared.append({
            'task': task,
            'window': _status_window(task, marks, now),
            'files': {},
            'binary': False,
            'truncated': False,
            'overlap': False,
            'broken': False,
        })
    _apply_window(run, top, prepared, head_ok)
    _apply_branch(run, top, prepared, _worktree_of(session), head_ok)
    for item in prepared:
        if 'diff' not in item['task']:
            item['task']['diff'] = _unavailable()
    tickets = _read_tickets(top)
    return _ticket_rows(run, top, tickets, head_ok)
