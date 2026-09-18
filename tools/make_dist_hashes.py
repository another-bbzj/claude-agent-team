#!/usr/bin/env python3
"""
发布前运行：把 agents/ 与 skills/ 里每个文件在**所有历史提交**中的内容哈希写进 dist-hashes.json。
install.py 更新时靠它判断用户装的旧文件有没有被改过：哈希在表里 = 原样的旧版本 → 直接覆盖；不在 = 用户改过 → 保留并另存 .new。

    python tools/make_dist_hashes.py
"""
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'dist-hashes.json'


def git(*args):
    return subprocess.run(['git', *args], cwd=str(ROOT), capture_output=True, text=True, encoding='utf-8', errors='replace').stdout


def main():
    hashes = {}
    commits = git('rev-list', '--all').split()
    for c in commits:
        for line in git('ls-tree', '-r', c, '--', 'agents', 'skills').splitlines():
            meta, path = line.split('\t', 1)
            blob = meta.split()[2]
            data = subprocess.run(['git', 'cat-file', 'blob', blob], cwd=str(ROOT), capture_output=True).stdout
            for variant in (data, data.replace(b'\r\n', b'\n'), data.replace(b'\n', b'\r\n')):  # 行尾差异不算改动
                hashes.setdefault(path, set()).add(hashlib.sha256(variant).hexdigest())
    # 工作区当前内容也算
    for sub in ('agents', 'skills'):
        for f in (ROOT / sub).rglob('*'):
            if f.is_file():
                data = f.read_bytes()
                for variant in (data, data.replace(b'\r\n', b'\n'), data.replace(b'\n', b'\r\n')):
                    hashes.setdefault(f.relative_to(ROOT).as_posix(), set()).add(hashlib.sha256(variant).hexdigest())
    OUT.write_text(json.dumps({k: sorted(v) for k, v in sorted(hashes.items())}, indent=1) + '\n', encoding='utf-8')
    print(f'{len(hashes)} 个文件，{sum(len(v) for v in hashes.values())} 个历史哈希 -> {OUT}')


if __name__ == '__main__':
    main()
