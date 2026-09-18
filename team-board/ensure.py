#!/usr/bin/env python3
"""确保 Agent Team 看板服务在后台运行：端口已监听则跳过，否则启动 team_board.py（无窗口）。

    python ensure.py          # 只保证服务在跑（SessionStart 钩子用）
    python ensure.py --open   # 保证在跑，并在默认浏览器打开看板（10 分钟内只开一次）
"""
import os
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get('TEAM_BOARD_PORT', '7788'))


def listening() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(('127.0.0.1', PORT)) == 0


def open_board():
    """在系统默认浏览器里打开看板；10 分钟内只开一次，避免每次派工都弹新标签页。"""
    import time, webbrowser
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
    want_open = '--open' in sys.argv[1:]
    if listening():
        if want_open:
            open_board()
        return
    exe = Path(sys.executable)
    pyw = exe.with_name('pythonw.exe') if os.name == 'nt' and exe.with_name('pythonw.exe').exists() else exe
    log = open(HERE / 'server.log', 'ab')
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
    subprocess.Popen([str(pyw), str(HERE / 'team_board.py'), '--port', str(PORT)],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True, cwd=str(HERE))
    print(f'team-board started on http://127.0.0.1:{PORT}/')
    if want_open:
        open_board()


if __name__ == '__main__':
    main()
