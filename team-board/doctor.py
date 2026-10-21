#!/usr/bin/env python3
"""看板环境自检：端口、进程工具和版本是否允许换掉旧服务。

    python doctor.py
    python doctor.py --json
    python doctor.py --port 7799 --root ~/.claude/team-board
    python doctor.py --strict          # 有警告也返回 1

ensure.py 在 Unix 上依次用 lsof、ss、fuser 找监听进程；三个都没有时仍会继续启动。
旧进程若拒绝 /api/shutdown 并继续占着端口，listening() 立刻为真，脚本会打印 started。
本命令把这种情况直接说出来，不会去结束任何进程。有任意一个工具即视为可以按端口替换。
"""
import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
import urllib.request
from pathlib import Path

# 本机看板不走 HTTP 代理，否则 HTTP_PROXY 会把 127.0.0.1 送出本机。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_LABEL = {'ok': '通过', 'warn': '警告', 'fail': '失败'}
_LAYOUT = ('team_board.py', 'index.html', 'ensure.py')


def check(check_id, status, summary, hint=''):
    if status not in _LABEL:
        raise ValueError(status)
    return {'id': check_id, 'status': status, 'summary': summary, 'hint': hint}


def _is_windows():
    """测试打这个函数，不要改 os.name：那是进程全局值，会牵动标准库。"""
    return os.name == 'nt'


def _missing_tools_phrase():
    """与 ensure.pids_on_port 一致：Windows 只认 netstat；Unix 上 lsof、ss、fuser 有一个即可。"""
    if _is_windows():
        return 'netstat'
    return 'lsof、ss 或 fuser'


def tool_install_hint():
    if _is_windows():
        return '确认 netstat 在 PATH 中（Windows 通常位于 System32）'
    return ('安装其中之一：lsof，iproute2（提供 ss），或 psmisc（提供 fuser）。'
            'Debian/Ubuntu：sudo apt-get install lsof，或 sudo apt-get install iproute2，或 sudo apt-get install psmisc')


def _found_process_tools():
    if _is_windows():
        path = shutil.which('netstat')
        return [('netstat', path)] if path else []
    found = []
    for name in ('lsof', 'ss', 'fuser'):
        path = shutil.which(name)
        if path:
            found.append((name, path))
    return found


def parse_port(raw):
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, '端口无效: %s' % raw
    if port < 1 or port > 65535:
        return None, '端口超出 1–65535: %s' % port
    return port, ''


def read_local_version(root):
    root = Path(root)
    for path in (root / 'VERSION', root.parent / 'VERSION'):
        try:
            text = path.read_text(encoding='utf-8').strip()
        except OSError:
            continue
        if text:
            return text
    return ''


