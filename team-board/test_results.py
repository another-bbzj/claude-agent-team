"""从 Bash 工具调用里认出测试命令，并解析通过 / 失败 / 跳过与退出状态。

认不出、输出被截断、或摘要和退出状态对不上时，状态是 unknown。
不根据零星的 PASS 行猜总数。结果里不保留原始输出。
"""
import re
import shlex

_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_ENV = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_PYTHON = re.compile(r'^python(?:[23](?:\.\d+)?)?$')
_WRAPPERS = {'env', 'command', 'exec', 'time', 'nice', 'sudo', 'nohup'}
_NO_VALUE = {'--silent', '--quiet', '-s', '-q', '--ignore-scripts', '--if-present', '--offline'}
_VALUE_FLAGS = {'--prefix', '-C', '--dir', '--filter', '--workspace', '-w'}
_SHELLS = {'sh', 'bash', 'dash'}
_DIRECT = {
    'pytest': 'pytest',
    'py.test': 'pytest',
    'jest': 'jest',
    'vitest': 'vitest',
    'mocha': 'mocha',
    'gotestsum': 'go',
}
_MAX_OUTPUT = 2000000
_TAIL = 200000
_TICKET = re.compile(
    r'(?:^|[/\\\s])\.team[/\\]tickets[/\\]([A-Za-z0-9][A-Za-z0-9._-]{0,80})', re.I)
_TICKET_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$')
_EXIT_LINE = re.compile(
    r'(?im)^(?:exit(?:ed)?(?:\s+with)?\s+code|process exited with code)[:\s]+(\d+)\s*$')
_TRUNC_MARK = re.compile(
    r'\[\s*(?:truncated|output truncated|omitted)\s*\]|<\s*truncated\b|output truncated', re.I)
_PYTEST_SUMMARY = re.compile(
    r'^=+\s*([^=\n]+?)\s+in\s+\d+(?:\.\d+)?s(?:\s+\([^)\n]*\))?\s*=+\s*$', re.M)
_PYTEST_INTERRUPT = re.compile(r'Interrupted:\s+(\d+)\s+errors?\s+during collection', re.I)
_PYTEST_STARTED = re.compile(r'test session starts|^collected \d+', re.I | re.M)
_CARGO_RESULT = re.compile(
    r'^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; (\d+) measured; (\d+) filtered out',
    re.M)
_SUREFIRE = re.compile(
    r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)')
_STATUSES = ('pass', 'fail', 'unknown')
_EXIT_STATUSES = ('zero', 'nonzero', 'unknown')

# npm / pnpm / yarn 的脚本输出通常是下面这些；make test 则哪一种都可能。
_FALLBACK = {
    'npm': ('jest', 'vitest', 'mocha', 'pytest', 'unittest'),
    'pnpm': ('jest', 'vitest', 'mocha', 'pytest', 'unittest'),
    'yarn': ('jest', 'vitest', 'mocha', 'pytest', 'unittest'),
    'make': ('pytest', 'unittest', 'jest', 'vitest', 'mocha', 'go', 'cargo', 'surefire', 'bun', 'deno'),
    'mvn': ('surefire',),
    'gradle': ('surefire',),
    'bun': ('bun', 'jest'),
    'deno': ('deno',),
}


def detect_frameworks(command, depth=0):
    """按出现顺序返回测试框架名。不是测试命令时返回空列表。"""
    found = []
    if not isinstance(command, str) or not command.strip() or depth > 2:
        return found
    for segment in _segments(command):
        for name in _frameworks_in_segment(segment, depth):
            if name and name not in found:
                found.append(name)
    return found


def ticket_basename(text):
    """只在路径里明确写了 .team/tickets/<文件名> 时返回文件名。"""
    if not isinstance(text, str) or not text:
        return ''
    found = ''
    for match in _TICKET.finditer(text.replace('\\', '/')):
        name = match.group(1).strip()
        if not _TICKET_NAME.match(name) or '..' in name:
            continue
        found = name
    return found


def interpret_exit(result_block, output):
    """返回 (exitCode, exitStatus)。没有确切数字时 exitCode 是 None。"""
    block = result_block if isinstance(result_block, dict) else {}
    for key in ('exit_code', 'exitCode'):
        code = _as_code(block.get(key))
        if code is not None:
            return code, 'zero' if code == 0 else 'nonzero'
    if isinstance(output, str):
        match = _EXIT_LINE.search(output)
        if match:
            code = _as_code(int(match.group(1)))
            if code is not None:
                return code, 'zero' if code == 0 else 'nonzero'
    if block.get('is_error') is True:
        return None, 'nonzero'
    if block.get('is_error') is False:
        return 0, 'zero'
    return None, 'unknown'


