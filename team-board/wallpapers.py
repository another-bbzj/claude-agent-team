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

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / 'backdrops' / '.cache'   # scene 高清原图提取结果缓存（测试可通过设 wallpapers.CACHE_DIR 改路径）

WORKSHOP_APPID = '431960'
PREVIEW_NAMES = ('preview.jpg', 'preview.png', 'preview.gif')
VIDEO_EXTS = ('.mp4', '.webm')
CHUNK = 256 * 1024

_scan_cache = {'sig': None, 'items': None}
_video_probe_cache = {}   # path(str) -> (sig, result)


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

    pkg = None
    if typ == 'scene':
        p = d / 'scene.pkg'
        if p.is_file():
            pkg = p

    item = {'id': d.name, 'title': title, 'type': typ, '_preview': preview, '_media': media,
            '_mediaKind': media_kind, '_pkg': pkg}
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
    if it['type'] == 'scene':
        # lean: 扫描 pkg 找高清原图较慢，列表里不做；真实值靠 GET /api/wallpapers/<id>/probe 按需查（并缓存）
        out['quality'] = 'unknown'
    if it.get('_mediaKind') == 'video' and it.get('_media'):
        info = probe_video(it['_media'])
        out['codec'] = info['codec']
        if info.get('width'):
            out['width'] = info['width']
        if info.get('height'):
            out['height'] = info['height']
        out['playable'] = info['playable']
        out['note'] = info['note']
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


# ---- scene.pkg 高清原图提取（流式分块扫描，不整读进内存） ----------------------------------

_PNG_SIG = b'\x89PNG\r\n\x1a\n'
_JPEG_SOI = b'\xff\xd8\xff'
MAX_CANDIDATE_BYTES = 24 * 1024 * 1024   # 单张候选图超过这个大小就放弃（大概率误判/损坏）
MIN_W, MIN_H = 640, 360


class _ByteScanner:
    """对一个只读文件做流式、按需前向扫描：只在“正在提取的一张候选图”期间缓冲会变大，
    找下一个候选起点时缓冲会裁到很小（≤ 一个签名长度），不会把整个 pkg 读进内存。"""

    def __init__(self, f, chunk=CHUNK):
        self.f = f
        self.buf = b''
        self.base = 0   # self.buf[0] 对应文件里的绝对偏移
        self.eof = False
        self.chunk = chunk
        self.floor = None   # 不为 None 时，drop_before 最多裁到这个位置——保证正在提取的候选图起点不被冲掉

    def _grow(self):
        c = self.f.read(self.chunk)
        if not c:
            self.eof = True
            return False
        self.buf += c
        return True

    def drop_before(self, pos: int):
        if self.floor is not None:
            pos = min(pos, self.floor)
        cut = pos - self.base
        if cut > 0:
            self.buf = self.buf[cut:]
            self.base = pos

    def slice(self, a: int, b: int) -> bytes:
        while self.base + len(self.buf) < b and not self.eof:
            if not self._grow():
                break
        lo, hi = max(a, self.base) - self.base, min(b, self.base + len(self.buf)) - self.base
        if hi <= lo:
            return b''
        return self.buf[lo:hi]

    def find(self, sig: bytes, start: int) -> int:
        """从绝对偏移 start 开始找 sig；不假定缓冲已经裁到 start（floor 可能挡住了裁剪），
        每次都用 start-self.base 换算成缓冲内下标去搜，语义上永远正确。"""
        while True:
            idx = self.buf.find(sig, max(0, start - self.base))
            if idx != -1:
                return self.base + idx
            if self.eof:
                return -1
            keep = len(sig) - 1
            self.drop_before(max(start, self.base + len(self.buf) - keep))
            if not self._grow():
                return -1

    def find_any(self, sigs, start: int):
        maxlen = max(len(s) for s in sigs)
        while True:
            off = max(0, start - self.base)
            best_idx, best_sig = None, None
            for s in sigs:
                i = self.buf.find(s, off)
                if i != -1 and (best_idx is None or i < best_idx):
                    best_idx, best_sig = i, s
            if best_idx is not None:
                return self.base + best_idx, best_sig
            if self.eof:
                return -1, None
            keep = maxlen - 1
            self.drop_before(max(start, self.base + len(self.buf) - keep))
            if not self._grow():
                return -1, None


