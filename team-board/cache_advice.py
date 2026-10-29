"""Prompt-cache 命中率偏低时的诊断。

只根据调用方交来的请求记录说话。记录里没有的字段（时间戳、系统提示、工具列表）
就当没看见，不补一个原因。金额用调用方给的价格表，按「这些写入若变成读取」或
「没被读到的写入比普通输入多付的差价」估算，并在句子里标明是估算。

同一段 token 只归给一条已经成立的原因，优先级是：换模型、系统提示变化、
工具列表变化、间隔超过 TTL，剩下的重复冷写入才算「缓存反复重建」。
"""

import hashlib
import re
from datetime import datetime

DEFAULT_HIT_RATIO = 0.5
DEFAULT_MIN_REQUESTS = 3
DEFAULT_MIN_CONTEXT = 1000
DEFAULT_REBUILD_RATIO = 2.0
DEFAULT_MIN_COLD_WRITES = 2
DEFAULT_TTL_SECONDS = 300
DEFAULT_MIN_GAPS = 1
DEFAULT_MIN_MODEL_SWITCHES = 2
DEFAULT_MIN_SUBAGENTS = 2
DEFAULT_MAX_SUGGESTIONS = 6

# (下限, 上限)。整数项不允许小数。
_RANGES = {
    'hitRatio': (0.05, 1.0, False),
    'minRequests': (2, 100, True),
    'minContextTokens': (1, 10000000, True),
    'rebuildRatio': (1.0, 100.0, False),
    'minColdWrites': (2, 100, True),
    'ttlSeconds': (60, 86400, True),
    'minGaps': (1, 100, True),
    'minModelSwitches': (2, 50, True),
    'minSubagents': (2, 50, True),
    'maxSuggestions': (1, 12, True),
}
_DEFAULTS = {
    'hitRatio': DEFAULT_HIT_RATIO,
    'minRequests': DEFAULT_MIN_REQUESTS,
    'minContextTokens': DEFAULT_MIN_CONTEXT,
    'rebuildRatio': DEFAULT_REBUILD_RATIO,
    'minColdWrites': DEFAULT_MIN_COLD_WRITES,
    'ttlSeconds': DEFAULT_TTL_SECONDS,
    'minGaps': DEFAULT_MIN_GAPS,
    'minModelSwitches': DEFAULT_MIN_MODEL_SWITCHES,
    'minSubagents': DEFAULT_MIN_SUBAGENTS,
    'maxSuggestions': DEFAULT_MAX_SUGGESTIONS,
}
_CAUSE_PRIORITY = ('model', 'system', 'tools', 'ttl')
_NOTE = '估算按 team.json 的公开单价，不是服务商账单。同一段 token 只归一条原因，这些数字也不能加总当账单。'


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float('inf'), float('-inf')):
        return None
    return float(value)


def _clip(value, limit):
    if not isinstance(value, str):
        return ''
    return value.strip()[:limit]


def _num(value):
    text = '{:.6f}'.format(value).rstrip('0').rstrip('.')
    if '.' not in text:
        return text + '.00'
    if len(text.split('.', 1)[1]) < 2:
        return text + '0'
    return text


def _usd(value):
    return '$' + _num(value)


def _tokens(value):
    return '{:,}'.format(int(value))


def _pct(hit):
    pct = hit * 100
    if abs(pct - round(pct)) < 0.05:
        return '{}%'.format(int(round(pct)))
    return '{:.1f}%'.format(pct)


def fingerprint(text):
    """系统提示原文不进建议，只留 12 位十六进制。"""
    if not isinstance(text, str):
        return ''
    return hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:12]


def _public_fp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    token = value.strip()
    if re.fullmatch(r'[0-9a-f]{12}', token):
        return token
    return fingerprint(token)


def model_family(model):
    low = model.lower() if isinstance(model, str) else ''
    for fam in ('opus', 'sonnet', 'haiku', 'fable'):
        if fam in low:
            return fam
    low = low.rsplit('/', 1)[-1]
    head = re.split(r'[-_/:.]', low, maxsplit=1)[0] if low else ''
    return head or ''


