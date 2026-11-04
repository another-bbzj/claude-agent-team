#!/usr/bin/env python3
"""Export one board snapshot as a self-contained offline HTML replay.

The file embeds a desensitized document, not the raw snapshot. Absolute home
paths, e-mail addresses, API-key-like strings, session ids, prompts and tool
arguments are left out. Aggregate totals are copied only through
export_snapshot.export_metrics. Run ``python tools/export_replay.py --help``.
``--demo`` reads examples/board-demo.synthetic.json. ``--session`` and
``--project`` select a loopback snapshot the same way export_snapshot does.
"""
import argparse
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_FIXTURE = ROOT / 'examples' / 'board-demo.synthetic.json'
MAX_MEMBERS = 24
MAX_EVENTS = 240
MAX_COMMS = 80
MAX_TASKS = 40
MAX_DEPARTMENTS = 12
_NAME_LIMIT = 48
_LABEL_LIMIT = 160
_COMM_LIMIT = 240
_SUBJECT_LIMIT = 120

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
}
_TASK_STATUS = {
    'pending': '待处理',
    'in_progress': '进行中',
    'in progress': '进行中',
    'completed': '已完成',
    'deleted': '已删除',
}
_EVENT_KIND = {'tool': '工具', 'text': '文本', 'thinking': '思考'}
_COMM_KIND = {
    'assign': '派工',
    'report': '回报',
    'relay': '转告',
    'handoff': '交接',
    'direct': '直达',
    'peer': '协作',
    'bus': '总线',
    'undelivered': '未送达',
    'error': '出错',
}
_TEST_STATUS = {'pass': '通过', 'fail': '失败', 'unknown': '未知'}

# Home paths are consumed through the rest of the path, not only the prefix.
_PATH = re.compile(
    r'(?:~[/\\][^\s<>]*'
    r'|/(?:home|Users)(?:/[^\s<>]*)?'
    r'|[A-Za-z]:[/\\]+Users[/\\][^\s<>]*)',
    re.IGNORECASE)
_TOKEN = re.compile(
    r'(?:-----BEGIN [A-Z ]*PRIVATE KEY-----'
    r'|Bearer\s+[A-Za-z0-9._\-]+'
    r'|api[_-]?key\s*[:=]\s*[^\s<>]+'
    r'|\b[A-Za-z_][A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY)\b\s*=\s*[^\s<>]+'
    r'|sk-(?:ant-)?[A-Za-z0-9][A-Za-z0-9_\-]{7,}'
    r'|(?:ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_\-]+'
    r'|AKIA[0-9A-Z]{8,}'
    r'|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})',
    re.IGNORECASE)
# Same categories as the fixture privacy scan, plus a leftover tilde path.
_LEFTOVER = re.compile(
    r'(?:/Users/|/home/|[A-Za-z]:[/\\]+Users[/\\]|~[/\\]'
    r'|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'
    r'|sk-ant-|sk-[A-Za-z0-9][A-Za-z0-9_\-]{7,}|ghp_|github_pat_|xox[baprs]-'
    r'|AKIA[0-9A-Z]{8,}|BEGIN [A-Z ]*PRIVATE KEY|api[_-]?key\s*[:=]'
    r'|Bearer\s+[A-Za-z0-9._\-]+)',
    re.IGNORECASE)
_MARKS = ('[path]', '[email]', '[redacted]')


def _load_exporter():
    path = Path(__file__).resolve().parent / 'export_snapshot.py'
    name = 'export_replay_export_snapshot'
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


_exporter = _load_exporter()
ExportError = _exporter.ExportError
export_metrics = _exporter.export_metrics
read_snapshot = _exporter.read_snapshot


def _port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('port must be an integer from 1 to 65535')
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('port must be an integer from 1 to 65535')
    return port


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _count(value):
    return type(value) is int and value >= 0


def _token_name(match):
    text = match.group(0)
    if '@' in text and 'BEGIN' not in text.upper() and not text.lower().startswith('bearer'):
        return '[email]'
    return '[redacted]'


def redact_text(value, limit):
    """Collapse a display string and replace private fragments.

    Returns None when the value is not a string or is empty after cleaning.
    The full string is redacted before clipping, so a secret past the limit
    cannot survive as a prefix.
    """
    if not isinstance(value, str):
        return None
    cleaned = ''.join(ch for ch in value if ch >= ' ' or ch == '\t')
    text = ' '.join(cleaned.split())
    if not text:
        return None
    text = _PATH.sub('[path]', text)
    text = _TOKEN.sub(_token_name, text)
    if _LEFTOVER.search(text):
        return '[redacted]'
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + '…'
    return text


def public_name(value, fallback, limit=_NAME_LIMIT):
    """A short label safe to show, or the fallback when it is missing or private."""
    text = redact_text(value, limit)
    if not text or text in _MARKS:
        return fallback
    lowered = text.lower()
    if any(mark in text for mark in _MARKS):
        return fallback
    if '/' in text or '\\' in text or '@' in text or '<' in text or '>' in text:
        return fallback
    if text.startswith('~') or '://' in text or 'javascript:' in lowered:
        return fallback
    if _LEFTOVER.search(text):
        return fallback
    return text


def _status_label(value):
    if not isinstance(value, str):
        return None
    if value in _STATUS:
        return _STATUS[value]
    return public_name(value, None, 24)