def probe_port(port, timeout=0.4):
    """返回 listening / board / version / error。不修改端口上的进程。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        if sock.connect_ex(('127.0.0.1', port)) != 0:
            return {'listening': False, 'board': False, 'version': '', 'error': ''}
    try:
        with _OPENER.open('http://127.0.0.1:%s/snapshot.json' % port, timeout=2) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('snapshot 不是 JSON 对象')
        return {'listening': True, 'board': True, 'version': str(payload.get('version') or ''), 'error': ''}
    except Exception as exc:
        return {'listening': True, 'board': False, 'version': '', 'error': type(exc).__name__}


def python_check():
    version = '.'.join(str(part) for part in sys.version_info[:3])
    if sys.version_info < (3, 8):
        return check('python', 'fail', 'Python %s 低于 3.8' % version, '请安装 Python 3.8 或更新版本')
    return check('python', 'ok', 'Python %s' % version, '')


def encoding_check():
    bad = []
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name)
        encoding = getattr(stream, 'encoding', None) or ''
        if encoding and encoding.lower().replace('-', '') != 'utf8':
            bad.append('%s=%s' % (name, encoding))
    if bad:
        return check('encoding', 'warn', '控制台编码不是 UTF-8（%s）' % ', '.join(bad),
                     '设置 PYTHONIOENCODING=utf-8，避免 Windows 控制台把中文日志打崩')
    return check('encoding', 'ok', 'stdout/stderr 为 UTF-8，或未声明编码', '')


def layout_check(root):
    missing = []
    for name in _LAYOUT:
        path = root / name
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(name)
    if missing:
        return check('layout', 'fail', '缺少或为空: %s' % '、'.join(missing), '在 team-board 目录中运行，或传入 --root')
    return check('layout', 'ok', '启动所需文件都在', '')


def config_check(root):
    path = root / 'team.json'
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as exc:
        return check('config', 'fail', '无法读取 team.json（%s）' % type(exc).__name__, '')
    except UnicodeError:
        return check('config', 'fail', 'team.json 不是 UTF-8', '')
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return check('config', 'fail', 'team.json 不是合法 JSON', '')
    if not isinstance(data, dict):
        return check('config', 'fail', 'team.json 必须是对象', '')
    missing = [key for key in ('agents', 'departments') if key not in data]
    if missing:
        return check('config', 'fail', 'team.json 缺少 %s，导入 team_board.py 会失败' % '、'.join(missing), '')
    if not isinstance(data['agents'], dict) or not isinstance(data['departments'], list):
        return check('config', 'fail', 'agents 必须是对象，departments 必须是数组', '')
    return check('config', 'ok', 'team.json 含有 agents 与 departments', '')


def version_check(local_version):
    if not local_version:
        return check('version', 'warn', '没有读到 VERSION', '安装副本应有 team-board/VERSION，仓库检出时在上一级')
    return check('version', 'ok', '本机版本 v%s' % local_version, '')


def writable_check(root):
    name = None
    failed = None
    try:
        fd, name = tempfile.mkstemp(prefix='.doctor-', suffix='.tmp', dir=str(root))
        os.close(fd)
    except OSError as exc:
        failed = exc
    if name:
        try:
            os.unlink(name)
        except OSError:
            pass
    if failed:
        return check('writable', 'fail', '目录不可写，无法写 server.log（%s）' % type(failed).__name__,
                     '检查 %s 的权限' % root)
    return check('writable', 'ok', '可以写入看板目录', '')


def pillow_check():
    try:
        import PIL  # noqa: F401
    except ImportError:
        return check('pillow', 'warn', '未安装 Pillow', '立绘缩放和 fit_pet 改图会降级。运行看板不需要它；需要时再 pip install pillow')
    return check('pillow', 'ok', '已安装 Pillow', '')


def tool_check():
    found = _found_process_tools()
    if found:
        shown = '、'.join('%s（%s）' % (name, path) for name, path in found)
        return check('process-tool', 'ok', '找到 %s' % shown, '')
    return check('process-tool', 'warn', '找不到 %s' % _missing_tools_phrase(), tool_install_hint())


def port_check(port, probed, local_version):
    if not probed['listening']:
        return check('port', 'ok', '127.0.0.1:%s 空闲' % port, '')
    if not probed['board']:
        detail = probed.get('error') or '没有 snapshot'
        return check('port', 'fail', '127.0.0.1:%s 已有程序监听，但不是看板（%s）' % (port, detail),
                     'doctor 不会建议结束未知进程。')
    remote = probed['version'] or '未知'
    if local_version and remote == local_version:
        return check('port', 'ok', '127.0.0.1:%s 上的看板已是 v%s' % (port, local_version), '')
    if not local_version:
        return check('port', 'warn', '127.0.0.1:%s 上的看板是 v%s，本机没有 VERSION' % (port, remote), '')
    return check('port', 'warn', '127.0.0.1:%s 上是 v%s，本机文件是 v%s' % (port, remote, local_version), '')


def restart_check(port, probed, local_version, tool_ok):
    phrase = _missing_tools_phrase()
    hint = tool_install_hint()
    if not probed['listening']:
        if tool_ok:
            return check('restart', 'ok', '端口空闲。以后若要换掉不响应关机接口的旧进程，可以按端口找到它。', '')
        return check('restart', 'warn',
                     '端口目前空闲，但找不到 %s。旧进程若拒绝 /api/shutdown，ensure 会把仍被占用的端口误判成新服务已启动。' % phrase,
                     hint)
    if not probed['board']:
        return check('restart', 'fail', '127.0.0.1:%s 被非看板进程占用，不能靠 ensure 安全替换。' % port,
                     '改用别的端口：TEAM_BOARD_PORT=7799 python team_board.py --port 7799')
    remote = probed['version'] or '未知'
    # ensure 只有在读到本机 VERSION 时才会因为版本不同去重启。没有 VERSION 就不能说它会误报启动成功。
    if not local_version:
        return check('restart', 'warn',
                     '本机没有 VERSION，不能判断 127.0.0.1:%s 上的 v%s 是不是旧进程。' % (port, remote),
                     '安装副本应有 team-board/VERSION，仓库检出时在上一级')
    if remote == local_version and tool_ok:
        return check('restart', 'ok', '服务已是本机 v%s，不需要替换。' % local_version, '')
    if remote == local_version:
        return check('restart', 'warn',
                     '当前已是本机 v%s，但找不到 %s。下次升级时，不退出的旧进程会换不掉。' % (local_version, phrase),
                     hint)
    summary = '在跑的是 v%s，本机文件是 v%s。' % (remote, local_version)
    if tool_ok:
        return check('restart', 'warn', summary + '可以运行 ensure.py：先请求 /api/shutdown，失败再按端口结束监听进程。',
                     'python ensure.py')
    return check('restart', 'fail',
                 summary + '找不到 %s。关机接口失败时 ensure 无法按端口结束旧进程，并可能把仍在监听的旧服务当成启动成功。' % phrase,
                 hint)


def diagnose(root, port):
    root = Path(root)
    local = read_local_version(root)
    probed = probe_port(port)
    tool = tool_check()
    checks = [
        python_check(),
        encoding_check(),
        layout_check(root),
        config_check(root),
        version_check(local),
        writable_check(root),
        pillow_check(),
        tool,
        port_check(port, probed, local),
        restart_check(port, probed, local, tool['status'] == 'ok'),
    ]
    counts = {'ok': 0, 'warn': 0, 'fail': 0}
    for item in checks:
        counts[item['status']] += 1
    return {
        'ok': counts['fail'] == 0,
        'port': port,
        'root': str(root),
        'counts': counts,
        'checks': checks,
    }


def format_text(report):
    port = report.get('port')
    lines = ['team-board doctor  端口 %s' % (port if port else '无效')]
    for item in report['checks']:
        lines.append('[%s] %s: %s' % (_LABEL[item['status']], item['id'], item['summary']))
        if item.get('hint'):
            lines.append('      提示: %s' % item['hint'])
    counts = report['counts']
    lines.append('结果: 失败 %s，警告 %s，通过 %s' % (counts['fail'], counts['warn'], counts['ok']))
    return '\n'.join(lines) + '\n'


def build_parser():
    parser = argparse.ArgumentParser(prog='doctor.py', description='检查本机看板能否启动，以及旧进程能否被换掉')
    parser.add_argument('--json', action='store_true', help='打印 JSON')
    parser.add_argument('--strict', action='store_true', help='有警告时也返回退出码 1')
    parser.add_argument('--port', default=None, help='覆盖 TEAM_BOARD_PORT，默认 7788')
    parser.add_argument('--root', type=Path, default=None, help='看板目录，默认是本脚本所在目录')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    root = args.root if args.root is not None else Path(__file__).resolve().parent
    if args.port is not None:
        port, error = parse_port(args.port)
    else:
        port, error = parse_port(os.environ.get('TEAM_BOARD_PORT', '7788'))
    if error:
        report = {
            'ok': False,
            'port': None,
            'root': str(root),
            'counts': {'ok': 0, 'warn': 0, 'fail': 1},
            'checks': [check('port', 'fail', error, '设置 1 到 65535 之间的端口')],
        }
    else:
        report = diagnose(root, port)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_text(report), end='')
    if args.strict and (report['counts']['fail'] or report['counts']['warn']):
        return 1
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
