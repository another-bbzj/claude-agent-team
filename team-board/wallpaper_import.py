"""Import local wallpaper media without executing projects or changing settings.

Image validation/storage is delegated to backdrop_store in an isolated staging
directory: its normal cleanup/config writes must not affect the active backdrop.
The caller applies the returned config patch only after this function succeeds.
"""
import base64
import binascii
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import tempfile


HERE = Path(__file__).resolve().parent
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_PROJECT_BYTES = 1024 * 1024
_MIME = {
    'image/png': 'png', 'image/webp': 'webp', 'image/jpeg': 'jpg',
    'image/gif': 'gif', 'video/mp4': 'mp4', 'video/webm': 'webm',
}
_MP4_BRANDS = {b'isom', b'iso2', b'iso3', b'iso4', b'iso5', b'iso6',
               b'mp41', b'mp42', b'avc1', b'dash', b'M4V ', b'qt  '}


def _backdrop_store():
    spec = importlib.util.spec_from_file_location('backdrop_store_for_wallpaper_import',
                                                 HERE / 'backdrop_store.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _plain_path(path, base=None):
    """Reject symlinks and Windows reparse points, including intermediate dirs."""
    path = Path(os.path.abspath(str(path)))
    boundary = Path(os.path.abspath(str(base))) if base is not None else Path(path.anchor)
    try:
        path.relative_to(boundary)
    except ValueError:
        raise ValueError('壁纸引用必须位于所选目录内') from None
    current = path
    while True:
        if _is_link(current):
            raise ValueError('壁纸路径不能包含符号链接或目录联接')
        if current == boundary:
            break
        current = current.parent
    return path


def _read_file(path, limit, base=None):
    path = _plain_path(path, base)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('壁纸媒体必须是普通文件')
    flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0))
    descriptor = os.open(str(path), flags)
    with os.fdopen(descriptor, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('壁纸媒体必须是普通文件')
        if before.st_size > limit:
            raise ValueError('壁纸文件超过 {} MB'.format(limit // 1024 // 1024))
        # A changed path must not silently replace the object we validated.
        current = _plain_path(path, base).stat()
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError('读取时壁纸文件已变更，请重试')
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError('壁纸文件超过 {} MB'.format(limit // 1024 // 1024))
    return data


def _project_reference(directory, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('壁纸工程中的文件引用无效')
    value = value.replace('\\', '/')
    windows = PureWindowsPath(value)
    parts = value.split('/')
    if windows.drive or windows.root or value.startswith('/') or '..' in parts or ':' in value:
        raise ValueError('壁纸工程不能引用目录外文件或远程 URL')
    path = directory.joinpath(*parts)
    safe = _plain_path(path, directory)
    try:
        safe.resolve().relative_to(directory.resolve())
    except ValueError:
        raise ValueError('壁纸引用必须位于所选目录内') from None
    if not safe.is_file():
        raise ValueError('壁纸工程引用的媒体文件不存在')
    return safe


def _project_media(directory):
    metadata = _read_file(directory / 'project.json', MAX_PROJECT_BYTES, directory)
    try:
        project = json.loads(metadata.decode('utf-8-sig'))
    except (UnicodeError, ValueError):
        raise ValueError('project.json 不是合法的 UTF-8 JSON') from None
    if not isinstance(project, dict):
        raise ValueError('project.json 必须是对象')
    kind = str(project.get('type') or '').strip().lower()
    if kind not in ('image', 'video', 'scene', 'web'):
        raise ValueError('不支持该壁纸工程类型')
    media = _project_reference(directory, project['file']) if project.get('file') else None
    if kind in ('image', 'video'):
        if media is None:
            raise ValueError('壁纸工程缺少媒体 file 字段')
        return media, str(project.get('title') or directory.name), '', kind
    # Scene/web files are never executed. Only an explicitly local preview is used.
    if project.get('preview'):
        preview = _project_reference(directory, project['preview'])
    else:
        preview = next((directory / name for name in ('preview.jpg', 'preview.png', 'preview.gif', 'preview.webp')
                        if (directory / name).exists()), None)
        if preview is None:
            raise ValueError('场景或网页壁纸没有可用预览图，不会执行工程')
        preview = _project_reference(directory, preview.name)
    return preview, str(project.get('title') or directory.name), '场景或网页工程仅导入预览图，不会执行工程。', 'image'


def _video_type(data):
    if len(data) >= 8 and data.startswith(b'\x1aE\xdf\xa3'):
        return 'webm'
    if len(data) < 16 or data[4:8] != b'ftyp':
        return None
    size = int.from_bytes(data[:4], 'big')
    offset = 8
    if size == 1:
        if len(data) < 24:
            return None
        size, offset = int.from_bytes(data[8:16], 'big'), 16
    if size < offset + 8 or size > len(data) or (size - offset - 8) % 4:
        return None
    brands = [data[offset:offset + 4]]
    brands.extend(data[i:i + 4] for i in range(offset + 8, size, 4))
    return 'mp4' if any(brand in _MP4_BRANDS for brand in brands) else None


def _decode_upload(value):
    data = value.get('data')
    if not isinstance(data, str) or not data:
        raise ValueError('壁纸数据不是合法的 base64')
    mime = ''
    if data.startswith('data:'):
        match = re.match(r'^data:([^;,]+);base64,', data, flags=re.IGNORECASE)
        if match is None:
            raise ValueError('只支持 base64 图片或视频 data URI')
        mime = match.group(1).lower()
        if mime not in _MIME:
            raise ValueError('不支持该壁纸 MIME 类型')
        data = data[match.end():]
    if len(data) > 4 * ((MAX_VIDEO_BYTES + 2) // 3):
        raise ValueError('壁纸视频超过 50 MB')
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('壁纸数据不是合法的 base64') from None
    return decoded, mime


def _publish(data, filename, root):
    """Publish complete bytes without overwriting even a preexisting hash collision."""
    target = root / filename
    if target.exists() or target.is_symlink():
        if _read_file(target, MAX_VIDEO_BYTES, root) != data:
            raise ValueError('同名壁纸文件已存在且内容不同，未覆盖')
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(root), prefix='.wallpaper-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Windows rename and POSIX link both refuse to overwrite a destination.
            # Using rename on Windows also works where hardlinks are unavailable.
            if os.name == 'nt':
                os.rename(str(temporary), str(target))
            else:
                os.link(str(temporary), str(target))
        except FileExistsError:
            if _read_file(target, MAX_VIDEO_BYTES, root) != data:
                raise ValueError('同名壁纸文件已存在且内容不同，未覆盖') from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def import_wallpaper(value, root):
    """Return {'config': patch, 'note': text}; never write the active config."""
    if not isinstance(value, dict):
        raise ValueError('壁纸导入参数必须是对象')
    if ('data' in value) == ('path' in value):
        raise ValueError('请只提供 data 或本地 path')
    source, note, expected, mime = 'upload', '', '', ''
    if 'data' in value:
        data, mime = _decode_upload(value)
        title = str(value.get('title') or value.get('name') or '本机壁纸')
    else:
        raw_path = value.get('path')
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError('请提供本地壁纸文件或工程目录')
        raw_path = raw_path.strip().strip('"\'')
        if re.match(r'^[a-z][a-z0-9+.-]*://', raw_path, re.IGNORECASE) or raw_path.startswith(('//', '\\\\')):
            raise ValueError('不支持远程壁纸 URL 或网络路径')
        path = _plain_path(Path(os.path.expanduser(raw_path)))
        title = path.name
        if path.is_dir():
            directory = path
            path, title, note, expected = _project_media(directory)
            data = _read_file(path, MAX_VIDEO_BYTES, directory)
        else:
            data = _read_file(path, MAX_VIDEO_BYTES)
        source = 'path'
        title = str(value.get('title') or title)
    bs = _backdrop_store()
    image_ext = bs.sniff(data)
    video_ext = _video_type(data) if image_ext is None else None
    if image_ext:
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError('壁纸图片超过 15 MB')
        kind, ext = 'image', image_ext
    elif video_ext:
        if len(data) > MAX_VIDEO_BYTES:
            raise ValueError('壁纸视频超过 50 MB')
        kind, ext = 'video', video_ext
    else:
        raise ValueError('只支持 png/webp/jpg/gif 图片或有效文件头的 mp4/webm 视频')
    if expected and expected != kind:
        raise ValueError('壁纸工程声明类型与媒体内容不一致')
    if mime and _MIME[mime] != ext:
        raise ValueError('壁纸 MIME 类型与文件内容不一致')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    root = _plain_path(root)
    title = title[:200]
    stem = 'imported-' + hashlib.sha256(data).hexdigest()[:24]
    if kind == 'image':
        with tempfile.TemporaryDirectory(dir=str(root), prefix='.wallpaper-image-') as staging:
            staged = Path(staging)
            saved = bs.save_image(data, stem, staged, credit=title)
            filename = saved['file']
            _publish((staged / filename).read_bytes(), filename, root)
    else:
        filename = stem + '.' + ext
        _publish(data, filename, root)
        note = ' '.join(part for part in (note, '视频仅复制到本机，不转码；能否播放取决于浏览器解码支持。') if part)
    return {'config': {'file': filename, 'enabled': True, 'credit': title, 'kind': kind,
                       'source': source, 'title': title, 'wallpaperId': ''}, 'note': note}
