#!/usr/bin/env python3
"""
看板背景板（立绘 / 壁纸 / 上传图）的本地存储工具 —— 零依赖（有 Pillow 时顺便缩图转 webp）。

v1.4.1：内置立绘预设已移除（不再联网下载任何图片），本文件只保留本地存储的纯函数：
读写 backdrop.json、校验/裁剪图片文件、缩图、保存图片。原 fetch_backdrop.py 已删除。
"""
import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKDROPS = HERE / 'backdrops'
MAX_BYTES = 15 * 1024 * 1024
MAX_SIDE = 1600
FILE_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,40}\.(png|webp|jpg)$')   # backdrops/ 里只认自己生成的文件名
DEFAULTS = {'file': '', 'credit': '', 'opacity': 0.9, 'side': 'right', 'enabled': False}


def sniff(data: bytes):
    """按文件头认图片类型：png / webp / jpg，认不出返回 None。"""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    if data[:3] == b'\xff\xd8\xff':
        return 'jpg'
    return None


def read_config(root: Path = None) -> dict:
    root = root or BACKDROPS
    cfg = dict(DEFAULTS)
    try:
        j = json.loads((root / 'backdrop.json').read_text(encoding='utf-8'))
        if isinstance(j, dict):
            cfg.update({k: j[k] for k in DEFAULTS if k in j})
    except Exception:
        pass
    return cfg


def clean_config(cfg: dict) -> dict:
    """规范字段：文件名只认 FILE_RE，透明度 0-1，位置 left/right。"""
    out = dict(DEFAULTS)
    f = str(cfg.get('file') or '')
    out['file'] = f if FILE_RE.match(f) else ''
    out['credit'] = str(cfg.get('credit') or '')[:200]
    try:
        out['opacity'] = round(min(1.0, max(0.0, float(cfg.get('opacity', 0.9)))), 3)
    except (TypeError, ValueError):
        out['opacity'] = 0.9
    out['side'] = 'left' if cfg.get('side') == 'left' else 'right'
    out['enabled'] = bool(cfg.get('enabled')) and bool(out['file'])
    return out


def write_config(cfg: dict, root: Path = None) -> dict:
    root = root or BACKDROPS
    root.mkdir(parents=True, exist_ok=True)
    cfg = clean_config(cfg)
    tmp = root / 'backdrop.json.tmp'
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, root / 'backdrop.json')
    return cfg


def shrink(data: bytes):
    """有 Pillow：长边缩到 ≤ MAX_SIDE 并转 webp（保留透明）；没有 Pillow 或失败就原样返回。返回 (bytes, ext)。"""
    ext = sniff(data)
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data))
        im.load()
        if max(im.size) > MAX_SIDE:
            k = MAX_SIDE / max(im.size)
            im = im.resize((max(1, round(im.size[0] * k)), max(1, round(im.size[1] * k))), Image.LANCZOS)
        if im.mode not in ('RGB', 'RGBA'):
            im = im.convert('RGBA')
        buf = io.BytesIO()
        im.save(buf, 'WEBP', quality=90, method=4)
        out = buf.getvalue()
        if sniff(out) == 'webp' and len(out) < len(data):
            return out, 'webp'
    except Exception:
        pass
    return data, ext


def save_image(data: bytes, stem: str, root: Path = None, credit: str = '', optimize: bool = False) -> dict:
    """把图片存成 backdrops/<stem>.<ext> 并启用；删掉之前的背景图（只删 FILE_RE 认得的），保留透明度 / 位置设置。"""
    root = root or BACKDROPS
    if not data or len(data) > MAX_BYTES:
        raise ValueError(f'图片为空或超过 {MAX_BYTES // 1024 // 1024} MB')
    if not sniff(data):
        raise ValueError('只支持 png / webp / jpg 图片')
    stem = re.sub(r'[^a-z0-9_-]+', '-', str(stem or 'custom').lower()).strip('-')[:36] or 'custom'
    if optimize:
        data, ext = shrink(data)
    else:
        ext = sniff(data)
    name = f'{stem}.{ext}'
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (name + '.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, root / name)
    for old in root.iterdir():
        if old.is_file() and old.name != name and FILE_RE.match(old.name):
            try:
                old.unlink()
            except OSError:
                pass
    cfg = read_config(root)
    cfg.update({'file': name, 'credit': credit, 'enabled': True})
    return write_config(cfg, root)