def _extract_png(sc: _ByteScanner, start: int, max_span: int):
    """PNG：签名(8) + IHDR chunk 头(长度4+类型4) + width(4) + height(4) …… 结束于 IEND(4)+CRC(4)。"""
    ihdr = sc.slice(start + 16, start + 24)
    if len(ihdr) < 8:
        return None
    w = int.from_bytes(ihdr[0:4], 'big')
    h = int.from_bytes(ihdr[4:8], 'big')
    idx = sc.find(b'IEND', start)
    if idx == -1 or idx - start > max_span:
        return None
    return idx + 8, w, h


_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_NO_LEN_MARKERS = {0x01} | set(range(0xD0, 0xD8))   # TEM、RSTn：无长度字段


def _extract_jpeg(sc: _ByteScanner, start: int, max_span: int):
    """JPEG：按段结构走到真正的 EOI（跳过 EXIF 缩略图等内嵌 JPEG，也跳过熵编码数据里被
    0xFF00 字节填充/重启标记伪装出来的假 0xFFD9），别用简单地查第一个 FFD9。"""
    pos = start + 2
    width = height = None
    while True:
        if pos - start > max_span:
            return None
        m = sc.slice(pos, pos + 2)
        if len(m) < 2 or m[0] != 0xFF:
            return None
        marker = m[1]
        if marker == 0xD9:   # EOI
            if width and height:
                return pos + 2, width, height
            return None
        if marker in _NO_LEN_MARKERS:
            pos += 2
            continue
        if marker == 0xDA:   # SOS：段头之后是熵编码数据，得专门扫描下一个“真”标记
            seg = sc.slice(pos + 2, pos + 4)
            if len(seg) < 2:
                return None
            pos = pos + 2 + int.from_bytes(seg, 'big')
            while True:
                i = sc.find(b'\xff', pos)
                if i == -1 or i - start > max_span:
                    return None
                nb = sc.slice(i + 1, i + 2)
                if len(nb) < 1:
                    return None
                b0 = nb[0]
                if b0 == 0x00 or 0xD0 <= b0 <= 0xD7:   # 填充字节 / 重启标记，继续找
                    pos = i + 2
                    continue
                pos = i
                break
            continue
        seg = sc.slice(pos + 2, pos + 4)
        if len(seg) < 2:
            return None
        seglen = int.from_bytes(seg, 'big')
        if marker in _SOF_MARKERS:
            data = sc.slice(pos + 4, pos + 9)
            if len(data) >= 5:
                height = int.from_bytes(data[1:3], 'big')
                width = int.from_bytes(data[3:5], 'big')
        pos = pos + 2 + seglen


def extract_scene_image(pkg_path: Path):
    """流式分块扫描 scene.pkg，找像素面积最大且 ≥ 640×360 的内嵌 PNG/JPEG。
    返回 (bytes, ext, width, height) 或 None（没有可用原图）。"""
    best = None
    try:
        with open(pkg_path, 'rb') as f:
            sc = _ByteScanner(f)
            pos = 0
            while True:
                sc.floor = None
                start, sig = sc.find_any((_PNG_SIG, _JPEG_SOI), pos)
                if start == -1:
                    break
                sc.floor = start   # 提取过程中不准把候选图起点冲掉（find() 内部会主动裁剪缓冲）
                if sig == _PNG_SIG:
                    res = _extract_png(sc, start, MAX_CANDIDATE_BYTES)
                    ext = 'png'
                else:
                    res = _extract_jpeg(sc, start, MAX_CANDIDATE_BYTES)
                    ext = 'jpg'
                if res:
                    end, w, h = res
                    if w >= MIN_W and h >= MIN_H and (best is None or w * h > best[0]):
                        best = (w * h, bytes(sc.slice(start, end)), ext, w, h)
                    pos = end
                else:
                    pos = start + 1
                sc.floor = None
                sc.drop_before(pos)
    except OSError:
        return None
    if best is None:
        return None
    _, data, ext, w, h = best
    return data, ext, w, h


