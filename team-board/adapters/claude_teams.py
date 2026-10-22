"""读取官方 Claude Code Agent Teams 目录。

字段取舍见 docs/AGENT_TEAMS.md。官方没有公布的 JSON 键不会被要求存在；
未知键留在 raw / rawConfig 里。坏文件记警告，不向外抛。
"""
import json
import os
from pathlib import Path

NAME_KEYS = ('name', 'teammate_name')
AGENT_ID_KEYS = ('agentId', 'agent_id')
AGENT_TYPE_KEYS = ('agentType', 'agent_type')
TASK_ID_KEYS = ('id', 'taskId', 'task_id')
SUBJECT_KEYS = ('subject', 'task_subject')
DESCRIPTION_KEYS = ('description', 'task_description')
ACTIVE_FORM_KEYS = ('activeForm', 'active_form')
KNOWN_STATUSES = ('pending', 'in_progress', 'completed', 'deleted')
LEAD_TYPE = 'team-lead'
MAX_TASK_FILE_BYTES = 2 * 1024 * 1024


def default_root():
    """官方的 Claude 配置目录。看板会话发现仍用 Path.home()/.claude，不调用这里。"""
    override = os.environ.get('CLAUDE_CONFIG_DIR')
    if override:
        return Path(override)
    return Path.home() / '.claude'


def team_name_for_session(session_id):
    """session- 加上会话 ID 的前 8 个字符。短于 8 个字符时用全部。"""
    if not isinstance(session_id, str):
        return ''
    text = session_id.strip()
    if not text:
        return ''
    return 'session-' + text[:8]


