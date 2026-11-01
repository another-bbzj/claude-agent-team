"""Read-only Codex rollout discovery and bounded incremental session parsing.

Only visible messages, tool summaries, metadata and measured cumulative usage
are exposed. Reasoning content and tool output never enter conversation rows.
"""
import copy
import hashlib
import heapq
import json
import math
import re
import threading
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path

MAX_SCAN_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 512 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_DISCOVER = 500
MAX_CACHE = 48
MAX_INHERITED_BYTES = 16 * 1024 * 1024
MAX_TEXT = 4000
_cache = OrderedDict()
_headers = OrderedDict()
_index = {}
_lock = threading.RLock()


def _short(value, limit=160):
    return value[:limit] if isinstance(value, str) else ''


def _dict(value):
    return value if isinstance(value, dict) else {}


def _timestamp(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else 0.0
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
        except (ValueError, OverflowError):
            pass
    return 0.0


def _decode(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError, RecursionError):
        return None


def _warn(state, message):
    if message not in state['warnings']:
        state['warnings'].append(message)


def _source_meta(payload):
    allowed = ('id', 'session_id', 'cwd', 'parent_thread_id', 'agent_nickname',
               'agent_role', 'agent_path', 'model', 'model_provider')
    result = {key: _short(payload[key], 1024) for key in allowed if isinstance(payload.get(key), str)}
    ordinal = payload.get('subagent_history_start_ordinal')
    if type(ordinal) is int and ordinal >= 0:
        result['history_start'] = ordinal
    source = payload.get('source', payload.get('thread_source'))
    result['subagent'] = isinstance(source, dict) and isinstance(source.get('subagent'), dict)
    if result['subagent']:
        spawn = _dict(source['subagent'].get('thread_spawn'))
        for key in ('parent_thread_id', 'agent_nickname', 'agent_role', 'agent_path'):
            if not result.get(key) and isinstance(spawn.get(key), str):
                result[key] = _short(spawn[key], 1024)
    return result


def _item_type(item):
    return re.sub(r'[_\-.]', '', str(item.get('type', ''))).lower()


def _record_link(state, item):
    child = item.get('agent_thread_id') or item.get('agent_id')
    if isinstance(child, str) and child and len(child) <= 256:
        data = {'id': child, 'agent_path': _short(item.get('agent_path'), 256),
                'agent_role': _short(item.get('agent_type') or item.get('agent_role'), 80),
                'model': _short(item.get('model'), 160)}
        prior = state['links'].get(child, {})
        state['links'][child] = {key: value or prior.get(key, '') for key, value in data.items()}
        while len(state['links']) > 128:
            state['links'].pop(next(iter(state['links'])))


def _new_state():
    return {'meta': {}, 'model': '', 'usage': {'in': 0, 'out': 0, 'cache_read': 0, 'cache_write': 0},
            'timeline': deque(maxlen=80), 'conversation': deque(maxlen=120), 'pending': OrderedDict(),
            'seen': OrderedDict(), 'message_copies': OrderedDict(), 'links': {}, 'warnings': [],
            'tool_count': 0, 'thinking_count': 0, 'first': 0.0, 'last': 0.0, 'last_user': '',
            'last_text': '', 'ended': None, 'error': False, 'offset': 0, 'skip_fragment': False,
            'stamp': None, 'identity': None, 'checkpoint': b'', 'prefix': b''}


def _remember(state, key):
    if key in state['seen']:
        return False
    state['seen'][key] = True
    if len(state['seen']) > 2048:
        state['seen'].popitem(last=False)
    return True


def _visible_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n'.join(item['text'] for item in content if isinstance(item, dict)
                     and item.get('type') in ('text', 'input_text', 'output_text') and isinstance(item.get('text'), str))


