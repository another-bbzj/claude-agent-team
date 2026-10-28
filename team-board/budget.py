"""会话估算成本的预算护栏。

金额来自 team.json 的公开单价，不是服务商账单。未定价的模型不计入合计，
并标成「成本为下限」，不补一个猜测数字。没有配置预算时 enabled 为 false，
页面不画这块。

档位表对应 skills/agent-team/roles.md：调研/文档 haiku，写代码/测试 sonnet，
架构和联调审查 opus。只建议降到写明的档位，不给没写档位的角色编一个。
"""

DEFAULT_WARN_RATIO = 0.8

# (家族, effort, 这类活的简称)。effort 只写进建议句，比较时只看家族高低。
ROLE_TIER = {
    'team-architect': ('opus', 'high', '架构拆解'),
    'backend-dev': ('sonnet', 'medium', '常规实现'),
    'frontend-dev': ('sonnet', 'medium', '常规实现'),
    'qa-tester': ('sonnet', 'medium', '测试'),
    'code-reviewer': ('opus', 'high', '联调审查'),
    'researcher': ('haiku', 'low', '调研'),
    'docs-writer': ('haiku', 'low', '文档'),
    'release-ops': ('sonnet', 'low', '脚本与发布'),
}
# 质量部里审查是 opus、测试是 sonnet，部门不能再猜一档。指挥部的队长不在成员列表里。
DEPT_TIER = {
    'dev': ('sonnet', 'medium', '常规实现'),
    'rnd': ('haiku', 'low', '调研'),
    'docs': ('haiku', 'low', '文档'),
    'ops': ('sonnet', 'low', '脚本与发布'),
}
TIER_RANK = {'haiku': 1, 'sonnet': 2, 'opus': 3, 'fable': 3}
STATUS_RANK = {'ok': 0, 'warn': 1, 'over': 2}
SUGGESTION_LIMIT = 8


