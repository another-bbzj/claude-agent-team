#!/usr/bin/env python3
"""确保 Agent Team 看板服务在后台运行：端口已监听则跳过，否则启动 team_board.py（无窗口）。"""
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


def main():
    if listening():
        return
    exe = Path(sys.executable)
    pyw = exe.with_name('pythonw.exe') if os.name == 'nt' and exe.with_name('pythonw.exe').exists() else exe
    log = open(HERE / 'server.log', 'ab')
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
    subprocess.Popen([str(pyw), str(HERE / 'team_board.py'), '--port', str(PORT)],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True, cwd=str(HERE))
    print(f'team-board started on http://127.0.0.1:{PORT}/')


if __name__ == '__main__':
    main()
