#!/usr/bin/env python3
"""确保 Agent Team 看板服务在后台运行，并且跑的是本机当前版本的代码。

    python ensure.py            # 只保证服务在跑（SessionStart 钩子用）；发现在跑的是旧版本会自动重启
    python ensure.py --open     # 保证在跑，并在默认浏览器打开看板（10 分钟内只开一次）
    python ensure.py --restart  # 无条件重启（install.py / update.py 装完后调用）

为什么要比版本：`git pull && python install.py` 只换了文件，之前启动的 team_board.py 进程还在内存里跑旧代码，
页面是新的、服务是旧的，就会出现「导入形象 not found」「没有更新提示」这类怪问题。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get('TEAM_BOARD_PORT', '7788'))


def local_version() -> str:
    try:
        return (HERE / 'VERSION').read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def listening() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(('127.0.0.1', PORT)) == 0


def running_version() -> str:
    """问正在跑的服务它的版本；老版本没有 version 字段返回 ''。"""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/snapshot.json', timeout=3) as r:
            return str(json.loads(r.read().decode('utf-8')).get('version') or '')
    except Exception:
        return ''


def pids_on_port():
    out = set()
    try:
        if os.name == 'nt':
            txt = subprocess.run(['netstat', '-ano', '-p', 'tcp'], capture_output=True, text=True, timeout=10).stdout
            for line in txt.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f':{PORT}') and parts[3] == 'LISTENING':
                    out.add(int(parts[4]))
        else:
            txt = subprocess.run(['lsof', '-ti', f'tcp:{PORT}', '-sTCP:LISTEN'], capture_output=True, text=True, timeout=10).stdout
            out.update(int(x) for x in txt.split() if x.strip().isdigit())
    except Exception:
        pass
    return out


def stop_server(reason: str):
    """先礼后兵：POST /api/shutdown（1.3+ 有），不行就按端口找进程杀掉（老版本）。"""
    print(f'重启看板服务（{reason}）…')
    try:
        urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{PORT}/api/shutdown', data=b'{}',
                                                      headers={'Content-Type': 'application/json'}), timeout=3).read()
    except Exception:
        pass
    for _ in range(12):
        if not listening():
            return
        time.sleep(0.25)
    for pid in pids_on_port():
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
            else:
                os.kill(pid, 9)
        except Exception:
            pass
    for _ in range(12):
        if not listening():
            return
        time.sleep(0.25)


def start_server():
    exe = Path(sys.executable)
    pyw = exe.with_name('pythonw.exe') if os.name == 'nt' and exe.with_name('pythonw.exe').exists() else exe
    log = open(HERE / 'server.log', 'ab')
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True   # mac / linux：脱离终端会话，关掉终端也不会跟着死
    subprocess.Popen([str(pyw), str(HERE / 'team_board.py'), '--port', str(PORT)],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, close_fds=True, cwd=str(HERE), **kwargs)
    for _ in range(40):
        if listening():
            break
        time.sleep(0.25)
    print(f'team-board v{local_version() or "?"} started on http://127.0.0.1:{PORT}/')


def open_board():
    """在系统默认浏览器里打开看板；10 分钟内只开一次，避免每次派工都弹新标签页。"""
    import webbrowser
    stamp = HERE / '.last-open'
    try:
        if stamp.exists() and time.time() - stamp.stat().st_mtime < 600:
            print(f'看板已在浏览器中：http://127.0.0.1:{PORT}/  ← 回复里请用这个链接：[Agent Team 指挥室](http://127.0.0.1:{PORT}/)')
            return
    except OSError:
        pass
    for _ in range(20):  # 等服务起来
        if listening():
            break
        time.sleep(0.25)
    webbrowser.open(f'http://127.0.0.1:{PORT}/')
    stamp.write_text(str(time.time()), encoding='utf-8')
    print(f'已在浏览器打开 http://127.0.0.1:{PORT}/  ← 回复里请用这个链接：[Agent Team 指挥室](http://127.0.0.1:{PORT}/)')


def main():
    args = sys.argv[1:]
    want_open = '--open' in args
    if listening():
        want = local_version()
        have = running_version()
        if '--restart' in args:
            stop_server('--restart')
        elif want and have != want:
            stop_server(f'在跑的是 v{have or "旧版本"}，本机文件是 v{want}')
        else:
            if want_open:
                open_board()
            return
    start_server()
    if want_open:
        open_board()


if __name__ == '__main__':
    main()
