#!/usr/bin/env python3
"""合成转录上的解析性能基准。

用固定种子在临时目录生成约一万条 Claude Code 形态的 JSONL，调用
team_board.build_snapshot 测量清空缓存后的耗时中位数和 tracemalloc 峰值。
另跑一段固定的纯 Python 校准循环，用「解析中位数 / 校准中位数」抵消机器快慢。
--check 在这个比值比基线慢超过 30% 时以退出码 1 告警。

不读取本机 ~/.claude 或 ~/.codex，也不写入它们。

    python tools/bench.py
    python tools/bench.py --json
    python tools/bench.py --check
    python tools/bench.py --write-baseline
"""
import argparse
import gc
import hashlib
import importlib.util
import json
import os
import platform
import random
import shutil
import statistics
import sys
import tempfile
import time
import tracemalloc
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / 'benchmarks' / 'baseline.json'
ENTRY = 'team_board.build_snapshot'
DEFAULT_EVENTS = 10000
DEFAULT_SEED = 29
DEFAULT_REPEATS = 5
DEFAULT_CALIBRATION_LOOPS = 20000
DEFAULT_THRESHOLD = 0.30
MIN_EVENTS = 36
MAX_EVENTS = 100000
MIN_REPEATS = 1
MAX_REPEATS = 20
MIN_LOOPS = 1
MAX_LOOPS = 5000000
SCHEMA_VERSION = 1

PROJECT = 'synthetic-bench'
SESSION = 'sess-bench0001'
CWD = '/synthetic/bench-workspace'
SHARED = '/synthetic/bench-workspace/src/shared.js'
INBOX = '/synthetic/bench-workspace/.team/inbox/frontend-dev.md'
MEMBERS = (
    ('a1', 'backend-dev', ''),
    ('b2', 'frontend-dev', ''),
    ('c3', 'qa-tester', ''),
    ('d4', 'researcher', ''),
    ('e5', 'docs-writer', 'wave-1'),
)
PARSE_CACHES = ('_agent_cache', '_lead_cache', '_conversation_cache', '_lead_git_cache')
ENV_KEYS = (
    'HOME', 'USERPROFILE', 'CODEX_HOME', 'PETDEX_HOME',
    'CLAUDE_CONFIG_DIR', 'TEAM_BOARD_CONFIG',
)
BASELINE_NOTE = (
    '比值 = 解析中位数 / 校准中位数。--check 在比值比该文件慢超过 30% 时退出码为 1。'
    '容差在 tools/bench.py 的 DEFAULT_THRESHOLD，不读本文件里的阈值。'
)

# 固定的一行 JSON。校准循环反复解析它，结构接近转录里的 assistant/tool_use，
# 这样解释器变慢时分子和分母一起变，比值仍主要反映解析代码本身。
CALIB_LINE = (
    '{"bench":"synthetic","cwd":"/synthetic/bench-workspace","gitBranch":"bench-synthetic",'
    '"message":{"content":[{"id":"tu","input":{"file_path":"/synthetic/bench-workspace/src/mod.js"},'
    '"name":"Read","type":"tool_use"}],"id":"calib","model":"claude-sonnet-5","role":"assistant",'
    '"stop_reason":"tool_use","usage":{"cache_creation_input_tokens":4,"cache_read_input_tokens":800,'
    '"input_tokens":120,"output_tokens":40}},"requestId":"req-calib","sessionId":"sess-bench0001",'
    '"timestamp":"2026-01-01T00:00:00Z","type":"assistant"}'
)


class BenchError(Exception):
    """基准自身的错误。消息里只有参数和状态，没有转录正文。"""


