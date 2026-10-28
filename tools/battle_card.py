#!/usr/bin/env python3
"""Render a self-contained SVG battle card from a board snapshot.

The card copies only fields that are already on the snapshot. Missing sections
are left out. Aggregate cost, token and cache figures come from
export_snapshot.export_metrics so they cannot drift from the metrics export.
Local paths, session ids, e-mail addresses, prompts and tool arguments are not
drawn. Run ``python tools/battle_card.py --help`` for the offline file and
loopback-port modes; both reuse export_snapshot.read_snapshot.
"""
import argparse
import importlib.util
import math
import re
import sys
import time
from pathlib import Path

WIDTH = 960
PAD = 36
INNER = WIDTH - PAD * 2
MAX_MEMBERS = 8
MAX_DEPARTMENTS = 6
MAX_TASKS = 6
_SANS = 'Segoe UI, PingFang SC, Noto Sans SC, Microsoft YaHei, sans-serif'
_MONO = 'ui-monospace, Cascadia Mono, SF Mono, Consolas, monospace'
_EMAIL = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
_HEX_ID = re.compile(r'[0-9a-fA-F]{16,}')
_UUID = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
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
_PALETTES = {
    'light': {
        'paper': '#e7e9ed', 'card': '#ffffff', 'ink': '#131722', 'muted': '#465066',
        'faint': '#858c9d', 'line': '#d2d6de', 'accent': '#9c7818', 'ok': '#2b8466',
        'run': '#1c74a8', 'warn': '#b36f0b',
    },
    'dark': {
        'paper': '#070a11', 'card': '#141926', 'ink': '#eceae3', 'muted': '#a6acba',
        'faint': '#676f83', 'line': '#232a3a', 'accent': '#d3b35c', 'ok': '#62b596',
        'run': '#5fb1e3', 'warn': '#e3a444',
    },
}


def _load_exporter():
    path = Path(__file__).resolve().parent / 'export_snapshot.py'
    name = 'battle_card_export_snapshot'
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


def xml_escape(value):
    """Escape text for an XML text node or attribute."""
    return (str(value)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&apos;'))


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
    if '://' in text or re.match(r'^[A-Za-z]:', text):
        return None
    if _EMAIL.search(text) or _HEX_ID.search(text) or _UUID.search(text):
        return None
    if re.search(r'(?i)javascript\s*:', text) or re.search(r'(?i)\bsk-[A-Za-z0-9]', text):
        return None
    return text


def _status_label(value):
    if not isinstance(value, str):
        return None
    if value in _STATUS:
        return _STATUS[value]
    return safe_text(value, 24)


def _positive(value, kind):
    if kind == 'count':
        return _count(value) and value > 0
    if kind == 'ratio':
        return _ratio(value) and value > 0
    return _cost(value) and value > 0


def format_seconds(value):
    if type(value) is float and not value.is_integer():
        return num_text(value) + 's'
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


def _duration(obj):
    if 'startedAt' not in obj or 'endedAt' not in obj:
        return None
    start, end = obj['startedAt'], obj['endedAt']
    if not _cost(start) or not _cost(end) or end < start:
        return None
    return end - start


def _money(obj, key):
    """Return 'missing', 'unpriced' or a number. Invalid values are dropped."""
    if key not in obj:
        return 'missing'
    value = obj[key]
    if value is None:
        return 'unpriced'
    if _cost(value):
        return value
    return 'missing'


def _dept_label(snapshot, dept):
    if not isinstance(dept, str):
        return None
    departments = snapshot.get('departments')
    if isinstance(departments, list):
        for item in departments:
            if isinstance(item, dict) and item.get('id') == dept:
                name = safe_text(item.get('name'), 32) if isinstance(item.get('name'), str) else None
                return name or safe_text(dept, 32)
    return safe_text(dept, 32)


def _icon(value):
    icon = safe_text(value, 4) if isinstance(value, str) else None
    if icon and len(icon) <= 2:
        return icon
    return None


def _format_generated(value):
    if not _finite(value) or value < 0 or value > 10000000000:
        return None
    try:
        return time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(int(value)))
    except (OverflowError, OSError, ValueError):
        return None


