"""Read-only Codex rollout discovery and bounded incremental session parsing.

Only visible messages, tool summaries, plan tasks, metadata and measured usage
are exposed. Reasoning content, tool output and encrypted agent text never
enter conversation rows. Token totals prefer a cumulative thread snapshot;
per-response records are summed only until that snapshot arrives, and a
repeated response id replaces its previous contribution.
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
    git = payload.get('git')
    if isinstance(git, dict):
        branch = _branch(git.get('branch'))
        if branch:
            result['git'] = {'branch': branch}
    return result


def _branch(value):
    """A branch name only. Commit hashes and repository URLs stay out of the snapshot."""
    if not isinstance(value, str):
        return ''
    text = value.strip()
    if not text or len(text) > 200 or '..' in text or '/' in text or '\\' in text:
        return ''
    if '\n' in text or '\r' in text:
        return ''
    return text


def _item_type(item):
    return re.sub(r'[_\-.]', '', str(item.get('type', ''))).lower()


def _record_link(state, item):
    child = item.get('agent_thread_id') or item.get('agent_id')
    if isinstance(child, str) and child and len(child) <= 256:
        nickname = item.get('agent_nickname')
        data = {'id': child, 'agent_path': _short(item.get('agent_path'), 256),
                'agent_role': _short(item.get('agent_type') or item.get('agent_role'), 80),
                'agent_nickname': _short(nickname, 80) if isinstance(nickname, str) else '',
                'model': _short(item.get('model'), 160)}
        prior = state['links'].get(child, {})
        state['links'][child] = {key: value or prior.get(key, '') for key, value in data.items()}
        while len(state['links']) > 128:
            state['links'].pop(next(iter(state['links'])))


def _new_state():
    return {'meta': {}, 'model': '', 'usage': {'in': 0, 'out': 0, 'cache_read': 0, 'cache_write': 0},
            'usage_mode': '', 'response_usage': OrderedDict(), 'usage_seq': 0, 'tasks': [],
            'timeline': deque(maxlen=80), 'conversation': deque(maxlen=120), 'communications': deque(maxlen=80), 'pending': OrderedDict(),
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
    parts = []
    for item in content:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str):
            continue
        if str(item.get('type') or '').lower() in ('text', 'input_text', 'output_text'):
            parts.append(item['text'])
    return '\n'.join(parts)


def _plain_message(value):
    """Visible inter-agent text. Fernet payloads (gAAAAA…) are not conversation."""
    if not isinstance(value, str):
        return ''
    text = value.strip()
    if not text or text.startswith('gAAAAA'):
        return ''
    return text


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


def _int_field(value, keys):
    """Return (present, number). A present but invalid field is (True, None)."""
    for key in keys:
        if key not in value:
            continue
        number = value.get(key)
        if type(number) is not int or number < 0:
            return True, None
        return True, number
    return False, 0


def _usage_parts(state, value):
    if not isinstance(value, dict):
        return None
    has_in, inputs = _int_field(value, ('input_tokens', 'inputTokens'))
    has_out, outputs = _int_field(value, ('output_tokens', 'outputTokens'))
    if not has_in or not has_out or inputs is None or outputs is None:
        if has_in or has_out:
            _warn(state, 'Invalid token snapshot ignored.')
        return None
    _, cached = _int_field(value, (
        'cached_input_tokens', 'cachedInputTokens', 'cache_read_input_tokens', 'cacheReadInputTokens'))
    _, written = _int_field(value, (
        'cache_write_input_tokens', 'cacheWriteInputTokens', 'cache_creation_input_tokens', 'cacheCreationInputTokens'))
    if cached is None or written is None:
        _warn(state, 'Invalid token snapshot ignored.')
        return None
    if cached + written > inputs:
        _warn(state, 'Cached token counts exceed input; uncached input was clamped.')
    return inputs, cached, written, outputs


def _store_usage(state, parts):
    inputs, cached, written, outputs = parts
    state['usage'] = {'in': max(0, inputs - cached - written), 'out': outputs,
                      'cache_read': cached, 'cache_write': written}


def _usage(state, value):
    """Replace totals with one cumulative thread snapshot. Cached tokens stay inside input."""
    parts = _usage_parts(state, value)
    if not parts:
        return
    state['usage_mode'] = 'cumulative'
    state['response_usage'].clear()
    _store_usage(state, parts)


def _add_response_usage(state, value, response_id):
    """Sum per-response usage until a cumulative snapshot exists. Same response id replaces."""
    if state.get('usage_mode') == 'cumulative':
        return
    parts = _usage_parts(state, value)
    if not parts:
        return
    state['usage_mode'] = 'summed'
    key = response_id if isinstance(response_id, str) and response_id else ''
    if not key:
        state['usage_seq'] += 1
        key = 'anon-%d' % state['usage_seq']
    elif len(key) > 256:
        key = key[:256]
    state['response_usage'][key] = parts
    state['response_usage'].move_to_end(key)
    while len(state['response_usage']) > 512:
        state['response_usage'].popitem(last=False)
    totals = [0, 0, 0, 0]
    for item in state['response_usage'].values():
        for index, number in enumerate(item):
            totals[index] += number
    _store_usage(state, totals)


def _arguments(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and len(value) <= MAX_LINE_BYTES:
        return _dict(_decode(value))
    return {}


def _norm_status(value):
    return re.sub(r'[_\-.\s]', '', str(value or '')).lower()


def _tool_flags(item, completed):
    status = _norm_status(item.get('status'))
    exit_code = item.get('exit_code', 0)
    error = status in ('failed', 'error', 'declined', 'interrupted') or exit_code not in (None, 0)
    if item.get('success') is False:
        error = True
    if status == 'inprogress':
        done = False
    elif completed or status in ('completed', 'success', 'failed', 'error', 'declined', 'interrupted'):
        done = True
    else:
        done = False
    return done, error


def _command_text(command):
    if isinstance(command, list):
        parts = [str(part) for part in command if isinstance(part, (str, int)) and not isinstance(part, bool)]
        return _short(' '.join(parts), 160)
    if isinstance(command, str):
        return _short(command, 160)
    return ''


def _task_status(value, completed=None):
    if isinstance(completed, bool):
        return 'completed' if completed else 'pending'
    mapped = {
        'pending': 'pending', 'todo': 'pending',
        'inprogress': 'in_progress', 'active': 'in_progress',
        'completed': 'completed', 'complete': 'completed', 'done': 'completed',
    }
    return mapped.get(_norm_status(value), 'pending')


def _task_subject(text):
    if not isinstance(text, str):
        return ''
    return _short(' '.join(text.split()), 160)


def _replace_tasks(state, rows):
    """The latest plan or todo list is the task list. Earlier steps are replaced."""
    cleaned = []
    for index, row in enumerate(rows[:40]):
        if not isinstance(row, dict):
            continue
        subject = _task_subject(row.get('subject'))
        if not subject:
            continue
        status = row.get('status') if row.get('status') in ('pending', 'in_progress', 'completed') else 'pending'
        ident = row.get('id') if isinstance(row.get('id'), str) and row['id'] else str(index + 1)
        cleaned.append({
            'id': _short(ident, 80), 'subject': subject, 'description': '',
            'status': status, 'normalizedStatus': status,
            'owner': row.get('owner') if isinstance(row.get('owner'), str) and row.get('owner') else None,
            'blockedBy': [], 'blocks': [],
        })
    if len(rows) > 40:
        _warn(state, 'Plan longer than 40 steps was truncated.')
    state['tasks'] = cleaned


def _tasks_from_lines(state, text):
    steps = []
    if isinstance(text, str):
        for line in text.splitlines():
            line = re.sub(r'^(?:[-*]|\d+[.)])\s+', '', line.strip())
            if line:
                steps.append(line)
        if not steps and text.strip():
            steps.append(text.strip())
    _replace_tasks(state, [{'subject': step, 'status': 'pending'} for step in steps])


def _tasks_from_items(state, items):
    rows = []
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, str):
            rows.append({'subject': item, 'status': 'pending'})
            continue
        if not isinstance(item, dict):
            continue
        subject = item.get('step') or item.get('text') or item.get('subject') or item.get('content')
        if isinstance(item.get('completed'), bool):
            status = _task_status(None, item.get('completed'))
        else:
            status = _task_status(item.get('status'))
        rows.append({'subject': subject, 'status': status})
    _replace_tasks(state, rows)


def _maybe_plan_call(state, name, arguments):
    if not isinstance(name, str) or name.split('.')[-1] != 'update_plan':
        return
    if isinstance(arguments, dict) and isinstance(arguments.get('plan'), list):
        _tasks_from_items(state, arguments.get('plan'))


def _tool(state, item, ts, completed=False):
    kind = _item_type(item)
    name = _short(item.get('name') or item.get('tool') or item.get('type') or 'tool', 100)
    item_id = _short(item.get('call_id') or item.get('id'), 256)
    done, error = _tool_flags(item, completed)
    if item_id:
        existing = state['pending'].get(item_id)
        if existing:
            if completed or done:
                existing['row']['done'] = done
                existing['row']['error'] = error
                if ts and existing['row'].get('ts'):
                    existing['row']['ms'] = max(0, int((ts - existing['row']['ts']) * 1000))
            return
    arguments = _arguments(item.get('arguments')) or _arguments(item.get('input'))
    _maybe_plan_call(state, name, arguments)
    label = name
    if kind in ('commandexecution', 'commandexecutionitem', 'localshellcall'):
        label = _command_text(item.get('command')) or name
    elif arguments:
        command = arguments.get('cmd') or arguments.get('command') or arguments.get('description')
        label = _command_text(command) or _short(command) or name
    row = {'ts': ts, 'kind': 'tool', 'name': name, 'label': label, 'done': done, 'error': error}
    state['timeline'].append(row)
    state['tool_count'] += 1
    # Only recorded send calls are communication evidence; ordinary assistant
    # text and tool outputs are never guessed to be messages to another agent.
    if name.split('.')[-1] in ('send_input', 'send_message', 'followup_task', 'send_message_to_thread'):
        recipient = arguments.get('target') or arguments.get('id') or arguments.get('thread_id')
        message = _plain_message(arguments.get('message') or arguments.get('text') or arguments.get('prompt'))
        if isinstance(recipient, str) and message:
            state['communications'].append({'ts': ts, 'to': _short(recipient, 256),
                                            'text': _short(message, 6000), 'callId': item_id})
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


def _event_type(payload):
    return str(payload.get('type') or '').replace('.', '_').replace('-', '_')


def _note_communication(state, recipient, text, ts, call_id):
    text = _plain_message(text)
    if not isinstance(recipient, str) or not recipient.strip() or not text:
        return
    state['communications'].append({'ts': ts, 'to': _short(recipient, 256),
                                    'text': _short(text, 6000), 'callId': _short(call_id, 256)})


def _collab(state, item, ts):
    name = _short(item.get('tool') or 'collab', 80)
    _tool(state, {'id': item.get('id'), 'name': name, 'status': item.get('status'), 'type': name}, ts, True)
    agents = item.get('receiver_agents') if isinstance(item.get('receiver_agents'), list) else []
    by_id = {}
    for agent in agents:
        if isinstance(agent, dict) and isinstance(agent.get('thread_id'), str):
            by_id[agent['thread_id']] = agent
    receivers = item.get('receiver_thread_ids') if isinstance(item.get('receiver_thread_ids'), list) else []
    linked = []
    for child in receivers:
        if isinstance(child, str) and child and child not in linked:
            linked.append(child)
    for child in linked:
        agent = by_id.get(child, {})
        _record_link(state, {
            'agent_id': child, 'agent_path': agent.get('agent_path') or '',
            'agent_role': agent.get('agent_role') or '',
            'agent_nickname': agent.get('agent_nickname') or '',
            'model': item.get('model') or '',
        })
    if name.split('.')[-1] in ('send_input', 'send_message', 'followup_task', 'send_message_to_thread') and len(linked) == 1:
        _note_communication(state, linked[0], item.get('prompt') or item.get('message'), ts, item.get('id'))


def _legacy_event(state, payload, ts):
    """Older rollout progress events that are not item_completed records."""
    typ = _event_type(payload)
    call_id = payload.get('call_id') or payload.get('id')
    if typ in ('exec_command_begin', 'exec_command_end'):
        exit_code = payload.get('exit_code')
        _tool(state, {
            'call_id': call_id, 'name': 'exec_command', 'type': 'command_execution',
            'command': payload.get('command'),
            'exit_code': exit_code if type(exit_code) is int else 0,
            'status': 'completed' if typ == 'exec_command_end' else '',
        }, ts, typ == 'exec_command_end')
        return True
    if typ == 'patch_apply_end':
        changes = payload.get('changes')
        count = len(changes) if isinstance(changes, dict) else 0
        status = payload.get('status') or ('completed' if payload.get('success') is not False else 'failed')
        _tool(state, {'call_id': call_id, 'name': 'apply_patch', 'status': status,
                      'arguments': {'description': 'apply_patch' + (' · %d files' % count if count else '')}}, ts, True)
        return True
    if typ == 'web_search_end':
        query = payload.get('query') if isinstance(payload.get('query'), str) else 'web_search'
        _tool(state, {'call_id': call_id, 'name': 'web_search', 'status': 'completed',
                      'arguments': {'description': query}}, ts, True)
        return True
    if typ == 'mcp_tool_call_end':
        invocation = _dict(payload.get('invocation'))
        server = _short(invocation.get('server'), 40)
        tool_name = _short(invocation.get('tool'), 60)
        name = (server + '/' + tool_name) if server and tool_name else (tool_name or server or 'mcp')
        failed = isinstance(payload.get('result'), dict) and 'Err' in payload.get('result')
        _tool(state, {'call_id': call_id, 'name': name, 'status': 'failed' if failed else 'completed'}, ts, True)
        return True
    if typ == 'image_generation_end':
        failed = isinstance(payload.get('failure'), dict) or payload.get('status') in ('failed', 'error')
        _tool(state, {'call_id': call_id, 'name': 'image_generation',
                      'status': 'failed' if failed else 'completed'}, ts, True)
        return True
    if typ == 'sub_agent_activity':
        _record_link(state, payload)
        interrupted = payload.get('kind') == 'interrupted'
        _tool(state, {'id': payload.get('event_id'), 'name': 'SubAgentActivity', 'type': 'SubAgentActivity',
                      'status': 'interrupted' if interrupted else ''}, ts, payload.get('kind') != 'started')
        return True
    return False


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
        thread = payload.get('thread_token_usage')
        if not isinstance(thread, dict):
            thread = event.get('thread_token_usage') if isinstance(event.get('thread_token_usage'), dict) else None
        if isinstance(thread, dict) and ('input_tokens' in thread or 'inputTokens' in thread):
            _usage(state, thread)
        else:
            per = payload.get('usage') if isinstance(payload.get('usage'), dict) else None
            _add_response_usage(state, per, payload.get('response_id') or event.get('response_id') or '')
    elif kind == 'inter_agent_communication':
        _note_communication(state, payload.get('recipient'), payload.get('content'), ts, payload.get('id'))
    elif kind == 'response_item':
        typ = _item_type(payload)
        if typ == 'message':
            _message(state, payload.get('role'), _visible_text(payload.get('content')), ts, 'response', _short(payload.get('id'), 256))
        elif typ == 'agentmessage':
            _message(state, 'assistant', payload.get('text') or _visible_text(payload.get('content')), ts, 'response', _short(payload.get('id'), 256))
        elif typ in ('functioncall', 'customtoolcall'):
            _tool(state, payload, ts)
        elif typ in ('functioncalloutput', 'customtoolcalloutput', 'localshellcalloutput'):
            _tool_result(state, payload, ts)
        elif typ in ('localshellcall', 'websearchcall', 'imagegenerationcall'):
            action = _dict(payload.get('action'))
            item = dict(payload)
            if typ == 'localshellcall':
                if item.get('command') is None and action.get('command') is not None:
                    item['command'] = action.get('command')
                item['name'] = item.get('name') or 'shell'
            elif typ == 'websearchcall':
                query = action.get('query') if isinstance(action.get('query'), str) else payload.get('query')
                item['name'] = item.get('name') or 'web_search'
                item['arguments'] = {'description': query if isinstance(query, str) else 'web_search'}
            else:
                item['name'] = item.get('name') or 'image_generation'
            _tool(state, item, ts, _norm_status(item.get('status')) in ('completed', 'failed', 'incomplete', 'declined'))
        elif typ == 'reasoning':
            state['thinking_count'] += 1
    elif kind == 'event_msg':
        typ = _event_type(payload)
        if typ == 'token_count':
            info = payload.get('info')
            if isinstance(info, dict):
                _usage(state, info.get('total_token_usage'))
        elif typ in ('task_started', 'turn_started'):
            state['ended'], state['error'] = None, False
            state['message_copies'].clear()
        elif typ in ('task_complete', 'task_completed', 'turn_complete', 'turn_completed'):
            state['ended'] = ts or state['last']
        elif typ in ('turn_aborted', 'error', 'task_failed', 'stream_error'):
            state['error'] = True
            state['ended'] = ts or state['last']
        elif typ in ('user_message', 'agent_message'):
            role = 'user' if typ == 'user_message' else 'assistant'
            _message(state, role, _plain_message(payload.get('message')) or _visible_text(payload.get('content')), ts, 'event', _short(payload.get('id'), 256))
        elif typ == 'item_completed':
            _completed_item(state, payload, ts)
        elif _legacy_event(state, payload, ts):
            pass


def _completed_item(state, payload, ts):
    item = _dict(payload.get('item'))
    itype = _item_type(item)
    iid = _short(item.get('id'), 256)
    if itype in ('usermessage', 'agentmessage'):
        role = 'user' if itype == 'usermessage' else 'assistant'
        _message(state, role, _visible_text(item.get('content')) or _plain_message(item.get('text')), ts, 'completed', iid)
    elif itype == 'plan':
        _tasks_from_lines(state, item.get('text'))
    elif itype == 'todolist':
        _tasks_from_items(state, item.get('items') if isinstance(item.get('items'), list) else item.get('plan'))
    elif itype == 'subagentactivity':
        _record_link(state, item)
        if iid and not _remember(state, ('activity', iid)):
            return
        _tool(state, {'id': iid, 'name': 'SubAgentActivity', 'type': 'SubAgentActivity', 'status': item.get('status')}, ts, True)
    elif itype == 'collabagenttoolcall':
        _collab(state, item, ts)
    elif itype == 'functioncalloutput':
        _tool_result(state, item, ts)
    elif itype == 'dynamictoolcall':
        copied = dict(item)
        copied['name'] = copied.get('name') or copied.get('tool') or 'dynamic_tool'
        _tool(state, copied, ts, True)
    elif itype == 'websearch':
        copied = dict(item)
        copied['name'] = copied.get('name') or 'web_search'
        if not copied.get('arguments') and isinstance(copied.get('query'), str):
            copied['arguments'] = {'description': copied['query']}
        _tool(state, copied, ts, True)
    elif itype in ('commandexecution', 'mcptoolcall', 'filechange', 'extension', 'imageview', 'imagegeneration'):
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


def _scoped_tasks(state, owner, scope):
    rows = []
    for task in state.get('tasks') or []:
        if not isinstance(task, dict):
            continue
        row = dict(task)
        if scope:
            row['id'] = _short(str(scope), 80) + ':' + str(row.get('id') or '')
        if owner and not row.get('owner'):
            row['owner'] = owner
        rows.append(row)
    return rows


def parse_session(chosen, now, stale):
    """Parse one root and verified descendants; all returned data is independent."""
    with _lock:
        warnings, conversation, members = list(chosen.get('meta', {}).get('warnings', [])), [], []
        actors = []
        try:
            state = _read_state(chosen['path'])
        except OSError:
            state = _new_state()
            _warn(state, 'Selected rollout could not be read.')
        warnings.extend(state['warnings'])
        lead = _lead(state, chosen, now, stale)
        tasks = _scoped_tasks(state, None, '')
        actors.append((chosen, state, '__lead'))
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
            member = _member(child_state, descriptor, now, stale)
            members.append(member)
            tasks.extend(_scoped_tasks(child_state, member.get('baseName'), sid))
            actors.append((descriptor, child_state, sid))
            conversation.extend({**row, 'agentId': sid} for row in child_state['conversation'])
            warnings.extend(child_state['warnings'])
            queue.extend(descriptor.get('children', []))
            known.update(child['session'] for child in descriptor.get('children', []))
            append_links(sid, child_state['links'])
            if len(seen) >= 128:
                warnings.append('Subagent limit reached; some activity was not parsed.')
                break
        aliases, identities = {}, {}
        for descriptor, actor_state, identity in actors:
            metadata = dict(descriptor.get('meta', {}), **actor_state['meta'])
            identities[identity] = next((m for m in members if m['id'] == identity),
                                        {'name': '队长', 'avatar': 'team-lead'})
            refs = [descriptor['session'], metadata.get('agent_path'), metadata.get('agent_nickname')]
            if identity == '__lead':
                refs.extend(['__lead', 'lead', 'main', '/root'])
            for ref in refs:
                if isinstance(ref, str) and ref:
                    aliases.setdefault(ref, set()).add(identity)
                    if '/' in ref:
                        aliases.setdefault(ref.rsplit('/', 1)[-1], set()).add(identity)
        comms = []
        for _descriptor, actor_state, sender in actors:
            for message in actor_state['communications']:
                candidates = aliases.get(message['to'], set())
                if len(candidates) != 1:
                    continue  # Unknown or ambiguous recipients must not become fake edges.
                recipient = next(iter(candidates))
                if recipient == sender:
                    continue
                origin, target = identities[sender], identities[recipient]
                pending = actor_state['pending'].get(message['callId'])
                failed = bool(pending and pending['row']['error'])
                comms.append({'ts': message['ts'], 'kind': 'direct' if sender == '__lead' else 'peer',
                              'from': sender, 'fromName': origin.get('name') or origin.get('baseName'),
                              'fromAvatar': origin['avatar'], 'to': recipient,
                              'toName': target.get('name') or target.get('baseName'), 'toAvatar': target['avatar'],
                              'text': ('发送失败：' if failed else '发送调用：') + message['text'],
                              'source': 'codex', 'delivery': 'failed' if failed else 'unconfirmed'})
        return copy.deepcopy({'lead': lead, 'members': members, 'tasks': tasks,
                              'comms': sorted(comms, key=lambda row: row['ts'])[-80:],
                              'conversation': sorted(conversation, key=lambda row: row['ts'])[-120:],
                              'warnings': list(dict.fromkeys(warnings))})
