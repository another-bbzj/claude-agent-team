#!/usr/bin/env python3
"""Write a Markdown team retrospective from a board snapshot.

The report copies fields that are already on the snapshot. Missing sections
are left out. Null prices render as 未知. A numeric cost is marked 部分 only
when that same object has an unpriced count above zero. Totals, idle ages and
savings are not calculated. Aggregate figures are the snapshot's own totals,
not a sum of the rows beneath them.

``--demo`` reads ``examples/board-demo.synthetic.json``. ``--input`` and
``--port`` reuse export_snapshot.read_snapshot, including its loopback,
size and redirect limits. ``--session`` and ``--project`` select a loopback
snapshot the same way that reader does.
"""
import argparse
import datetime
import importlib.util
import math
import re
import sys
from pathlib import Path

LOW_CACHE_HIT = 0.2
_ROOT = Path(__file__).resolve().parents[1]
_DEMO = _ROOT / 'examples' / 'board-demo.synthetic.json'
_EMAIL = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
_HEX_ID = re.compile(r'[0-9a-fA-F]{16,}')
_UUID = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
_FRAMEWORK = re.compile(r'[A-Za-z][A-Za-z0-9_+-]{0,20}')
_STATUS = {
    'running': '进行中',
    'completed': '已完成',
    'stalled': '停滞',
    'error': '出错',
    'idle': '待命',
    'empty': '未出场',
    'finished': '收尾完成',
    'coordinating': '协调中',
    'waiting': '等待中',
    'forming': '组队中',
    'ok': '预算内',
    'warn': '接近预算',
    'over': '超出预算',
}
_USAGE_GROUPS = (('usage', '成员'), ('leadUsage', '队长'), ('allUsage', '全队'))
_USAGE_KEYS = (
    ('in', '输入', 'count'),
    ('out', '输出', 'count'),
    ('cache_read', '缓存读', 'count'),
    ('cache_write', '缓存写', 'count'),
    ('total', '合计', 'count'),
    ('cacheHit', '缓存命中', 'ratio'),
)
_FAR = 10 ** 18


def _load_exporter():
    path = Path(__file__).resolve().parent / 'export_snapshot.py'
    name = 'retro_report_export_snapshot'
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


_exporter = _load_exporter()
ExportError = _exporter.ExportError
read_snapshot = _exporter.read_snapshot


def _port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('port must be an integer from 1 to 65535')
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('port must be an integer from 1 to 65535')
    return port


def num_text(value):
    """Stable, non-scientific rendering shared with the board page."""
    if type(value) is int:
        return str(value)
    scaled = int(math.floor(float(value) * 1000000 + 0.5))
    whole, frac = divmod(scaled, 1000000)
    if frac == 0:
        return str(whole)
    return '{}.{}'.format(whole, '{:06d}'.format(frac).rstrip('0'))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _count(value):
    return type(value) is int and value >= 0


def _cost(value):
    return _finite(value) and value >= 0


def _ratio(value):
    return _cost(value) and value <= 1


def safe_text(value, limit=48):
    """Return a short display string, or None when it is missing or private."""
    if not isinstance(value, str):
        return None
    text = ' '.join(''.join(ch for ch in value if ch >= ' ' or ch == '\t').split())
    if not text or len(text) > limit:
        return None
    if '@' in text or '\\' in text or '/' in text or text.startswith('~'):
        return None
    if '--' in text or '..' in text:
        return None
    if '://' in text or re.match(r'^[A-Za-z]:', text):
        return None
    if _EMAIL.search(text) or _HEX_ID.search(text) or _UUID.search(text):
        return None
    if re.search(r'(?i)javascript\s*:', text) or re.search(r'(?i)\bsk-[A-Za-z0-9]', text):
        return None
    return text


def _md(value):
    return str(value).replace('\\', '\\\\').replace('|', '\\|').replace('`', "'").replace('\r', ' ').replace('\n', ' ')


def _cell(value):
    if value is None:
        return '—'
    return _md(value)


def _status_label(value):
    if not isinstance(value, str):
        return None
    if value in _STATUS:
        return _STATUS[value]
    return safe_text(value, 24)


def _format_ts(value):
    """Format a unix-second timestamp. Other numbers stay raw; invalid values drop."""
    if not _finite(value) or value < 0:
        return None
    if type(value) is float and not value.is_integer():
        return num_text(value)
    whole = int(value)
    if whole < 1000000000 or whole > 10000000000:
        return str(whole)
    moment = datetime.datetime.fromtimestamp(whole, datetime.timezone.utc)
    return moment.strftime('%Y-%m-%d %H:%M:%S') + ' UTC'