def _format_version(value):
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}', value):
        return value
    return None


def _metrics(snapshot):
    totals = snapshot.get('totals')
    members = snapshot.get('members')
    needed = ('cost', 'membersCost', 'unpriced', 'tokens', 'usage', 'leadUsage', 'allUsage')
    if isinstance(totals, dict) and isinstance(members, list) and all(key in totals for key in needed):
        return export_metrics(snapshot)
    return None


def _member_dicts(snapshot):
    members = snapshot.get('members')
    if isinstance(members, list) and all(isinstance(item, dict) for item in members):
        return members
    return []


def _person_bits(obj, title):
    bits = []
    role = safe_text(obj.get('roleTitle'), 32) if 'roleTitle' in obj else None
    if role is None and 'agentType' in obj:
        role = safe_text(obj.get('agentType'), 32)
    if role and role != title:
        bits.append(role)
    if obj.get('officialTeam') is True:
        bits.append('官方')
    family = safe_text(obj.get('modelFamily'), 24) if 'modelFamily' in obj else None
    if family:
        bits.append(family)
    effort = safe_text(obj.get('effort'), 16) if 'effort' in obj else None
    if effort:
        bits.append(effort)
    money = _money(obj, 'cost')
    if money == 'unpriced':
        bits.append('未定价')
    elif money != 'missing' and money > 0:
        bits.append('$' + num_text(money))
    if _positive(obj.get('totalTokens'), 'count'):
        bits.append(num_text(obj['totalTokens']) + ' tok')
    if _positive(obj.get('cacheHit'), 'ratio'):
        bits.append('缓存 ' + num_text(obj['cacheHit']))
    if _positive(obj.get('toolCount'), 'count'):
        bits.append('工具 ' + num_text(obj['toolCount']))
    writes = obj.get('writes') if 'writes' in obj else None
    if isinstance(writes, list) and writes and all(isinstance(item, dict) for item in writes):
        bits.append('写入 ' + num_text(len(writes)))
    duration = _duration(obj)
    rendered = format_seconds(duration) if duration is not None else None
    if rendered:
        bits.append('用时 ' + rendered)
    return bits


def _lead_row(snapshot):
    lead = snapshot.get('lead')
    if not isinstance(lead, dict):
        return None
    name = safe_text(lead.get('name'), 32) if 'name' in lead else None
    phase = _status_label(lead.get('phase')) if 'phase' in lead else None
    bits = []
    for key, label in (('running', '进行中'), ('done', '已完成'), ('total', '成员')):
        if key in lead and _positive(lead.get(key), 'count'):
            bits.append(num_text(lead[key]) + ' ' + label)
    bits.extend(_person_bits(lead, name or ''))
    if not name and not phase and not bits:
        return None
    return {'title': name or '队长', 'status': phase, 'bits': bits}


def _department_rows(snapshot):
    departments = snapshot.get('departments')
    if not isinstance(departments, list):
        return []
    rows = []
    for dept in departments:
        if not isinstance(dept, dict):
            continue
        name = safe_text(dept.get('name'), 32) if 'name' in dept else None
        if name is None and 'id' in dept:
            name = safe_text(dept.get('id'), 24)
        if not name:
            continue
        icon = _icon(dept.get('icon')) if 'icon' in dept else None
        title = (icon + ' ' + name) if icon else name
        status = _status_label(dept.get('status')) if 'status' in dept else None
        members = dept.get('members') if 'members' in dept else None
        count = len(members) if isinstance(members, list) else None
        active = (count or 0) > 0 or status in ('进行中', '已完成', '停滞', '出错', '收尾完成', '协调中')
        bits = []
        if count is not None:
            bits.append(num_text(count) + ' 人')
        money = _money(dept, 'cost')
        if money == 'unpriced':
            bits.append('未定价')
            active = True
        elif money != 'missing' and money > 0:
            bits.append('$' + num_text(money))
            active = True
        if _positive(dept.get('tokens'), 'count'):
            bits.append(num_text(dept['tokens']) + ' tok')
            active = True
        if _positive(dept.get('cacheHit'), 'ratio') and _positive(dept.get('tokens'), 'count'):
            bits.append('缓存 ' + num_text(dept['cacheHit']))
        rendered = format_seconds(dept['seconds']) if 'seconds' in dept and _count(dept.get('seconds')) else None
        if rendered:
            bits.append('用时 ' + rendered)
            active = True
        for key, label in (('running', '进行中'), ('done', '完成'), ('failed', '失败'), ('tools', '工具'), ('unpriced', '未定价')):
            if key in dept and _positive(dept.get(key), 'count'):
                bits.append(label + ' ' + num_text(dept[key]))
                active = True
        models = dept.get('models') if 'models' in dept else None
        if isinstance(models, list) and models:
            names = []
            safe_models = True
            for item in models:
                label = safe_text(item, 24)
                if not label:
                    safe_models = False
                    break
                names.append(label)
            if safe_models and names:
                bits.append('、'.join(names))
                active = True
        if active:
            rows.append({'title': title, 'status': status, 'bits': bits})
    return rows