def _format_seconds(value):
    if type(value) is float and not value.is_integer():
        return None
    whole = int(value)
    if whole <= 0:
        return None
    if whole < 60:
        return '{}s'.format(whole)
    minutes, seconds = divmod(whole, 60)
    if minutes < 60:
        return '{}m {}s'.format(minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    return '{}h {}m {}s'.format(hours, minutes, seconds)


def _duration_text(obj):
    """Elapsed time only when both ends are already on the object."""
    if not isinstance(obj, dict) or 'startedAt' not in obj or 'endedAt' not in obj:
        return None
    start, end = obj.get('startedAt'), obj.get('endedAt')
    if not _finite(start) or not _finite(end) or end < start:
        return None
    return _format_seconds(end - start)


def _timestamp(value):
    if not _finite(value) or value < 0 or value > 10000000000:
        return None
    if type(value) is float and value.is_integer():
        return int(value)
    return value


def _positive_number(value):
    return _finite(value) and value > 0


def _ratio(value):
    return _finite(value) and 0 < value <= 1


def _metrics(snapshot):
    totals = snapshot.get('totals')
    members = snapshot.get('members')
    if not isinstance(totals, dict) or not isinstance(members, list):
        return None
    try:
        return export_metrics(snapshot)
    except ExportError:
        return None


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}', value):
        return None
    if _LEFTOVER.search(value):
        return None
    return value


def _branch(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._/+-]{1,64}', value):
        return None
    if value.startswith('/') or '//' in value or '..' in value or '@' in value:
        return None
    if _LEFTOVER.search(value) or _PATH.search(value):
        return None
    return value


def _session(snapshot):
    session = snapshot.get('session')
    out = {}
    if not isinstance(session, dict):
        return out
    project = public_name(session.get('project'), None, 64)
    if project:
        out['project'] = project
    branch = _branch(session.get('branch'))
    if branch:
        out['branch'] = branch
    source = session.get('source')
    if source in ('claude', 'codex'):
        out['source'] = source
    return out


def _dept_names(snapshot):
    found = {}
    departments = snapshot.get('departments')
    if not isinstance(departments, list):
        return found
    for item in departments:
        if not isinstance(item, dict):
            continue
        ident = item.get('id')
        if not isinstance(ident, str):
            continue
        name = public_name(item.get('name'), None)
        if not name:
            name = public_name(ident, None, 24)
        if name:
            found[ident] = name
    return found


def _test_summary(value):
    if not isinstance(value, dict):
        return None
    status = value.get('status')
    if status not in _TEST_STATUS:
        return None
    out = {'status': status, 'statusLabel': _TEST_STATUS[status]}
    framework = public_name(value.get('framework'), None, 24)
    if framework:
        out['framework'] = framework
    for key in ('passed', 'failed', 'skipped'):
        if _count(value.get(key)):
            out[key] = value[key]
    if value.get('truncated') is True:
        out['truncated'] = True
    return out


def _person_bits(obj):
    """Copy numeric card fields only when the snapshot already has them."""
    bits = {}
    if 'cost' in obj and obj.get('cost') is None:
        bits['unpriced'] = True
    elif _positive_number(obj.get('cost')):
        bits['cost'] = obj['cost']
    if _positive_number(obj.get('totalTokens')) and _count(obj.get('totalTokens')):
        bits['tokens'] = obj['totalTokens']
    if _ratio(obj.get('cacheHit')):
        bits['cacheHit'] = obj['cacheHit']
    if _count(obj.get('toolCount')) and obj.get('toolCount') > 0:
        bits['toolCount'] = obj['toolCount']
    family = public_name(obj.get('modelFamily'), None, 24)
    if family:
        bits['modelFamily'] = family
    effort = public_name(obj.get('effort'), None, 16)
    if effort:
        bits['effort'] = effort
    duration = _duration_text(obj)
    if duration:
        bits['duration'] = duration
    if obj.get('officialTeam') is True:
        bits['official'] = True
    return bits


def _current_label(obj, events):
    current = obj.get('current')
    if isinstance(current, dict):
        label = redact_text(current.get('label') or current.get('name'), _LABEL_LIMIT)
        if label:
            return label
    for event in reversed(events):
        if event.get('label'):
            return event['label']
    return None


def _timeline_events(obj, who, key):
    timeline = obj.get('timeline') if isinstance(obj, dict) else None
    if not isinstance(timeline, list):
        return []
    rows = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        kind = item.get('kind') if item.get('kind') in _EVENT_KIND else None
        label = redact_text(item.get('label') or item.get('name'), _LABEL_LIMIT)
        if not label:
            continue
        row = {'key': key, 'who': who, 'label': label}
        if kind:
            row['kind'] = kind
            row['kindLabel'] = _EVENT_KIND[kind]
        else:
            row['kind'] = 'other'
            row['kindLabel'] = '动作'
        ts = _timestamp(item.get('ts'))
        if ts is not None:
            row['ts'] = ts
        if item.get('done') is True:
            row['done'] = True
        if item.get('error') is True:
            row['error'] = True
        test = _test_summary(item.get('test'))
        if test:
            row['test'] = test
        rows.append(row)
    return rows


def _person(obj, key, fallback, dept_names):
    if not isinstance(obj, dict):
        return None, []
    name = public_name(obj.get('name'), fallback)
    events = _timeline_events(obj, name, key)
    card = {'key': key, 'name': name}
    role = public_name(obj.get('roleTitle') or obj.get('agentType'), None)
    if role and role != name:
        card['role'] = role
    dept = obj.get('dept')
    if isinstance(dept, str) and dept in dept_names:
        card['dept'] = dept_names[dept]
    elif isinstance(dept, str):
        label = public_name(dept, None, 24)
        if label:
            card['dept'] = label
    status = obj.get('status') if 'status' in obj else obj.get('phase')
    if isinstance(status, str) and status in _STATUS:
        card['status'] = status
        card['statusLabel'] = _STATUS[status]
    card.update(_person_bits(obj))
    current = _current_label(obj, events)
    if current:
        card['current'] = current
    test = _test_summary(obj.get('lastTest'))
    if test:
        card['lastTest'] = test
    useful = any(field in card for field in (
        'role', 'dept', 'status', 'cost', 'unpriced', 'tokens', 'toolCount', 'current', 'lastTest', 'duration'))
    if not useful and not events and name == fallback:
        return None, []
    return card, events