def _finite(value):
    """正数或零以外的非法值返回 None。布尔值不算数字。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value == float('inf') or value == float('-inf'):
        return None
    return float(value)


def _positive(value):
    number = _finite(value)
    if number is None or number <= 0:
        return None
    return number


def _warn_ratio(value):
    number = _finite(value)
    if number is None or number <= 0 or number > 1:
        return None
    return number


def _clip(value, limit):
    if not isinstance(value, str):
        return ''
    return value.strip()[:limit]


def _family(model):
    low = (model or '').lower() if isinstance(model, str) else ''
    for fam in ('opus', 'sonnet', 'haiku', 'fable'):
        if fam in low:
            return fam
    return ''


def _blank(status='off', error='', warn_ratio=DEFAULT_WARN_RATIO):
    return {
        'enabled': False,
        'status': status,
        'error': error,
        'warnRatio': warn_ratio,
        'lowerBound': False,
        'lowerBoundLabel': '',
        'unpriced': 0,
        'unpricedModels': [],
        'spent': None,
        'limit': None,
        'ratio': None,
        'currency': 'USD',
        'scopes': [],
        'topSpender': None,
        'suggestions': [],
    }


def _normalize_limits(raw, label):
    if raw is None:
        return {}, ''
    if not isinstance(raw, dict):
        return None, label + ' 必须是对象'
    out = {}
    for key, value in raw.items():
        name = _clip(key, 80)
        if not name:
            return None, label + ' 的键不能为空'
        number = _positive(value)
        if number is None:
            return None, '{}.{} 必须是大于 0 的数字'.format(label, name)
        out[name] = number
    return out, ''


def normalize_config(raw):
    """把 team.json 的 budget 收成内部结构。

    返回 (config, error)。没有设置任何上限时 config 是 None、error 为空，
    表示「没设预算」。非法时 config 是 None，error 是一句中文原因。
    """
    if raw is None or raw == {}:
        return None, ''
    if not isinstance(raw, dict):
        return None, 'budget 必须是对象'
    warn = DEFAULT_WARN_RATIO
    if 'warnRatio' in raw and raw.get('warnRatio') is not None:
        warn = _warn_ratio(raw.get('warnRatio'))
        if warn is None:
            return None, 'budget.warnRatio 必须是大于 0、不超过 1 的数字'
    usd = None
    if 'usd' in raw and raw.get('usd') is not None:
        usd = _positive(raw.get('usd'))
        if usd is None:
            return None, 'budget.usd 必须是大于 0 的数字'
    members, err = _normalize_limits(raw.get('members') if 'members' in raw else None, 'budget.members')
    if err:
        return None, err
    departments, err = _normalize_limits(raw.get('departments') if 'departments' in raw else None, 'budget.departments')
    if err:
        return None, err
    if usd is None and not members and not departments:
        return None, ''
    return {'usd': usd, 'warnRatio': warn, 'members': members or {}, 'departments': departments or {}}, ''


def parse_pair_list(items, label):
    """解析重复的启动参数 name=数字。没传返回 (None, '')。"""
    if not items:
        return None, ''
    out = {}
    for raw in items:
        if not isinstance(raw, str) or '=' not in raw:
            return None, '{}上限要写成 名字=数字，例如 backend-dev=1.5'.format(label)
        key, _, val = raw.partition('=')
        name = _clip(key, 80)
        if not name:
            return None, '{}上限的名字不能为空'.format(label)
        try:
            number = float(val.strip())
        except (TypeError, ValueError):
            number = None
        number = _positive(number)
        if number is None:
            return None, '{}上限 {} 必须是大于 0 的数字'.format(label, name)
        out[name] = number
    return out, ''


def cli_from_args(args):
    """从 argparse 命名空间取出本次进程的覆盖项。缺字段就当没传。"""
    if args is None:
        return {}
    members, member_err = parse_pair_list(getattr(args, 'budget_member', None), '成员')
    departments, dept_err = parse_pair_list(getattr(args, 'budget_dept', None), '部门')
    errors = [err for err in (member_err, dept_err) if err]
    return {
        'usd': getattr(args, 'budget', None),
        'warn_ratio': getattr(args, 'budget_warn', None),
        'members': members,
        'departments': departments,
        'errors': errors,
    }


def _cli_touched(cli):
    if not cli:
        return False
    if cli.get('usd') is not None or cli.get('warn_ratio') is not None:
        return True
    return bool(cli.get('members') or cli.get('departments'))


def merge_config(file_budget, cli=None):
    """文件配置打底，启动参数只盖过自己写到的字段。不改传入的字典。"""
    cli = cli or {}
    errors = [err for err in (cli.get('errors') or []) if err]
    if errors:
        return None, errors[0]
    touched = _cli_touched(cli)
    if file_budget is not None and file_budget != {} and not isinstance(file_budget, dict):
        if not touched:
            return None, 'budget 必须是对象'
        base = {}
    else:
        base = dict(file_budget or {})
    if cli.get('usd') is not None:
        base['usd'] = cli['usd']
    if cli.get('warn_ratio') is not None:
        base['warnRatio'] = cli['warn_ratio']
    if cli.get('members'):
        current = base.get('members')
        current = dict(current) if isinstance(current, dict) else {}
        current.update(cli['members'])
        base['members'] = current
    if cli.get('departments'):
        current = base.get('departments')
        current = dict(current) if isinstance(current, dict) else {}
        current.update(cli['departments'])
        base['departments'] = current
    return normalize_config(base)


def _priced(raw):
    """返回 (金额或 None, 是否未定价)。金额不含猜测。"""
    if not isinstance(raw, dict):
        return 0.0, False
    has_cost = 'cost' in raw
    amount = _finite(raw.get('cost')) if has_cost else None
    if amount is not None and amount >= 0:
        return amount, False
    model = raw.get('model') if isinstance(raw.get('model'), str) else ''
    if has_cost or model:
        return None, True
    return 0.0, False


def _people(snapshot):
    lead = snapshot.get('lead') if isinstance(snapshot.get('lead'), dict) else {}
    amount, unpriced = _priced(lead)
    rows = [{
        'id': '__lead',
        'name': _clip(lead.get('name'), 80) or '队长',
        'kind': 'lead',
        'agentType': '',
        'dept': _clip(lead.get('dept'), 40) or 'hq',
        'modelFamily': _family(lead.get('modelFamily')) or _family(lead.get('model')),
        'model': _clip(lead.get('model'), 120),
        'dispatchModel': '',
        'definedModel': '',
        'amount': amount,
        'unpriced': unpriced,
    }]
    members = snapshot.get('members')
    if not isinstance(members, list):
        return rows
    for raw in members:
        if not isinstance(raw, dict):
            continue
        amount, unpriced = _priced(raw)
        rows.append({
            'id': _clip(raw.get('id'), 80),
            'name': _clip(raw.get('name'), 80) or _clip(raw.get('id'), 80) or '成员',
            'kind': 'member',
            'agentType': _clip(raw.get('agentType'), 80),
            'dept': _clip(raw.get('dept'), 40),
            'modelFamily': _family(raw.get('modelFamily')) or _family(raw.get('model')),
            'model': _clip(raw.get('model'), 120),
            'dispatchModel': _clip(raw.get('dispatchModel'), 120),
            'definedModel': _clip(raw.get('definedModel'), 120),
            'amount': amount,
            'unpriced': unpriced,
        })
    return rows


def _classify(spent, limit, warn_ratio):
    spent = round(spent, 4)
    limit = round(limit, 4)
    ratio = round(spent / limit, 4) if limit else 0.0
    if spent >= limit:
        status = 'over'
    elif (spent / limit if limit else 0.0) >= warn_ratio:
        status = 'warn'
    else:
        status = 'ok'
    return status, ratio, spent, limit


def _scope(kind, ident, name, spent, limit, warn_ratio, lower, present, extra=None):
    status, ratio, spent, limit = _classify(spent, limit, warn_ratio)
    row = {
        'kind': kind,
        'id': ident,
        'name': name,
        'limit': limit,
        'spent': spent,
        'ratio': ratio,
        'status': status,
        'lowerBound': bool(lower),
        'present': bool(present),
    }
    if extra:
        row.update(extra)
    return row


def _dept_names(snapshot):
    names = {}
    departments = snapshot.get('departments')
    if not isinstance(departments, list):
        return names
    for raw in departments:
        if isinstance(raw, dict) and isinstance(raw.get('id'), str) and raw.get('id').strip():
            names[raw['id'].strip()] = _clip(raw.get('name'), 80) or raw['id'].strip()
    return names


def _sum_dept(people, dept_id):
    spent = 0.0
    lower = False
    present = False
    for person in people:
        if person['dept'] != dept_id:
            continue
        if person['kind'] != 'member' and not (dept_id == 'hq' and person['kind'] == 'lead'):
            continue
        present = True
        if person['unpriced']:
            lower = True
            continue
        spent += person['amount'] or 0
    if dept_id == 'hq':
        lead = next((p for p in people if p['kind'] == 'lead'), None)
        if lead:
            present = True
            if lead['unpriced']:
                lower = True
            elif lead['dept'] != 'hq':
                spent += lead['amount'] or 0
            # dept == hq 的队长已经在循环里加过
    return round(spent, 4), lower, present


def _member_keys(person, limits):
    hits = []
    ident = person['id']
    name = person['name']
    agent_type = person['agentType']
    for key, limit in limits.items():
        if key == ident or (name and key == name) or (agent_type and key == agent_type):
            hits.append((key, limit))
    return hits


def _suggestion(person, dept_names):
    if person['kind'] != 'member':
        return None
    agent_type = person['agentType']
    if agent_type in ROLE_TIER:
        rec_fam, effort, work = ROLE_TIER[agent_type]
        where = '协议档位是 {} · {}（{}）'.format(rec_fam, effort, work)
    elif person['dept'] in DEPT_TIER:
        rec_fam, effort, work = DEPT_TIER[person['dept']]
        dept_label = dept_names.get(person['dept']) or person['dept']
        where = '{}的协议档位是 {} · {}（{}）'.format(dept_label, rec_fam, effort, work)
    else:
        return None
    fam = person['modelFamily']
    if fam not in TIER_RANK or TIER_RANK[fam] <= TIER_RANK[rec_fam]:
        return None
    dispatch = _family(person['dispatchModel'])
    if dispatch in TIER_RANK and TIER_RANK[dispatch] > TIER_RANK[rec_fam]:
        how = '派工把 model 指定成了 {}'.format(dispatch)
    else:
        how = '这次用了 {}'.format(fam)
    defined = _family(person['definedModel'])
    extra = ''
    if defined in TIER_RANK and TIER_RANK[defined] > TIER_RANK[rec_fam]:
        extra = '成员定义里的 model 也改回 {}。'.format(rec_fam)
    who = person['name']
    if agent_type:
        who = '{}（{}）'.format(person['name'], agent_type)
    action = '{}{}，{}。下次派工把 model 设为 {}。{}'.format(who, how, where, rec_fam, extra).strip()
    return {
        'memberId': person['id'],
        'name': person['name'],
        'agentType': agent_type,
        'dept': person['dept'],
        'modelFamily': fam,
        'recommended': rec_fam,
        'recommendedLabel': '{} · {}'.format(rec_fam, effort),
        'work': work,
        'action': action,
        'cost': None if person['unpriced'] else round(person['amount'] or 0, 4),
    }


def _unpriced_models(snapshot, people):
    totals = snapshot.get('totals') if isinstance(snapshot.get('totals'), dict) else {}
    models = []
    raw = totals.get('unpricedModels')
    if isinstance(raw, list):
        for item in raw:
            text = _clip(item, 120)
            if text and text not in models:
                models.append(text)
    if not models:
        for person in people:
            if person['unpriced'] and person['model'] and person['model'] not in models:
                models.append(person['model'])
    return models[:12]


def budget_status(snapshot, cli=None, file_budget=None):
    """根据快照里的已知成本和预算配置，算出预算状态。不修改 snapshot。"""
    if not isinstance(snapshot, dict):
        return _blank('invalid', '快照必须是对象')
    config, error = merge_config(file_budget, cli)
    if error:
        return _blank('invalid', error)
    if config is None:
        return _blank()
    warn = config['warnRatio']
    people = _people(snapshot)
    dept_names = _dept_names(snapshot)
    unpriced_people = [p for p in people if p['unpriced']]
    models = _unpriced_models(snapshot, people)
    lower = bool(unpriced_people or models)
    totals = snapshot.get('totals') if isinstance(snapshot.get('totals'), dict) else {}
    session_spent = _finite(totals.get('cost')) if 'cost' in totals else None
    if session_spent is None or session_spent < 0:
        session_spent = round(sum((p['amount'] or 0) for p in people if not p['unpriced']), 4)
    else:
        session_spent = round(session_spent, 4)
    if lower:
        if models:
            label = '成本为下限（未计入：{}）'.format('、'.join(models))
        else:
            label = '成本为下限'
    else:
        label = ''
    scopes = []
    if config['usd'] is not None:
        scopes.append(_scope('session', 'session', '本次会话', session_spent, config['usd'], warn, lower, True))
    used_member_keys = set()
    for person in people:
        if person['kind'] != 'member':
            continue
        for key, limit in _member_keys(person, config['members']):
            used_member_keys.add(key)
            scopes.append(_scope(
                'member', person['id'] or key, person['name'],
                0.0 if person['unpriced'] else person['amount'],
                limit, warn, person['unpriced'], True,
                {'capKey': key, 'agentType': person['agentType'], 'modelFamily': person['modelFamily']},
            ))
    for key, limit in config['members'].items():
        if key not in used_member_keys:
            scopes.append(_scope('member', key, key, 0.0, limit, warn, False, False, {'capKey': key}))
    for key, limit in config['departments'].items():
        spent, dept_lower, present = _sum_dept(people, key)
        name = dept_names.get(key) or key
        known = key in dept_names or present
        scopes.append(_scope('department', key, name, spent, limit, warn, dept_lower, known))
    status = 'ok'
    for scope in scopes:
        if STATUS_RANK[scope['status']] > STATUS_RANK[status]:
            status = scope['status']
    priced = [p for p in people if not p['unpriced']]
    priced.sort(key=lambda p: (-p['amount'], p['name'], p['id']))
    top = None
    if priced:
        person = priced[0]
        top = {
            'id': person['id'],
            'name': person['name'],
            'kind': person['kind'],
            'agentType': person['agentType'],
            'dept': person['dept'],
            'modelFamily': person['modelFamily'],
            'cost': round(person['amount'], 4),
        }
    suggestions = []
    for person in people:
        item = _suggestion(person, dept_names)
        if item:
            suggestions.append(item)
    suggestions.sort(key=lambda item: (item['cost'] is None, -(item['cost'] or 0), item['name'], item['memberId']))
    suggestions = suggestions[:SUGGESTION_LIMIT]
    session = next((s for s in scopes if s['kind'] == 'session'), None)
    return {
        'enabled': True,
        'status': status,
        'error': '',
        'warnRatio': warn,
        'lowerBound': lower,
        'lowerBoundLabel': label,
        'unpriced': len(unpriced_people),
        'unpricedModels': models,
        'spent': session_spent,
        'limit': None if session is None else session['limit'],
        'ratio': None if session is None else session['ratio'],
        'currency': 'USD',
        'scopes': scopes,
        'topSpender': top,
        'suggestions': suggestions,
    }


def attach_budget(snapshot, cli=None, file_budget=None):
    """把 budget 写进快照。旧字段保持原样。"""
    if not isinstance(snapshot, dict):
        return snapshot
    snapshot['budget'] = budget_status(snapshot, cli, file_budget)
    return snapshot


def over_budget_demo(snapshot):
    """给一份已有合计的合成快照配一个必然超出的预算。不改成员金额。"""
    totals = snapshot.get('totals') if isinstance(snapshot, dict) else None
    spent = _finite((totals or {}).get('cost')) if isinstance(totals, dict) else None
    if spent is None or spent <= 0:
        spent = 1.0
    config = {'usd': round(spent * 0.5, 4), 'warnRatio': DEFAULT_WARN_RATIO, 'members': {}, 'departments': {}}
    members = snapshot.get('members') if isinstance(snapshot, dict) else None
    if isinstance(members, list):
        for member in members:
            if not isinstance(member, dict) or member.get('agentType') != 'docs-writer':
                continue
            cost = _finite(member.get('cost'))
            if cost is not None and cost > 0:
                config['members']['docs-writer'] = round(cost * 0.5, 4)
            break
    departments = snapshot.get('departments') if isinstance(snapshot, dict) else None
    if isinstance(departments, list):
        for dept in departments:
            if isinstance(dept, dict) and dept.get('id') == 'docs':
                cost = _finite(dept.get('cost'))
                if cost is not None and cost > 0:
                    config['departments']['docs'] = round(cost * 0.5, 4)
                break
    return config
