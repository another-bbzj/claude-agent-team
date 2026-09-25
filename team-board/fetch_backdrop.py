#!/usr/bin/env python3
"""
看板背景板（立绘）下载 / 管理 —— 零依赖（有 Pillow 时顺便缩图转 webp）。

版权：立绘归原作者所有（黍 · 明日方舟 © Hypergryph），仓库不附带任何立绘；本脚本只把图下载到你本机的
team-board/backdrops/（已 gitignore），仅供个人看板装饰使用。

用法:
  python fetch_backdrop.py                 # 默认下载「黍」精英零立绘并启用
  python fetch_backdrop.py shu --elite 2   # 精二立绘
  python fetch_backdrop.py --off           # 关闭背景（图片保留）
看板服务端 POST /api/backdrop {preset:'shu'} 调的是这里的 fetch_preset()。
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):   # Windows 的 GBK 控制台：打印中文不能把脚本弄崩
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
BACKDROPS = HERE / 'backdrops'
UA = 'Mozilla/5.0 (agent-team-board; +https://github.com/) backdrop-fetch/1.0'
MAX_BYTES = 15 * 1024 * 1024
MAX_SIDE = 1600
FILE_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,40}\.(png|webp|jpg)$')   # backdrops/ 里只认自己生成的文件名
NOTICE = '[backdrop] 立绘版权归原作者所有（黍 · 明日方舟 © Hypergryph，图源 PRTS）；仅下载到本机作个人看板装饰，请勿再分发。'

PRESETS = {
    'shu': {'name': '黍（明日方舟）', 'title': '立绘_黍_{elite}.png', 'credit': '黍 · 明日方舟 © Hypergryph · 来源 PRTS'},
}
PRTS_API = 'https://prts.wiki/api.php?action=query&titles={}&prop=imageinfo&iiprop=url&format=json'
PRTS_MEDIA = 'https://media.prts.wiki/{}/{}/{}'   # MediaWiki 的上传目录：按文件名 md5 的前 1 / 2 位分桶
DEFAULTS = {'file': '', 'credit': '', 'opacity': 0.9, 'side': 'right', 'enabled': False}


def preset_list() -> list:
    return [{'id': k, 'name': v['name']} for k, v in PRESETS.items()]


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


def _http_get(url: str, timeout: float = 30) -> bytes:
    """先按系统 / 环境代理下载；代理不通（TLS 握手失败、连不上）再直连试一次。"""
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, OSError):
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with direct.open(req, timeout=timeout) as r:
            data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError('下载的图片超过 15 MB')   # 站点侧问题 → 服务端 502，而不是 400
    return data


def media_url(filename: str) -> str:
    """不查 API 直接推 PRTS 原图地址（MediaWiki 标准：/<md5[0]>/<md5[:2]>/<文件名>）。"""
    h = hashlib.md5(filename.replace(' ', '_').encode('utf-8')).hexdigest()
    return PRTS_MEDIA.format(h[0], h[:2], urllib.parse.quote(filename.replace(' ', '_')))


def preset_url(name: str, elite: int = 1) -> str:
    """查 PRTS MediaWiki API 拿立绘原图地址；API 被拦（常见 403）或不通时按文件名 md5 推地址。"""
    filename = PRESETS[name]['title'].format(elite=elite)
    try:
        j = json.loads(_http_get(PRTS_API.format(urllib.parse.quote('File:' + filename)), timeout=20).decode('utf-8'))
        for page in (j.get('query') or {}).get('pages', {}).values():
            for ii in page.get('imageinfo') or []:
                if ii.get('url'):
                    return ii['url']
    except Exception:
        pass
    return media_url(filename)


def fetch_preset(name: str = 'shu', elite: int = 1, root: Path = None) -> dict:
    """下载预设立绘到 backdrops/ 并启用，写好 backdrop.json 并返回它。
    未知预设 → ValueError；网络 / 站点问题 → RuntimeError 或 OSError（服务端据此回 502）。"""
    name = str(name or 'shu').strip().lower()
    if name not in PRESETS:
        raise ValueError('未知的预设：' + name + '（可选：' + '、'.join(PRESETS) + '）')
    try:
        elite = int(elite)
    except (TypeError, ValueError):
        elite = 1
    if elite not in (1, 2):
        raise ValueError('elite 只能是 1 或 2')
    print(NOTICE)
    url = preset_url(name, elite)
    data = _http_get(url, timeout=60)
    if not sniff(data):
        raise RuntimeError('下载到的不是图片（可能被站点拦截）：' + url)
    return save_image(data, name, root, credit=PRESETS[name]['credit'], optimize=True)


def main():
    ap = argparse.ArgumentParser(description='下载看板背景立绘（仅存本机，不入库）')
    ap.add_argument('preset', nargs='?', default='shu', help='预设：' + ' / '.join(PRESETS))
    ap.add_argument('--elite', type=int, default=1, help='精英化阶段立绘：1（默认）或 2')
    ap.add_argument('--off', action='store_true', help='关闭背景（不删图）')
    a = ap.parse_args()
    if a.off:
        cfg = read_config()
        cfg['enabled'] = False
        write_config(cfg)
        print('[backdrop] 已关闭背景板')
        return 0
    t = time.time()
    try:
        cfg = fetch_preset(a.preset, a.elite)
    except Exception as e:
        print(f'[backdrop] 失败：{type(e).__name__}: {e}')
        return 1
    size = (BACKDROPS / cfg['file']).stat().st_size
    print(f"[backdrop] 已保存 {BACKDROPS / cfg['file']}（{size // 1024} KB，{time.time() - t:.1f}s），并启用。刷新看板即可看到。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