def as_int(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchError('%s 必须是整数' % label)
    if value < low or value > high:
        raise BenchError('%s 必须在 %d 和 %d 之间' % (label, low, high))
    return value


def positive_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchError('%s 必须是正数' % label)
    number = float(value)
    if number != number or number in (float('inf'), float('-inf')) or number <= 0:
        raise BenchError('%s 必须是正数' % label)
    return number


def iso(index):
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=int(index))
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def dump_line(obj, index, file_key):
    obj['bench'] = 'synthetic'
    obj['cwd'] = CWD
    obj['gitBranch'] = 'bench-synthetic'
    obj['requestId'] = 'req-%s-%d' % (file_key, index)
    obj['sessionId'] = SESSION
    obj['timestamp'] = iso(index)
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def usage_for(rng):
    return {
        'input_tokens': 80 + rng.randrange(20),
        'output_tokens': 12 + rng.randrange(10),
        'cache_read_input_tokens': 400 + rng.randrange(50),
        'cache_creation_input_tokens': rng.randrange(5),
    }


def assistant_line(file_key, index, model, content, rng, stop='tool_use', extra=None):
    if index % 10 == 0 and file_key != 'lead':
        model = 'claude-haiku-5'
    message = {
        'id': 'msg-%s-%d' % (file_key, index // 3),
        'role': 'assistant',
        'model': model,
        'stop_reason': stop,
        'content': content,
        'usage': usage_for(rng),
    }
    obj = {
        'type': 'assistant',
        'message': message,
        'system': 'synthetic system prompt v2' if index == 20 else 'synthetic system prompt v1',
        'tools': (
            [{'name': 'Read'}, {'name': 'Bash'}] if index == 40
            else [{'name': 'Read'}, {'name': 'Write'}, {'name': 'Bash'}, {'name': 'Edit'}]
        ),
    }
    if extra:
        obj.update(extra)
    return dump_line(obj, index, file_key)


def user_line(file_key, index, text):
    return dump_line({
        'type': 'user',
        'message': {'role': 'user', 'content': text},
    }, index, file_key)


def result_line(file_key, index, tool_id, content, is_error=False):
    block = {'type': 'tool_result', 'tool_use_id': tool_id, 'content': content}
    if is_error:
        block['is_error'] = True
    return dump_line({
        'type': 'user',
        'message': {'role': 'user', 'content': [block]},
    }, index, file_key)


def tool_block(tool_id, name, payload):
    return {'type': 'tool_use', 'id': tool_id, 'name': name, 'input': payload}


def data_path(file_key, index, mode):
    if mode == 'write' and file_key == 'a1' and index == 3:
        return INBOX
    if mode == 'write' and file_key == 'a1' and index % 50 == 3:
        return SHARED
    if mode == 'read' and file_key == 'b2' and index % 50 == 1:
        return SHARED
    return '/synthetic/bench-workspace/src/%s_%d.js' % (file_key, index)


def pair(file_key, index, model, name, payload, result, rng, is_error=False):
    tool_id = 'tu-%s-%d' % (file_key, index)
    if name == 'Agent':
        tool_id = 'tu-dispatch-%s' % payload.get('name', file_key)
    first = assistant_line(file_key, index, model, [tool_block(tool_id, name, payload)], rng)
    second = result_line(file_key, index + 1, tool_id, result, is_error=is_error)
    return [first, second]


def emit_chunk(kind, file_key, model, step, start, rng):
    slot = step % 8
    if kind == 'lead':
        return emit_lead(file_key, model, slot, start, step, rng)
    return emit_member(file_key, model, slot, start, rng)


def emit_lead(file_key, model, slot, start, step, rng):
    if slot == 0:
        return [user_line(file_key, start, 'synthetic lead request %d' % start)]
    if slot == 1:
        agent_id, agent_type, _workflow = MEMBERS[step % len(MEMBERS)]
        payload = {
            'description': 'synthetic task %s' % agent_type,
            'subagent_type': agent_type,
            'prompt': 'synthetic prompt for %s' % agent_id,
            'name': agent_id,
            'model': 'claude-sonnet-5',
            'run_in_background': True,
        }
        return pair(file_key, start, model, 'Agent', payload, 'launched', rng)
    if slot == 2:
        return [assistant_line(
            file_key, start, model,
            [{'type': 'text', 'text': 'synthetic sidechain'}],
            rng, stop='end_turn', extra={'isSidechain': True},
        )]
    if slot == 3:
        return pair(
            file_key, start, model, 'Bash',
            {'command': 'python -m unittest discover -s tests -q', 'description': 'synthetic unit tests'},
            'Ran 2 tests in 0.001s\n\nOK\n', rng,
        )
    if slot == 4:
        return [assistant_line(
            file_key, start, model,
            [{'type': 'thinking', 'thinking': 'synthetic thought'},
             {'type': 'text', 'text': 'synthetic step %d done' % start}],
            rng, stop='end_turn',
        )]
    if slot == 5:
        agent_id = MEMBERS[step % len(MEMBERS)][0]
        raw = (
            '<task-notification><task-id>%s</task-id><status>completed</status>'
            '<summary>synthetic done</summary></task-notification>'
        ) % agent_id
        return [dump_line({'type': 'queue-operation', 'content': raw}, start, file_key)]
    if slot == 6:
        return pair(
            file_key, start, model, 'Read',
            {'file_path': data_path(file_key, start, 'read')},
            'synthetic file body', rng,
        )
    return pair(
        file_key, start, model, 'Write',
        {'file_path': data_path(file_key, start, 'write'), 'content': 'synthetic'},
        'ok', rng,
    )


def emit_member(file_key, model, slot, start, rng):
    if slot == 0:
        return [user_line(file_key, start, 'synthetic prompt for %s %d' % (file_key, start))]
    if slot == 1:
        return pair(
            file_key, start, model, 'Read',
            {'file_path': data_path(file_key, start, 'read')},
            'synthetic file body', rng, is_error=(start % 29 == 0),
        )
    if slot == 2:
        return pair(
            file_key, start, model, 'Write',
            {'file_path': data_path(file_key, start, 'write'), 'content': 'synthetic body'},
            'ok', rng,
        )
    if slot == 3:
        return pair(
            file_key, start, model, 'Edit',
            {'file_path': data_path(file_key, start, 'edit'), 'old_string': 'a', 'new_string': 'b'},
            'ok', rng,
        )
    if slot == 4:
        return [assistant_line(
            file_key, start, model,
            [{'type': 'thinking', 'thinking': 'synthetic thought'},
             {'type': 'text', 'text': 'synthetic step %d done' % start}],
            rng, stop='end_turn',
        )]
    if slot == 5:
        return pair(
            file_key, start, model, 'Bash',
            {'command': 'python -m unittest discover -s tests -q', 'description': 'synthetic unit tests'},
            'Ran 2 tests in 0.001s\n\nOK\n', rng,
        )
    if slot == 6:
        return pair(
            file_key, start, model, 'TaskUpdate',
            {'taskId': 'task-%s' % file_key, 'status': 'in_progress', 'owner': file_key},
            'ok', rng,
        )
    return pair(
        file_key, start, model, 'SendMessage',
        {'to': 'frontend-dev', 'summary': 'synthetic note', 'message': 'synthetic note'},
        'delivered', rng,
    )


def fill(file_key, count, model, kind, rng):
    lines = []
    step = 0
    invalid = False
    while len(lines) < count:
        if kind == 'lead' and not invalid and len(lines) >= 7 and count - len(lines) >= 1:
            lines.append('{')
            invalid = True
            continue
        chunk = emit_chunk(kind, file_key, model, step, len(lines), rng)
        if not chunk:
            raise BenchError('生成器产出了空块')
        if len(chunk) > count - len(lines):
            while len(lines) < count:
                lines.append(user_line(file_key, len(lines), 'synthetic note %d' % len(lines)))
            break
        lines.extend(chunk)
        step += 1
        if step > count * 4:
            raise BenchError('生成器没有在预期步数内写完')
    if len(lines) != count:
        raise BenchError('生成器条数不符')
    return lines


def allocate(events):
    keys = ['lead'] + [item[0] for item in MEMBERS]
    counts = {}
    for key in keys:
        counts[key] = 2
    left = events - 2 * len(keys)
    index = 0
    while left > 0:
        counts[keys[index % len(keys)]] += 1
        left -= 1
        index += 1
    return keys, counts


def write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(('\n'.join(lines) + '\n').encode('utf-8'))


def write_meta(path, obj):
    text = json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(',', ':')) + '\n'
    path.write_bytes(text.encode('utf-8'))


