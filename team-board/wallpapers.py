#!/usr/bin/env python3
"""
Wallpaper Engine 壁纸发现 + 白名单文件服务（零依赖）。

只读扫描本机 Steam 库下的创意工坊壁纸（appid 431960）与本地壁纸工程，
供看板背景板选用；不修改、不复制任何用户文件（复制动作由调用方 team_board.py 决定）。

发现范围：
  <steam库>/steamapps/workshop/content/431960/*        （创意工坊订阅）
  <steam库>/steamapps/common/wallpaper_engine/projects/myprojects/*  （本地工程）
Steam 库来源：Windows 注册表 HKCU/HKLM\\...\\Valve\\Steam + libraryfolders.vdf；
macOS ~/Library/Application Support/Steam；Linux ~/.steam/steam、~/.local/share/Steam；
再加环境变量 WALLPAPER_ENGINE_DIRS（; 或 : 分隔，测试用它伪造 Steam 库）。
"""
import json
import mimetypes
import os
import re
import sys
from pathlib import Path

WORKSHOP_APPID = '431960'
PREVIEW_NAMES = ('preview.jpg', 'preview.png', 'preview.gif')
VIDEO_EXTS = ('.mp4', '.webm')
CHUNK = 256 * 1024

_scan_cache = {'sig': None, 'items': None}


def _vdf_paths(text: str):
    """libraryfolders.vdf 里粗略抠出所有 "path" "<...>"（够用，不需要完整 VDF 解析器）。"""
    return [p.replace('\\\\', '\\') for p in re.findall(r'"path"\s*"([^"]+)"', text)]


