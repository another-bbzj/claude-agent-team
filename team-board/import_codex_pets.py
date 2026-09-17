#!/usr/bin/env python3
"""
从你本机已安装的 Codex 桌面客户端里提取 9 只宠物雪碧图，放进看板的 sprites/ 目录。

    python import_codex_pets.py            # 自动查找 Codex 安装位置
    python import_codex_pets.py <app.asar 路径>

这些素材属于 OpenAI，仓库不附带；脚本只读你自己电脑上的安装文件，提取结果只在你本机使用。
纯标准库，asar 格式：4 字节 | 4 字节 header pickle 大小 | 4 字节 | 4 字节 JSON 长度 | JSON 目录 | 文件数据。
"""
import glob
import json
import os
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'sprites'


def candidates():
    pats = []
    if sys.platform == 'win32':
        for root in ('C:/Program Files/WindowsApps', 'D:/WindowsApps', 'E:/WindowsApps',
                     os.environ.get('LOCALAPPDATA', '') + '/Programs'):
            pats.append(f'{root}/*Codex*/app/resources/app.asar')
            pats.append(f'{root}/*Codex*/resources/app.asar')
        # Get-AppxPackage 能给出准确位置（WindowsApps 目录通常禁止列目录，但允许按完整路径读）
        try:
            import subprocess
            loc = subprocess.run(['powershell', '-NoProfile', '-Command',
                                  '(Get-AppxPackage -Name *Codex* | Select-Object -First 1).InstallLocation'],
                                 capture_output=True, text=True, timeout=20).stdout.strip()
            if loc:
                pats.insert(0, loc.replace('\\', '/') + '/app/resources/app.asar')
        except Exception:
            pass
    elif sys.platform == 'darwin':
        pats += ['/Applications/Codex.app/Contents/Resources/app.asar',
                 os.path.expanduser('~/Applications/Codex.app/Contents/Resources/app.asar')]
    else:
        pats += ['/opt/Codex/resources/app.asar', '/usr/lib/codex/resources/app.asar',
                 os.path.expanduser('~/.local/share/codex/resources/app.asar')]
    out = []
    for p in pats:
        out += glob.glob(p) if any(ch in p for ch in '*?') else ([p] if os.path.exists(p) else [])
    return out


def extract(asar: Path):
    with open(asar, 'rb') as f:
        _, pickle_size, _, json_len = struct.unpack('<IIII', f.read(16))
        header = json.loads(f.read(json_len).decode('utf-8'))
        base = 8 + pickle_size
        found = []

        def walk(node, path=''):
            for name, v in node.get('files', {}).items():
                if 'files' in v:
                    walk(v, f'{path}/{name}')
                elif '-spritesheet-' in name and name.endswith('.webp') and 'offset' in v:
                    found.append((name, int(v['offset']), int(v['size'])))
        walk(header)
        if not found:
            sys.exit('这个 app.asar 里没有找到 *-spritesheet-*.webp，可能是 Codex 版本不同')
        OUT.mkdir(exist_ok=True)
        n = 0
        for name, off, size in found:
            pet = name.split('-spritesheet-')[0]
            f.seek(base + off)
            data = f.read(size)
            if data[:4] != b'RIFF':
                print('跳过（不是 webp）:', name)
                continue
            (OUT / f'{pet}.webp').write_bytes(data)
            print(f'  {pet:12s} {size/1024:7.0f} KB')
            n += 1
        return n


def main():
    asar = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if asar is None:
        found = candidates()
        if not found:
            sys.exit('没找到 Codex 的 app.asar。把路径作为参数传进来：python import_codex_pets.py <app.asar>')
        asar = Path(found[0])
    print('读取', asar)
    n = extract(asar)
    print(f'完成：{n} 只宠物已放入 {OUT}。刷新看板即可使用（无需重启服务）。')


if __name__ == '__main__':
    main()