def from_invocation(command, output, result_block=None, *extras):
    """认出测试命令时返回一条结果；否则返回 None。不含原始输出。"""
    frameworks = detect_frameworks(command)
    if not frameworks:
        return None
    exit_code, exit_status = interpret_exit(result_block, output if isinstance(output, str) else '')
    parsed = parse_output(frameworks, output, exit_code, exit_status)
    ticket = ''
    for text in (command,) + extras:
        found = ticket_basename(text)
        if found:
            ticket = found
    if ticket:
        parsed['ticket'] = ticket
    return parsed


def parse_output(frameworks, output, exit_code=None, exit_status='unknown'):
    """解析已确认的测试命令输出。frameworks 来自 detect_frameworks。"""
    names = []
    for name in frameworks or ():
        if name and name not in names:
            names.append(name)
    primary = names[0] if len(names) == 1 else 'mixed'
    if exit_status not in _EXIT_STATUSES:
        exit_status = 'unknown'
    text, cut = _clean(output)
    hits = []
    truncated = False
    for label, parser in _parsers_for(names):
        hit = parser(text)
        if not hit:
            continue
        if hit.get('truncated'):
            truncated = True
        if hit.get('conclusive'):
            chosen = dict(hit)
            chosen['framework'] = label
            hits.append(chosen)
    if cut and not hits:
        truncated = True
    if not hits and _TRUNC_MARK.search(text):
        truncated = True
    if len(hits) == 1:
        hit = hits[0]
    elif len(hits) > 1:
        statuses = {item['status'] for item in hits}
        counts = {(item['passed'], item['failed'], item['skipped'], item['errors']) for item in hits}
        if len(statuses) == 1 and len(counts) == 1:
            hit = hits[0]
        else:
            return _public(primary, 'unknown', None, None, None, None, exit_code, exit_status, False)
    else:
        return _public(primary, 'unknown', None, None, None, None, exit_code, exit_status, truncated)
    status = _verdict(hit['status'], exit_status)
    return _public(
        hit.get('framework') or primary, status,
        hit.get('passed'), hit.get('failed'), hit.get('skipped'), hit.get('errors'),
        exit_code, exit_status, False)


def attach_last_tests(tasks, people):
    """owner 与成员 name 完全一致时，把该成员最近一次测试抄到任务上。

    同一个名字有多条测试时留下时间较新的一条。不改 people。尾部空格不算对上。
    """
    by_name = {}
    for person in people or ():
        if not isinstance(person, dict):
            continue
        test = person.get('lastTest')
        name = person.get('name')
        if not isinstance(test, dict) or test.get('status') not in _STATUSES:
            continue
        if not isinstance(name, str) or not name:
            continue
        current = by_name.get(name)
        if current is not None and (test.get('ts') or 0) < (current.get('ts') or 0):
            continue
        copied = dict(test)
        if person.get('id'):
            copied['memberId'] = person.get('id')
        by_name[name] = copied
    for task in tasks or ():
        if not isinstance(task, dict):
            continue
        owner = task.get('owner')
        if not isinstance(owner, str):
            continue
        test = by_name.get(owner)
        if test is not None:
            task['lastTest'] = dict(test)
    return tasks


def _public(framework, status, passed, failed, skipped, errors, exit_code, exit_status, truncated):
    if status not in _STATUSES:
        status = 'unknown'
    return {
        'framework': framework or 'unknown',
        'status': status,
        'passed': passed,
        'failed': failed,
        'skipped': skipped,
        'errors': errors,
        'exitCode': exit_code,
        'exitStatus': exit_status if exit_status in _EXIT_STATUSES else 'unknown',
        'truncated': bool(truncated),
    }


def _verdict(status, exit_status):
    """摘要说通过、退出却非 0 时不选边，标成未知。失败的摘要仍然算失败。"""
    if status == 'fail':
        return 'fail'
    if status == 'pass' and exit_status == 'nonzero':
        return 'unknown'
    if status == 'pass':
        return 'pass'
    return 'unknown'


def _clean(output):
    if not isinstance(output, str) or not output:
        return '', False
    cut = len(output) > _MAX_OUTPUT
    text = output[-_TAIL:] if cut else output
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return _ANSI.sub('', text), cut