def _member_rows(snapshot):
    rows = []
    for index, member in enumerate(_member_dicts(snapshot), 1):
        name = safe_text(member.get('name'), 32) if 'name' in member else None
        title = name or '成员 {:02d}'.format(index)
        dept = _dept_label(snapshot, member.get('dept')) if 'dept' in member else None
        bits = _person_bits(member, title)
        if dept and dept not in bits and dept != title:
            bits.insert(0, dept)
        status = _status_label(member.get('status')) if 'status' in member else None
        if name or status or bits:
            rows.append({'title': title, 'status': status, 'bits': bits})
    return rows


def _task_rows(snapshot):
    official = snapshot.get('officialTeam')
    if not isinstance(official, dict) or not isinstance(official.get('tasks'), list):
        return []
    rows = []
    for task in official['tasks']:
        if not isinstance(task, dict):
            continue
        if 'normalizedStatus' in task:
            state = task.get('normalizedStatus')
        elif 'status' in task:
            state = task.get('status')
        else:
            continue
        if state != 'completed':
            continue
        title = safe_text(task.get('subject'), 72) if 'subject' in task else None
        owner = safe_text(task.get('owner'), 32) if isinstance(task.get('owner'), str) else None
        bits = ['负责 ' + owner] if owner else []
        rows.append({'title': title or '已完成', 'status': '已完成', 'bits': bits})
    return rows


def _summary(snapshot, metrics):
    members = _member_dicts(snapshot)
    if metrics is not None:
        count = metrics['memberCount']
    elif members:
        count = len(members)
    else:
        count = None
    parts = []
    if count is not None:
        parts.append('{} 名成员'.format(num_text(count)))
    seen = {}
    for member in members:
        if 'status' not in member:
            continue
        label = _STATUS.get(member.get('status'))
        if label:
            seen[member['status']] = seen.get(member['status'], 0) + 1
    for key in ('completed', 'running', 'stalled', 'error'):
        if seen.get(key):
            parts.append('{} {}'.format(num_text(seen[key]), _STATUS[key]))
    return parts


def _tiles(metrics, summary_count):
    if metrics is None:
        if summary_count is None:
            return []
        return [{'label': '成员', 'value': num_text(summary_count)}]
    totals = metrics['totals']
    partial = totals['unpriced'] > 0
    return [
        {'label': '成员', 'value': num_text(metrics['memberCount'])},
        {'label': '估算成本 · 部分' if partial else '估算成本', 'value': '$' + num_text(totals['cost'])},
        {'label': '合计 token', 'value': num_text(totals['allUsage']['total'])},
        {'label': '缓存命中', 'value': num_text(totals['allUsage']['cacheHit'])},
    ]