def _stamp(obj, key):
    if not isinstance(obj, dict) or key not in obj:
        return None
    return _format_ts(obj.get(key))


def format_seconds(value):
    if not _count(value) and not (type(value) is float and math.isfinite(value) and value >= 0):
        return None
    if type(value) is float and not value.is_integer():
        return num_text(value) + 's'
    whole = int(value)
    if whole < 60:
        return '{}s'.format(whole)
    minutes, seconds = divmod(whole, 60)
    if minutes < 60:
        return '{}m {}s'.format(minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    return '{}h {}m {}s'.format(hours, minutes, seconds)


def _duration(obj):
    if 'startedAt' not in obj or 'endedAt' not in obj:
        return None
    start, end = obj.get('startedAt'), obj.get('endedAt')
    if not _cost(start) or not _cost(end) or end < start:
        return None
    if (type(start) is float and not start.is_integer()) or (type(end) is float and not end.is_integer()):
        return None
    return format_seconds(int(end) - int(start))


def _field_money(obj, key, partial=False):
    """None omits an invalid value. 未知 is an explicit null. A number is copied."""
    if not isinstance(obj, dict) or key not in obj:
        return None
    value = obj.get(key)
    if value is None:
        return '未知'
    if not _cost(value):
        return None
    text = '$' + num_text(value)
    if partial:
        text += '（部分）'
    return text


def _field_count(obj, key):
    if not isinstance(obj, dict) or key not in obj:
        return None
    value = obj.get(key)
    if value is None:
        return '未知'
    if not _count(value):
        return None
    return str(value)


def _field_ratio(obj, key):
    if not isinstance(obj, dict) or key not in obj:
        return None
    value = obj.get(key)
    if value is None:
        return '未知'
    if not _ratio(value):
        return None
    return num_text(value)


def _partial(obj):
    if not isinstance(obj, dict):
        return False
    return _count(obj.get('unpriced')) and obj['unpriced'] > 0


def _dept_names(snapshot):
    found = {}
    departments = snapshot.get('departments')
    if not isinstance(departments, list):
        return found
    for dept in departments:
        if not isinstance(dept, dict) or not isinstance(dept.get('id'), str):
            continue
        name = safe_text(dept.get('name'), 32)
        if name:
            found[dept['id']] = name
    return found


def _person_label(person, index, kind, dept_names):
    name = safe_text(person.get('name'), 48)
    if not name and kind == 'lead':
        name = '队长'
    if not name:
        name = '成员 {:02d}'.format(index)
    dept = person.get('dept')
    dept_name = dept_names.get(dept) if isinstance(dept, str) else None
    if dept_name:
        return name + ' · ' + dept_name
    return name


def _people(snapshot):
    dept_names = _dept_names(snapshot)
    people = []
    lead = snapshot.get('lead')
    if isinstance(lead, dict):
        people.append({'kind': 'lead', 'obj': lead, 'index': 0,
                       'label': _person_label(lead, 0, 'lead', dept_names)})
    members = snapshot.get('members')
    if isinstance(members, list):
        seen = 0
        for member in members:
            if not isinstance(member, dict):
                continue
            seen += 1
            people.append({'kind': 'member', 'obj': member, 'index': seen,
                           'label': _person_label(member, seen, 'member', dept_names)})
    return people


def _sessions(snapshot):
    rows = snapshot.get('sessions')
    if isinstance(rows, list) and any(isinstance(item, dict) for item in rows):
        return [item for item in rows if isinstance(item, dict)]
    one = snapshot.get('session')
    if isinstance(one, dict):
        return [one]
    return []


def _state_label(obj, kind):
    if not isinstance(obj, dict):
        return None
    if kind == 'session':
        return _status_label(obj.get('status')) if 'status' in obj else None
    if 'status' in obj:
        return _status_label(obj.get('status'))
    if kind == 'lead' and 'phase' in obj:
        return _status_label(obj.get('phase'))
    return None


def _last_stamp(obj, kind):
    if kind == 'session':
        for key in ('lastActivity', 'mtime', 'lastActivityAt'):
            stamp = _stamp(obj, key)
            if stamp:
                return stamp
        return None
    return _stamp(obj, 'lastActivityAt')


def _timeline_section(snapshot):
    rows = []
    order = 0
    for person in _people(snapshot):
        obj = person['obj']
        start = _stamp(obj, 'startedAt')
        last = _last_stamp(obj, person['kind'])
        end = _stamp(obj, 'endedAt')
        if not any((start, last, end)):
            continue
        order += 1
        first = _first_time(obj, person['kind'])
        rows.append((first, order, [
            person['label'], start, last, end, _duration(obj), _state_label(obj, person['kind']),
        ]))
    for index, session in enumerate(_sessions(snapshot), 1):
        project = safe_text(session.get('project'), 48)
        label = '会话 · ' + project if project else '会话 {:02d}'.format(index)
        start = _stamp(session, 'startedAt')
        last = _last_stamp(session, 'session')
        end = _stamp(session, 'endedAt')
        if not any((start, last, end)):
            continue
        order += 1
        rows.append((_first_time(session, 'session'), order, [
            label, start, last, end, _duration(session), _state_label(session, 'session'),
        ]))
    if not rows:
        return None
    rows.sort(key=lambda item: (item[0], item[1]))
    lines = [
        '## 时间线',
        '',
        '开始、停滞或最后活动、结束都来自快照里的时间戳。用时只在同时有开始和结束时出现。没有结束时间就留空，不用当前时间补上。',
        '',
        '| 对象 | 开始 | 停滞或最后活动 | 结束 | 用时 | 状态 |',
        '| --- | --- | --- | --- | --- | --- |',
    ]
    for _first, _order, cells in rows:
        lines.append('| ' + ' | '.join(_cell(cell) for cell in cells) + ' |')
    return lines


def _first_time(obj, kind):
    if kind == 'session':
        keys = ('startedAt', 'lastActivity', 'mtime', 'lastActivityAt', 'endedAt')
    else:
        keys = ('startedAt', 'lastActivityAt', 'endedAt')
    for key in keys:
        if key not in obj:
            continue
        value = obj.get(key)
        if _cost(value):
            return float(value)
    return _FAR


def _markdown_table(columns, rows):
    """Keep a column when any row has that field. Absent cells stay an em dash."""
    kept = [item for item in columns if item[0] == 'label' or any(row.get(item[0]) is not None for row in rows)]
    lines = [
        '| ' + ' | '.join(title for _key, title in kept) + ' |',
        '| ' + ' | '.join('---' for _key in kept) + ' |',
    ]
    for row in rows:
        lines.append('| ' + ' | '.join(_cell(row.get(key)) for key, _title in kept) + ' |')
    return lines


def _usage_table(totals):
    groups = []
    for key, label in _USAGE_GROUPS:
        usage = totals.get(key)
        if isinstance(usage, dict):
            groups.append((key, label, usage))
    if not groups:
        return None
    present = []
    for key, label, kind in _USAGE_KEYS:
        if any(_field_count(usage, key) if kind == 'count' else _field_ratio(usage, key) for _name, _title, usage in groups):
            present.append((key, label, kind))
    if not present:
        return None
    header = '| 项 | ' + ' | '.join(label for _key, label, _usage in groups) + ' |'
    rule = '| --- | ' + ' | '.join('---' for _group in groups) + ' |'
    lines = [header, rule]
    for key, label, kind in present:
        cells = [label]
        for _name, _title, usage in groups:
            shown = _field_count(usage, key) if kind == 'count' else _field_ratio(usage, key)
            cells.append(_cell(shown))
        lines.append('| ' + ' | '.join(cells) + ' |')
    return lines


def _budget_lines(snapshot):
    budget = snapshot.get('budget')
    if not isinstance(budget, dict) or budget.get('enabled') is not True:
        return None
    lines = []
    status = _status_label(budget.get('status')) if 'status' in budget else None
    if status:
        lines.append('- 状态：' + _md(status))
    ratio = budget.get('ratio') if 'ratio' in budget else None
    if 'ratio' in budget:
        if ratio is None:
            lines.append('- 使用率：未知')
        elif _cost(ratio):
            text = '- 使用率：' + num_text(ratio)
            if budget.get('lowerBound') is True:
                text += '（下限，未定价未计入）'
            lines.append(text)
    for key, label in (('spent', '已估算花费'), ('limit', '上限')):
        shown = _field_money(budget, key, partial=False)
        if shown:
            lines.append('- {}：{}'.format(label, shown))
    if _count(budget.get('unpriced')) and budget['unpriced'] > 0:
        lines.append('- 未定价：' + str(budget['unpriced']))
    if not lines:
        return None
    return ['### 预算', ''] + lines


def _cost_section(snapshot):
    blocks = []
    totals = snapshot.get('totals') if isinstance(snapshot.get('totals'), dict) else None
    if totals:
        partial = _partial(totals)
        rows = []
        for key, label in (('cost', '全队估算成本'), ('membersCost', '成员估算成本')):
            shown = _field_money(totals, key, partial=partial)
            if shown:
                rows.append('- {}：{}'.format(label, shown))
        unpriced = _field_count(totals, 'unpriced')
        if unpriced:
            rows.append('- 未定价条目：' + unpriced)
        tokens = _field_count(totals, 'tokens')
        if tokens:
            rows.append('- 成员输出 token：' + tokens)
        usage = _usage_table(totals)
        if rows or usage:
            blocks.append('### 快照总量')
            blocks.append('')
            blocks.append('这些数字是 totals 里的原字段。报告没有把下面的成员或部门相加。')
            blocks.append('')
            blocks.extend(rows)
            if usage:
                if rows:
                    blocks.append('')
                blocks.extend(usage)
    departments = snapshot.get('departments')
    dept_rows = []
    if isinstance(departments, list):
        seen = 0
        for dept in departments:
            if not isinstance(dept, dict):
                continue
            seen += 1
            partial = _partial(dept)
            row = {
                'label': _dept_label(dept, seen),
                'status': _status_label(dept.get('status')) if 'status' in dept else None,
                'cost': _field_money(dept, 'cost', partial=partial),
                'tokens': _field_count(dept, 'tokens'),
                'hit': _field_ratio(dept, 'cacheHit'),
                'unpriced': _field_count(dept, 'unpriced'),
            }
            if any(row[key] is not None for key in ('cost', 'tokens', 'hit', 'unpriced')):
                dept_rows.append(row)
    if dept_rows:
        blocks.append('### 按部门')
        blocks.append('')
        blocks.extend(_markdown_table(
            (('label', '部门'), ('status', '状态'), ('cost', '估算成本'),
             ('tokens', 'token'), ('hit', '缓存命中'), ('unpriced', '未定价')),
            dept_rows))
    member_rows = []
    for person in _people(snapshot):
        obj = person['obj']
        if 'totalTokens' in obj:
            tokens = _field_count(obj, 'totalTokens')
        elif 'outputTokens' in obj:
            output = _field_count(obj, 'outputTokens')
            tokens = None if output is None else '输出 ' + output
        else:
            tokens = None
        row = {
            'label': person['label'],
            'cost': _field_money(obj, 'cost', partial=False),
            'tokens': tokens,
            'hit': _field_ratio(obj, 'cacheHit'),
        }
        if any(row[key] is not None for key in ('cost', 'tokens', 'hit')):
            member_rows.append(row)
    if member_rows:
        blocks.extend([
            '### 按成员',
            '',
            '有数字的成本是该成员自己的估算。null 写成未知，不写成 0，报告也不另算一个总数。',
            '',
        ])
        blocks.extend(_markdown_table(
            (('label', '成员'), ('cost', '估算成本'), ('tokens', 'token'), ('hit', '缓存命中')),
            member_rows))
    budget = _budget_lines(snapshot)
    if budget:
        blocks.extend(budget)
    if not blocks:
        return None
    lines = ['## 成本与 token', '']
    previous = ''
    for line in blocks:
        if line.startswith('### ') and previous != '':
            lines.append('')
        lines.append(line)
        previous = line
    return lines


def _dept_label(dept, index):
    name = safe_text(dept.get('name'), 32)
    return name or '部门 {:02d}'.format(index)


def _framework(value):
    if isinstance(value, str) and _FRAMEWORK.fullmatch(value):
        return value
    return None


def _test_counts(test):
    bits = []
    failed = _field_count(test, 'failed')
    passed = _field_count(test, 'passed')
    if failed:
        bits.append('failed ' + failed)
    if passed:
        bits.append('passed ' + passed)
    framework = _framework(test.get('framework'))
    if framework:
        bits.append('框架 ' + framework)
    return bits


def _matched_candidate(snapshot, person):
    alerts = snapshot.get('stallAlerts')
    if not isinstance(alerts, dict) or not isinstance(alerts.get('candidates'), list):
        return None
    obj = person['obj']
    ident = obj.get('id') if isinstance(obj.get('id'), str) else None
    for candidate in alerts['candidates']:
        if not isinstance(candidate, dict):
            continue
        if ident and candidate.get('id') == ident:
            return candidate
        if not ident and isinstance(obj.get('name'), str) and candidate.get('name') == obj.get('name'):
            return candidate
    return None


def _idle_fact(snapshot, person):
    candidate = _matched_candidate(snapshot, person)
    if candidate is not None and _count(candidate.get('idleSeconds')):
        return 'stallAlerts', candidate['idleSeconds']
    obj = person['obj']
    if 'idleSeconds' in obj and _count(obj.get('idleSeconds')):
        return 'idleSeconds', obj['idleSeconds']
    return None


def _threshold(snapshot):
    alerts = snapshot.get('stallAlerts')
    if not isinstance(alerts, dict) or not _count(alerts.get('thresholdSec')):
        return None
    return alerts['thresholdSec']


def _idle_clause(snapshot, person):
    fact = _idle_fact(snapshot, person)
    if fact is None:
        return ''
    source, seconds = fact
    text = ' {} 记下的安静时间是 {} 秒。'.format(
        '成员 idleSeconds' if source == 'idleSeconds' else 'stallAlerts', seconds)
    threshold = _threshold(snapshot)
    if threshold is None:
        return text
    if seconds >= threshold:
        return text + '已达到阈值 {} 秒。'.format(threshold)
    return text + '未达到阈值 {} 秒。'.format(threshold)


def _error_events(obj):
    timeline = obj.get('timeline')
    if not isinstance(timeline, list):
        return 0
    return sum(1 for event in timeline if isinstance(event, dict) and event.get('error') is True)


def _tasks(snapshot):
    official = snapshot.get('officialTeam')
    if not isinstance(official, dict) or not isinstance(official.get('tasks'), list):
        return []
    return [task for task in official['tasks'] if isinstance(task, dict) and task.get('blocked') is True]


def _task_label(task, index):
    subject = safe_text(task.get('subject'), 80)
    if subject:
        return '「' + subject + '」'
    return '第 {} 项'.format(index)


def _blockers_section(snapshot):
    lines = []
    for person in _people(snapshot):
        status = person['obj'].get('status')
        if status == 'stalled':
            lines.append('- {} · 停滞。{}'.format(_md(person['label']), _idle_clause(snapshot, person)).rstrip())
        elif status == 'error':
            reason = safe_text(person['obj'].get('errorReason'), 80)
            text = '- {} · 出错。'.format(_md(person['label']))
            if reason:
                text += '原因：{}。'.format(_md(reason))
            text += _idle_clause(snapshot, person)
            lines.append(text.rstrip())
        elif status == 'idle' and _alerted(snapshot, person):
            lines.append('- {} · 待命，已达到停滞提醒阈值。{}'.format(
                _md(person['label']), _idle_clause(snapshot, person)).rstrip())
    for person in _people(snapshot):
        count = _error_events(person['obj'])
        if count:
            lines.append('- {} 的动作流里有 {} 条 error。工具参数没有写入这份报告。'.format(
                _md(person['label']), count))
    for person in _people(snapshot):
        test = person['obj'].get('lastTest')
        if isinstance(test, dict) and test.get('status') == 'fail':
            extra = _test_counts(test)
            text = '- {} 最近测试失败'.format(_md(person['label']))
            if extra:
                text += '（' + '，'.join(extra) + '）'
            lines.append(text + '。')
    for index, task in enumerate(_tasks(snapshot), 1):
        text = '- 任务{}的 blocked 为 true。'.format(_task_label(task, index))
        waiting = task.get('waitingOn')
        if isinstance(waiting, list):
            text += 'waitingOn 有 {} 项。'.format(len(waiting))
        lines.append(text)
    if not lines:
        return None
    return ['## 阻塞与停滞', ''] + lines


def _alerted(snapshot, person):
    if person['obj'].get('status') not in ('idle', 'error', 'stalled'):
        return False
    threshold = _threshold(snapshot)
    fact = _idle_fact(snapshot, person)
    if threshold is None or fact is None or fact[0] != 'stallAlerts':
        return False
    return fact[1] >= threshold


def _low_cache(snapshot):
    found = []
    totals = snapshot.get('totals') if isinstance(snapshot.get('totals'), dict) else {}
    for key, label in _USAGE_GROUPS:
        usage = totals.get(key)
        if not isinstance(usage, dict):
            continue
        hit = usage.get('cacheHit') if 'cacheHit' in usage else None
        total = usage.get('total') if 'total' in usage else None
        if _ratio(hit) and hit < LOW_CACHE_HIT and _count(total) and total > 0:
            found.append('{}用量的 cacheHit 是 {}，同一处的 token 合计是 {}。'.format(
                label, num_text(hit), total))
    for person in _people(snapshot):
        obj = person['obj']
        hit = obj.get('cacheHit') if 'cacheHit' in obj else None
        total = obj.get('totalTokens') if 'totalTokens' in obj else None
        if not (_count(total) and total > 0) and isinstance(obj.get('usage'), dict):
            usage_total = obj['usage'].get('total')
            if _count(usage_total) and usage_total > 0:
                total = usage_total
        if _ratio(hit) and hit < LOW_CACHE_HIT and _count(total) and total > 0:
            found.append('{} 的 cacheHit 是 {}，同一处的 token 数是 {}。'.format(
                person['label'], num_text(hit), total))
    departments = snapshot.get('departments')
    if isinstance(departments, list):
        seen = 0
        for dept in departments:
            if not isinstance(dept, dict):
                continue
            seen += 1
            hit = dept.get('cacheHit') if 'cacheHit' in dept else None
            tokens = dept.get('tokens') if 'tokens' in dept else None
            if _ratio(hit) and hit < LOW_CACHE_HIT and _count(tokens) and tokens > 0:
                found.append('{} 的 cacheHit 是 {}，同一处的 token 数是 {}。'.format(
                    _dept_label(dept, seen), num_text(hit), tokens))
    return found


def _suggestions_section(snapshot):
    lines = []
    for person in _people(snapshot):
        obj = person['obj']
        if obj.get('status') != 'stalled':
            continue
        if _stamp(obj, 'endedAt'):
            end_note = '结束时间已经在时间线里。'
        elif 'endedAt' in obj and obj.get('endedAt') is None:
            end_note = '结束时间是 null，这里不用当前时间补上。'
        else:
            end_note = '快照里没有结束时间。'
        lines.append('- {} 的状态是停滞。{}{}先确认是在等依赖，还是已经停住；不要把它算进已完成。'.format(
            _md(person['label']), end_note, _idle_clause(snapshot, person)))
    for person in _people(snapshot):
        if person['obj'].get('status') != 'error':
            continue
        reason = safe_text(person['obj'].get('errorReason'), 80)
        text = '- {} 的状态是出错。'.format(_md(person['label']))
        if reason:
            text += '快照里的原因是「{}」。'.format(_md(reason))
        else:
            text += '快照里没有可公开的原因，这里不补一句。'
        lines.append(text)
    for index, task in enumerate(_tasks(snapshot), 1):
        lines.append('- 任务{}被标成阻塞。前置没完成之前，不要把后续任务当成可以收尾。'.format(
            _task_label(task, index)))
    for person in _people(snapshot):
        test = person['obj'].get('lastTest')
        if not isinstance(test, dict) or test.get('status') != 'fail':
            continue
        extra = _test_counts(test)
        text = '- {} 最近一次测试是失败'.format(_md(person['label']))
        if extra:
            text += '（' + '，'.join(extra) + '）'
        lines.append(text + '。先处理这条失败，不要用其他成员的通过结果代替。')
    for person in _people(snapshot):
        test = person['obj'].get('lastTest')
        if not isinstance(test, dict) or test.get('status') != 'unknown':
            continue
        text = '- {} 最近一次测试是未知。'.format(_md(person['label']))
        if test.get('truncated') is True:
            text += 'truncated 为 true。'
        lines.append(text + '它不能算通过，也不能算失败。')
    totals = snapshot.get('totals') if isinstance(snapshot.get('totals'), dict) else None
    if totals and _partial(totals):
        lines.append('- 未定价条目是 {}。对应成本是部分估算，缺的价格没有按 0 计入。'.format(totals['unpriced']))
    budget = snapshot.get('budget') if isinstance(snapshot.get('budget'), dict) else None
    if isinstance(budget, dict) and budget.get('enabled') is True:
        ratio = budget.get('ratio') if 'ratio' in budget else None
        ratio_ok = _cost(ratio)
        over = budget.get('status') == 'over' or (ratio_ok and ratio > 1)
        warn = budget.get('status') == 'warn' and not over
        if over or warn:
            text = '- 会话预算是超出。' if over else '- 会话预算接近上限。'
            if ratio_ok:
                text += ' budget.ratio 为 {}。'.format(num_text(ratio))
            elif 'ratio' in budget and ratio is None:
                text += ' budget.ratio 是 null，这里不估算比例。'
            else:
                text += ' 快照里没有 budget.ratio，这里不估算比例。'
            if budget.get('lowerBound') is True:
                text += ' lowerBound 为 true，未定价没有计入，这个比例是下限。'
            text += '这是估算花费和上限的关系，不是服务商账单。'
            lines.append(text)
            suggestions = budget.get('suggestions')
            if isinstance(suggestions, list):
                added = 0
                for item in suggestions:
                    if not isinstance(item, dict):
                        continue
                    action = safe_text(item.get('action'), 160)
                    if not action:
                        continue
                    lines.append('- ' + _md(action))
                    added += 1
                    if added >= 3:
                        break
    for item in _low_cache(snapshot):
        lines.append('- {}低于本报告的参考线 {}。参考线不是快照字段。这里不估算能省下的金额。'.format(
            item, num_text(LOW_CACHE_HIT)))
    departments = snapshot.get('departments')
    if isinstance(departments, list):
        seen = 0
        for dept in departments:
            if not isinstance(dept, dict):
                continue
            seen += 1
            if dept.get('missing') is True:
                lines.append('- {} 的 missing 为 true，看板会把它显示成建议补位。这不是一条错误。'.format(
                    _md(_dept_label(dept, seen))))
    if not lines:
        return None
    return ['## 改进建议', ''] + lines


def render_report(snapshot):
    """Render one snapshot. The input object is not modified."""
    if not isinstance(snapshot, dict):
        raise ExportError('snapshot must be an object')
    lines = ['# 团队复盘', '']
    if snapshot.get('demo') is True:
        lines.append('这份报告用的是仓库里的合成演示快照，不代表真实用量。')
    generated = _stamp(snapshot, 'generatedAt')
    if generated:
        lines.append('快照时间：' + generated)
    version = safe_text(snapshot.get('version'), 32) if isinstance(snapshot.get('version'), str) else None
    if version:
        lines.append('版本：' + version)
    lines.append('只写快照里已有的字段。估算成本不是服务商账单。没有的数字不补 0。')
    sections = (
        _timeline_section(snapshot),
        _cost_section(snapshot),
        _blockers_section(snapshot),
        _suggestions_section(snapshot),
    )
    body = [section for section in sections if section]
    lines.append('')
    if not body:
        lines.append('这份快照里没有可写入时间线、成本、阻塞或改进建议的字段。')
    else:
        for section in body:
            lines.extend(section)
            lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def _same_file(input_path, output_path):
    try:
        same = input_path.resolve() == output_path.resolve()
        if not same and output_path.exists():
            same = input_path.samefile(output_path)
    except (OSError, RuntimeError):
        raise ExportError('cannot compare input and output files; check paths and permissions') from None
    if same:
        raise ExportError('output must differ from the snapshot input file')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--demo', action='store_true', help='read examples/board-demo.synthetic.json')
    source.add_argument('--input', type=Path, help='offline board snapshot JSON file')
    source.add_argument('--port', type=_port, help='local board port, normally 7788')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--session', help='session ID prefix (requires --port)')
    selection.add_argument('--project', help='project key or substring (requires --port)')
    parser.add_argument('--output', type=Path, help='write the Markdown here instead of stdout')
    args = parser.parse_args(argv)
    if (args.session is not None or args.project is not None) and args.port is None:
        parser.error('--session and --project require --port')
    if any(value is not None and not value.strip() for value in (args.session, args.project)):
        parser.error('session and project selectors must be non-empty')
    try:
        input_path = _DEMO if args.demo else args.input
        if input_path is not None and args.output is not None:
            _same_file(input_path, args.output)
        snapshot = read_snapshot(input_path, args.port, session=args.session, project=args.project)
        report = render_report(snapshot)
        if args.output:
            try:
                args.output.write_text(report, encoding='utf-8')
            except OSError:
                raise ExportError('cannot write retrospective; check directory and permissions') from None
        else:
            sys.stdout.write(report)
    except ExportError as exc:
        print('retro report failed: {}'.format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