def hash_corpus(home):
    base = Path(home)
    files = []
    root = base / '.claude' / 'projects'
    if root.is_dir():
        for path in root.rglob('*'):
            if path.is_file():
                files.append((path.relative_to(base).as_posix(), path))
    files.sort()
    digest = hashlib.sha256()
    for rel, path in files:
        digest.update(rel.encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest(), [rel for rel, _path in files]


def generate_corpus(home, events=DEFAULT_EVENTS, seed=DEFAULT_SEED):
    """在 home 下写出一套合成会话。返回条数、种子、sha256 和相对路径。不含绝对路径。"""
    events = as_int(events, 'events', MIN_EVENTS, MAX_EVENTS)
    seed = as_int(seed, 'seed', 0, 2 ** 31 - 1)
    rng = random.Random(seed)
    home = Path(home)
    project = home / '.claude' / 'projects' / PROJECT
    sub = project / SESSION / 'subagents'
    keys, counts = allocate(events)
    specs = {'lead': ('lead', '', '')}
    for agent_id, agent_type, workflow in MEMBERS:
        specs[agent_id] = (agent_id, agent_type, workflow)
    for key in keys:
        _agent_id, agent_type, workflow = specs[key]
        if key == 'lead':
            lines = fill('lead', counts[key], 'claude-opus-5', 'lead', rng)
            write_jsonl(project / (SESSION + '.jsonl'), lines)
            continue
        folder = sub / 'workflows' / workflow if workflow else sub
        lines = fill(key, counts[key], 'claude-sonnet-5', 'member', rng)
        write_jsonl(folder / ('agent-%s.jsonl' % key), lines)
        write_meta(folder / ('agent-%s.meta.json' % key), {
            'agentType': agent_type,
            'description': 'synthetic task %s' % agent_type,
            'toolUseId': 'tu-dispatch-%s' % key,
        })
    digest, rels = hash_corpus(home)
    return {'events': events, 'seed': seed, 'sha256': digest, 'files': rels}


def calibrate(loops):
    """固定工作量的纯 Python 循环。返回值必须被使用，避免被当成死代码。"""
    loops = as_int(loops, 'calibrationLoops', MIN_LOOPS, MAX_LOOPS)
    total = 0
    line = CALIB_LINE
    loads = json.loads
    dumps = json.dumps
    for index in range(loops):
        message = loads(line)['message']
        usage = message['usage']
        total += usage['input_tokens'] + usage['output_tokens'] + usage['cache_read_input_tokens']
        for block in message['content']:
            if isinstance(block, dict) and block.get('type') == 'tool_use':
                payload = block.get('input') or {}
                total += len(block.get('name') or '') + len(payload.get('file_path') or '')
        if index % 64 == 0:
            total += len(dumps(message, sort_keys=True))
    if total <= 0:
        raise BenchError('校准循环没有工作')
    return total


def median(values):
    if not values:
        raise BenchError('没有样本')
    return float(statistics.median(values))


def push_env(home):
    home = Path(home)
    saved = {}
    for key in ENV_KEYS:
        saved[key] = os.environ.get(key)
    os.environ['HOME'] = str(home)
    os.environ['USERPROFILE'] = str(home)
    os.environ['CODEX_HOME'] = str(home / '.codex')
    os.environ['PETDEX_HOME'] = str(home / '.petdex')
    os.environ['CLAUDE_CONFIG_DIR'] = str(home / '.claude')
    os.environ['TEAM_BOARD_CONFIG'] = str(ROOT / 'team-board' / 'team.json')
    return saved


def pop_env(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _same_path(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def load_board():
    name = 'team_board_bench_' + uuid.uuid4().hex
    path = ROOT / 'team-board' / 'team_board.py'
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise BenchError('无法加载 team_board.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def assert_isolated(module, home):
    """看板的会话、Codex、成员目录和消息总线都必须落在这次的临时 home 里。"""
    home = Path(home)
    expected_projects = home / '.claude' / 'projects'
    expected_codex = home / '.codex' / 'sessions'
    expected_agents = home / '.claude' / 'agents'
    expected_config = ROOT / 'team-board' / 'team.json'
    projects = getattr(module, 'PROJECTS', None)
    codex = getattr(module, 'CODEX_SESSIONS', None)
    agents = getattr(module, 'AGENTS_DIRS', None)
    bus = getattr(module, 'BUS', None)
    config = getattr(module, 'TEAM_CONFIG_PATH', None)
    agents_dir = agents[0] if isinstance(agents, list) and agents else None
    if not _same_path(projects, expected_projects) or not _inside(projects, home):
        raise BenchError('拒绝解析：Claude 项目目录不在临时目录内')
    if not _same_path(codex, expected_codex) or not _inside(codex, home):
        raise BenchError('拒绝解析：Codex 目录不在临时目录内')
    if agents_dir is None or not _same_path(agents_dir, expected_agents) or not _inside(agents_dir, home):
        raise BenchError('拒绝解析：成员目录不在临时目录内')
    if bus is None or not _inside(bus, home):
        raise BenchError('拒绝解析：消息总线不在临时目录内')
    if not _same_path(config, expected_config):
        raise BenchError('拒绝解析：价格表不是仓库里的 team.json')


def clear_parse_caches(module):
    for name in PARSE_CACHES:
        cache = getattr(module, name, None)
        if not hasattr(cache, 'clear'):
            raise BenchError('看板缺少解析缓存 %s，无法测量冷解析' % name)
        cache.clear()


def parse_once(module):
    args = module.argparse.Namespace(
        session=None, project=None, stale=600, source='claude', stall_alert=600,
        budget=None, budget_warn=None, budget_member=None, budget_dept=None,
    )
    try:
        snapshot = module.build_snapshot(args, merge=True)
    except BenchError:
        raise
    except Exception as exc:
        raise BenchError('build_snapshot 失败: %s: %s' % (type(exc).__name__, exc)) from exc
    if not isinstance(snapshot, dict):
        raise BenchError('build_snapshot 没有返回快照')
    return snapshot


def snapshot_counts(snapshot):
    members = snapshot.get('members') if isinstance(snapshot.get('members'), list) else []
    lead = snapshot.get('lead') if isinstance(snapshot.get('lead'), dict) else {}
    tools = 0
    for member in members:
        if isinstance(member, dict):
            tools += int(member.get('toolCount') or 0)
    tools += int(lead.get('toolCount') or 0)
    return len(members), tools


def measure_peak(module):
    clear_parse_caches(module)
    gc.collect()
    started = False
    try:
        tracemalloc.start()
        started = True
        snapshot = parse_once(module)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        if started:
            tracemalloc.stop()
    return snapshot, int(peak)


def runtime_facts():
    py = sys.version_info
    return {
        'python': '%d.%d.%d' % (py[0], py[1], py[2]),
        'platform': platform.platform(),
    }


def run_benchmark(events=DEFAULT_EVENTS, seed=DEFAULT_SEED, repeats=DEFAULT_REPEATS,
                  calibration_loops=DEFAULT_CALIBRATION_LOOPS, home=None):
    """生成语料并测量。home 为空时使用临时目录，结束时删除。环境变量会恢复。"""
    events = as_int(events, 'events', MIN_EVENTS, MAX_EVENTS)
    seed = as_int(seed, 'seed', 0, 2 ** 31 - 1)
    repeats = as_int(repeats, 'repeats', MIN_REPEATS, MAX_REPEATS)
    calibration_loops = as_int(calibration_loops, 'calibrationLoops', MIN_LOOPS, MAX_LOOPS)
    own_home = home is None
    if own_home:
        home = Path(tempfile.mkdtemp(prefix='bench-corpus-'))
    else:
        home = Path(home)
    saved = None
    try:
        manifest = generate_corpus(home, events, seed)
        saved = push_env(home)
        module = load_board()
        bus = home / 'bench-bus'
        bus.mkdir(parents=True, exist_ok=True)
        module.BUS = bus
        assert_isolated(module, home)
        clear_parse_caches(module)
        parse_once(module)
        calibrate(calibration_loops)
        parse_samples = []
        calibrate_samples = []
        members = tools = 0
        for _repeat in range(repeats):
            clear_parse_caches(module)
            started = time.perf_counter()
            snapshot = parse_once(module)
            parse_samples.append(time.perf_counter() - started)
            started = time.perf_counter()
            calibrate(calibration_loops)
            calibrate_samples.append(time.perf_counter() - started)
            members, tools = snapshot_counts(snapshot)
        _snapshot, peak = measure_peak(module)
        if members < 1 or tools < 1:
            raise BenchError('解析没有产生成员或工具调用，基准无效')
        parse_median = median(parse_samples)
        calibrate_median = median(calibrate_samples)
        if calibrate_median <= 0:
            raise BenchError('校准耗时异常')
        facts = runtime_facts()
        return {
            'schemaVersion': SCHEMA_VERSION,
            'entry': ENTRY,
            'events': events,
            'seed': seed,
            'repeats': repeats,
            'calibrationLoops': calibration_loops,
            'corpusSha256': manifest['sha256'],
            'parseSeconds': parse_samples,
            'parseSecondsMedian': parse_median,
            'calibrateSeconds': calibrate_samples,
            'calibrateSecondsMedian': calibrate_median,
            'ratio': parse_median / calibrate_median,
            'peakBytes': peak,
            'members': members,
            'toolCount': tools,
            'python': facts['python'],
            'platform': facts['platform'],
            'warmup': True,
            'cachesCleared': list(PARSE_CACHES),
        }
    finally:
        if saved is not None:
            pop_env(saved)
        if own_home:
            shutil.rmtree(str(home), ignore_errors=True)


def _display_width(text):
    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
    return width


def _pad(text, width):
    text = str(text)
    gap = width - _display_width(text)
    if gap > 0:
        return text + (' ' * gap)
    return text


def format_table(report):
    rows = (
        ('事件', str(report.get('events'))),
        ('种子', str(report.get('seed'))),
        ('入口', str(report.get('entry'))),
        ('解析中位数', '%.4f s' % report['parseSecondsMedian']),
        ('解析样本', ', '.join('%.4f' % item for item in report.get('parseSeconds') or ())),
        ('校准中位数', '%.4f s' % report['calibrateSecondsMedian']),
        ('校准样本', ', '.join('%.4f' % item for item in report.get('calibrateSeconds') or ())),
        ('校准循环', str(report.get('calibrationLoops'))),
        ('比值', '%.6f' % report['ratio']),
        ('峰值内存', '%.2f MiB (%d bytes)' % (report['peakBytes'] / (1024.0 * 1024.0), report['peakBytes'])),
        ('成员', str(report.get('members'))),
        ('工具调用', str(report.get('toolCount'))),
        ('重复次数', str(report.get('repeats'))),
        ('语料 sha256', str(report.get('corpusSha256'))),
        ('Python', str(report.get('python'))),
        ('平台', str(report.get('platform'))),
    )
    lines = ['解析性能基准（合成转录，不读本机会话；每次计时前清空解析缓存）']
    for label, value in rows:
        lines.append('%s  %s' % (_pad(label, 14), value))
    lines.append('比值 = 解析中位数 / 校准中位数。机器更慢时两边一起变。')
    return '\n'.join(lines) + '\n'


def protocol_problems(report, baseline):
    problems = []
    if report.get('schemaVersion') != SCHEMA_VERSION or baseline.get('schemaVersion') != SCHEMA_VERSION:
        problems.append('schemaVersion')
    for key in ('entry', 'events', 'seed', 'repeats', 'calibrationLoops', 'corpusSha256'):
        if report.get(key) != baseline.get(key):
            problems.append('%s 基线 %s 当前 %s' % (key, baseline.get(key), report.get(key)))
    return problems


def judge(report, baseline, threshold, baseline_path=''):
    """比较比值。慢超过 threshold 为 regression；协议对不上不算性能告警。"""
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise BenchError('threshold 必须是 0 到 10 之间的数字')
    threshold = float(threshold)
    if threshold < 0 or threshold > 10:
        raise BenchError('threshold 必须是 0 到 10 之间的数字')
    if not isinstance(report, dict) or not isinstance(baseline, dict):
        raise BenchError('结果和基线都必须是对象')
    problems = protocol_problems(report, baseline)
    if problems:
        return {
            'ok': False,
            'kind': 'protocol',
            'change': None,
            'threshold': threshold,
            'message': '基线与本次运行不一致，比值不能比较：' + '；'.join(problems),
        }
    try:
        current = positive_number(report.get('ratio'), '当前比值')
        base = positive_number(baseline.get('ratio'), '基线比值')
    except BenchError as exc:
        return {
            'ok': False,
            'kind': 'invalid',
            'change': None,
            'threshold': threshold,
            'message': '比值无效：%s' % exc,
        }
    change = (current - base) / base
    where = str(baseline_path or 'baseline.json')
    parse_text = '%.4f s' % report['parseSecondsMedian'] if _is_number(report.get('parseSecondsMedian')) else 'n/a'
    calib_text = '%.4f s' % report['calibrateSecondsMedian'] if _is_number(report.get('calibrateSecondsMedian')) else 'n/a'
    if change <= threshold + 1e-12:
        return {
            'ok': True,
            'kind': 'ok',
            'change': change,
            'threshold': threshold,
            'currentRatio': current,
            'baselineRatio': base,
            'message': '检查通过: 比值 %.6f，相对基线 %.6f 变化 %+.1f%%，未超过 %.0f%%。' % (
                current, base, change * 100.0, threshold * 100.0),
        }
    message = (
        '告警: 解析比基线慢 %.1f%%，超过 %.0f%% 容差。\n'
        '  当前比值 %.6f（解析中位数 %s / 校准中位数 %s）\n'
        '  基线比值 %.6f（%s）\n'
        '  比较的是解析耗时与固定纯 Python 校准循环的比值，用来抵消机器快慢。'
    ) % (change * 100.0, threshold * 100.0, current, parse_text, calib_text, base, where)
    return {
        'ok': False,
        'kind': 'regression',
        'change': change,
        'threshold': threshold,
        'currentRatio': current,
        'baselineRatio': base,
        'message': message,
    }


def _is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value == value


def python_note(report, baseline):
    """主版本不一致时提示一声。仍然比较比值，不因此单独失败。"""
    if not isinstance(report, dict) or not isinstance(baseline, dict):
        return ''
    current = str(report.get('python') or '')
    recorded = str(baseline.get('python') or '')
    if current.split('.')[:2] != recorded.split('.')[:2]:
        return '注意: 基线记录于 Python %s，当前是 %s。比值仍会比较。' % (recorded or '未知', current or '未知')
    return ''


def github_warning(verdict):
    if not isinstance(verdict, dict) or verdict.get('kind') != 'regression':
        return ''
    current = verdict.get('currentRatio')
    base = verdict.get('baselineRatio')
    change = verdict.get('change')
    threshold = verdict.get('threshold')
    if not all(_is_number(item) for item in (current, base, change, threshold)):
        return '::warning title=解析性能回退::解析比值超过基线容差'
    return '::warning title=解析性能回退::解析比值 %.6f 比基线 %.6f 慢 %.1f%%，超过 %.0f%% 容差' % (
        current, base, change * 100.0, threshold * 100.0)


def load_baseline(path):
    path = Path(path)
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        raise BenchError('找不到基线文件') from None
    except OSError:
        raise BenchError('无法读取基线文件') from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise BenchError('基线不是 JSON') from None
    if not isinstance(data, dict):
        raise BenchError('基线必须是 JSON 对象')
    return data


def baseline_document(report):
    if not isinstance(report, dict):
        raise BenchError('没有可写入的结果')
    keys = (
        'schemaVersion', 'entry', 'events', 'seed', 'repeats', 'calibrationLoops',
        'corpusSha256', 'ratio', 'parseSecondsMedian', 'calibrateSecondsMedian',
        'peakBytes', 'python', 'platform',
    )
    missing = [key for key in keys if key not in report]
    if missing:
        raise BenchError('结果缺少字段，不能写成基线')
    document = {}
    for key in keys:
        document[key] = report[key]
    document['recordedAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    document['note'] = BASELINE_NOTE
    return document


def write_baseline(path, report):
    path = Path(path)
    if path.suffix.lower() != '.json':
        raise BenchError('基线路径必须以 .json 结尾')
    document = baseline_document(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(payload.encode('utf-8'))
    os.replace(str(temporary), str(path))
    return document


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='用合成转录测量 team_board.build_snapshot，并用校准比值检查回退。',
    )
    parser.add_argument('--events', type=int, default=DEFAULT_EVENTS, help='合成 JSONL 条数，默认 10000')
    parser.add_argument('--seed', type=int, default=DEFAULT_SEED, help='随机种子，默认 29')
    parser.add_argument('--repeats', type=int, default=DEFAULT_REPEATS, help='计时次数，取中位数，默认 5')
    parser.add_argument('--calibration-loops', type=int, default=DEFAULT_CALIBRATION_LOOPS,
                        help='每次校准的循环次数，默认 %d' % DEFAULT_CALIBRATION_LOOPS)
    parser.add_argument('--baseline', default=str(DEFAULT_BASELINE), help='基线 JSON 路径')
    parser.add_argument('--threshold', type=float, default=DEFAULT_THRESHOLD,
                        help='允许的比值回退比例，默认 0.30。只由命令行决定，不读基线文件')
    parser.add_argument('--check', action='store_true', help='比基线慢超过容差时退出码 1')
    parser.add_argument('--write-baseline', action='store_true', help='把本次结果写成 --baseline')
    parser.add_argument('--json', action='store_true', help='向标准输出写 JSON，不写表格')
    args = parser.parse_args(argv)
    if args.check and args.write_baseline:
        parser.error('不要同时使用 --check 和 --write-baseline')
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        report = run_benchmark(args.events, args.seed, args.repeats, args.calibration_loops)
        verdict = None
        if args.write_baseline:
            write_baseline(args.baseline, report)
            print('已写入基线', file=sys.stderr)
        if args.check:
            baseline = load_baseline(args.baseline)
            note = python_note(report, baseline)
            if note:
                print(note, file=sys.stderr)
            verdict = judge(report, baseline, args.threshold, args.baseline)
            print(verdict['message'], file=sys.stderr)
            warning = github_warning(verdict)
            if warning:
                print(warning, file=sys.stderr)
        if args.json:
            json.dump(report, sys.stdout, ensure_ascii=True, sort_keys=True)
            sys.stdout.write('\n')
        else:
            sys.stdout.write(format_table(report))
    except BenchError as exc:
        print('错误: %s' % exc, file=sys.stderr)
        return 2
    if verdict is None or verdict['ok']:
        return 0
    if verdict['kind'] == 'regression':
        return 1
    return 2


if __name__ == '__main__':
    sys.exit(main())