def discover_team_names(root):
    """teams/ 与 tasks/ 下的目录名。会话结束后配置目录会消失，只剩任务目录也算。"""
    root = Path(root)
    names = set()
    for folder in ('teams', 'tasks'):
        base = root / folder
        if not _safe_dir(root, base):
            continue
        try:
            children = list(base.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name.startswith('.'):
                continue
            if _safe_dir(root, child):
                names.add(child.name)
    return sorted(names)


def load_team(root, team_name):
    """解析一个团队。返回 members、tasks、mailboxes、warnings、rawConfig。"""
    root = Path(root)
    team = {
        'teamName': team_name if isinstance(team_name, str) else '',
        'members': [],
        'tasks': [],
        'mailboxes': [],
        'warnings': [],
        'rawConfig': None,
    }
    if not _valid_team_name(team['teamName']):
        team['warnings'].append('团队名无效')
        return team

    config_path = root / 'teams' / team['teamName'] / 'config.json'
    tasks_dir = root / 'tasks' / team['teamName']
    inbox_dir = root / 'teams' / team['teamName'] / 'inboxes'
    has_config = _safe_file(root, config_path)
    has_tasks = _safe_dir(root, tasks_dir)
    has_inbox = _safe_dir(root, inbox_dir)
    if not (has_config or has_tasks or has_inbox):
        team['warnings'].append('没有找到该团队的配置、任务或邮箱目录')
        return team
    if not has_config and (has_tasks or has_inbox):
        team['warnings'].append('没有 config.json；会话结束后官方会删除团队配置目录')

    if has_config:
        _read_config(config_path, team)
    if has_tasks:
        _read_tasks(root, tasks_dir, team)
    if has_inbox:
        _read_inboxes(root, inbox_dir, team)
    leads = [member for member in team['members'] if member['role'] == 'lead']
    if has_config and not leads:
        team['warnings'].append('members 里没有 agent type 为 team-lead 的条目')
    elif len(leads) > 1:
        team['warnings'].append('members 里有多名 team-lead')
    return team


def _valid_team_name(name):
    if not name or name.startswith('.'):
        return False
    return '/' not in name and '\\' not in name and name not in ('.', '..')


def _within(root, path):
    try:
        path.resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return True


def _safe_dir(root, path):
    try:
        if not path.exists():
            return False
        resolved = path.resolve()
    except OSError:
        return False
    return resolved.is_dir() and _within(root, resolved)


def _safe_file(root, path):
    try:
        if not path.exists() or path.is_symlink() and not _within(root, path):
            return False
        resolved = path.resolve()
    except OSError:
        return False
    return resolved.is_file() and _within(root, resolved)


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _file_size(path):
    try:
        return path.stat().st_size
    except OSError:
        return None


def _relative(root, path):
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _read_json(path):
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return None, '无法读取'
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, '不是合法 JSON'


def _pick_string(obj, keys, label, warnings):
    found = []
    for key in keys:
        if key not in obj:
            continue
        value = obj[key]
        if isinstance(value, str) and value != '':
            found.append((key, value))
        elif value not in (None, ''):
            warnings.append(label + ' 的 ' + key + ' 不是字符串，已忽略')
    if not found:
        return None
    chosen = found[0][1]
    if any(value != chosen for _, value in found[1:]):
        warnings.append(label + ' 的候选键不一致，采用 ' + found[0][0])
    return chosen


def _read_config(path, team):
    data, error = _read_json(path)
    if error:
        team['warnings'].append('config.json ' + error)
        return
    if not isinstance(data, dict):
        team['warnings'].append('config.json 不是对象，已跳过')
        return
    team['rawConfig'] = data
    members = data.get('members')
    if not isinstance(members, list):
        team['warnings'].append('config.json 没有 members 数组')
        return
    for index, item in enumerate(members):
        if not isinstance(item, dict):
            team['warnings'].append('members[' + str(index) + '] 不是对象，已跳过')
            continue
        name = _pick_string(item, NAME_KEYS, '成员', team['warnings'])
        agent_id = _pick_string(item, AGENT_ID_KEYS, '成员', team['warnings'])
        agent_type = _pick_string(item, AGENT_TYPE_KEYS, '成员', team['warnings'])
        if name is None and agent_id is None:
            team['warnings'].append('members[' + str(index) + '] 没有名字或 agent ID')
        team['members'].append({
            'name': name,
            'agentId': agent_id,
            'agentType': agent_type,
            'role': 'lead' if agent_type == LEAD_TYPE else 'teammate',
            'raw': item,
        })


def _read_tasks(root, directory, team):
    try:
        paths = sorted(directory.iterdir())
    except OSError:
        team['warnings'].append('无法列出任务目录')
        return
    for path in paths:
        if path.name.startswith('.') or path.suffix.lower() != '.json':
            continue
        if not _safe_file(root, path):
            team['warnings'].append('跳过任务文件 ' + path.name)
            continue
        size = _file_size(path)
        if size is None:
            team['warnings'].append(path.name + ' 无法读取')
            continue
        if size > MAX_TASK_FILE_BYTES:
            team['warnings'].append(path.name + ' 超过 2MB，已跳过')
            continue
        data, error = _read_json(path)
        source = _relative(root, path)
        if error:
            team['warnings'].append(path.name + ' ' + error)
            continue
        found = _tasks_from_value(data, source, team['warnings'])
        mtime = _mtime(path)
        for task in found:
            task['sourceMtime'] = mtime
        team['tasks'].extend(found)


def _tasks_from_value(value, source, warnings):
    if isinstance(value, list):
        return _task_list(value, source, warnings)
    if isinstance(value, dict) and isinstance(value.get('tasks'), list):
        return _task_list(value['tasks'], source, warnings)
    if isinstance(value, dict) and isinstance(value.get('task'), dict):
        return _task_list([value['task']], source, warnings)
    if isinstance(value, dict) and _looks_like_task(value):
        return _task_list([value], source, warnings)
    warnings.append(source + ' 不是已公布的任务形状，已跳过')
    return []


def _task_list(items, source, warnings):
    tasks = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            warnings.append(source + ' 的第 ' + str(index) + ' 条不是对象，已跳过')
            continue
        tasks.append(_normalize_task(item, source, warnings))
    return tasks


def _looks_like_task(obj):
    keys = TASK_ID_KEYS + SUBJECT_KEYS + ('status',)
    return any(key in obj for key in keys)


def _string_list(obj, key, warnings):
    if key not in obj:
        return None
    value = obj[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        warnings.append('任务的 ' + key + ' 不是字符串数组，已忽略')
        return None
    return list(value)


def _normalize_task(obj, source, warnings):
    status = obj.get('status') if isinstance(obj.get('status'), str) else None
    if 'status' in obj and not isinstance(obj.get('status'), str):
        warnings.append('任务状态不是字符串，已忽略')
    normalized = None
    if status == 'in progress':
        normalized = 'in_progress'
    elif status in KNOWN_STATUSES:
        normalized = status
    elif status is not None:
        warnings.append('未识别的任务状态')
    owner = obj.get('owner') if isinstance(obj.get('owner'), str) else None
    if 'owner' in obj and not isinstance(obj.get('owner'), str):
        warnings.append('任务 owner 不是字符串，已忽略')
    return {
        'id': _pick_string(obj, TASK_ID_KEYS, '任务', warnings),
        'subject': _pick_string(obj, SUBJECT_KEYS, '任务', warnings),
        'description': _pick_string(obj, DESCRIPTION_KEYS, '任务', warnings),
        'status': status,
        'normalizedStatus': normalized,
        'owner': owner,
        'blocks': _string_list(obj, 'blocks', warnings),
        'blockedBy': _string_list(obj, 'blockedBy', warnings),
        'activeForm': _pick_string(obj, ACTIVE_FORM_KEYS, '任务', warnings),
        'source': source,
        'raw': obj,
    }


def _read_inboxes(root, directory, team):
    try:
        paths = sorted(directory.iterdir())
    except OSError:
        team['warnings'].append('无法列出邮箱目录')
        return
    for path in paths:
        if path.name.startswith('.') or path.suffix.lower() != '.json':
            continue
        if not _safe_file(root, path):
            team['warnings'].append('跳过邮箱 ' + path.name)
            continue
        data, error = _read_json(path)
        mailbox = {'agentName': path.stem, 'path': _relative(root, path), 'entries': [], 'raw': None}
        if error:
            team['warnings'].append(path.name + ' ' + error)
        elif isinstance(data, list):
            mailbox['entries'] = data
            mailbox['raw'] = data
        else:
            team['warnings'].append(path.name + ' 不是 JSON 数组，已整份保留')
            mailbox['entries'] = [data]
            mailbox['raw'] = data
        team['mailboxes'].append(mailbox)
