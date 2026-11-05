#!/usr/bin/env python3
"""Write an OTLP/JSON trace file from a board snapshot.

The file is a subset of ExportTraceServiceRequest. It copies timestamps,
estimated cost, token counts and test fields that are already on the
snapshot. It does not invent costs, call the network, or add dependencies.
See docs/INTEGRATIONS.md for the field list and the Langfuse / Phoenix
import commands.

``--demo`` reads examples/board-demo.synthetic.json. ``--input`` and
``--port`` reuse export_snapshot.read_snapshot. ``--session`` and
``--project`` select a loopback snapshot the same way.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

SCHEMA_VERSION = '1'
SCOPE_NAME = 'team-board.export_otel'
SERVICE_NAME = 'claude-agent-team'
MAX_SPANS = 400
_NAME_LIMIT = 48
_LABEL_LIMIT = 80
_TEXT_LIMIT = 160
_UINT64_MAX = 2 ** 64 - 1
_MAX_SECONDS = _UINT64_MAX // 1000000000
_ROOT = Path(__file__).resolve().parents[1]
_DEMO = _ROOT / 'examples' / 'board-demo.synthetic.json'

# Span.kind and Status.code. OTLP/JSON requires these as integers.
_SPAN_KIND_INTERNAL = 1
_STATUS_OK = 1
_STATUS_ERROR = 2

_ACTOR_STATUS = {
    'running', 'completed', 'stalled', 'error', 'idle', 'empty',
    'finished', 'coordinating', 'waiting', 'forming',
}
_EVENT_KINDS = {'tool', 'text', 'thinking'}
_COMM_KINDS = {
    'assign', 'report', 'relay', 'handoff', 'direct', 'peer', 'bus',
    'undelivered', 'error',
}
_TASK_STATUS = {'pending', 'in_progress', 'in progress', 'completed', 'deleted'}
_TEST_STATUS = {'pass', 'fail', 'unknown'}
_EXIT_STATUS = {'zero', 'nonzero', 'unknown'}
_USAGE_FIELDS = (
    ('in', 'input'),
    ('out', 'output'),
    ('cache_read', 'cache_read'),
    ('cache_write', 'cache_write'),
    ('total', 'total'),
)
_TEST_COUNTS = (
    ('passed', 'passed'),
    ('failed', 'failed'),
    ('skipped', 'skipped'),
    ('errors', 'errors'),
    ('exitCode', 'exit_code'),
)

# Same home-path, email and credential categories as export_replay.py.
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
_LEFTOVER = re.compile(
    r'(?:/Users/|/home/|[A-Za-z]:[/\\]+Users[/\\]|~[/\\]'
    r'|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'
    r'|sk-ant-|sk-[A-Za-z0-9][A-Za-z0-9_\-]{7,}|ghp_|github_pat_|xox[baprs]-'
    r'|AKIA[0-9A-Z]{8,}|BEGIN [A-Z ]*PRIVATE KEY|api[_-]?key\s*[:=]'
    r'|Bearer\s+[A-Za-z0-9._\-]+)',
    re.IGNORECASE)
_HEX_ID = re.compile(r'[0-9a-fA-F]{16,}')
_UUID = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
_TOOL_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_+-]{0,40}$')
_HOOK_EVENT = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,40}$')
_FRAMEWORK = re.compile(r'^[A-Za-z][A-Za-z0-9_+-]{0,20}$')
_MARKS = ('[path]', '[email]', '[redacted]')


def _load_exporter():
    path = Path(__file__).resolve().parent / 'export_snapshot.py'
    name = 'export_otel_export_snapshot'
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


def _token_name(match):
    text = match.group(0)
    if '@' in text and 'BEGIN' not in text.upper() and not text.lower().startswith('bearer'):
        return '[email]'
    return '[redacted]'


def redact_text(value, limit):
    """Replace private fragments, then clip. A secret past the limit cannot survive."""
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


def _visible_text(value, limit):
    text = redact_text(value, limit)
    if not text or text in _MARKS:
        return None
    if _LEFTOVER.search(text) or _HEX_ID.search(text) or _UUID.search(text):
        return None
    if 'javascript:' in text.lower():
        return None
    return text


def _public_name(value, limit):
    """A short label safe to store, or None when it is missing or private."""
    text = _visible_text(value, limit)
    if not text or any(mark in text for mark in _MARKS):
        return None
    if any(ch in text for ch in '/\\@<>'):
        return None
    if text.startswith('~') or '://' in text:
        return None
    return text


def _enum(value, allowed):
    if isinstance(value, str) and value in allowed:
        return value
    return None


def _safe_token(value, pattern):
    """Accept a short identifier only when it is not itself a secret or a path."""
    if not isinstance(value, str) or not pattern.match(value):
        return None
    if _TOKEN.search(value) or _LEFTOVER.search(value) or _PATH.search(value):
        return None
    return value


def _append(attrs, item):
    if item is not None:
        attrs.append(item)


def _attr_str(key, value):
    if not isinstance(value, str) or not value:
        return None
    return {'key': key, 'value': {'stringValue': value}}


def _attr_bool(key, value):
    if type(value) is not bool:
        return None
    return {'key': key, 'value': {'boolValue': value}}


def _attr_int(key, value):
    if type(value) is not int or value < 0 or value > 2 ** 63 - 1:
        return None
    return {'key': key, 'value': {'intValue': str(value)}}


def _attr_num(key, value):
    if type(value) is bool or type(value) not in (int, float):
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    if value < 0:
        return None
    return {'key': key, 'value': {'doubleValue': value}}


def _attr_ratio(key, value):
    attr = _attr_num(key, value)
    if attr is None or value > 1:
        return None
    return attr


def _seconds_to_nano(value):
    """Unix seconds already on the snapshot, as an integer nanosecond count."""
    if type(value) is bool:
        return None
    if type(value) is int:
        seconds = value
    elif type(value) is float and math.isfinite(value) and value.is_integer():
        seconds = int(value)
    else:
        return None
    if seconds < 0 or seconds > _MAX_SECONDS:
        return None
    return seconds * 1000000000


def _end_with_ms(start, item):
    """Extend a tool event only when that same object has a nonnegative integer ``ms``."""
    if 'ms' not in item:
        return start
    ms = item.get('ms')
    if type(ms) is not int or ms < 0:
        return start
    extra = ms * 1000000
    if extra > _UINT64_MAX - start:
        return None
    return start + extra


def _hex_id(seed, nbytes):
    """Lowercase hex id. All-zero ids are reserved by OTLP, so they are rehashed."""
    text = '00' * nbytes
    step = 0
    while text == '00' * nbytes:
        digest = hashlib.sha256(seed.encode('utf-8')).digest()
        text = digest[:nbytes].hex()
        step += 1
        seed = seed + '\n' + str(step)
    return text


def _cost_attributes(obj):
    attrs = []
    cost_attr = None
    if 'cost' in obj and obj.get('cost') is not None:
        cost_attr = _attr_num('team.cost.usd', obj.get('cost'))
        _append(attrs, cost_attr)
    unpriced = obj.get('unpriced')
    if 'unpriced' in obj and type(unpriced) is int and unpriced >= 0:
        _append(attrs, _attr_int('team.cost.unpriced', unpriced))
        if unpriced > 0 and cost_attr is not None:
            attrs.append({'key': 'team.cost.partial', 'value': {'boolValue': True}})
    return attrs


def _token_attributes(obj):
    attrs = []
    if 'totalTokens' in obj:
        _append(attrs, _attr_int('team.tokens.total', obj.get('totalTokens')))
    if 'outputTokens' in obj:
        _append(attrs, _attr_int('team.tokens.output', obj.get('outputTokens')))
    if 'toolCount' in obj:
        _append(attrs, _attr_int('team.tool_count', obj.get('toolCount')))
    if 'cacheHit' in obj:
        _append(attrs, _attr_ratio('team.cache_hit', obj.get('cacheHit')))
    usage = obj.get('usage')
    if isinstance(usage, dict):
        for source, dest in _USAGE_FIELDS:
            if source in usage:
                _append(attrs, _attr_int('team.usage.' + dest, usage.get(source)))
    return attrs


def _test_attributes(test):
    if not isinstance(test, dict):
        return []
    attrs = []
    _append(attrs, _attr_str('team.test.framework', _safe_token(test.get('framework'), _FRAMEWORK)))
    _append(attrs, _attr_str('team.test.status', _enum(test.get('status'), _TEST_STATUS)))
    for source, dest in _TEST_COUNTS:
        if source in test and test.get(source) is not None:
            _append(attrs, _attr_int('team.test.' + dest, test.get(source)))
    _append(attrs, _attr_str('team.test.exit_status', _enum(test.get('exitStatus'), _EXIT_STATUS)))
    if 'truncated' in test:
        _append(attrs, _attr_bool('team.test.truncated', test.get('truncated')))
    return attrs


def _iter_actors(snapshot):
    lead = snapshot.get('lead')
    if isinstance(lead, dict):
        yield lead
    members = snapshot.get('members')
    if isinstance(members, list):
        for member in members:
            if isinstance(member, dict):
                yield member


def _names_by_id(snapshot):
    """Map member id to display name. The id itself is never written out."""
    found = {}
    for actor in _iter_actors(snapshot):
        ident = actor.get('id')
        name = actor.get('name')
        if isinstance(ident, str) and isinstance(name, str) and ident not in found:
            found[ident] = name
    return found


def _lookup_name(item, names):
    for key in ('memberId', 'agent_id'):
        ident = item.get(key)
        if isinstance(ident, str) and ident in names:
            return names[ident]
    return None


def _who_and_tool(item, fallback, names):
    raw = item.get('name')
    tool = _safe_token(raw, _TOOL_NAME)
    if tool:
        who = fallback if fallback is not None else _lookup_name(item, names)
        return who, tool
    if isinstance(raw, str):
        return raw, None
    if fallback is not None:
        return fallback, None
    return _lookup_name(item, names), None


def _record(name, start, end, attributes, status_code, group, order, actor_name=None, parent_name=None):
    return {
        'name': name,
        'start': str(start),
        'end': str(end),
        'start_i': start,
        'end_i': end,
        'attributes': attributes,
        'status_code': status_code,
        'group': group,
        'order': order,
        'actor_name': actor_name,
        'parent_name': parent_name,
    }


def _actor_status(actor):
    status = actor.get('status')
    if status == 'error':
        return _STATUS_ERROR
    if status in ('completed', 'finished'):
        return _STATUS_OK
    return None


def _actor_span(actor, kind, order):
    if 'startedAt' not in actor or 'endedAt' not in actor:
        return None
    start = _seconds_to_nano(actor.get('startedAt'))
    end = _seconds_to_nano(actor.get('endedAt'))
    if start is None or end is None or end < start:
        return None
    public = _public_name(actor.get('name'), _NAME_LIMIT)
    attrs = []
    _append(attrs, _attr_str('team.actor.kind', kind))
    _append(attrs, _attr_str('team.actor', public))
    _append(attrs, _attr_str('team.dept', _public_name(actor.get('dept'), 24)))
    _append(attrs, _attr_str('team.role', _public_name(actor.get('roleTitle'), _NAME_LIMIT)))
    _append(attrs, _attr_str('team.status', _enum(actor.get('status'), _ACTOR_STATUS)))
    attrs.extend(_cost_attributes(actor))
    attrs.extend(_token_attributes(actor))
    return _record(
        public or kind, start, end, attrs, _actor_status(actor), 0, order, actor_name=public)


def _actor_spans(snapshot):
    spans = []
    order = 0
    lead = snapshot.get('lead')
    if isinstance(lead, dict):
        span = _actor_span(lead, 'lead', order)
        if span is not None:
            spans.append(span)
            order += 1
    members = snapshot.get('members')
    if isinstance(members, list):
        for member in members:
            if not isinstance(member, dict):
                continue
            span = _actor_span(member, 'member', order)
            if span is not None:
                spans.append(span)
                order += 1
    return spans


def _event_status(item):
    test = item.get('test')
    test_status = test.get('status') if isinstance(test, dict) else None
    if item.get('error') is True or test_status == 'fail':
        return _STATUS_ERROR
    if test_status == 'unknown':
        return None
    if test_status == 'pass' or (item.get('done') is True and item.get('error') is False):
        return _STATUS_OK
    return None


def _activity_rows(snapshot, names):
    feed = snapshot.get('feed')
    if isinstance(feed, list):
        rows = []
        for index, item in enumerate(feed):
            if isinstance(item, dict):
                who, tool = _who_and_tool(item, None, names)
                rows.append((item, who, tool, index))
        return rows
    rows = []
    index = 0
    for actor in _iter_actors(snapshot):
        timeline = actor.get('timeline')
        if not isinstance(timeline, list):
            continue
        for item in timeline:
            if isinstance(item, dict):
                who, tool = _who_and_tool(item, actor.get('name'), names)
                rows.append((item, who, tool, index))
                index += 1
    return rows


def _activity_spans(snapshot, names):
    spans = []
    for item, who, tool, order in _activity_rows(snapshot, names):
        if 'ts' not in item:
            continue
        start = _seconds_to_nano(item.get('ts'))
        if start is None:
            continue
        end = _end_with_ms(start, item)
        if end is None:
            continue
        kind = _enum(item.get('kind'), _EVENT_KINDS)
        label = _visible_text(item.get('label'), _LABEL_LIMIT)
        if label:
            name = label
        elif tool:
            name = tool
        elif kind:
            name = kind
        else:
            name = 'event'
        attrs = []
        _append(attrs, _attr_str('team.actor', _public_name(who, _NAME_LIMIT)))
        _append(attrs, _attr_str('team.event.kind', kind))
        if 'done' in item:
            _append(attrs, _attr_bool('team.event.done', item.get('done')))
        if 'error' in item:
            _append(attrs, _attr_bool('team.event.error', item.get('error')))
        _append(attrs, _attr_str('team.tool.name', tool))
        attrs.extend(_test_attributes(item.get('test')))
        spans.append(_record(
            name, start, end, attrs, _event_status(item), 1, order, parent_name=who))
    return spans


def _comm_spans(snapshot):
    comms = snapshot.get('comms')
    if not isinstance(comms, list):
        return []
    spans = []
    for order, item in enumerate(comms):
        if not isinstance(item, dict) or 'ts' not in item:
            continue
        start = _seconds_to_nano(item.get('ts'))
        if start is None:
            continue
        kind = _enum(item.get('kind'), _COMM_KINDS)
        attrs = []
        _append(attrs, _attr_str('team.comm.kind', kind))
        _append(attrs, _attr_str('team.comm.from', _public_name(item.get('fromName'), _NAME_LIMIT)))
        _append(attrs, _attr_str('team.comm.to', _public_name(item.get('toName'), _NAME_LIMIT)))
        _append(attrs, _attr_str('team.comm.text', _visible_text(item.get('text'), _TEXT_LIMIT)))
        status = _STATUS_ERROR if kind == 'error' else None
        spans.append(_record(
            kind or 'message', start, start, attrs, status, 2, order, parent_name=item.get('fromName')))
    return spans


def _task_spans(snapshot):
    team = snapshot.get('officialTeam')
    tasks = team.get('tasks') if isinstance(team, dict) else None
    if not isinstance(tasks, list):
        return []
    spans = []
    for order, item in enumerate(tasks):
        if not isinstance(item, dict) or 'stateSince' not in item:
            continue
        start = _seconds_to_nano(item.get('stateSince'))
        if start is None:
            continue
        subject = _visible_text(item.get('subject'), _LABEL_LIMIT)
        status = _enum(item.get('normalizedStatus'), _TASK_STATUS)
        if status is None:
            status = _enum(item.get('status'), _TASK_STATUS)
        attrs = []
        _append(attrs, _attr_str('team.task.subject', subject))
        _append(attrs, _attr_str('team.task.status', status))
        if 'blocked' in item:
            _append(attrs, _attr_bool('team.task.blocked', item.get('blocked')))
        _append(attrs, _attr_str('team.task.owner', _public_name(item.get('owner'), _NAME_LIMIT)))
        spans.append(_record(
            subject or 'task', start, start, attrs, None, 3, order, parent_name=item.get('owner')))
    return spans


def _hook_spans(snapshot, names):
    live = snapshot.get('live')
    if not isinstance(live, dict) or live.get('merged') is True:
        return []
    events = live.get('events')
    if not isinstance(events, list):
        return []
    spans = []
    for order, item in enumerate(events):
        if not isinstance(item, dict) or 'ts' not in item:
            continue
        start = _seconds_to_nano(item.get('ts'))
        if start is None:
            continue
        event = _safe_token(item.get('event'), _HOOK_EVENT)
        tool = _safe_token(item.get('tool_name'), _TOOL_NAME)
        if event and tool:
            name = event + ' ' + tool
        else:
            name = event or tool or 'hook'
        who = _lookup_name(item, names)
        attrs = []
        _append(attrs, _attr_str('team.actor', _public_name(who, _NAME_LIMIT)))
        _append(attrs, _attr_str('team.hook.event', event))
        _append(attrs, _attr_str('team.tool.name', tool))
        spans.append(_record(name, start, start, attrs, None, 4, order, parent_name=who))
    return spans


def _span_sort_key(span):
    when = 0 if span['group'] == 0 else span['start_i']
    return (span['group'], when, span['order'])


def _find_parent(parents, raw_name, when):
    name = _public_name(raw_name, _NAME_LIMIT)
    if not name:
        return None
    for parent in parents:
        if parent.get('actor_name') == name and parent['start_i'] <= when <= parent['end_i']:
            return parent
    return None


def _content_part(span):
    payload = {
        'name': span['name'],
        'start': span['start'],
        'end': span['end'],
        'attributes': span['attributes'],
        'status': span.get('status_code'),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _public_span(trace_id, span):
    public = {
        'traceId': trace_id,
        'spanId': span['span_id'],
    }
    parent = span.get('parent')
    if parent is not None:
        public['parentSpanId'] = parent['span_id']
    public['name'] = span['name']
    public['kind'] = _SPAN_KIND_INTERNAL
    public['startTimeUnixNano'] = span['start']
    public['endTimeUnixNano'] = span['end']
    if span['attributes']:
        public['attributes'] = span['attributes']
    if span.get('status_code') in (_STATUS_OK, _STATUS_ERROR):
        public['status'] = {'code': span['status_code']}
    return public


def _metric_attributes(snapshot):
    try:
        metrics = export_metrics(snapshot)
    except ExportError:
        return []
    attrs = []
    totals = metrics['totals']
    _append(attrs, _attr_int('team.member_count', metrics['memberCount']))
    _append(attrs, _attr_num('team.totals.cost', totals['cost']))
    _append(attrs, _attr_num('team.totals.members_cost', totals['membersCost']))
    _append(attrs, _attr_int('team.totals.unpriced', totals['unpriced']))
    _append(attrs, _attr_int('team.totals.output_tokens', totals['tokens']))
    scopes = (('members', 'usage'), ('lead', 'leadUsage'), ('all', 'allUsage'))
    for scope, group in scopes:
        usage = totals[group]
        for source, dest in _USAGE_FIELDS:
            _append(attrs, _attr_int('team.usage.' + scope + '.' + dest, usage[source]))
        _append(attrs, _attr_ratio('team.usage.' + scope + '.cache_hit', usage['cacheHit']))
    return attrs


def _budget_attribute(snapshot):
    budget = snapshot.get('budget')
    if not isinstance(budget, dict) or budget.get('enabled') is not True or 'ratio' not in budget:
        return None
    if budget.get('ratio') is None:
        return None
    return _attr_num('team.budget.utilization_ratio', budget.get('ratio'))


def _resource_attributes(snapshot, omitted):
    attrs = [
        {'key': 'service.name', 'value': {'stringValue': SERVICE_NAME}},
        {'key': 'team.export.schema', 'value': {'stringValue': SCHEMA_VERSION}},
        {'key': 'team.export.signal', 'value': {'stringValue': 'traces'}},
    ]
    version = _public_name(snapshot.get('version'), 32)
    if version:
        attrs.append({'key': 'service.version', 'value': {'stringValue': version}})
    if 'demo' in snapshot and type(snapshot.get('demo')) is bool:
        attrs.append({'key': 'team.snapshot.demo', 'value': {'boolValue': snapshot.get('demo')}})
    if 'generatedAt' in snapshot:
        generated = _seconds_to_nano(snapshot.get('generatedAt'))
        if generated is not None:
            attrs.append({
                'key': 'team.snapshot.generated_at_unix_nano',
                'value': {'stringValue': str(generated)},
            })
    attrs.extend(_metric_attributes(snapshot))
    _append(attrs, _budget_attribute(snapshot))
    if omitted > 0:
        _append(attrs, _attr_int('team.export.omitted_spans', omitted))
    return attrs


def build_otlp(snapshot, max_spans=MAX_SPANS):
    """Build one OTLP/JSON trace document from fields already on ``snapshot``."""
    if not isinstance(snapshot, dict):
        raise ExportError('snapshot must be a JSON object')
    if type(max_spans) is not int or max_spans < 0:
        raise ExportError('span limit must be a nonnegative integer')
    names = _names_by_id(snapshot)
    collected = []
    collected.extend(_actor_spans(snapshot))
    collected.extend(_activity_spans(snapshot, names))
    collected.extend(_comm_spans(snapshot))
    collected.extend(_task_spans(snapshot))
    collected.extend(_hook_spans(snapshot, names))
    ordered = sorted(collected, key=_span_sort_key)
    kept = ordered[:max_spans]
    omitted = len(ordered) - len(kept)
    parents = [span for span in kept if span.get('actor_name')]
    for span in kept:
        if span.get('actor_name'):
            span['parent'] = None
        else:
            span['parent'] = _find_parent(parents, span.get('parent_name'), span['start_i'])
    parts = []
    for index, span in enumerate(kept):
        part = _content_part(span)
        parts.append(part)
        span['span_id'] = _hex_id('span\n{}\n{}'.format(index, part), 8)
    trace_id = _hex_id('trace\n' + '\n'.join(parts), 16)
    spans = [_public_span(trace_id, span) for span in kept]
    return {
        'resourceSpans': [{
            'resource': {'attributes': _resource_attributes(snapshot, omitted)},
            'scopeSpans': [{
                'scope': {'name': SCOPE_NAME, 'version': SCHEMA_VERSION},
                'spans': spans,
            }],
        }],
    }


def render_otlp(snapshot, max_spans=MAX_SPANS):
    """Serialize ``build_otlp`` and refuse a document that still matches a private pattern."""
    document = build_otlp(snapshot, max_spans=max_spans)
    try:
        text = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
    except (TypeError, ValueError):
        raise ExportError('export produced a non-finite number') from None
    if _LEFTOVER.search(text):
        raise ExportError('refusing to write an export that still matches a private-data pattern')
    return text


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
                                         dir=str(output.parent), prefix='.otel-json-',
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        if mode is not None:
            os.chmod(str(temporary), mode)
        os.replace(str(temporary), str(output))
    except OSError:
        raise ExportError('cannot write OTLP export; check directory and permissions') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--demo', action='store_true', help='read examples/board-demo.synthetic.json')
    source.add_argument('--input', type=Path, help='offline board snapshot JSON file')
    source.add_argument('--port', type=_port, help='local board port, normally 7788')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--session', help='session ID prefix (requires --port)')
    selection.add_argument('--project', help='project key or substring (requires --port)')
    parser.add_argument('-o', '--output', type=Path, help='write the OTLP JSON here instead of stdout')
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
        text = render_otlp(snapshot)
        if args.output:
            _write_atomic(args.output, text)
        elif hasattr(sys.stdout, 'buffer'):
            sys.stdout.buffer.write(text.encode('utf-8'))
        else:
            sys.stdout.write(text)
    except ExportError as exc:
        print('otel export failed: {}'.format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