def _message(state, role, text, ts, representation, item_id=''):
    if role not in ('user', 'assistant') or not isinstance(text, str) or not text.strip():
        return
    if item_id and not _remember(state, ('message', role, item_id)):
        return
    digest = hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()
    key = (role, digest)
    prior = state['message_copies'].get(key)
    if prior and representation not in prior['sources'] and abs(ts - prior['ts']) <= 2:
        prior['sources'].add(representation)
        return
    state['message_copies'][key] = {'sources': {representation}, 'ts': ts}
    state['message_copies'].move_to_end(key)
    if len(state['message_copies']) > 240:
        state['message_copies'].popitem(last=False)
    state['conversation'].append({'ts': ts, 'role': role, 'text': _short(text, MAX_TEXT), 'agentId': '__lead'})
    if role == 'user':
        state['last_user'] = _short(text, 600)
    else:
        state['last_text'] = _short(text, 400)
        state['timeline'].append({'ts': ts, 'kind': 'text', 'label': _short(text)})


def _usage(state, value):
    if not isinstance(value, dict):
        return
    keys = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens')
    if 'input_tokens' not in value or 'output_tokens' not in value:
        return
    values = [value.get(key, 0) for key in keys]
    if any(type(number) is not int or number < 0 for number in values):
        _warn(state, 'Invalid token snapshot ignored.')
        return
    inputs, cached, written, outputs = values
    if cached + written > inputs:
        _warn(state, 'Cached token counts exceed input; uncached input was clamped.')
    state['usage'] = {'in': max(0, inputs - cached - written), 'out': outputs,
                      'cache_read': cached, 'cache_write': written}