def lookup_price(model, table):
    """和 team_board.price_for 一样：价格表键的最长子串，否则家族名。"""
    if not isinstance(table, dict):
        return '', None
    low = model.lower() if isinstance(model, str) else ''
    best_key, best = '', None
    for key, row in table.items():
        if not isinstance(key, str) or not isinstance(row, dict):
            continue
        name = key.lower()
        if name and name in low and len(name) > len(best_key):
            best_key, best = name, row
    if best is not None:
        return best_key, best
    fam = model_family(model)
    row = table.get(fam)
    if isinstance(row, dict):
        return fam, row
    return '', None


def normalize_config(raw):
    """team.json 的 cacheAdvice。缺省用默认值。

    返回 (config, error)。false 或 enabled=false 表示关掉，error 为空。
    非法时 config 是 None。
    """
    if raw is None:
        raw = {}
    if raw is False:
        return {'enabled': False}, ''
    if not isinstance(raw, dict):
        return None, 'cacheAdvice 必须是对象或 false'
    if raw.get('enabled') is False:
        return {'enabled': False}, ''
    config = dict(_DEFAULTS)
    config['enabled'] = True
    for key, (low, high, whole) in _RANGES.items():
        if key not in raw or raw.get(key) is None:
            continue
        number = _finite(raw.get(key))
        if number is None or number < low or number > high or (whole and int(number) != number):
            kind = '整数' if whole else '数字'
            return None, 'cacheAdvice.{} 必须是 {} 到 {} 的{}'.format(key, _num(low) if not whole else int(low), _num(high) if not whole else int(high), kind)
        config[key] = int(number) if whole else float(number)
    return config, ''


def _blank(status, error='', config=None):
    config = config or dict(_DEFAULTS)
    return {
        'enabled': status in ('advice', 'quiet'),
        'status': status,
        'error': error,
        'hitRatio': config.get('hitRatio', DEFAULT_HIT_RATIO),
        'ttlSeconds': config.get('ttlSeconds', DEFAULT_TTL_SECONDS),
        'sessionHit': None,
        'sessionContext': 0,
        'suggestions': [],
        'note': '' if status in ('off', 'invalid') else _NOTE,
    }