def _departments(snapshot):
    departments = snapshot.get('departments')
    if not isinstance(departments, list):
        return []
    rows = []
    for item in departments:
        if not isinstance(item, dict):
            continue
        name = public_name(item.get('name'), None)
        if not name:
            name = public_name(item.get('id'), None, 24)
        if not name:
            continue
        icon = item.get('icon')
        if isinstance(icon, str) and 1 <= len(icon) <= 2 and not _LEFTOVER.search(icon):
            name = icon + ' ' + name
        row = {'name': name}
        status = item.get('status')
        if isinstance(status, str) and status in _STATUS:
            row['status'] = status
            row['statusLabel'] = _STATUS[status]
        members = item.get('members')
        if isinstance(members, list):
            row['count'] = len(members)
        if _positive_number(item.get('cost')):
            row['cost'] = item['cost']
        rows.append(row)
    return rows


def _comms(snapshot, name_keys):
    comms = snapshot.get('comms')
    if not isinstance(comms, list):
        return []
    rows = []
    for item in comms:
        if not isinstance(item, dict):
            continue
        kind = item.get('kind') if item.get('kind') in _COMM_KIND else None
        text = redact_text(item.get('text'), _COMM_LIMIT)
        sender = public_name(item.get('fromName'), '成员')
        receiver = public_name(item.get('toName'), None)
        if not text and not kind:
            continue
        row = {'from': sender}
        if kind:
            row['kind'] = kind
            row['kindLabel'] = _COMM_KIND[kind]
        else:
            row['kind'] = 'other'
            row['kindLabel'] = '消息'
        if receiver:
            row['to'] = receiver
        if text:
            row['text'] = text
        ts = _timestamp(item.get('ts'))
        if ts is not None:
            row['ts'] = ts
        if sender in name_keys:
            row['key'] = name_keys[sender]
        rows.append(row)
    return rows


def _tasks(snapshot):
    team = snapshot.get('officialTeam')
    if not isinstance(team, dict):
        return []
    tasks = team.get('tasks')
    if not isinstance(tasks, list):
        return []
    rows = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        subject = redact_text(item.get('subject'), _SUBJECT_LIMIT)
        status = item.get('normalizedStatus')
        if not isinstance(status, str):
            status = item.get('status') if isinstance(item.get('status'), str) else None
        row = {}
        ident = item.get('id')
        if type(ident) is int and ident >= 0:
            row['id'] = str(ident)
        elif isinstance(ident, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,16}', ident) and not _LEFTOVER.search(ident):
            row['id'] = ident
        if subject:
            row['subject'] = subject
        if isinstance(status, str) and status in _TASK_STATUS:
            row['status'] = status
            row['statusLabel'] = _TASK_STATUS[status]
        owner = public_name(item.get('owner'), None)
        if owner:
            row['owner'] = owner
        if item.get('blocked') is True:
            row['blocked'] = True
        if 'subject' in row or 'status' in row:
            rows.append(row)
    return rows


def _stall_count(snapshot):
    if 'stallAlerts' not in snapshot:
        return None
    block = snapshot.get('stallAlerts')
    if not isinstance(block, dict) or 'candidates' not in block or 'thresholdSec' not in block:
        return None
    threshold = block.get('thresholdSec')
    candidates = block.get('candidates')
    if not _finite(threshold) or threshold < 0 or not isinstance(candidates, list):
        return None
    count = 0
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if item.get('status') not in ('idle', 'error', 'stalled'):
            continue
        age = item.get('idleSeconds')
        if not _finite(age) or age < 0 or age < threshold:
            continue
        count += 1
    return count


def _theater_count(snapshot):
    if 'theaters' not in snapshot:
        return None
    theaters = snapshot.get('theaters')
    if not isinstance(theaters, list):
        return None
    return sum(1 for item in theaters if isinstance(item, dict))


def _live_retained(snapshot):
    if 'live' not in snapshot:
        return None
    live = snapshot.get('live')
    if not isinstance(live, dict) or live.get('available') is not True:
        return None
    retained = live.get('retained')
    if not _count(retained):
        return None
    return retained


def _budget(snapshot):
    budget = snapshot.get('budget')
    if not isinstance(budget, dict) or budget.get('enabled') is not True:
        return None
    ratio = budget.get('ratio')
    if not _finite(ratio) or ratio < 0:
        return None
    out = {'enabled': True, 'ratio': ratio}
    status = budget.get('status')
    if status in ('ok', 'warn', 'over'):
        out['status'] = status
    if budget.get('lowerBound') is True:
        out['lowerBound'] = True
    return out


def _sort_rows(rows):
    def key(pair):
        ts = pair[1].get('ts')
        if _finite(ts):
            return (0, float(ts), pair[0])
        return (1, 0.0, pair[0])

    ordered = sorted(enumerate(rows), key=key)
    return [row for _index, row in ordered]


def _clip_head(rows, limit):
    if len(rows) <= limit:
        return rows, 0
    return rows[:limit], len(rows) - limit


def _clip_tail(rows, limit):
    """Keep the latest rows. Callers pass rows already sorted oldest-first."""
    if len(rows) <= limit:
        return rows, 0
    return rows[-limit:], len(rows) - limit


