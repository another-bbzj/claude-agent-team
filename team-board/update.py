#!/usr/bin/env python3
"""
检查 / 一键更新 claude-agent-team（看板顶栏的「更新」按钮调用的就是它）。

    python ~/.claude/team-board/update.py            # 只检查：比较本机 VERSION 与 GitHub 上的
    python ~/.claude/team-board/update.py --apply    # 下载最新版并安装（跑它自带的 install.py，你的配置 / 形象 / 改过的成员都保留）
    python ~/.claude/team-board/update.py --apply --restart   # 装完让看板服务重启到新代码

纯标准库；下载的是 GitHub 的 main 分支 zip（约 3 MB）。环境变量 TEAM_UPDATE_REPO 可指向 fork（owner/repo）。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
REPO = os.environ.get('TEAM_UPDATE_REPO', 'another-bbzj/claude-agent-team')
VERSION_URL = os.environ.get('TEAM_UPDATE_VERSION_URL') or f'https://raw.githubusercontent.com/{REPO}/main/VERSION'
ZIP_URL = os.environ.get('TEAM_UPDATE_ZIP_URL') or f'https://github.com/{REPO}/archive/refs/heads/main.zip'
UA = {'User-Agent': 'claude-agent-team-updater'}
PORT = int(os.environ.get('TEAM_BOARD_PORT', '7788'))


def local_version() -> str:
    for f in (HERE / 'VERSION', HERE.parent / 'VERSION'):   # 安装后在 team-board/ 里；仓库检出时在根目录
        try:
            return f.read_text(encoding='utf-8').strip()
        except OSError:
            continue
    return '0.0.0'


def parse_ver(v: str):
    out = []
    for part in (v or '0').strip().lstrip('v').split('.'):
        num = ''.join(ch for ch in part if ch.isdigit())
        out.append(int(num or 0))
    return tuple(out + [0] * (3 - len(out)))


def fetch(url: str, timeout=20) -> bytes:
    if url.startswith('file:'):
        from urllib.parse import urlparse
        from urllib.request import url2pathname
        return Path(url2pathname(urlparse(url).path)).read_bytes()
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def check() -> dict:
    local = local_version()
    try:
        remote = fetch(VERSION_URL, timeout=10).decode('utf-8', 'replace').strip().splitlines()[0]
        return {'local': local, 'remote': remote, 'hasUpdate': parse_ver(remote) > parse_ver(local), 'checkedAt': time.time()}
    except Exception as e:
        return {'local': local, 'remote': '', 'hasUpdate': False, 'error': f'{type(e).__name__}: {e}', 'checkedAt': time.time()}


def apply(restart: bool = False) -> int:
    print(f'下载 {ZIP_URL} …')
    data = fetch(ZIP_URL, timeout=120)
    tmp = Path(tempfile.mkdtemp(prefix='team-update-'))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(tmp)
    roots = [d for d in tmp.iterdir() if d.is_dir() and (d / 'install.py').exists()]
    if not roots:
        print('压缩包里没有 install.py，放弃')
        return 1
    root = roots[0]
    print(f'安装 v{(root / "VERSION").read_text(encoding="utf-8").strip() if (root / "VERSION").exists() else "?"} …')
    r = subprocess.run([sys.executable, str(root / 'install.py'), '--no-pets'], cwd=str(root),
                       env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, capture_output=True, text=True, encoding='utf-8', errors='replace')
    print(r.stdout.strip())
    if r.returncode != 0:
        print(r.stderr.strip())
        return r.returncode
    if restart:
        restart_board()
    return 0


def restart_board():
    """让正在跑的看板服务退出，再用 ensure.py 起新代码。"""
    try:
        urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{PORT}/api/shutdown', data=b'{}',
                                                      headers={'Content-Type': 'application/json'}), timeout=5).read()
    except Exception:
        pass
    for _ in range(40):
        time.sleep(0.25)
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{PORT}/snapshot.json', timeout=1).read()
        except Exception:
            break
    subprocess.run([sys.executable, str(HERE / 'ensure.py')], env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    print('看板已重启到新版本')


def main():
    args = sys.argv[1:]
    if '--json' in args:
        print(json.dumps(check(), ensure_ascii=False))
        return
    if '--apply' in args:
        sys.exit(apply(restart='--restart' in args))
    c = check()
    if c.get('error'):
        print(f'本机 v{c["local"]}；检查失败：{c["error"]}')
    elif c['hasUpdate']:
        print(f'本机 v{c["local"]} → 有新版本 v{c["remote"]}。运行：python {HERE / "update.py"} --apply --restart')
    else:
        print(f'本机 v{c["local"]} 已是最新（GitHub v{c["remote"]}）')


if __name__ == '__main__':
    main()