def steam_library_dirs():
    """本机所有 Steam 库根目录（含 steamapps 的那一层），去重。
    lean: 设 WALLPAPER_ENGINE_TEST_ISOLATE=1 时跳过真实注册表/默认路径探测，只用 WALLPAPER_ENGINE_DIRS——
    测试专用开关，本机真实跑 /api/wallpapers 时不要设它。"""
    roots = []
    if os.environ.get('WALLPAPER_ENGINE_TEST_ISOLATE'):
        pass
    elif sys.platform == 'win32':
        try:
            import winreg
            for hive, sub, key in (
                (winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Valve\Steam', 'InstallPath'),
            ):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        v, _ = winreg.QueryValueEx(k, key)
                        roots.append(Path(v))
                except OSError:
                    pass
        except ImportError:
            pass
    else:
        home = Path.home()
        roots += [home / 'Library/Application Support/Steam', home / '.steam/steam', home / '.local/share/Steam']

    extra = os.environ.get('WALLPAPER_ENGINE_DIRS', '').strip()
    if extra:
        roots += [Path(p) for p in extra.split(os.pathsep) if p.strip()]   # Windows 用 ;，其他系统用 :（按 ':' 切会把 C:\ 盘符切断）

    out, seen = [], set()
    for base in roots:
        base = Path(base)
        if not base.is_dir():
            continue
        libs = [base]
        vdf = base / 'steamapps' / 'libraryfolders.vdf'
        if vdf.is_file():
            try:
                libs += [Path(p) for p in _vdf_paths(vdf.read_text(encoding='utf-8', errors='replace'))]
            except OSError:
                pass
        for lib in libs:
            key = str(lib)
            if key not in seen:
                seen.add(key)
                out.append(lib)
    return out


def _project_dirs():
    seen = set()
    for lib in steam_library_dirs():
        for root in (Path(lib) / 'steamapps' / 'workshop' / 'content' / WORKSHOP_APPID,
                     Path(lib) / 'steamapps' / 'common' / 'wallpaper_engine' / 'projects' / 'myprojects'):
            if not root.is_dir():
                continue
            rk = str(root.resolve())
            if rk in seen:
                continue
            seen.add(rk)
            for d in sorted(root.iterdir()):
                if d.is_dir():
                    yield d


def parse_project(d: Path):
    """解析一个壁纸工程目录；project.json 缺失/损坏 → None（调用方跳过）。"""
    pj = d / 'project.json'
    if not pj.is_file():
        return None
    try:
        j = json.loads(pj.read_text(encoding='utf-8', errors='replace'))
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    title = str(j.get('title') or d.name)
    typ = str(j.get('type') or '').strip().lower()
    if typ not in ('scene', 'video', 'web', 'image'):
        typ = 'scene'   # lean: 未知类型当 scene 处理，前端只能用预览图

    preview = next((d / n for n in PREVIEW_NAMES if (d / n).is_file()), None)

    media, media_kind = None, ''
    f = j.get('file')
    if typ == 'video' and f:
        p = d / str(f)
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            media, media_kind = p, 'video'
    elif typ == 'image' and f:
        p = d / str(f)
        if p.is_file():
            media, media_kind = p, 'image'
    if media is None and typ == 'web':   # web 类若目录里带了视频文件也当视频播放
        for name in ('video.webm', 'video.mp4'):
            p = d / name
            if p.is_file():
                media, media_kind = p, 'video'
                break

    item = {'id': d.name, 'title': title, 'type': typ, '_preview': preview, '_media': media, '_mediaKind': media_kind}
    for k, jk in (('_width', 'width'), ('_height', 'height')):
        try:
            v = int(j.get(jk))
            if v > 0:
                item[k] = v
        except (TypeError, ValueError):
            pass
    return item


def discover_wallpapers():
    """按 (目录路径, mtime) 签名缓存扫描结果；库/工程增删或改动会自动失效。"""
    dirs = list(_project_dirs())
    try:
        sig = tuple(sorted((str(d), d.stat().st_mtime) for d in dirs))
    except OSError:
        sig = None
    if sig is not None and _scan_cache['sig'] == sig and _scan_cache['items'] is not None:
        return _scan_cache['items']
    items = [it for it in (parse_project(d) for d in dirs) if it]
    _scan_cache['sig'], _scan_cache['items'] = sig, items
    return items


def find_item(wid: str):
    wid = str(wid or '')
    return next((it for it in discover_wallpapers() if it['id'] == wid), None)


def resolve_media(wid: str, kind: str):
    """kind: 'preview' | 'media'。只返回扫描登记过的路径（白名单）。"""
    it = find_item(wid)
    if not it:
        return None
    return it.get('_preview') if kind == 'preview' else it.get('_media')


def public_item(it: dict) -> dict:
    out = {'id': it['id'], 'title': it['title'], 'type': it['type'],
           'preview': f"/api/wallpapers/{it['id']}/preview" if it.get('_preview') else '',
           'media': f"/api/wallpapers/{it['id']}/media" if it.get('_media') else '',
           'mediaKind': it.get('_mediaKind') or ''}
    if it.get('_width'):
        out['width'] = it['_width']
    if it.get('_height'):
        out['height'] = it['_height']
    return out


def desktop_wallpaper_path():
    """当前 Windows 桌面壁纸文件（Wallpaper Engine 会把 scene 壁纸的快照也写到这里）。非 Windows 或没有则 None。"""
    if sys.platform != 'win32':
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Control Panel\Desktop') as k:
            v, _ = winreg.QueryValueEx(k, 'WallPaper')
        p = Path(v)
        return p if v and p.is_file() else None
    except Exception:
        return None


def desktop_info() -> dict:
    p = desktop_wallpaper_path()
    return {'available': bool(p), 'url': '/api/wallpapers/desktop' if p else ''}


def list_wallpapers() -> dict:
    items = discover_wallpapers()
    return {'items': [public_item(it) for it in items], 'desktop': desktop_info(), 'found': bool(items)}


def guess_type(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or 'application/octet-stream'


def serve_file(handler, path: Path):
    """支持 Range 的白名单文件服务：分块流式写，不整读进内存。handler 是 BaseHTTPRequestHandler。"""
    try:
        size = path.stat().st_size
    except OSError:
        handler.send_response(404)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
        return
    start, end, status = 0, size - 1, 200
    rng = handler.headers.get('Range', '')
    if rng.startswith('bytes='):
        try:
            spec = rng.split('=', 1)[1].split(',')[0].strip()
            a, b = spec.split('-', 1)
            if a == '':
                start, end = max(0, size - int(b)), size - 1
            else:
                start = int(a)
                end = int(b) if b else size - 1
            if 0 <= start <= end < size:
                status = 206
            else:
                start, end, status = 0, size - 1, 200
        except (ValueError, IndexError):
            start, end, status = 0, size - 1, 200
    length = end - start + 1
    handler.send_response(status)
    handler.send_header('Content-Type', guess_type(path))
    handler.send_header('Accept-Ranges', 'bytes')
    handler.send_header('Content-Length', str(length))
    handler.send_header('Cache-Control', 'public, max-age=86400')
    if status == 206:
        handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
    handler.end_headers()
    if handler.command == 'HEAD':
        return
    with open(path, 'rb') as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(CHUNK, remaining))
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionAbortedError, OSError):
                return
            remaining -= len(chunk)