def _usage(metrics):
    if metrics is None:
        return None
    groups = [metrics['totals'][name] for name in ('usage', 'leadUsage', 'allUsage')]
    rows = []
    for label, key in (
            ('输入', 'in'), ('输出', 'out'), ('缓存读', 'cache_read'),
            ('缓存写', 'cache_write'), ('合计', 'total'), ('缓存命中', 'cacheHit')):
        rows.append({'label': label, 'cells': [num_text(group[key]) for group in groups]})
    totals = metrics['totals']
    note = '成员成本 {} · 成员输出 {} · 未定价 {}'.format(
        num_text(totals['membersCost']), num_text(totals['tokens']), num_text(totals['unpriced']))
    if totals['unpriced'] > 0:
        note += ' · 部分'
    return {'columns': ['成员', '队长', '合计'], 'rows': rows, 'note': note}


def public_view(snapshot):
    """Allowlisted display model. Raises ExportError when present totals are invalid."""
    if not isinstance(snapshot, dict):
        raise ExportError('snapshot must be an object')
    metrics = _metrics(snapshot)
    members = _member_rows(snapshot)
    departments = _department_rows(snapshot)
    tasks = _task_rows(snapshot)
    summary = _summary(snapshot, metrics)
    count = metrics['memberCount'] if metrics is not None else (len(_member_dicts(snapshot)) or None)
    right = []
    if snapshot.get('demo') is True:
        right.append('合成数据')
    version = _format_version(snapshot.get('version')) if 'version' in snapshot else None
    if version:
        right.append('v' + version)
    generated = _format_generated(snapshot.get('generatedAt')) if 'generatedAt' in snapshot else None
    if generated:
        right.append(generated)
    return {
        'right': right,
        'summary': summary,
        'tiles': _tiles(metrics, count if members or metrics is not None else None),
        'usage': _usage(metrics),
        'partial': bool(metrics and metrics['totals']['unpriced'] > 0),
        'lead': _lead_row(snapshot),
        'departments': departments[:MAX_DEPARTMENTS],
        'more_departments': max(0, len(departments) - MAX_DEPARTMENTS),
        'members': members[:MAX_MEMBERS],
        'more_members': max(0, len(members) - MAX_MEMBERS),
        'tasks': tasks[:MAX_TASKS],
        'more_tasks': max(0, len(tasks) - MAX_TASKS),
    }


def _describe(view):
    parts = ['Agent Team 战报。只包含快照里已有的字段。']
    if view['summary']:
        parts.append(' · '.join(view['summary']) + '。')
    usage = view['usage']
    if usage:
        parts.append(usage['note'] + '。')
    if view['partial']:
        parts.append('成本为部分估算。')
    elif usage:
        parts.append('成本是估算，不是服务商账单。')
    return ''.join(parts)


def _stylesheet(theme, static):
    lines = [
        'text { font-family: "Segoe UI", "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif; }',
        '.mono { font-family: ui-monospace, "Cascadia Mono", "SF Mono", Consolas, monospace; }',
    ]

    def paint(palette, indent=''):
        painted = []
        for key, color in palette.items():
            if key == 'line':
                continue
            painted.append('{}{} {{ fill: {}; }}'.format(indent, '.' + key, color))
        painted.append('{}{} {{ stroke: {}; }}'.format(indent, '.card', palette['line']))
        painted.append('{}{} {{ stroke: {}; }}'.format(indent, '.rule', palette['accent']))
        return painted

    if theme == 'auto':
        lines.extend(paint(_PALETTES['light']))
        lines.append('@media (prefers-color-scheme: dark) {')
        lines.extend(paint(_PALETTES['dark'], '  '))
        lines.append('}')
    else:
        lines.extend(paint(_PALETTES[theme]))
    if not static:
        lines.append('@media (prefers-reduced-motion: no-preference) {')
        lines.append('  .dash { animation: battle-dash 7s linear infinite; }')
        lines.append('}')
        lines.append('@keyframes battle-dash { from { stroke-dashoffset: 0; } to { stroke-dashoffset: -%d; } }' % WIDTH)
    return '\n'.join(lines)


def _font(cls):
    return _MONO if 'mono' in cls.split() else _SANS