def build_replay(snapshot):
    """Build the allowlisted document embedded in the HTML file."""
    if not isinstance(snapshot, dict):
        raise ExportError('snapshot must be a JSON object')
    dept_names = _dept_names(snapshot)
    lead_card, lead_events = _person(snapshot.get('lead'), 'lead', '队长', dept_names)
    members = snapshot.get('members')
    cards = []
    events = list(lead_events)
    if isinstance(members, list):
        for index, member in enumerate(members):
            if not isinstance(member, dict):
                continue
            card, member_events = _person(member, 'm{}'.format(index), '成员 {:02d}'.format(index + 1), dept_names)
            if card is None:
                continue
            cards.append(card)
            events.extend(member_events)
    name_keys = {}
    if lead_card is not None:
        name_keys[lead_card['name']] = lead_card['key']
    for card in cards:
        name_keys.setdefault(card['name'], card['key'])
    departments, dropped_departments = _clip_head(_departments(snapshot), MAX_DEPARTMENTS)
    shown_cards, dropped_members = _clip_head(cards, MAX_MEMBERS)
    events, dropped_events = _clip_tail(_sort_rows(events), MAX_EVENTS)
    comms, dropped_comms = _clip_tail(_sort_rows(_comms(snapshot, name_keys)), MAX_COMMS)
    tasks, dropped_tasks = _clip_head(_tasks(snapshot), MAX_TASKS)
    counts = {}
    stalls = _stall_count(snapshot)
    theaters = _theater_count(snapshot)
    retained = _live_retained(snapshot)
    if stalls is not None:
        counts['stalls'] = stalls
    if theaters is not None:
        counts['theaters'] = theaters
    if retained is not None:
        counts['liveRetained'] = retained
    clipped = {}
    if dropped_members:
        clipped['members'] = dropped_members
    if dropped_events:
        clipped['events'] = dropped_events
    if dropped_comms:
        clipped['comms'] = dropped_comms
    if dropped_tasks:
        clipped['tasks'] = dropped_tasks
    if dropped_departments:
        clipped['departments'] = dropped_departments
    document = {
        'schemaVersion': 1,
        'kind': 'claude-agent-team-replay',
        'synthetic': snapshot.get('demo') is True,
        'generatedAt': _timestamp(snapshot.get('generatedAt')),
        'notice': '数字只来自快照里已有的字段。本机路径、邮箱、密钥、会话标识和提示词已去掉。',
        'session': _session(snapshot),
        'metrics': _metrics(snapshot),
        'budget': _budget(snapshot),
        'lead': lead_card,
        'members': shown_cards,
        'departments': departments,
        'events': events,
        'comms': comms,
        'tasks': tasks,
        'counts': counts,
    }
    version = _version(snapshot.get('version'))
    if version:
        document['version'] = version
    if clipped:
        document['clipped'] = clipped
    return document


def embed_json(document):
    """JSON for a script tag. Angle brackets cannot close the script element."""
    try:
        text = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError, OverflowError):
        raise ExportError('replay document could not be encoded') from None
    return (text
            .replace('&', '\\u0026')
            .replace('<', '\\u003c')
            .replace('>', '\\u003e')
            .replace('\u2028', '\\u2028')
            .replace('\u2029', '\\u2029'))


_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>离线回放 · Agent Team</title>
<style>
:root {
  --bg: #e7e9ed; --bg-2: #f7f7f4; --card: #ffffff; --line: #d2d6de; --line-2: #a9b0bf;
  --text: #131722; --text-2: #465066; --text-3: #858c9d;
  --brand: #2c3d9e; --gold: #9c7818; --ok: #2b8466; --warn: #b36f0b; --err: #a8452e; --info: #1c74a8;
  --gold-soft: #f6efd8; --ok-soft: #e5f4ee; --warn-soft: #f8efd8; --err-soft: #f8e8e4; --info-soft: #e5f1f8; --brand-soft: #e6e9f7;
  --shadow: 0 12px 34px rgba(24, 30, 52, .12);
  --sans: "Segoe UI Variable Text", "Segoe UI", -apple-system, "PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  --mono: ui-monospace, "Cascadia Mono", "SF Mono", Consolas, "Liberation Mono", monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #070a11; --bg-2: #0e121c; --card: #141926; --line: #232a3a; --line-2: #3b4559;
    --text: #eceae3; --text-2: #a6acba; --text-3: #676f83;
    --brand: #7085f0; --gold: #d3b35c; --ok: #62b596; --warn: #e3a444; --err: #dd6d4f; --info: #5fb1e3;
    --gold-soft: #2a2416; --ok-soft: #143028; --warn-soft: #2c2416; --err-soft: #321c16; --info-soft: #142838; --brand-soft: #1a2144;
    --shadow: 0 18px 44px rgba(0, 0, 0, .5);
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #070a11; --bg-2: #0e121c; --card: #141926; --line: #232a3a; --line-2: #3b4559;
  --text: #eceae3; --text-2: #a6acba; --text-3: #676f83;
  --brand: #7085f0; --gold: #d3b35c; --ok: #62b596; --warn: #e3a444; --err: #dd6d4f; --info: #5fb1e3;
  --gold-soft: #2a2416; --ok-soft: #143028; --warn-soft: #2c2416; --err-soft: #321c16; --info-soft: #142838; --brand-soft: #1a2144;
  --shadow: 0 18px 44px rgba(0, 0, 0, .5);
  color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; }