def _arguments(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and len(value) <= MAX_LINE_BYTES:
        return _dict(_decode(value))
    return {}


def _tool(state, item, ts, completed=False):
    kind = _item_type(item)
    name = _short(item.get('name') or item.get('tool') or item.get('type') or 'tool', 100)
    item_id = _short(item.get('call_id') or item.get('id'), 256)
    if item_id:
        existing = state['pending'].get(item_id)
        if existing:
            if completed:
                existing['row']['done'] = True
                existing['row']['error'] = item.get('status') in ('failed', 'error') or item.get('exit_code', 0) not in (None, 0)
            return
    arguments = _arguments(item.get('arguments'))
    label = name
    if kind in ('commandexecution', 'commandexecutionitem'):
        command = item.get('command')
        label = _short(' '.join(str(part) for part in command), 160) if isinstance(command, list) else _short(command) or name
    elif arguments:
        label = _short(arguments.get('cmd') or arguments.get('command') or arguments.get('description')) or name
    row = {'ts': ts, 'kind': 'tool', 'name': name, 'label': label, 'done': completed,
           'error': item.get('status') in ('failed', 'error') or item.get('exit_code', 0) not in (None, 0)}
    state['timeline'].append(row)
    state['tool_count'] += 1
    if item_id:
        state['pending'][item_id] = {'row': row, 'name': name, 'args': {k: _short(arguments.get(k), 600)
                                            for k in ('agent_type', 'task_name', 'message', 'description', 'model')}}
        if len(state['pending']) > 128:
            state['pending'].popitem(last=False)


def _tool_result(state, item, ts):
    call_id = _short(item.get('call_id') or item.get('id'), 256)
    pending = state['pending'].get(call_id)
    if not pending:
        return
    row = pending['row']
    row['done'] = True
    row['error'] = bool(item.get('is_error'))
    if ts and row['ts']:
        row['ms'] = max(0, int((ts - row['ts']) * 1000))
    if pending['name'].split('.')[-1] == 'spawn_agent':
        result = _arguments(item.get('output'))
        child = result.get('agent_id') or result.get('thread_id') or result.get('id')
        if isinstance(child, str):
            _record_link(state, {'agent_id': child, **pending['args']})


def _process(state, event):
    kind = event.get('type')
    payload = _dict(event.get('payload'))
    ts = _timestamp(event.get('timestamp') or payload.get('timestamp'))
    if ts:
        state['last'] = max(state['last'], ts)
        state['first'] = min(state['first'] or ts, ts)
    if kind == 'session_meta':
        # Fork rollouts can contain their parent's metadata after their own.
        # The first record owns this file's identity, never the inherited one.
        if state['meta'].get('id') or state['meta'].get('session_id'):
            return
        state['meta'].update(_source_meta(payload))
        state['model'] = state['meta'].get('model') or state['model']
    elif kind == 'turn_context':
        if isinstance(payload.get('model'), str) and payload['model']:
            state['model'] = _short(payload['model'], 160)
        if isinstance(payload.get('cwd'), str):
            state['meta']['cwd'] = _short(payload['cwd'], 1024)
    elif kind == 'token_usage_record':
        _usage(state, payload.get('thread_token_usage') or event.get('thread_token_usage'))
    elif kind == 'response_item':
        typ = _item_type(payload)
        if typ == 'message':
            _message(state, payload.get('role'), _visible_text(payload.get('content')), ts, 'response', _short(payload.get('id'), 256))
        elif typ == 'agentmessage':
            _message(state, 'assistant', payload.get('text') or _visible_text(payload.get('content')), ts, 'response', _short(payload.get('id'), 256))
        elif typ in ('functioncall', 'customtoolcall'):
            _tool(state, payload, ts)
        elif typ in ('functioncalloutput', 'customtoolcalloutput'):
            _tool_result(state, payload, ts)
        elif typ == 'reasoning':
            state['thinking_count'] += 1
    elif kind == 'event_msg':
        typ = payload.get('type')
        if typ == 'token_count':
            _usage(state, _dict(payload.get('info')).get('total_token_usage'))
        elif typ in ('task_started', 'turn_started'):
            state['ended'], state['error'] = None, False
            state['message_copies'].clear()
        elif typ in ('task_complete', 'task_completed', 'turn_complete', 'turn_completed'):
            state['ended'] = ts or state['last']
        elif typ in ('turn_aborted', 'error', 'task_failed'):
            state['error'] = True
            state['ended'] = ts or state['last']
        elif typ in ('user_message', 'agent_message'):
            role = 'user' if typ == 'user_message' else 'assistant'
            _message(state, role, payload.get('message') or _visible_text(payload.get('content')), ts, 'event', _short(payload.get('id'), 256))
        elif typ == 'item_completed':
            item = _dict(payload.get('item'))
            itype = _item_type(item)
            iid = _short(item.get('id'), 256)
            if itype in ('usermessage', 'agentmessage'):
                role = 'user' if itype == 'usermessage' else 'assistant'
                _message(state, role, _visible_text(item.get('content')) or item.get('text'), ts, 'completed', iid)
            elif itype == 'subagentactivity':
                _record_link(state, item)
                if iid and not _remember(state, ('activity', iid)):
                    return
                _tool(state, {'id': iid, 'name': 'SubAgentActivity', 'type': 'SubAgentActivity'}, ts, True)
            elif itype in ('commandexecution', 'mcpToolCall'.lower(), 'filechange', 'websearch', 'extension', 'imageview'):
                _tool(state, item, ts, True)
            elif itype == 'reasoning' and (not iid or _remember(state, ('reasoning', iid))):
                state['thinking_count'] += 1


def _fingerprint(stream, offset):
    stream.seek(max(0, offset - 256))
    return stream.read(min(offset, 256))


def _read_state(path):
    """Cache per-file state and consume only complete appended lines."""
    path = Path(path)
    stat = path.stat()
    stamp = (stat.st_size, stat.st_mtime_ns)
    identity = (stat.st_dev, stat.st_ino)
    key = str(path.resolve())
    state = _cache.get(key)
    if state and state['stamp'] == stamp and state['identity'] == identity:
        _cache.move_to_end(key)
        return state
    with path.open('rb') as stream:
        stream.seek(0)
        prefix = stream.read(256)
        append = bool(state and state['identity'] == identity and stat.st_size > state['stamp'][0]
                      and state['offset'] <= stat.st_size and prefix == state['prefix']
                      and _fingerprint(stream, state['offset']) == state['checkpoint'])
        if not append:
            state = _new_state()
            stream.seek(0)
            first_line = stream.readline(MAX_LINE_BYTES + 1)
            first_event = _decode(first_line) if first_line.endswith(b'\n') else None
            if first_event and first_event.get('type') == 'session_meta':
                _process(state, first_event)
                history_start = state['meta'].get('history_start', 0)
                if history_start:
                    ordinal, skipped = 1, len(first_line)
                    while ordinal < history_start and skipped < MAX_INHERITED_BYTES:
                        line = stream.readline(min(MAX_LINE_BYTES + 1, MAX_INHERITED_BYTES - skipped))
                        if not line:
                            break
                        skipped += len(line)
                        if line.endswith(b'\n'):
                            ordinal += 1
                    if ordinal < history_start:
                        state['offset'] = stat.st_size
                        _warn(state, 'Inherited history boundary was not reached; only subsequent activity will be parsed.')
                    else:
                        state['offset'] = stream.tell()
        offset = state['offset']
        if stat.st_size - offset > MAX_SCAN_BYTES:
            if not offset:
                stream.seek(0)
                for line in stream.read(MAX_HEADER_BYTES).splitlines():
                    event = _decode(line)
                    if event and event.get('type') in ('session_meta', 'turn_context'):
                        _process(state, event)
            offset = max(offset, stat.st_size - MAX_SCAN_BYTES)
            stream.seek(offset)
            stream.readline(MAX_LINE_BYTES + 1)
            offset = stream.tell()
            _warn(state, 'Older events were not scanned; activity and token coverage may be incomplete.')
        stream.seek(offset)
        data = stream.read(MAX_SCAN_BYTES)
        complete = data.rfind(b'\n') + 1
        if not complete and len(data) > MAX_LINE_BYTES:
            state['offset'] = offset + len(data)
            state['skip_fragment'] = True
            _warn(state, 'Oversized rollout line ignored.')
        else:
            lines = data[:complete].splitlines()
            if state['skip_fragment'] and lines:
                lines = lines[1:]
                state['skip_fragment'] = False
            for raw in lines:
                if len(raw) > MAX_LINE_BYTES:
                    _warn(state, 'Oversized rollout line ignored.')
                    continue
                event = _decode(raw)
                if event:
                    _process(state, event)
                elif raw.strip():
                    _warn(state, 'Malformed rollout line ignored.')
            state['offset'] = offset + complete
        state['checkpoint'] = _fingerprint(stream, state['offset'])
        state['prefix'], state['identity'], state['stamp'] = prefix, identity, stamp
    _cache[key] = state
    _cache.move_to_end(key)
    while len(_cache) > MAX_CACHE:
        _cache.popitem(last=False)
    return state


def _header(path, stat):
    key = str(path.resolve())
    stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    cached = _headers.get(key)
    if cached and cached[0] == stamp:
        _headers.move_to_end(key)
        return cached[1]
    state = _new_state()
    with path.open('rb') as stream:
        first_line = stream.readline(MAX_LINE_BYTES + 1)
        first_event = _decode(first_line) if first_line.endswith(b'\n') else None
        if first_event and first_event.get('type') == 'session_meta':
            _process(state, first_event)
        # Inherited parent records are not discovery evidence for this child.
        if state['meta'].get('history_start'):
            result = {'meta': state['meta'], 'links': {}}
            _headers[key] = (stamp, result)
            while len(_headers) > MAX_DISCOVER:
                _headers.popitem(last=False)
            return result
        stream.seek(0)
        data = stream.read(MAX_HEADER_BYTES)
        for line in data[:data.rfind(b'\n') + 1].splitlines():
            event = _decode(line)
            if event:
                _process(state, event)
        if stat.st_size > MAX_HEADER_BYTES:
            stream.seek(max(MAX_HEADER_BYTES, stat.st_size - MAX_HEADER_BYTES))
            stream.readline(MAX_LINE_BYTES + 1)
            tail = stream.read(MAX_HEADER_BYTES)
            for line in tail[:tail.rfind(b'\n') + 1].splitlines():
                event = _decode(line)
                if event:
                    _process(state, event)
    result = {'meta': state['meta'], 'links': state['links']}
    _headers[key] = (stamp, result)
    _headers.move_to_end(key)
    while len(_headers) > MAX_DISCOVER:
        _headers.popitem(last=False)
    return result


def discover_sessions(root):
    """List roots, attaching children only from explicit parent or spawn evidence."""
    with _lock:
        root = Path(root)
        if not root.is_dir():
            return []
        paths, total = [], 0
        try:
            for path in root.rglob('*.jsonl'):
                if path.is_symlink():
                    continue
                stat = path.stat()
                total += 1
                entry = (stat.st_mtime_ns, str(path), path, stat)
                if len(paths) < MAX_DISCOVER:
                    heapq.heappush(paths, entry)
                elif entry[:2] > paths[0][:2]:
                    heapq.heapreplace(paths, entry)
        except OSError:
            pass
        paths.sort(reverse=True)
        descriptors, links = {}, {}
        for _, _, path, stat in paths:
            try:
                header = _header(path, stat)
            except OSError:
                continue
            meta = copy.deepcopy(header['meta'])
            sid = meta.get('id') or meta.get('session_id')
            if not sid:
                continue
            descriptor = {'session': sid, 'project': meta.get('cwd') or 'Codex', 'dir': path.parent,
                          'path': path, 'mtime': stat.st_mtime, 'count': 1, 'source': 'codex',
                          'meta': meta, 'children': []}
            if sid in descriptors:
                continue
            descriptors[sid] = descriptor
            for child, hint in header['links'].items():
                links.setdefault(child, []).append((sid, hint))
            cached = _cache.get(str(path.resolve()))
            if cached:
                for child, hint in cached['links'].items():
                    links.setdefault(child, []).append((sid, hint))
        parents = {}
        for sid, descriptor in descriptors.items():
            meta = descriptor['meta']
            explicit = meta.get('parent_thread_id')
            candidates = {parent for parent, _ in links.get(sid, []) if parent != sid}
            if explicit:
                candidates.add(explicit)
            if len(candidates) == 1:
                parent = next(iter(candidates))
                if parent in descriptors and parent != sid:
                    parents[sid] = parent
            if len(candidates) > 1:
                meta.setdefault('warnings', []).append('Conflicting parent evidence; shown as a separate session.')
            elif (meta.get('subagent') or explicit) and sid not in parents:
                meta.setdefault('warnings', []).append('Subagent parent was not verified; shown as a separate session.')
        for sid in list(parents):
            chain, current = set(), sid
            while current in parents and current not in chain:
                chain.add(current)
                current = parents[current]
            if current in chain:
                for child in chain:
                    descriptors[child]['meta'].setdefault('warnings', []).append('Cyclic parent metadata ignored.')
                    parents.pop(child, None)
        for child, parent in parents.items():
            descriptor = descriptors[child]
            descriptor['parent'] = parent
            for linked_parent, hint in links.get(child, []):
                if linked_parent == parent:
                    for key, value in hint.items():
                        if value and not descriptor['meta'].get(key):
                            descriptor['meta'][key] = value
            descriptors[parent]['children'].append(descriptor)
        roots = [descriptor for sid, descriptor in descriptors.items() if sid not in parents]
        for descriptor in roots:
            descriptor['count'] = 1 + len(descriptor['children'])
            if total > MAX_DISCOVER:
                descriptor['meta'].setdefault('warnings', []).append('Session discovery limit reached; some rollouts were not listed.')
        _index.clear()
        _index.update(descriptors)
        return copy.deepcopy(roots)


def _lead(state, descriptor, now, stale):
    meta = {**descriptor.get('meta', {}), **state['meta']}
    ts = state['last'] or descriptor.get('mtime', 0.0)
    current = next((row for row in reversed(state['timeline']) if row['kind'] == 'tool'), None)
    mtime = descriptor.get('mtime', 0.0)
    return {'name': 'Codex', 'source': 'codex', 'timeline': list(state['timeline']), 'current': current,
            'active': not state['ended'] and not state['error'] and now - mtime < min(stale, 20),
            'toolCount': state['tool_count'], 'outputTokens': state['usage']['out'], 'model': state['model'],
            'usage': state['usage'], 'cost': None, 'dispatches': {child: {'ts': state['first'],
                'description': hint.get('agent_path') or child, 'agentType': hint.get('agent_role', ''),
                'model': hint.get('model', ''), 'name': hint.get('agent_path', ''), 'prompt': ''}
                for child, hint in state['links'].items()}, 'messages': [], 'notifications': {},
            'lastUserPrompt': state['last_user'], 'lastTs': ts, 'firstTs': state['first'] or ts,
            'mtime': mtime, 'cwd': meta.get('cwd', ''), 'lastTest': None}


def _member(state, descriptor, now, stale):
    lead = _lead(state, descriptor, now, stale)
    meta = {**descriptor.get('meta', {}), **state['meta']}
    sid = descriptor['session']
    status = 'error' if state['error'] else ('completed' if state['ended'] is not None else
                                          ('stalled' if now - lead['mtime'] > stale else 'running'))
    role = meta.get('agent_role') or 'codex-agent'
    name = meta.get('agent_nickname') or meta.get('agent_path') or 'Codex agent'
    return {'id': sid, 'avatar': 'server-engineer', 'baseName': name, 'roleTitle': role,
            'agentType': role, 'model': lead['model'] or meta.get('model', ''), 'modelFamily': 'codex',
            'usage': lead['usage'], 'cost': None, 'priced': False, 'priceKey': None, 'messages': [],
            'writes': [], 'reads': [], 'toolUseId': sid, 'group': None, 'description': name,
            'prompt': state['last_user'], 'status': status, 'startedAt': lead['firstTs'],
            'lastActivityAt': lead['lastTs'], 'endedAt': state['ended'], 'toolCount': lead['toolCount'],
            'thinkingCount': state['thinking_count'], 'outputTokens': lead['outputTokens'],
            'current': lead['current'], 'lastText': state['last_text'], 'timeline': lead['timeline'], 'lastTest': None}


def parse_session(chosen, now, stale):
    """Parse one root and verified descendants; all returned data is independent."""
    with _lock:
        warnings, conversation, members = list(chosen.get('meta', {}).get('warnings', [])), [], []
        try:
            state = _read_state(chosen['path'])
        except OSError:
            state = _new_state()
            _warn(state, 'Selected rollout could not be read.')
        warnings.extend(state['warnings'])
        lead = _lead(state, chosen, now, stale)
        conversation.extend(state['conversation'])
        queue = list(chosen.get('children', []))
        known = {descriptor['session'] for descriptor in queue}
        def append_links(parent, links):
            for child, hint in links.items():
                candidate = _index.get(child)
                if not candidate or child in known or child == chosen['session']:
                    continue
                parent_hint = candidate.get('parent') or candidate.get('meta', {}).get('parent_thread_id')
                if parent_hint and parent_hint != parent:
                    continue
                candidate = copy.deepcopy(candidate)
                for key, value in hint.items():
                    if value and not candidate['meta'].get(key):
                        candidate['meta'][key] = value
                queue.append(candidate)
                known.add(child)

        append_links(chosen['session'], state['links'])
        seen = {chosen['session']}
        for descriptor in queue:
            sid = descriptor['session']
            if sid in seen:
                continue
            seen.add(sid)
            try:
                child_state = _read_state(descriptor['path'])
            except OSError:
                warnings.append('A linked subagent rollout could not be read.')
                continue
            members.append(_member(child_state, descriptor, now, stale))
            conversation.extend({**row, 'agentId': sid} for row in child_state['conversation'])
            warnings.extend(child_state['warnings'])
            queue.extend(descriptor.get('children', []))
            known.update(child['session'] for child in descriptor.get('children', []))
            append_links(sid, child_state['links'])
            if len(seen) >= 128:
                warnings.append('Subagent limit reached; some activity was not parsed.')
                break
        return copy.deepcopy({'lead': lead, 'members': members,
                              'conversation': sorted(conversation, key=lambda row: row['ts'])[-120:],
                              'warnings': list(dict.fromkeys(warnings))})