def _parse_ts(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (TypeError, ValueError):
            return None
    number = _finite(value)
    if number is None or number <= 0:
        return None
    return number


def _token(value, missing_ok):
    """非负整数 token。缺省是 0；写了但不是整数则整条请求作废。"""
    if value is None and missing_ok:
        return 0
    number = _finite(value)
    if number is None or number < 0 or int(number) != number:
        return None
    return int(number)


def _tool_names(value):
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    names = []
    for item in value[:64]:
        if isinstance(item, str) and item.strip():
            names.append(item.strip()[:80])
        elif isinstance(item, dict):
            name = item.get('name')
            if isinstance(name, str) and name.strip():
                names.append(name.strip()[:80])
    return names


def _system_text(entry):
    bags = [entry]
    message = entry.get('message')
    if isinstance(message, dict):
        bags.append(message)
    for bag in bags:
        for key in ('system', 'systemPrompt', 'system_prompt'):
            value = bag.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _tools_field(entry):
    """字段不在则 None。显式列表（含空列表）原样收成名字。"""
    bags = [entry]
    message = entry.get('message')
    if isinstance(message, dict):
        bags.append(message)
    for bag in bags:
        if 'tools' not in bag:
            continue
        names = _tool_names(bag.get('tools'))
        if names is not None:
            return names
    return None


def _real_model(model):
    return bool(model) and isinstance(model, str) and not model.startswith('<')


class Trace(object):
    """按 message.id 去重，和 UsageLedger 一样只留每条 API 消息的最后一份 usage。"""

    def __init__(self, actor, name, subagent):
        self.actor = _clip(actor, 80) or 'member'
        self.name = _clip(name, 80) or self.actor
        self.subagent = bool(subagent)
        self._by_id = {}
        self._order = []
        self._anon = 0
        self._system = None
        self._system_seen = False
        self._tools = None
        self._tools_seen = False

    def see(self, entry):
        if not isinstance(entry, dict):
            return
        system = _system_text(entry)
        if system is not None:
            self._system = fingerprint(system)
            self._system_seen = True
        if 'tools' in entry or (isinstance(entry.get('message'), dict) and 'tools' in entry.get('message')):
            names = _tools_field(entry)
            if names is not None:
                self._tools = names
                self._tools_seen = True
        if entry.get('type') != 'assistant':
            return
        message = entry.get('message') if isinstance(entry.get('message'), dict) else {}
        usage = message.get('usage') if isinstance(message.get('usage'), dict) else None
        if not usage:
            return
        read = _token(usage.get('cache_read_input_tokens'), True)
        write = _token(usage.get('cache_creation_input_tokens'), True)
        incoming = _token(usage.get('input_tokens'), True)
        outgoing = _token(usage.get('output_tokens'), True)
        if None in (read, write, incoming, outgoing):
            return
        request_id = entry.get('requestId') if isinstance(entry.get('requestId'), str) else ''
        mid = message.get('id') or (('req:' + request_id) if request_id else None)
        if not mid:
            self._anon += 1
            mid = '_anon{}'.format(self._anon)
        if not isinstance(mid, str):
            mid = str(mid)
        turn = self._by_id.get(mid)
        if turn is None:
            turn = {'actor': self.actor, 'name': self.name, 'subagent': self.subagent}
            self._by_id[mid] = turn
            self._order.append(mid)
        model = message.get('model') if _real_model(message.get('model')) else ''
        turn.update({
            'ts': _parse_ts(entry.get('timestamp')),
            'model': _clip(model, 120),
            'cache_read': read,
            'cache_write': write,
            'input': incoming,
            'output': outgoing,
            'system': self._system if self._system_seen else None,
            'tools': list(self._tools) if self._tools_seen else None,
        })

    def turns(self):
        return [dict(self._by_id[mid]) for mid in self._order]


def turns_from_entries(entries, actor, name, subagent=False, skip_sidechain=False):
    """把一份 JSONL 记录收成诊断用的请求列表。不读文件。"""
    trace = Trace(actor, name, subagent)
    if not isinstance(entries, list):
        return []
    for entry in entries:
        if skip_sidechain and isinstance(entry, dict) and entry.get('isSidechain'):
            continue
        trace.see(entry)
    return trace.turns()


def _normalize_turn(raw, index):
    if not isinstance(raw, dict):
        return None
    actor = _clip(raw.get('actor'), 80)
    if not actor:
        return None
    read = _token(raw.get('cache_read', 0), True)
    write = _token(raw.get('cache_write', 0), True)
    incoming = _token(raw.get('input', 0), True)
    outgoing = _token(raw.get('output', 0), True)
    if None in (read, write, incoming, outgoing):
        return None
    tools = raw.get('tools') if 'tools' in raw else None
    if tools is not None:
        tools = _tool_names(tools)
    model = raw.get('model') if _real_model(raw.get('model')) else ''
    return {
        'i': index,
        'actor': actor,
        'name': _clip(raw.get('name'), 80) or actor,
        'subagent': raw.get('subagent') is True,
        'ts': _parse_ts(raw.get('ts')),
        'model': _clip(model, 120),
        'cache_read': read,
        'cache_write': write,
        'input': incoming,
        'output': outgoing,
        'system': _public_fp(raw.get('system')) if raw.get('system') is not None else None,
        'tools': tools,
    }


def _stats(requests):
    read = write = incoming = outgoing = 0
    for req in requests:
        read += req['cache_read']
        write += req['cache_write']
        incoming += req['input']
        outgoing += req['output']
    context = incoming + read + write
    hit = (read / context) if context else None
    return {
        'read': read,
        'write': write,
        'input': incoming,
        'output': outgoing,
        'context': context,
        'hit': hit,
        'count': len(requests),
    }


def _ordered(requests):
    if requests and all(req['ts'] is not None for req in requests):
        return sorted(requests, key=lambda req: (req['ts'], req['i']))
    return list(requests)


def _cold(req):
    return req['cache_write'] > 0 and req['cache_read'] == 0


def _tags(prev, cur, ttl):
    if prev is None or not _cold(cur):
        return []
    found = []
    if prev['model'] and cur['model'] and prev['model'] != cur['model']:
        found.append('model')
    if prev['system'] is not None and cur['system'] is not None and prev['system'] != cur['system']:
        found.append('system')
    if prev['tools'] is not None and cur['tools'] is not None and list(prev['tools']) != list(cur['tools']):
        found.append('tools')
    if prev['ts'] is not None and cur['ts'] is not None:
        gap = cur['ts'] - prev['ts']
        if gap > ttl and (prev['cache_read'] + prev['cache_write']) > 0:
            found.append(('ttl', gap))
    return found


def _tag_name(tag):
    return tag[0] if isinstance(tag, tuple) else tag


def _eligible(stats, config):
    hit = stats['hit']
    return hit is not None and stats['context'] >= config['minContextTokens'] and hit < config['hitRatio']


def _sum_savings(requests, table, high_key, low_key):
    total = 0.0
    tokens = 0
    rates = []
    for req in requests:
        key, price = lookup_price(req['model'], table)
        if not price:
            continue
        high = _finite(price.get(high_key))
        low = _finite(price.get(low_key))
        if high is None or low is None or high <= low:
            continue
        tokens += req['cache_write']
        total += req['cache_write'] * (high - low) / 1e6
        if key not in [item[0] for item in rates]:
            rates.append((key, high, low))
    money = round(total, 6)
    if money <= 0 or tokens <= 0:
        return None, 0, []
    return money, tokens, rates


def _price_phrase(rates, high_name, low_name):
    if len(rates) == 1:
        key, high, low = rates[0]
        return '按价格行 {} 的 {} {} 与 {} {}（USD/百万 token）'.format(key, high_name, _num(high), low_name, _num(low))
    names = '、'.join(item[0] for item in rates)
    return '按各请求命中的价格行（{}）计算 {} 与 {} 的差价（USD/百万 token）'.format(names, high_name, low_name)


def _tools_public(names):
    if not names:
        return '空列表'
    safe = len(names) <= 8 and all(re.fullmatch(r'[A-Za-z0-9_.:-]{1,40}', name) for name in names)
    if safe:
        return '、'.join(names)
    return '{} 个工具（指纹 {}）'.format(len(names), fingerprint('\n'.join(names)))


def _hit_clause(name, stats):
    return '{} 的缓存命中率 {}（读取 {} / 上下文 {}）'.format(
        name, _pct(stats['hit']), _tokens(stats['read']), _tokens(stats['context']))


def _suggestion(rule, actor, name, scope, stats, action, savings, rates):
    return {
        'id': rule,
        'scope': scope,
        'actor': actor,
        'name': name,
        'hit': round(stats['hit'], 4) if stats['hit'] is not None else None,
        'context': stats['context'],
        'cacheRead': stats['read'],
        'cacheWrite': stats['write'],
        'action': action,
        'savingsUsd': savings,
        'priceKeys': [item[0] for item in rates],
        'estimate': True,
    }


def _rebuild_fact(ordered, config):
    """只写转录里真能核对的条件。缺字段就说明没看见，不说成「没有变化」。"""
    timed = all(req['ts'] is not None for req in ordered)
    models = {req['model'] for req in ordered if req['model']}
    systems = [req['system'] for req in ordered]
    tools = [tuple(req['tools']) if req['tools'] is not None else None for req in ordered]
    parts = []
    if len(models) == 1:
        parts.append('模型没有变')
    elif not models:
        parts.append('转录里没有模型名')
    if all(item is not None for item in systems) and len(set(systems)) == 1:
        parts.append('系统提示指纹都是 {}'.format(systems[0]))
    elif any(item is None for item in systems):
        parts.append('转录没有系统提示字段，不把它算成原因')
    if all(item is not None for item in tools) and len(set(tools)) == 1:
        parts.append('工具列表没有变')
    elif any(item is None for item in tools):
        parts.append('转录没有工具列表字段，不把它算成原因')
    if timed:
        parts.append('相邻间隔都没有超过 {} 秒'.format(config['ttlSeconds']))
    else:
        parts.append('时间戳不全，不能判断间隔是不是太长')
    return '，'.join(parts) if parts else '没有更具体的字段可以对照'


def _priced_subset(requests, table, high_key, low_key):
    kept = []
    for req in requests:
        _key, price = lookup_price(req['model'], table)
        if not price:
            continue
        high = _finite(price.get(high_key))
        low = _finite(price.get(low_key))
        if high is None or low is None or high <= low:
            continue
        kept.append(req)
    return kept


def _actor_buckets(requests, config):
    ordered = _ordered(requests)
    stats = _stats(ordered)
    buckets = {cause: [] for cause in _CAUSE_PRIORITY}
    unexplained = []
    cold_all = []
    for index, req in enumerate(ordered):
        prev = ordered[index - 1] if index else None
        tags = _tags(prev, req, config['ttlSeconds'])
        if not index or not _cold(req):
            continue
        gap = next((tag[1] for tag in tags if _tag_name(tag) == 'ttl'), None)
        item = {'i': req['i'], 'req': req, 'prev': prev, 'tags': [_tag_name(tag) for tag in tags], 'gap': gap}
        cold_all.append(item)
        if not item['tags']:
            unexplained.append(item)
        for cause in item['tags']:
            if cause in buckets:
                buckets[cause].append(item)
    return ordered, stats, buckets, unexplained, cold_all


def _take(buckets, cause, claimed, minimum, table):
    items = [item for item in buckets[cause] if item['i'] not in claimed]
    priced_ids = {req['i'] for req in _priced_subset([item['req'] for item in items], table, 'cache_write', 'cache_read')}
    items = [item for item in items if item['i'] in priced_ids]
    if len(items) < minimum:
        return []
    for item in items:
        claimed.add(item['i'])
    return items


def _advise_actor(actor, requests, config, table):
    ordered, stats, buckets, unexplained, cold_all = _actor_buckets(requests, config)
    if not _eligible(stats, config):
        return []
    name = ordered[-1]['name']
    scope = 'lead' if actor == '__lead' else 'member'
    claimed = set()
    suggestions = []
    model_items = _take(buckets, 'model', claimed, config['minModelSwitches'], table)
    if model_items:
        savings, priced, rates = _sum_savings([item['req'] for item in model_items], table, 'cache_write', 'cache_read')
    else:
        savings = None
    if model_items and savings:
        chain = []
        for item in model_items:
            for label in (item['prev']['model'], item['req']['model']):
                if label and (not chain or chain[-1] != label):
                    chain.append(label)
        shown = ' → '.join(chain[:5])
        if len(chain) > 5:
            shown += ' → …'
        action = '{}。{} 次请求里有 {} 次换模型后缓存读取为 0、写入合计 {}。序列：{}。换模型后同一前缀不能接着读，后续请求固定一个模型。估算可省 {}（估算，{}，不是账单）。'.format(
            _hit_clause(name, stats), stats['count'], len(model_items), _tokens(priced), shown, _usd(savings),
            _price_phrase(rates, 'cache_write', 'cache_read'))
        suggestions.append(_suggestion('cache-model', actor, name, scope, stats, action, savings, rates))
    system_items = _take(buckets, 'system', claimed, 1, table)
    if system_items:
        savings, priced, rates = _sum_savings([item['req'] for item in system_items], table, 'cache_write', 'cache_read')
    else:
        savings = None
    if system_items and savings:
        pairs = []
        for item in system_items:
            pair = '{} → {}'.format(item['prev']['system'], item['req']['system'])
            if pair not in pairs:
                pairs.append(pair)
        action = '{}。系统提示指纹变化 {} 次（{}）。变化后的请求缓存读取为 0、写入合计 {}。中途改系统提示会使已有缓存对不上，稳定前缀留在会话开头。估算可省 {}（估算，{}，不是账单）。'.format(
            _hit_clause(name, stats), len(system_items), '；'.join(pairs[:3]), _tokens(priced), _usd(savings),
            _price_phrase(rates, 'cache_write', 'cache_read'))
        suggestions.append(_suggestion('cache-system', actor, name, scope, stats, action, savings, rates))
    tool_items = _take(buckets, 'tools', claimed, 1, table)
    if tool_items:
        savings, priced, rates = _sum_savings([item['req'] for item in tool_items], table, 'cache_write', 'cache_read')
    else:
        savings = None
    if tool_items and savings:
        pairs = []
        for item in tool_items:
            pair = '{} → {}'.format(_tools_public(item['prev']['tools']), _tools_public(item['req']['tools']))
            if pair not in pairs:
                pairs.append(pair)
        action = '{}。工具列表变化 {} 次（{}）。变化后的请求缓存读取为 0、写入合计 {}。工具定义在缓存前缀里，能固定的工具放到会话开始。估算可省 {}（估算，{}，不是账单）。'.format(
            _hit_clause(name, stats), len(tool_items), '；'.join(pairs[:3]), _tokens(priced), _usd(savings),
            _price_phrase(rates, 'cache_write', 'cache_read'))
        suggestions.append(_suggestion('cache-tools', actor, name, scope, stats, action, savings, rates))
    ttl_items = _take(buckets, 'ttl', claimed, config['minGaps'], table)
    if ttl_items:
        savings, priced, rates = _sum_savings([item['req'] for item in ttl_items], table, 'cache_write', 'cache_read')
    else:
        savings = None
    if ttl_items and savings:
        gaps = [item['gap'] for item in ttl_items if item.get('gap') is not None]
        longest = max(gaps) if gaps else 0
        action = '{}。相邻请求有 {} 次间隔超过缓存 TTL {} 秒（最长 {:.0f} 秒）。这 {} 次之后的请求缓存读取为 0、写入合计 {}。把间隔压进 {} 秒，这些写入有机会按缓存读取计价。估算可省 {}（估算，{}，不是账单）。'.format(
            _hit_clause(name, stats), len(ttl_items), config['ttlSeconds'], longest, len(ttl_items),
            _tokens(priced), config['ttlSeconds'], _usd(savings), _price_phrase(rates, 'cache_write', 'cache_read'))
        suggestions.append(_suggestion('cache-ttl', actor, name, scope, stats, action, savings, rates))
    ratio_ok = stats['read'] == 0 or stats['write'] >= config['rebuildRatio'] * stats['read']
    if (stats['count'] >= config['minRequests'] and len(cold_all) >= config['minColdWrites']
            and unexplained and ratio_ok):
        priced_ids = {req['i'] for req in _priced_subset([item['req'] for item in unexplained], table, 'cache_write', 'cache_read')}
        owned = [item for item in unexplained if item['i'] in priced_ids]
        if owned:
            savings, priced, rates = _sum_savings([item['req'] for item in owned], table, 'cache_write', 'cache_read')
        else:
            savings = None
        if owned and savings:
            ratio_text = '' if stats['read'] == 0 else '，写入是读取的 {:.1f} 倍'.format(stats['write'] / stats['read'])
            action = '{}。{} 次请求里有 {} 次写入缓存时读取为 0，写入合计 {}{}。{}。排除换模型、系统提示或工具列表变化、以及过长间隔之后，仍有 {} 次共 {} token 对不上这些原因，缓存前缀在被反复重建。保持系统提示、工具列表和模型不变，并让相邻请求落在 TTL 内。估算可省 {}（估算，{}，不是账单）。'.format(
                _hit_clause(name, stats), stats['count'], len(cold_all), _tokens(stats['write']), ratio_text,
                _rebuild_fact(ordered, config), len(owned), _tokens(priced), _usd(savings),
                _price_phrase(rates, 'cache_write', 'cache_read'))
            suggestions.append(_suggestion('cache-rebuild', actor, name, scope, stats, action, savings, rates))
    return suggestions


def _subagent_suggestions(groups, config, table):
    """每个子代理只有一轮、且这一轮读取为 0。多轮的归到重建规则，这里不重复算。"""
    ones = []
    for _actor, requests in groups:
        if not requests or not requests[0]['subagent']:
            continue
        if len(requests) != 1 or not _cold(requests[0]):
            continue
        ones.append(requests[0])
    if len(ones) < config['minSubagents']:
        return []
    found = []
    used = set()
    clusters = {}
    for req in ones:
        if req['system'] is None or req['tools'] is None or not req['model']:
            continue
        key = (req['system'], tuple(req['tools']), req['model'])
        clusters.setdefault(key, []).append(req)
    for key, reqs in clusters.items():
        if len(reqs) < config['minSubagents']:
            continue
        ordered = _ordered(reqs)
        stats = _stats(ordered)
        if not _eligible(stats, config):
            continue
        rest = ordered[1:]
        savings, priced, rates = _sum_savings(rest, table, 'cache_write', 'cache_read')
        if savings is None:
            continue
        names = '、'.join(req['name'] for req in ordered[:6])
        action = '{} 个子代理各自只有 1 次请求，缓存读取都是 0，写入合计 {}（{}）。系统提示指纹同为 {}，工具列表同为 {}，模型同为 {}。除最早一次外的 {} token，若留在同一个子代理的后续回合里，有机会按缓存读取计价。间隔要短于 {} 秒。估算可省 {}（估算，{}，不是账单）。'.format(
            len(ordered), _tokens(stats['write']), names, key[0], _tools_public(list(key[1])), key[2],
            _tokens(priced), config['ttlSeconds'], _usd(savings), _price_phrase(rates, 'cache_write', 'cache_read'))
        found.append(_suggestion('cache-subagent', '__subagents', '子代理', 'session', stats, action, savings, rates))
        used.update(req['i'] for req in ordered)
    by_model = {}
    for req in ones:
        if req['i'] in used or not req['model']:
            continue
        by_model.setdefault(req['model'], []).append(req)
    for model, reqs in by_model.items():
        if len(reqs) < config['minSubagents']:
            continue
        stats = _stats(reqs)
        if not _eligible(stats, config):
            continue
        savings, priced, rates = _sum_savings(reqs, table, 'cache_write', 'in')
        if savings is None:
            continue
        names = '、'.join(req['name'] for req in reqs[:6])
        action = '{} 个子代理各自只有 1 次请求，缓存读取都是 0，写入合计 {}（{}），模型 {}。转录里看不到相同的系统提示和工具列表指纹，不能把这些写入算成一份可复用前缀。缓存写入价高于普通输入价，而这些 token 没有被读到。估算可省 {}（估算，{}，即写入价减去输入价，不是账单）。'.format(
            len(reqs), _tokens(priced), names, model, _usd(savings), _price_phrase(rates, 'cache_write', 'in'))
        found.append(_suggestion('cache-subagent', '__subagents', '子代理', 'session', stats, action, savings, rates))
    return found


def diagnose(turns, pricing=None, config=None):
    """纯函数。不修改 turns。信号不够或估不出正的差价时，suggestions 为空。"""
    normalized, error = normalize_config(config)
    if error:
        return _blank('invalid', error)
    if not normalized.get('enabled'):
        return _blank('off', '', normalized)
    prepared = []
    if isinstance(turns, list):
        for index, raw in enumerate(turns):
            turn = _normalize_turn(raw, index)
            if turn is not None:
                prepared.append(turn)
    session = _stats(prepared)
    result = _blank('quiet', '', normalized)
    result['sessionContext'] = session['context']
    result['sessionHit'] = round(session['hit'], 4) if session['hit'] is not None else None
    if not prepared:
        return result
    groups = []
    by_actor = {}
    for turn in prepared:
        if turn['actor'] not in by_actor:
            by_actor[turn['actor']] = []
            groups.append((turn['actor'], by_actor[turn['actor']]))
        by_actor[turn['actor']].append(turn)
    suggestions = []
    for actor, requests in groups:
        suggestions.extend(_advise_actor(actor, requests, normalized, pricing))
    suggestions.extend(_subagent_suggestions(groups, normalized, pricing))
    suggestions.sort(key=lambda item: (-item['savingsUsd'], item['id'], item['name'], item['actor']))
    result['suggestions'] = suggestions[:normalized['maxSuggestions']]
    if result['suggestions']:
        result['status'] = 'advice'
        result['enabled'] = True
    return result


def attach_cache_advice(snapshot, pricing=None, config=None, turns=None):
    """把 cacheAdvice 写进快照。turns 缺省时读 snapshot['cacheTurns']。"""
    if not isinstance(snapshot, dict):
        return snapshot
    if turns is None:
        turns = snapshot.get('cacheTurns')
    snapshot['cacheAdvice'] = diagnose(turns, pricing, config)
    return snapshot