def _text(x, y, content, cls, size, palette, weight=None, anchor=None):
    key = cls.split()[0]
    attrs = [
        'class="{}"'.format(cls),
        'fill="{}"'.format(palette[key]),
        'x="{}"'.format(x),
        'y="{}"'.format(y),
        'font-family="{}"'.format(_font(cls)),
        'font-size="{}"'.format(size),
    ]
    if weight:
        attrs.append('font-weight="{}"'.format(weight))
    if anchor:
        attrs.append('text-anchor="{}"'.format(anchor))
    return '<text {}>{}</text>'.format(' '.join(attrs), xml_escape(content))


def _rect(x, y, w, h, cls, palette, rx):
    stroke = ''
    if cls == 'card':
        stroke = ' stroke="{}"'.format(palette['line'])
    return '<rect class="{}" x="{}" y="{}" width="{}" height="{}" rx="{}" fill="{}"{}/>'.format(
        cls, x, y, w, h, rx, palette[cls], stroke)


def _status_class(label):
    if label in ('已完成', '收尾完成'):
        return 'ok'
    if label in ('进行中', '协调中'):
        return 'run'
    if label in ('停滞', '出错', '等待中'):
        return 'warn'
    return 'muted'


def _section(elements, y, title, rows, more, more_label, palette):
    if not rows and not more:
        return y
    elements.append(_text(PAD, y + 18, title, 'ink', 15, palette, weight='600'))
    y += 30
    for row in rows:
        height = 54 if row['bits'] else 38
        elements.append(_rect(PAD, y, INNER, height, 'card', palette, 8))
        elements.append(_text(PAD + 16, y + 24, row['title'], 'ink', 15, palette, weight='600'))
        if row.get('status'):
            elements.append(_text(
                WIDTH - PAD - 16, y + 24, row['status'], _status_class(row['status']), 13, palette, anchor='end'))
        if row['bits']:
            elements.append(_text(PAD + 16, y + 44, ' · '.join(row['bits']), 'muted', 12, palette))
        y += height + 8
    if more:
        elements.append(_text(PAD, y + 16, more_label.format(num_text(more)), 'faint', 12, palette))
        y += 24
    return y + 8