def get_scene_full_image(wid: str):
    """带缓存的 extract_scene_image：按 (id, pkg mtime, size) 判断要不要重新扫描，
    结果（含“扫描过但没找到”）落盘到 backdrops/.cache/scene-<id>.*，重复点不重复扫。
    返回 (path, width, height) 或 None。"""
    it = find_item(wid)
    if not it or it['type'] != 'scene' or not it.get('_pkg'):
        return None
    pkg = it['_pkg']
    try:
        st = pkg.stat()
        sig = [str(pkg), int(st.st_mtime), st.st_size]
    except OSError:
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = CACHE_DIR / f'scene-{wid}.json'
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
    except Exception:
        meta = None
    if isinstance(meta, dict) and meta.get('sig') == sig:
        if not meta.get('ext'):
            return None   # 之前扫描过：确认没有可用原图
        img_path = CACHE_DIR / f"scene-{wid}.{meta['ext']}"
        if img_path.is_file():
            return img_path, meta.get('width'), meta.get('height')

    result = extract_scene_image(pkg)
    if not result:
        try:
            meta_path.write_text(json.dumps({'sig': sig, 'ext': None}), encoding='utf-8')
        except OSError:
            pass
        return None
    data, ext, w, h = result
    img_path = CACHE_DIR / f'scene-{wid}.{ext}'
    tmp = img_path.with_suffix(img_path.suffix + '.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, img_path)
    for other in ('png', 'jpg'):   # 换编码格式后清掉旧扩展名的残留缓存
        if other != ext:
            stale = CACHE_DIR / f'scene-{wid}.{other}'
            if stale.is_file():
                try:
                    stale.unlink()
                except OSError:
                    pass
    try:
        meta_path.write_text(json.dumps({'sig': sig, 'ext': ext, 'width': w, 'height': h}), encoding='utf-8')
    except OSError:
        pass
    return img_path, w, h


# ---- 视频编码 / 分辨率 / 可播性（只读文件头部/尾部附近的 box，不读整个视频） ----------------

_MP4_CODECS = {b'avc1': 'h264', b'avc3': 'h264', b'hev1': 'hevc', b'hvc1': 'hevc',
               b'vp09': 'vp9', b'av01': 'av1', b'mp4v': 'mpeg4'}


def _iter_boxes(data: bytes):
    """解析同一层级里的 box 列表：[(type: bytes, payload: bytes), ...]。"""
    out = []
    i, n = 0, len(data)
    while i + 8 <= n:
        size = int.from_bytes(data[i:i + 4], 'big')
        btype = data[i + 4:i + 8]
        hdrlen = 8
        if size == 1:
            if i + 16 > n:
                break
            size = int.from_bytes(data[i + 8:i + 16], 'big')
            hdrlen = 16
        if size == 0:
            size = n - i
        if size < hdrlen or i + size > n:
            break
        out.append((btype, data[i + hdrlen:i + size]))
        i += size
    return out


def _find_box(boxes, btype):
    for t, p in boxes:
        if t == btype:
            return p
    return None


def _find_moov_bytes(path: Path, filesize: int):
    """先在头 64KB 里按 box 顺序走；moov 不在头部（常见于流式优化过的 mp4：moov 挪到文件尾）就在尾部按字面量找。"""
    MAX_MOOV = 64 * 1024 * 1024
    with open(path, 'rb') as f:
        head = f.read(65536)
        pos = 0
        while pos + 8 <= len(head):
            size = int.from_bytes(head[pos:pos + 4], 'big')
            btype = head[pos + 4:pos + 8]
            hdrlen = 8
            real_size = size
            if size == 1:
                if pos + 16 > len(head):
                    break
                real_size = int.from_bytes(head[pos + 8:pos + 16], 'big')
                hdrlen = 16
            if size == 0:
                real_size = filesize - pos
            if real_size < hdrlen or real_size <= 0:
                break
            if btype == b'moov':
                if real_size - hdrlen > MAX_MOOV:
                    return None
                f.seek(pos + hdrlen)
                return f.read(real_size - hdrlen)
            pos += real_size
            if pos > len(head) - 8:
                break

        tail_len = min(filesize, 8 * 1024 * 1024)
        f.seek(filesize - tail_len)
        tail = f.read(tail_len)
        idx = tail.find(b'moov')
        if idx < 4:
            return None
        size_pos = idx - 4
        size = int.from_bytes(tail[size_pos:size_pos + 4], 'big')
        hdrlen = 8
        if size == 1:
            ext = tail[idx + 4:idx + 12]
            if len(ext) < 8:
                return None
            size = int.from_bytes(ext, 'big')
            hdrlen = 16
        payload_len = size - hdrlen
        if payload_len <= 0 or payload_len > MAX_MOOV:
            return None
        payload_start = size_pos + hdrlen
        if payload_start + payload_len <= len(tail):
            return tail[payload_start:payload_start + payload_len]
        f.seek(filesize - tail_len + payload_start)
        return f.read(payload_len)


def _parse_moov(moov: bytes) -> dict:
    """在 moov 里找第一条视频轨（minf 下有 vmhd）：stsd 首个 sample entry 的四字符码 = 编码，
    tkhd 里的 width/height（16.16 定点，取整数部分）。"""
    for ttype, tpayload in _iter_boxes(moov):
        if ttype != b'trak':
            continue
        tboxes = _iter_boxes(tpayload)
        tkhd = _find_box(tboxes, b'tkhd')
        mdia = _find_box(tboxes, b'mdia')
        if not mdia:
            continue
        minf = _find_box(_iter_boxes(mdia), b'minf')
        if not minf:
            continue
        iboxes = _iter_boxes(minf)
        if _find_box(iboxes, b'vmhd') is None:   # 不是视频轨（音频轨是 smhd）
            continue
        stbl = _find_box(iboxes, b'stbl')
        if not stbl:
            continue
        stsd = _find_box(_iter_boxes(stbl), b'stsd')
        codec = ''
        if stsd and len(stsd) >= 16:
            fourcc = stsd[12:16]
            codec = _MP4_CODECS.get(fourcc, fourcc.decode('latin1', 'replace').strip('\x00').lower())
        w = h = None
        if tkhd and len(tkhd) >= 4:
            version = tkhd[0]
            w_off, h_off = (88, 92) if version == 1 else (76, 80)
            if len(tkhd) >= h_off + 4:
                w = int.from_bytes(tkhd[w_off:w_off + 4], 'big') >> 16
                h = int.from_bytes(tkhd[h_off:h_off + 4], 'big') >> 16
        return {'codec': codec, 'width': w or None, 'height': h or None}
    return {'codec': '', 'width': None, 'height': None}


def _judge_playable(codec: str, w, h):
    note = ''
    playable = True
    if codec == 'hevc':
        playable = False
        note = 'HEVC 编码，浏览器通常无法播放'
    elif codec not in ('h264', 'vp9', 'av1', 'webm', ''):
        playable = None
    if w and w > 4096:
        playable = False
        note = (note + '；' if note else '') + f'宽度 {w} 超过常见解码上限 4096'
    return playable, note


def probe_video(path: Path) -> dict:
    """编码 / 分辨率 / 是否可能播放（纯标准库；只读文件头/尾附近的 box，不读整个视频）。按 (mtime,size) 缓存。"""
    default = {'codec': '', 'width': None, 'height': None, 'playable': None, 'note': ''}
    try:
        st = path.stat()
        sig = (st.st_mtime, st.st_size)
    except OSError:
        return default
    cached = _video_probe_cache.get(str(path))
    if cached and cached[0] == sig:
        return cached[1]

    if path.suffix.lower() == '.webm':
        result = {'codec': 'webm', 'width': None, 'height': None, 'playable': True, 'note': ''}
    else:
        try:
            moov = _find_moov_bytes(path, st.st_size)
        except OSError:
            moov = None
        if moov is None:
            result = {**default, 'note': '无法解析视频信息'}
        else:
            info = _parse_moov(moov)
            playable, note = _judge_playable(info['codec'], info['width'], info['height'])
            result = {**info, 'playable': playable, 'note': note}
    _video_probe_cache[str(path)] = (sig, result)
    return result


def probe_item(wid: str):
    """GET /api/wallpapers/<id>/probe：scene 触发（可能较慢的）原图提取并缓存；video 复用 probe_video。"""
    it = find_item(wid)
    if not it:
        return None
    if it['type'] == 'scene':
        full = get_scene_full_image(wid)
        if full:
            _, w, h = full
            return {'quality': 'full', 'width': w, 'height': h}
        return {'quality': 'preview', 'width': None, 'height': None}
    if it.get('_mediaKind') == 'video' and it.get('_media'):
        info = probe_video(it['_media'])
        return {'quality': 'preview' if info.get('playable') is False else 'full',
                'width': info.get('width'), 'height': info.get('height')}
    return {'quality': 'preview', 'width': it.get('_width'), 'height': it.get('_height')}