def _as_code(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 0 <= value <= 255:
        return value
    return None


def _segments(command):
    parts = []
    buf = []
    quote = ''
    i = 0
    length = len(command)
    while i < length:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote and command[i - 1] != '\\':
                quote = ''
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith('&&', i) or command.startswith('||', i):
            parts.append(''.join(buf))
            buf = []
            i += 2
            continue
        if ch in ';&|\n':
            parts.append(''.join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append(''.join(buf))
    return [part.strip() for part in parts if part.strip()]


def _argv(segment):
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return []


def _strip_prefix(tokens):
    tokens = list(tokens)
    while tokens:
        if _ENV.match(tokens[0]):
            tokens.pop(0)
            continue
        if tokens[0] in _WRAPPERS:
            tokens.pop(0)
            if tokens and tokens[0].startswith('-'):
                return []
            continue
        break
    return tokens


def _prog(token):
    name = str(token).replace('\\', '/').rsplit('/', 1)[-1].lower()
    if name.endswith('.exe'):
        name = name[:-4]
    return name


def _frameworks_in_segment(segment, depth):
    tokens = _strip_prefix(_argv(segment))
    if not tokens:
        return []
    prog = _prog(tokens[0])
    if prog in _SHELLS and '-c' in tokens:
        index = tokens.index('-c')
        if index + 1 < len(tokens):
            return detect_frameworks(tokens[index + 1], depth + 1)
        return []
    name = _direct_framework(tokens, prog)
    return [name] if name else []


def _skip_runner_flags(args):
    index = 0
    while index < len(args):
        tok = args[index]
        if tok in _NO_VALUE:
            index += 1
            continue
        if tok in _VALUE_FLAGS and index + 1 < len(args):
            index += 2
            continue
        if tok.startswith('-'):
            return None
        return args[index:]
    return []


def _test_script(name):
    return name == 'test' or name == 't' or name.startswith(('test:', 'test-', 'test_'))


def _npm_like(tokens):
    rest = _skip_runner_flags(tokens[1:])
    if not rest:
        return False
    if rest[0] in ('test', 't'):
        return True
    return rest[0] == 'run' and len(rest) > 1 and _test_script(rest[1])


def _python_module(tokens):
    index = 1
    while index < len(tokens):
        tok = tokens[index]
        if tok == '-m' and index + 1 < len(tokens):
            return tokens[index + 1].lower()
        if tok.startswith('-'):
            index += 1
            continue
        return ''
    return ''


def _direct_framework(tokens, prog):
    if _PYTHON.match(prog):
        module = _python_module(tokens)
        if module == 'pytest':
            return 'pytest'
        if module == 'unittest':
            return 'unittest'
        return ''
    if prog == 'coverage' and 'run' in tokens:
        if '-m' in tokens:
            index = tokens.index('-m')
            if index + 1 < len(tokens) and tokens[index + 1].lower() == 'pytest':
                return 'pytest'
        return ''
    if prog in _DIRECT:
        return _DIRECT[prog]
    if prog == 'go' and len(tokens) > 1 and tokens[1] == 'test':
        return 'go'
    if prog == 'cargo':
        rest = tokens[1:]
        if rest and rest[0].startswith('+') and not rest[0].startswith('++'):
            rest = rest[1:]
        if rest and rest[0] == 'test':
            return 'cargo'
        return ''
    if prog in ('npm', 'pnpm', 'yarn') and _npm_like(tokens):
        return prog
    if prog == 'npx' and len(tokens) > 1:
        tool = _prog(tokens[1])
        if tool in ('jest', 'vitest', 'mocha', 'pytest'):
            return 'pytest' if tool == 'pytest' else tool
        return ''
    if prog == 'bun' and len(tokens) > 1 and tokens[1] == 'test':
        return 'bun'
    if prog == 'deno' and len(tokens) > 1 and tokens[1] == 'test':
        return 'deno'
    if prog in ('mvn', 'mvnw') and any(tok == 'test' or tok.endswith(':test') for tok in tokens[1:]):
        return 'mvn'
    if prog in ('gradle', 'gradlew') and any(tok == 'test' for tok in tokens[1:]):
        return 'gradle'
    if prog == 'make' and any(tok == 'test' for tok in tokens[1:]):
        return 'make'
    return ''


def _parsers_for(names):
    ordered = []
    seen = set()
    for name in names:
        if name in _PARSERS:
            key = (name, _PARSERS[name])
            if key not in seen:
                seen.add(key)
                ordered.append((name, _PARSERS[name]))
        for inner in _FALLBACK.get(name, ()):
            parser = _PARSERS.get(inner)
            if parser is None:
                continue
            key = (name, parser)
            if key in seen:
                continue
            seen.add(key)
            ordered.append((name, parser))
    return ordered


def _hit(status, passed, failed, skipped, errors=0):
    return {
        'conclusive': True,
        'status': status,
        'passed': passed,
        'failed': failed,
        'skipped': skipped,
        'errors': errors,
        'truncated': False,
    }


def _trunc():
    return {
        'conclusive': False,
        'status': 'unknown',
        'passed': None,
        'failed': None,
        'skipped': None,
        'errors': None,
        'truncated': True,
    }


def _word(body, word):
    match = re.search(r'(\d+)\s+' + word + r'\b', body)
    return int(match.group(1)) if match else 0


def _named(body):
    return {name: int(value) for value, name in re.findall(
        r'(\d+)\s+(passed|failed|skipped|pending|todo|ignored|total)\b', body)}


def _parse_pytest(text):
    summaries = list(_PYTEST_SUMMARY.finditer(text))
    if summaries:
        body = summaries[-1].group(1).strip()
        if re.search(r'\bno tests ran\b', body):
            return _hit('unknown', 0, 0, 0, 0)
        passed = _word(body, 'passed')
        failed = _word(body, 'failed')
        skipped = _word(body, 'skipped')
        errors = _word(body, 'errors?')
        extra = _word(body, 'xfailed') + _word(body, 'xpassed')
        if not (passed or failed or skipped or errors or extra):
            return _hit('unknown', None, None, None, None)
        if failed or errors:
            status = 'fail'
        else:
            status = 'pass'
        return _hit(status, passed, failed, skipped, errors)
    interrupt = list(_PYTEST_INTERRUPT.finditer(text))
    if interrupt:
        return _hit('fail', 0, 0, 0, int(interrupt[-1].group(1)))
    if _PYTEST_STARTED.search(text):
        return _trunc()
    return None


def _parse_unittest(text):
    ran = list(re.finditer(r'^Ran (\d+) tests? in ', text, re.M))
    if not ran:
        if re.search(r'^(?:FAIL|ERROR):', text, re.M):
            return _trunc()
        return None
    total = int(ran[-1].group(1))
    tail = text[ran[-1].end():]
    ok = re.search(r'^OK(?:\s+\(([^)]*)\))?\s*$', tail, re.M)
    failed = re.search(r'^FAILED \(([^)]*)\)\s*$', tail, re.M)
    if not ok and not failed:
        return _trunc()
    blob = (failed or ok).group(1) or ''
    kv = {key: int(value) for key, value in re.findall(r'(failures|errors|skipped)=(\d+)', blob)}
    failures = kv.get('failures', 0)
    errors = kv.get('errors', 0)
    skipped = kv.get('skipped', 0)
    passed = total - failures - errors - skipped
    if passed < 0:
        return _hit('fail' if failed else 'unknown', None, None, None, None)
    if failures or errors:
        status = 'fail'
    elif total == 0:
        status = 'unknown'
    else:
        status = 'pass'
    return _hit(status, passed, failures, skipped, errors)


def _parse_go(text):
    packages = re.findall(r'^(ok|FAIL|\?)\s+\S+', text, re.M)
    cases = re.findall(r'^--- (PASS|FAIL|SKIP):', text, re.M)
    if packages:
        if 'FAIL' in packages:
            status = 'fail'
        elif 'ok' in packages:
            status = 'pass'
        else:
            status = 'unknown'
        if cases:
            return _hit(status, cases.count('PASS'), cases.count('FAIL'), cases.count('SKIP'), None)
        return _hit(status, None, None, None, None)
    if re.search(r'\[(?:build|setup) failed\]', text):
        return _hit('fail', None, None, None, None)
    if cases or re.search(r'^=== (?:RUN|PAUSE|CONT)\s', text, re.M) or re.search(r'^(?:PASS|FAIL)\s*$', text, re.M):
        return _trunc()
    return None


def _parse_cargo(text):
    rows = list(_CARGO_RESULT.finditer(text))
    if rows:
        passed = failed = skipped = 0
        status = 'pass'
        for row in rows:
            if row.group(1) == 'FAILED':
                status = 'fail'
            passed += int(row.group(2))
            failed += int(row.group(3))
            skipped += int(row.group(4))
        if failed:
            status = 'fail'
        return _hit(status, passed, failed, skipped, 0)
    if re.search(r'^error: could not compile\b', text, re.M) or re.search(r'^error: test failed\b', text, re.M):
        return _hit('fail', None, None, None, None)
    if re.search(r'^running \d+ tests?', text, re.M) or 'test result:' in text:
        return _trunc()
    return None


def _parse_jest(text):
    lines = list(re.finditer(r'^Tests:\s+(.+)$', text, re.M))
    if not lines:
        found = re.search(r'No tests found, exiting with code (\d+)', text)
        if found:
            code = int(found.group(1))
            return _hit('fail' if code else 'unknown', 0, 0, 0, 0)
        if re.search(r'^Test Suites:', text, re.M):
            return _trunc()
        return None
    counts = _named(lines[-1].group(1))
    if 'passed' not in counts and 'failed' not in counts:
        if counts.get('total') == 0:
            return _hit('unknown', 0, 0, 0, 0)
        return None
    passed = counts.get('passed', 0)
    failed = counts.get('failed', 0)
    skipped = counts.get('skipped', 0) + counts.get('pending', 0) + counts.get('todo', 0)
    if failed:
        status = 'fail'
    elif passed or skipped:
        status = 'pass'
    else:
        status = 'unknown'
    return _hit(status, passed, failed, skipped, 0)


def _parse_vitest(text):
    lines = list(re.finditer(r'^\s*Tests\s+(.+)$', text, re.M))
    if not lines:
        if re.search(r'^\s*Test Files\s+', text, re.M):
            return _trunc()
        return None
    body = lines[-1].group(1)
    if body.lstrip().startswith(':'):
        return None
    counts = _named(body)
    if 'passed' not in counts and 'failed' not in counts:
        return None
    passed = counts.get('passed', 0)
    failed = counts.get('failed', 0)
    skipped = counts.get('skipped', 0) + counts.get('todo', 0)
    status = 'fail' if failed else 'pass' if (passed or skipped) else 'unknown'
    return _hit(status, passed, failed, skipped, 0)


def _parse_mocha(text):
    passing = list(re.finditer(r'^\s*(\d+)\s+passing\s+\(', text, re.M))
    if not passing:
        return None
    tail = text[passing[-1].end():]
    failing = re.search(r'^\s*(\d+)\s+failing\s*$', tail, re.M)
    pending = re.search(r'^\s*(\d+)\s+pending\s*$', tail, re.M)
    if re.search(r'^\s+\d+\)\s', tail, re.M) and not failing:
        return _trunc()
    passed = int(passing[-1].group(1))
    failed = int(failing.group(1)) if failing else 0
    skipped = int(pending.group(1)) if pending else 0
    status = 'fail' if failed else 'pass' if (passed or skipped) else 'unknown'
    return _hit(status, passed, failed, skipped, 0)


def _parse_bun(text):
    ran = re.search(r'^Ran (\d+) tests? across ', text, re.M)
    if not ran:
        if re.search(r'^\s*\d+\s+pass$', text, re.M):
            return _trunc()
        return None

    def grab(word):
        match = re.search(r'(?m)^\s*(\d+)\s+' + word + r'$', text)
        return int(match.group(1)) if match else 0

    passed, failed, skipped = grab('pass'), grab('fail'), grab('skip')
    total = int(ran.group(1))
    if failed:
        status = 'fail'
    elif total == 0:
        status = 'unknown'
    else:
        status = 'pass'
    return _hit(status, passed, failed, skipped, 0)


def _parse_deno(text):
    rows = list(re.finditer(r'^(ok|FAILED)\s+\|\s+(.+)$', text, re.M))
    if not rows:
        return None
    line = rows[-1]
    counts = _named(line.group(2))
    if 'passed' not in counts and 'failed' not in counts:
        return None
    passed = counts.get('passed', 0)
    failed = counts.get('failed', 0)
    skipped = counts.get('skipped', 0) + counts.get('ignored', 0)
    status = 'fail' if line.group(1) == 'FAILED' or failed else 'pass'
    return _hit(status, passed, failed, skipped, 0)


def _parse_surefire(text):
    rows = list(_SUREFIRE.finditer(text))
    if not rows:
        if re.search(r'^\[INFO\] Building\b', text, re.M) or re.search(r'^> Task ', text, re.M):
            return _trunc()
        return None
    passed = failed = skipped = errors = 0
    used = 0
    for row in rows:
        total, failures, errs, skips = (int(row.group(i)) for i in range(1, 5))
        got = total - failures - errs - skips
        if got < 0:
            continue
        used += 1
        passed += got
        failed += failures
        errors += errs
        skipped += skips
    if not used:
        return None
    if failed or errors:
        status = 'fail'
    elif passed or skipped:
        status = 'pass'
    else:
        status = 'unknown'
    return _hit(status, passed, failed, skipped, errors)


_PARSERS = {
    'pytest': _parse_pytest,
    'unittest': _parse_unittest,
    'go': _parse_go,
    'cargo': _parse_cargo,
    'jest': _parse_jest,
    'vitest': _parse_vitest,
    'mocha': _parse_mocha,
    'bun': _parse_bun,
    'deno': _parse_deno,
    'surefire': _parse_surefire,
}