body { min-height: 100vh; }
.hazard { height: 4px; background: repeating-linear-gradient(-45deg, #c8a64b 0 8px, #161a26 8px 16px); }
header { display: flex; flex-wrap: wrap; gap: 12px 18px; align-items: center; padding: 14px 22px;
  background: var(--bg-2); border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 2; }
h1 { margin: 0; font-size: 18px; letter-spacing: .04em; }
h2 { margin: 0; font-size: 13px; letter-spacing: .08em; }
.en { margin: 0 0 2px; font-family: var(--mono); font-size: 10px; letter-spacing: .18em; color: var(--text-3); }
#session-line { margin: 0; color: var(--text-2); font-family: var(--mono); font-size: 12px; }
.spacer { flex: 1; }
button, input { font: inherit; color: inherit; }
button { background: var(--card); border: 1px solid var(--line); cursor: pointer; }
button:hover { border-color: var(--gold); color: var(--gold); }
button:disabled { opacity: .45; cursor: default; }
:focus-visible { outline: 1.5px solid var(--gold); outline-offset: 2px; }
#theme-toggle, #replay-play, #replay-pause { padding: 4px 12px; }
#replay-play[aria-pressed="true"] { background: var(--gold-soft); border-color: var(--gold); color: var(--gold); }
main { padding: 18px 22px 32px; display: grid; gap: 18px; }
.banner { margin: 0; padding: 8px 12px; background: #153d50; color: #fff; font-size: 13px; }
.metrics, .cards, #departments, .controls { display: flex; flex-wrap: wrap; gap: 8px; }
.stat, .card { background: var(--card); border: 1px solid var(--line); box-shadow: var(--shadow); padding: 10px 12px; }
.stat { min-width: 128px; }
.stat .k, .meta, .kind, time { color: var(--text-3); font-family: var(--mono); font-size: 11px; }
.stat .v { margin: 4px 0 0; font-size: 20px; font-variant-numeric: tabular-nums; }
.card { width: min(100%, 280px); flex: 1 1 220px; }
.card.on { border-color: var(--gold); box-shadow: 0 0 0 1px var(--gold); }
.card-top, .rowline, .controls { display: flex; align-items: center; gap: 8px; }
.card h3 { margin: 0; font-size: 15px; }
.card p, li p { margin: 6px 0 0; }
.bits, .current { font-size: 12.5px; color: var(--text-2); }
.bit { white-space: nowrap; }
.pill { display: inline-flex; align-items: center; padding: 2px 8px; border: 1px solid var(--line); background: var(--bg-2); color: var(--text-2); font-size: 12px; }
.pill.run, .pill.info { background: var(--info-soft); color: var(--info); border-color: transparent; }
.pill.ok { background: var(--ok-soft); color: var(--ok); border-color: transparent; }
.pill.warn { background: var(--warn-soft); color: var(--warn); border-color: transparent; }
.pill.err { background: var(--err-soft); color: var(--err); border-color: transparent; }
.split { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(280px, .8fr); gap: 16px; }
.panel { min-width: 0; background: var(--card); border: 1px solid var(--line); padding: 12px; }
.panel h2 { margin-bottom: 8px; }
ol { list-style: none; margin: 0; padding: 0; max-height: 460px; overflow: auto; }
li { padding: 8px 8px 8px 10px; border-top: 1px solid var(--line); }
li.on { background: var(--gold-soft); box-shadow: inset 3px 0 0 var(--gold); }
li[role="button"] { cursor: pointer; }
time { min-width: 64px; }
.kind { margin-left: auto; }
#replay-range { flex: 1; min-width: 80px; }
.empty { color: var(--text-3); }
footer { padding: 0 22px 28px; color: var(--text-3); font-size: 12px; }
footer p { margin: 0; }
@media (max-width: 800px) {
  header, main, footer { padding-left: 14px; padding-right: 14px; }
  .split { grid-template-columns: 1fr; }
  .card { width: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  * { scroll-behavior: auto; }
}
</style>
</head>
<body data-replay="offline">
<div class="hazard"></div>
<header>
  <div>
    <p class="en">OFFLINE REPLAY</p>
    <h1>离线回放</h1>
  </div>
  <p id="session-line"></p>
  <div class="spacer"></div>
  <button id="theme-toggle" type="button">深色</button>
</header>
<main>
  <p id="banner" class="banner" hidden></p>
  <div id="departments"></div>
  <section id="metrics" class="metrics" aria-label="聚合"></section>
  <section>
    <h2>成员</h2>
    <div id="member-cards" class="cards"></div>
  </section>
  <section class="split">
    <div class="panel">
      <div class="controls">
        <h2>动作</h2>
        <button id="replay-play" type="button" aria-pressed="false">播放</button>
        <button id="replay-pause" type="button">暂停</button>
        <input id="replay-range" type="range" min="0" max="0" value="0" aria-label="回放进度">
        <span id="replay-pos" aria-live="polite"></span>
      </div>
      <p class="meta">空格播放，左右键步进。点一条可停在那里。</p>
      <ol id="timeline"></ol>
    </div>
    <div class="panel">
      <h2>关键消息</h2>
      <ol id="comms"></ol>
    </div>
  </section>
  <section class="panel">
    <h2>任务</h2>
    <ol id="tasks"></ol>
  </section>
</main>
<footer>
  <p id="notice"></p>
  <p>用浏览器直接打开这个文件即可。不需要看板进程，也不从网络加载脚本或字体。</p>
</footer>
<noscript><p>需要启用 JavaScript 才能回放。这个文件仍然不会发起网络请求。</p></noscript>
<script type="application/json" id="replay-data">__REPLAY_PAYLOAD__</script>
<script>
(function () {
  var STATUS_CLASS = {running: 'run', completed: 'ok', stalled: 'warn', error: 'err', idle: '', finished: 'ok', coordinating: 'run', waiting: '', forming: ''};
  var root = document.documentElement;
  var dataNode = document.getElementById('replay-data');
  var data = JSON.parse(dataNode.textContent);
  var playBtn = document.getElementById('replay-play');
  var pauseBtn = document.getElementById('replay-pause');
  var range = document.getElementById('replay-range');
  var posLabel = document.getElementById('replay-pos');
  var toggle = document.getElementById('theme-toggle');
  var timer = null;
  var cursor = -1;
  var story = [];

  function isNum(value) {
    return typeof value === 'number' && isFinite(value);
  }
  function num(value) {
    if (!isNum(value)) return '';
    if (Math.floor(value) === value && Math.abs(value) < 1e15) return String(value);
    return value.toFixed(6).replace(/0+$/, '').replace(/\.$/, '');
  }
  function money(value) { return '$' + num(value); }
  function pad(value) { return (value < 10 ? '0' : '') + value; }
  function fmtClock(ts) {
    if (!isNum(ts)) return '';
    var date = new Date(ts * 1000);
    if (isNaN(date.getTime())) return '';
    return pad(date.getUTCHours()) + ':' + pad(date.getUTCMinutes()) + ':' + pad(date.getUTCSeconds());
  }
  function fmtTime(ts) {
    if (!isNum(ts)) return '';
    var date = new Date(ts * 1000);
    if (isNaN(date.getTime())) return '';
    return date.getUTCFullYear() + '-' + pad(date.getUTCMonth() + 1) + '-' + pad(date.getUTCDate()) + ' ' + fmtClock(ts) + ' UTC';
  }
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null && text !== '') node.textContent = text;
    return node;
  }
  function joined(className, parts) {
    var node = el('p', className);
    parts.forEach(function (part, index) {
      if (index) node.appendChild(document.createTextNode(' · '));
      node.appendChild(el('span', 'bit', part));
    });
    return node;
  }
  function shownDark() {
    var mode = root.getAttribute('data-theme');
    if (mode === 'dark') return true;
    if (mode === 'light') return false;
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  function refreshThemeButton() {
    toggle.textContent = shownDark() ? '浅色' : '深色';
    toggle.setAttribute('aria-pressed', shownDark() ? 'true' : 'false');
  }
  function testLine(test) {
    if (!test || !test.statusLabel) return null;
    var parts = [];
    parts.push(test.framework ? test.framework + ' ' + test.statusLabel : '测试 ' + test.statusLabel);
    if (isNum(test.passed)) parts.push('通过 ' + num(test.passed));
    if (isNum(test.failed)) parts.push('失败 ' + num(test.failed));
    if (isNum(test.skipped)) parts.push('跳过 ' + num(test.skipped));
    if (test.truncated === true) parts.push('输出被截断');
    return el('p', 'test ' + test.status, parts.join(' · '));
  }
  function addCard(person) {
    var card = el('article', 'card');
    card.setAttribute('data-key', person.key || '');
    var top = el('div', 'card-top');
    top.appendChild(el('h3', '', person.name || '成员'));
    if (person.statusLabel) top.appendChild(el('span', 'pill ' + (STATUS_CLASS[person.status] || ''), person.statusLabel));
    card.appendChild(top);
    var meta = [];
    if (person.role) meta.push(person.role);
    if (person.dept) meta.push(person.dept);
    if (person.modelFamily) meta.push(person.modelFamily);
    if (person.effort) meta.push(person.effort);
    if (person.official === true) meta.push('官方');
    if (meta.length) card.appendChild(joined('meta', meta));
    var bits = [];
    if (isNum(person.cost)) bits.push(money(person.cost));
    if (person.unpriced === true) bits.push('未定价');
    if (isNum(person.tokens)) bits.push(num(person.tokens) + ' tok');
    if (isNum(person.cacheHit)) bits.push('缓存 ' + num(person.cacheHit));
    if (isNum(person.toolCount)) bits.push('工具 ' + num(person.toolCount));
    if (person.duration) bits.push('用时 ' + person.duration);
    if (bits.length) card.appendChild(joined('bits', bits));
    if (person.current) card.appendChild(el('p', 'current', person.current));
    var badge = testLine(person.lastTest);
    if (badge) card.appendChild(badge);
    document.getElementById('member-cards').appendChild(card);
  }
  function fillList(id, rows, render, emptyText) {
    var list = document.getElementById(id);
    if (!rows || !rows.length) {
      list.appendChild(el('li', 'empty', emptyText));
      return;
    }
    rows.forEach(render);
  }
  function renderSession() {
    var bits = [];
    var session = data.session || {};
    if (session.project) bits.push(session.project);
    if (session.branch) bits.push(session.branch);
    if (session.source) bits.push(session.source);
    if (data.version) bits.push(data.version);
    if (isNum(data.generatedAt)) bits.push(fmtTime(data.generatedAt));
    bits.push('已脱敏');
    document.getElementById('session-line').textContent = bits.join(' · ');
    if (session.project) document.title = session.project + ' · 离线回放';
    if (data.synthetic === true) {
      var banner = document.getElementById('banner');
      banner.hidden = false;
      banner.textContent = '合成数据 · 不是用量基准。这个文件可以离线打开。';
    }
    if (data.notice) document.getElementById('notice').textContent = data.notice;
  }
  function renderCounts() {
    var counts = data.counts || {};
    var host = document.getElementById('departments');
    (data.departments || []).forEach(function (dept) {
      var bits = [dept.name || ''];
      if (isNum(dept.count)) bits.push(dept.count + ' 人');
      if (dept.statusLabel) bits.push(dept.statusLabel);
      if (isNum(dept.cost)) bits.push(money(dept.cost));
      host.appendChild(el('span', 'pill', bits.join(' · ')));
    });
    if (isNum(counts.stalls)) host.appendChild(el('span', 'pill warn', '停滞提醒 ' + num(counts.stalls)));
    if (isNum(counts.theaters)) host.appendChild(el('span', 'pill', '作战区 ' + num(counts.theaters)));
    if (isNum(counts.liveRetained)) host.appendChild(el('span', 'pill', '实时事件 ' + num(counts.liveRetained)));
  }
  function renderMetrics() {
    var host = document.getElementById('metrics');
    var metrics = data.metrics;
    if (!metrics || !metrics.totals) {
      host.appendChild(el('p', 'empty', '这份快照没有可核对的聚合用量。'));
      return;
    }
    var totals = metrics.totals;
    host.appendChild(stat('成员', num(metrics.memberCount)));
    var cost = stat('估算成本', money(totals.cost));
    if (isNum(totals.unpriced) && totals.unpriced > 0) cost.appendChild(el('p', 'meta', '部分 · 未定价 ' + num(totals.unpriced)));
    host.appendChild(cost);
    host.appendChild(stat('成员成本', money(totals.membersCost)));
    var allUsage = totals.allUsage || {};
    if (isNum(allUsage.total)) host.appendChild(stat('合计 token', num(allUsage.total)));
    if (isNum(allUsage.cacheHit)) host.appendChild(stat('缓存命中', num(allUsage.cacheHit)));
    var budget = data.budget;
    if (budget && isNum(budget.ratio)) {
      var label = '预算';
      if (budget.status === 'ok') label = '预算内';
      else if (budget.status === 'warn') label = '接近预算';
      else if (budget.status === 'over') label = '超出预算';
      var node = stat(label, num(budget.ratio * 100) + '%');
      if (budget.lowerBound === true) node.appendChild(el('p', 'meta', '成本为下限'));
      host.appendChild(node);
    }
  }
  function stat(label, value) {
    var card = el('article', 'stat');
    card.appendChild(el('p', 'k', label));
    card.appendChild(el('p', 'v', value));
    return card;
  }
  function bindRow(node, lane, index) {
    node.id = lane + '-' + index;
    node.tabIndex = 0;
    node.setAttribute('role', 'button');
    node.addEventListener('click', function () {
      pause();
      var found = storyIndex(lane, index);
      if (found >= 0) apply(found);
    });
    node.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        node.click();
      }
    });
  }
  function renderTimeline() {
    fillList('timeline', data.events, function (event, index) {
      var item = el('li');
      bindRow(item, 'event', index);
      var head = el('div', 'rowline');
      head.appendChild(el('time', '', fmtClock(event.ts)));
      head.appendChild(el('b', '', event.who || ''));
      if (event.kindLabel) head.appendChild(el('span', 'kind', event.kindLabel));
      item.appendChild(head);
      if (event.label) item.appendChild(el('p', '', event.label));
      if (event.error === true) item.appendChild(el('span', 'pill err', '出错'));
      var badge = testLine(event.test);
      if (badge) item.appendChild(badge);
      document.getElementById('timeline').appendChild(item);
      story.push({lane: 'event', index: index, ts: isNum(event.ts) ? event.ts : null, key: event.key || ''});
    }, '这份快照没有可回放的动作。');
    noteClipped('timeline', (data.clipped || {}).events, '较早的动作未列入');
  }
  function renderComms() {
    fillList('comms', data.comms, function (comm, index) {
      var item = el('li');
      bindRow(item, 'comm', index);
      var head = el('div', 'rowline');
      head.appendChild(el('time', '', fmtClock(comm.ts)));
      var who = comm.from || '';
      if (comm.to) who += ' → ' + comm.to;
      head.appendChild(el('b', '', who));
      if (comm.kindLabel) head.appendChild(el('span', 'kind', comm.kindLabel));
      item.appendChild(head);
      if (comm.text) item.appendChild(el('p', '', comm.text));
      document.getElementById('comms').appendChild(item);
      story.push({lane: 'comm', index: index, ts: isNum(comm.ts) ? comm.ts : null, key: comm.key || ''});
    }, '这份快照没有可显示的消息。');
    noteClipped('comms', (data.clipped || {}).comms, '较早的消息未列入');
  }
  function noteClipped(id, count, text) {
    if (!isNum(count) || count <= 0) return;
    var list = document.getElementById(id);
    list.insertBefore(el('li', 'empty', text + ' ' + num(count) + ' 条'), list.firstChild);
  }
  function renderTasks() {
    var clipped = data.clipped || {};
    fillList('tasks', data.tasks, function (task) {
      var item = el('li');
      var title = task.subject || '';
      if (task.id) title = task.id + '  ' + title;
      item.appendChild(el('b', '', title.trim()));
      var bits = [];
      if (task.statusLabel) bits.push(task.statusLabel);
      if (task.owner) bits.push(task.owner);
      if (task.blocked === true) bits.push('阻塞');
      if (bits.length) item.appendChild(el('p', 'meta', bits.join(' · ')));
      document.getElementById('tasks').appendChild(item);
    }, '这份快照没有可显示的任务。');
    if (isNum(clipped.tasks) && clipped.tasks > 0) {
      document.getElementById('tasks').appendChild(el('li', 'empty', '另有 ' + num(clipped.tasks) + ' 项未列入'));
    }
  }
  function storyIndex(lane, index) {
    for (var i = 0; i < story.length; i += 1) {
      if (story[i].lane === lane && story[i].index === index) return i;
    }
    return -1;
  }
  function highlight(pos) {
    var cards = document.querySelectorAll('#member-cards .card');
    var rows = document.querySelectorAll('#timeline li, #comms li');
    var i;
    for (i = 0; i < cards.length; i += 1) cards[i].classList.remove('on');
    for (i = 0; i < rows.length; i += 1) rows[i].classList.remove('on');
    var step = story[pos];
    if (!step) return;
    var row = document.getElementById(step.lane + '-' + step.index);
    if (row) {
      row.classList.add('on');
      if (row.scrollIntoView) row.scrollIntoView({block: 'nearest'});
    }
    if (!step.key) return;
    for (i = 0; i < cards.length; i += 1) {
      if (cards[i].getAttribute('data-key') === step.key) cards[i].classList.add('on');
    }
  }
  function apply(pos) {
    if (!story.length) return;
    if (pos < 0) pos = 0;
    if (pos >= story.length) pos = story.length - 1;
    cursor = pos;
    range.value = String(pos);
    posLabel.textContent = (pos + 1) + ' / ' + story.length;
    highlight(pos);
  }
  function pause() {
    if (timer) { clearInterval(timer); timer = null; }
    playBtn.textContent = '播放';
    playBtn.setAttribute('aria-pressed', 'false');
  }
  function play() {
    if (!story.length || timer) return;
    if (cursor < 0 || cursor >= story.length - 1) apply(0);
    if (cursor >= story.length - 1) return;
    playBtn.textContent = '播放中';
    playBtn.setAttribute('aria-pressed', 'true');
    timer = setInterval(function () {
      if (cursor >= story.length - 1) { pause(); return; }
      apply(cursor + 1);
    }, 800);
  }
  function step(delta) {
    if (!story.length) return;
    if (cursor < 0) apply(delta < 0 ? story.length - 1 : 0);
    else apply(cursor + delta);
  }
  renderSession();
  renderCounts();
  renderMetrics();
  if (data.lead) addCard(data.lead);
  (data.members || []).forEach(addCard);
  if (!data.lead && !(data.members && data.members.length)) {
    document.getElementById('member-cards').appendChild(el('p', 'empty', '这份快照没有可显示的成员。'));
  }
  if (data.clipped && isNum(data.clipped.members) && data.clipped.members > 0) {
    document.getElementById('member-cards').appendChild(el('p', 'more', '另有 ' + num(data.clipped.members) + ' 位未列入'));
  }
  renderTimeline();
  renderComms();
  renderTasks();
  story.sort(function (a, b) {
    var at = a.ts == null ? 1 : 0;
    var bt = b.ts == null ? 1 : 0;
    if (at !== bt) return at - bt;
    if (a.ts != null && b.ts != null && a.ts !== b.ts) return a.ts - b.ts;
    if (a.lane !== b.lane) return a.lane < b.lane ? -1 : 1;
    return a.index - b.index;
  });
  range.max = story.length ? String(story.length - 1) : '0';
  range.disabled = !story.length;
  playBtn.disabled = !story.length;
  pauseBtn.disabled = !story.length;
  posLabel.textContent = story.length ? ('0 / ' + story.length) : '没有可回放的步骤';
  playBtn.addEventListener('click', function () { if (timer) pause(); else play(); });
  pauseBtn.addEventListener('click', pause);
  range.addEventListener('input', function () { pause(); apply(Number(range.value)); });
  document.addEventListener('keydown', function (event) {
    var tag = event.target && event.target.tagName;
    if (tag === 'INPUT' || tag === 'BUTTON' || tag === 'LI') return;
    if (event.key === ' ' || event.key === 'Spacebar') { event.preventDefault(); if (timer) pause(); else play(); }
    else if (event.key === 'ArrowRight') { event.preventDefault(); pause(); step(1); }
    else if (event.key === 'ArrowLeft') { event.preventDefault(); pause(); step(-1); }
  });
  toggle.addEventListener('click', function () {
    root.setAttribute('data-theme', shownDark() ? 'light' : 'dark');
    refreshThemeButton();
  });
  refreshThemeButton();
})();
</script>
</body>
</html>
"""


def render_html(document):
    """Wrap an already-sanitized replay document in one offline page."""
    if not isinstance(document, dict):
        raise ExportError('replay document must be an object')
    if _PAGE.count('__REPLAY_PAYLOAD__') != 1:
        raise ExportError('replay page template is missing its data slot')
    return _PAGE.replace('__REPLAY_PAYLOAD__', embed_json(document))


def _same_file(input_path, output_path):
    try:
        same = input_path.resolve() == output_path.resolve()
        if not same and output_path.exists():
            same = input_path.samefile(output_path)
    except (OSError, RuntimeError):
        raise ExportError('cannot compare input and output files; check paths and permissions') from None
    if same:
        raise ExportError('output must differ from the snapshot input file')


def _write_atomic(output, text):
    """Keep the previous file intact until a complete replacement is ready."""
    temporary = None
    try:
        mode = output.stat().st_mode & 0o777 if output.exists() else None
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=str(output.parent), prefix='.replay-html-',
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        if mode is not None:
            os.chmod(str(temporary), mode)
        os.replace(str(temporary), str(output))
    except OSError:
        raise ExportError('cannot write replay; check directory and permissions') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path, help='offline board snapshot JSON file')
    source.add_argument('--port', type=_port, help='local board port, normally 7788')
    source.add_argument('--demo', action='store_true',
                        help='export the synthetic board fixture, examples/board-demo.synthetic.json')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--session', help='session ID prefix (requires --port)')
    selection.add_argument('--project', help='project key or substring (requires --port)')
    parser.add_argument('-o', '--output', type=Path, help='write the HTML file here instead of stdout')
    args = parser.parse_args(argv)
    if (args.session is not None or args.project is not None) and args.port is None:
        parser.error('--session and --project require --port')
    if any(value is not None and not value.strip() for value in (args.session, args.project)):
        parser.error('session and project selectors must be non-empty')
    try:
        input_path = DEMO_FIXTURE if args.demo else args.input
        if input_path is not None and args.output is not None:
            _same_file(input_path, args.output)
        if args.demo:
            snapshot = read_snapshot(DEMO_FIXTURE)
        else:
            snapshot = read_snapshot(args.input, args.port, session=args.session, project=args.project)
        html = render_html(build_replay(snapshot))
        if args.output:
            _write_atomic(args.output, html)
        elif hasattr(sys.stdout, 'buffer'):
            sys.stdout.buffer.write(html.encode('utf-8'))
        else:
            sys.stdout.write(html)
    except ExportError as exc:
        print('replay export failed: {}'.format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