def render_svg(view, theme='auto', static=False):
    """Turn a public view into one SVG document. theme is auto, light or dark."""
    if theme not in _PALETTES and theme != 'auto':
        raise ExportError('theme must be auto, light, or dark')
    palette = _PALETTES['light' if theme == 'auto' else theme]
    elements = []
    y = 48
    elements.append(_text(PAD, y, 'AGENT TEAM', 'faint mono', 12, palette))
    if view['right']:
        elements.append(_text(WIDTH - PAD, y, ' · '.join(view['right']), 'faint mono', 12, palette, anchor='end'))
    y += 42
    elements.append(_text(PAD, y, '战报', 'ink', 36, palette, weight='600'))
    elements.append(_text(WIDTH - PAD, y, 'BATTLE CARD', 'faint mono', 13, palette, anchor='end'))
    y += 28
    if view['summary']:
        elements.append(_text(PAD, y, ' · '.join(view['summary']), 'muted', 15, palette))
        y += 22
    y += 10
    rule_y = y
    y += 26
    tiles = view['tiles']
    if tiles:
        tile_h = 86
        gap = 12
        count = len(tiles)
        base = (INNER - gap * (count - 1)) // count
        remain = INNER - (base * count + gap * (count - 1))
        x = PAD
        for index, tile in enumerate(tiles):
            tile_w = base + (1 if index < remain else 0)
            elements.append(_rect(x, y, tile_w, tile_h, 'card', palette, 8))
            elements.append(_text(x + 16, y + 30, tile['label'], 'faint', 12, palette))
            elements.append(_text(x + 16, y + 64, tile['value'], 'ink mono', 26, palette, weight='600'))
            x += tile_w + gap
        y += tile_h + 18
    usage = view['usage']
    if usage:
        row_h = 26
        block_h = 56 + row_h * len(usage['rows']) + 28
        elements.append(_rect(PAD, y, INNER, block_h, 'card', palette, 8))
        elements.append(_text(PAD + 18, y + 28, '用量', 'ink', 15, palette, weight='600'))
        rights = [WIDTH - PAD - 18 - 176 * (2 - index) for index in range(3)]
        for index, column in enumerate(usage['columns']):
            elements.append(_text(rights[index], y + 28, column, 'faint', 12, palette, anchor='end'))
        for index, row in enumerate(usage['rows']):
            row_y = y + 56 + index * row_h
            elements.append(_text(PAD + 18, row_y, row['label'], 'muted', 13, palette))
            for cell_index, cell in enumerate(row['cells']):
                elements.append(_text(rights[cell_index], row_y, cell, 'ink mono', 13, palette, anchor='end'))
        elements.append(_text(PAD + 18, y + block_h - 16, usage['note'], 'faint', 12, palette))
        y += block_h + 16
    if view['lead']:
        y = _section(elements, y, '队长', [view['lead']], 0, '', palette)
    y = _section(
        elements, y, '部门', view['departments'], view['more_departments'], '另有 {} 个部门未列入', palette)
    y = _section(
        elements, y, '成员', view['members'], view['more_members'], '另有 {} 位未列入', palette)
    y = _section(
        elements, y, '已完成的任务', view['tasks'], view['more_tasks'], '另有 {} 项未列入', palette)
    y += 6
    footer = ['未出现的字段不补']
    if view['partial']:
        footer.append('成本是部分估算，未定价的没有计入')
    elif usage:
        footer.append('估算成本，不是服务商账单')
    elements.append(_text(PAD, y + 14, ' · '.join(footer), 'faint', 12, palette))
    elements.append(_text(WIDTH - PAD, y + 14, 'claude-agent-team', 'faint mono', 12, palette, anchor='end'))
    height = y + 36
    rule = '<line class="rule{}" x1="{}" y1="{}" x2="{}" y2="{}" stroke="{}" stroke-width="1"{}/>'.format(
        '' if static else ' dash',
        PAD, rule_y, WIDTH - PAD, rule_y, palette['accent'],
        '' if static else ' stroke-dasharray="64 896"',
    )
    if not static:
        rule = rule[:-2] + '><animate attributeName="stroke-dashoffset" from="0" to="-%d" dur="7s" repeatCount="indefinite"/></line>' % WIDTH
    head = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="{0}" height="{1}" viewBox="0 0 {0} {1}" role="img" aria-labelledby="battleTitle battleDesc">'.format(WIDTH, height),
        '<title id="battleTitle">Agent Team 战报</title>',
        '<desc id="battleDesc">{}</desc>'.format(xml_escape(_describe(view))),
        '<style>',
        _stylesheet(theme, static),
        '</style>',
        _rect(0, 0, WIDTH, height, 'paper', palette, 0),
        _rect(0, 0, 4, height, 'accent', palette, 0),
        rule,
    ]
    return '\n'.join(head + elements) + '\n</svg>\n'


def build_card(snapshot, theme='auto', static=False):
    """Build the SVG for one snapshot object."""
    return render_svg(public_view(snapshot), theme, static)


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
    source.add_argument('--input', type=Path, help='offline board snapshot JSON file')
    source.add_argument('--port', type=_port, help='local board port, normally 7788')
    parser.add_argument('--output', type=Path, help='write the SVG here instead of stdout')
    parser.add_argument('--theme', choices=('auto', 'light', 'dark'), default='auto',
                        help='auto follows prefers-color-scheme; light or dark is fixed')
    parser.add_argument('--static', action='store_true', dest='static_card',
                        help='omit the restrained dash motion')
    args = parser.parse_args(argv)
    try:
        if args.input and args.output:
            _same_file(args.input, args.output)
        snapshot = read_snapshot(args.input, args.port)
        svg = build_card(snapshot, args.theme, args.static_card)
        if args.output:
            try:
                args.output.write_text(svg, encoding='utf-8')
            except OSError:
                raise ExportError('cannot write battle card; check directory and permissions') from None
        else:
            sys.stdout.write(svg)
    except ExportError as exc:
        print('battle card failed: {}'.format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
