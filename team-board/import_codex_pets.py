#!/usr/bin/env python3
"""
把你本机 Codex 桌面客户端里的宠物雪碧图放进看板的 sprites/ 目录：

  1. 内置 9 只：从 Codex 安装目录的 app.asar 里提取（素材属于 OpenAI，仓库不附带，只在你本机使用）；
  2. 你自己的桌宠：Codex 里 Create pet / 领养的（~/.codex/pets/<id>/）、npx petdex install 装的（~/.petdex/pets/<id>/）；
  3. 任意桌宠文件：把 zip 包 / 目录 / 雪碧图路径作为参数传入（Codex、petdex 同格式：pet.json + spritesheet.webp|png）。

    python import_codex_pets.py                 # 两种都自动找
    python import_codex_pets.py <app.asar 路径>   # 指定 asar
    python import_codex_pets.py my-pet.zip ./boba/ sheet.png   # 导入桌宠文件
    环境变量 CODEX_HOME 可改 ~/.codex 的位置；--out DIR 可改输出目录（默认 ./sprites）。

雪碧图规格与 Codex 一致：每格 192×208，8 列；内置版 11 行（1536×2288），自制版 9 行（1536×1872）。
看板只用前 9 行（idle / 左右跑 / 挥手 / 跳 / 失败 / 等待 / 运行 / 审查），两种都能直接用。
纯标准库。asar 格式：4 字节 | 4 字节 header pickle 大小 | 4 字节 | 4 字节 JSON 长度 | JSON 目录 | 文件数据。
"""
import glob
import json
import os
import re
import struct
import sys
from pathlib import Path

# Windows 控制台默认 cp936/cp1252 打印中文会崩，统一按 UTF-8 输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
OUT = HERE / 'sprites'
sys.path.insert(0, str(HERE))
import team_board as tb  # noqa: E402  复用雪碧图解析 / 校验 / 边车写入


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
                pats.insert(0, loc.replace(chr(92), '/') + '/app/resources/app.asar')
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


def extract(asar: Path, out: Path):
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
            print('这个 app.asar 里没有找到 *-spritesheet-*.webp，可能是 Codex 版本不同')
            return 0
        out.mkdir(exist_ok=True)
        n = 0
        for name, off, size in found:
            pet = name.split('-spritesheet-')[0]
            f.seek(base + off)
            data = f.read(size)
            if data[:4] != b'RIFF':
                print('跳过（不是 webp）:', name)
                continue
            (out / f'{pet}.webp').write_bytes(data)
            print(f'  {pet:14s} {size/1024:7.0f} KB  内置')
            n += 1
        return n


def custom_pets(out: Path):
    """用户自己的桌宠：~/.codex/pets/<id>/（Codex 里 Create pet / 领养的）与 ~/.petdex/pets/<id>/（npx petdex install 装的）。
    每个目录含 pet.json + spritesheet.webp|png。也可把任意 zip 包 / 目录路径作为参数传进来。"""
    roots = [Path(os.environ.get('CODEX_HOME') or (Path.home() / '.codex')) / 'pets',
             Path(os.environ.get('PETDEX_HOME') or (Path.home() / '.petdex')) / 'pets']
    n = 0
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if d.is_dir():
                n += import_path(d, out, tag=root.parent.name)
    return n


def import_path(path: Path, out: Path, tag: str = '') -> int:
    """导入一个目录（pet.json + 雪碧图）、一个 zip 包或一张雪碧图。返回导入数量（0/1）。"""
    import io as _io
    import zipfile
    try:
        if path.is_dir():
            meta = {}
            pj = path / 'pet.json'
            if pj.is_file():
                try:
                    meta = json.loads(pj.read_text(encoding='utf-8-sig'))
                except Exception:
                    meta = {}
            want = str(meta.get('spritesheetPath') or '').replace(chr(92), '/').rsplit('/', 1)[-1]
            cands = [path / want] if want and (path / want).is_file() else []
            cands += [f for f in sorted(path.iterdir()) if f.suffix.lower() in ('.webp', '.png')]
            if not cands:
                return 0
            data = cands[0].read_bytes()
            hint = meta.get('id') or meta.get('slug') or meta.get('name') or meta.get('displayName') or path.name
        else:
            data = path.read_bytes()
            meta = {}
            hint = re.sub(r'[-_ ]?sprite(sheet)?.*$', '', path.stem, flags=re.I) or path.stem
        m, ext, img = tb.parse_pet_package(data, hint)
        for k in ('displayName', 'description', 'spriteVersionNumber', 'author', 'credit', 'source', 'license'):
            if meta.get(k) and not m.get(k):
                m[k] = meta[k]
        if not m.get('id'):
            m['id'] = tb.slugify_pet_id(hint)
        info = tb.save_pet(m, ext, img, overwrite=True, source=str(path), credit=str(m.get('credit') or m.get('author') or (tag and f'{tag} 本机文件') or ''))
        print(f"  {info['id']:14s} {len(img)/1024:7.0f} KB  {tag or '文件'} {info.get('displayName', '')}".rstrip())
        return 1
    except ValueError as e:
        print(f'跳过 {path.name}：{e}')
        return 0


def main():
    args = [a for a in sys.argv[1:]]
    out = OUT
    if '--out' in args:
        i = args.index('--out')
        out = Path(args[i + 1]).resolve()
        del args[i:i + 2]
    out.mkdir(parents=True, exist_ok=True)
    tb.SPRITES = out   # 让 team_board 的 save_pet 也写到这里
    n = 0
    asar, extra = None, []
    for a in args:
        p = Path(a)
        if p.suffix.lower() == '.asar' or a.endswith('app.asar'):
            asar = p
        else:
            extra.append(p)
    if asar is None and not extra:
        found = candidates()
        asar = Path(found[0]) if found else None
    if asar is not None:
        print('读取', asar)
        try:
            n += extract(asar, out)
        except (OSError, ValueError, json.JSONDecodeError, struct.error) as e:
            print(f'读取 app.asar 失败（{e}），跳过内置宠物')
    elif not extra:
        print('没找到 Codex 的 app.asar（可作为参数传入：python import_codex_pets.py <app.asar>），跳过内置宠物')
    for p in extra:
        if p.exists():
            n += import_path(p, out)
        else:
            print('不存在：', p)
    if not extra:
        n += custom_pets(out)
    if not n:
        sys.exit('没有导入任何宠物')
    print(f'完成：{n} 只宠物已放入 {out}。刷新看板即可使用（无需重启服务）。')


if __name__ == '__main__':
    main()
