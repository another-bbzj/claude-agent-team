"""把官方任务的估算成本归到对得上名字的负责人。

金额沿用看板的价格表，不是服务商账单。只加总这位负责人已经记在转录里的用量，
而且只取任务处于 in_progress 的那几段。对不上、重叠、或时间定不住时不编造数字：
要么不写 estimate，要么标成 unknown / partial。

区间从哪来（按可信度）：

1. 转录里的 TaskUpdate / TaskCreate 工具入参。只留 task id、status、owner 和时间。
   谁调用的不重要，用的是任务自己的状态变化。owner 只在入参里真的写了才改写；
   全程没写 owner 时，沿用任务文件上的当前 owner。
2. 没有任何这类调用、当前状态是 in_progress、并且这条任务独占一个文件时，
   用文件修改时间到现在作为下限。这是 partial，basis 为 file。
   同一个文件里有多条任务时，修改时间分不出是哪一条，不用。
3. 待处理、没有转录、名字对不上、或进行中的尾巴关不上，不给金额。
"""


_API_USAGE = (
    ('input_tokens', 'in'),
    ('output_tokens', 'out'),
    ('cache_read_input_tokens', 'cache_read'),
    ('cache_creation_input_tokens', 'cache_write'),
)
_BUCKETS = ('in', 'out', 'cache_read', 'cache_write')
_STATUSES = ('pending', 'in_progress', 'completed', 'deleted')


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value == float('inf') or value == float('-inf'):
        return None
    return float(value)


def _num(value):
    number = _finite(value)
    if number is None or number < 0:
        return 0
    if number.is_integer():
        return int(number)
    return number


def normalize_usage(raw):
    """收成 in/out/cache_read/cache_write。

    字典里有 API 字段时只读那些字段。否则读看板已经汇总过的四个键。
    两套都在时不把它们加在一起。
    """
    if not isinstance(raw, dict):
        raw = {}
    api = any(key in raw for key, _dest in _API_USAGE)
    out = {}
    if api:
        for src, dest in _API_USAGE:
            out[dest] = _num(raw.get(src, 0) or 0)
    else:
        for dest in _BUCKETS:
            out[dest] = _num(raw.get(dest, 0) or 0)
    return out


def _tokens(usage):
    return any(usage.get(key) for key in _BUCKETS)


def _blank():
    return {'in': 0, 'out': 0, 'cache_read': 0, 'cache_write': 0}


def _add_usage(total, usage):
    for key in _BUCKETS:
        total[key] += usage.get(key, 0) or 0


def _unknown():
    return {
        'cost': None,
        'tokens': None,
        'status': 'unknown',
        'partial': False,
        'unpriced': False,
        'basis': 'none',
    }


def _matched_name(task):
    if not isinstance(task, dict):
        return ''
    match = task.get('ownerMatch')
    owner = task.get('owner')
    if not isinstance(match, str) or not match or owner != match:
        return ''
    return match


def _event(raw):
    if not isinstance(raw, dict):
        return None
    model = raw.get('model') if isinstance(raw.get('model'), str) else ''
    return {'ts': _finite(raw.get('ts')), 'model': model, 'usage': normalize_usage(raw.get('usage'))}


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
        owner = mark.get('owner') if isinstance(mark.get('owner'), str) else None
        found.append((ts, index, status, owner))
    found.sort()
    return found


def _file_window(task, now):
    if task.get('normalizedStatus') != 'in_progress' or task.get('stateSinceKind') != 'file':
        return None
    since = _finite(task.get('stateSince'))
    if since is None or since <= 0 or now is None:
        return None
    start = min(since, now)
    return {'segments': [(start, now)], 'partial': True, 'basis': 'file'}


def _window(task, marks, now):
    """返回 {segments, partial, basis}，定不住时返回 None。"""
    rows = _marks_for(marks, task.get('id'))
    status = None
    owner_state = None
    owner_seen = False
    open_start = None
    segments = []
    dropped_tail = False
    task_owner = task.get('owner')
    for ts, _index, mark_status, mark_owner in rows:
        if mark_status:
            status = mark_status
        if mark_owner is not None:
            owner_state = mark_owner
            owner_seen = True
        owned = (not owner_seen) or owner_state == task_owner
        active = status == 'in_progress' and owned
        if active and open_start is None:
            open_start = ts
        elif not active and open_start is not None:
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
        return {'segments': segments, 'partial': dropped_tail, 'basis': 'transcript'}
    if dropped_tail:
        return None
    return _file_window(task, now)


def _covers(window, ts):
    for start, end in window['segments']:
        if start <= ts <= end:
            return True
    return False


def _source_events(sources, name):
    if not isinstance(sources, dict):
        return None
    item = sources.get(name)
    if not isinstance(item, dict) or item.get('ambiguous'):
        return None
    events = item.get('events')
    if not isinstance(events, list):
        return None
    return events


def _price_of(price, model, usage):
    if not model or price is None:
        return None
    value = price(model, usage)
    number = _finite(value)
    if number is None or number < 0:
        return None
    return number


def _estimate_from(slices, price, basis, partial):
    tokens = _blank()
    priced = 0.0
    saw_priced = False
    saw_unpriced = False
    for item in slices:
        usage = item['usage']
        _add_usage(tokens, usage)
        amount = _price_of(price, item.get('model') or '', usage)
        if amount is None:
            if _tokens(usage):
                saw_unpriced = True
            continue
        saw_priced = True
        priced += amount
    unpriced = saw_unpriced
    if saw_unpriced and not saw_priced:
        cost = None
    elif not saw_priced and not saw_unpriced:
        cost = 0.0
    else:
        cost = round(priced, 4)
    if partial or unpriced:
        status = 'partial'
    else:
        status = 'known'
    return {
        'cost': cost,
        'tokens': tokens,
        'status': status,
        'partial': status == 'partial',
        'unpriced': unpriced,
        'basis': basis if basis in ('transcript', 'file') else 'none',
    }


def attach_estimates(tasks, sources, marks, now, price):
    """就地写上 estimate。owner 对不上的任务不带这个字段。

    price(model, usage) 与看板的 estimate_cost 相同：能计价时返回美元浮点，否则 None。
    返回 tasks。
    """
    now = _finite(now)
    groups = {}
    order = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        name = _matched_name(task)
        if not name:
            task.pop('estimate', None)
            continue
        groups.setdefault(name, []).append(task)
        order.append(task)
    for name, group in groups.items():
        events = _source_events(sources, name)
        prepared = []
        for task in group:
            window = _window(task, marks, now) if events is not None else None
            prepared.append((task, window))
        untimed = False
        parsed = []
        if events is not None:
            for raw in events:
                item = _event(raw)
                if item is None:
                    continue
                parsed.append(item)
                if item['ts'] is None and _tokens(item['usage']):
                    untimed = True
        buckets = {}
        overlapped = set()
        for item in parsed:
            if item['ts'] is None or not _tokens(item['usage']):
                continue
            hits = [task for task, window in prepared if window and _covers(window, item['ts'])]
            if len(hits) == 1:
                buckets.setdefault(id(hits[0]), []).append(item)
            elif len(hits) > 1:
                for task in hits:
                    overlapped.add(id(task))
        for task, window in prepared:
            if window is None:
                task['estimate'] = _unknown()
                continue
            partial = bool(window.get('partial')) or untimed or id(task) in overlapped
            task['estimate'] = _estimate_from(
                buckets.get(id(task)) or [], price, window.get('basis'), partial)
    return tasks
