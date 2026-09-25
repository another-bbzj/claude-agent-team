"""
Wallpaper Engine 壁纸发现 + 背景板 v2（尺寸/透明化/壁纸接入）测试。
运行：python -m unittest discover -s tests -v

用 WALLPAPER_ENGINE_DIRS 伪造一个 Steam 库，不碰真实注册表 / Steam 安装。
"""
import importlib.util
import json
import shutil
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PNG_1PX = b'\x89PNG\r\n\x1a\n' + (13).to_bytes(4, 'big') + b'IHDR' + (2).to_bytes(4, 'big') + (2).to_bytes(4, 'big') + bytes(5) + bytes(4)
GIF_1PX = b'GIF89a' + b'\x01\x00\x01\x00' + b'\x00' * 3 + b';'


def make_png(w, h, junk=b'x' * 50):
    """一张合成 PNG：签名 + IHDR(w,h) + 任意数据 + IEND(4)+CRC(4)——够 wallpapers._extract_png 读出尺寸/找到结尾。"""
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>I', 13) + b'IHDR' + struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0) + b'\x00' * 4
    return sig + ihdr + junk + struct.pack('>I', 0) + b'IEND' + b'\x00' * 4


def make_jpeg(w, h):
    """一张合成 JPEG：SOI + SOF0(w,h) + SOS + 熵编码数据（含一个被填充字节 0xFF00 伪装的假标记）+ EOI。"""
    soi = b'\xff\xd8'
    sof0 = b'\xff\xc0' + struct.pack('>H', 11) + bytes([8]) + struct.pack('>H', h) + struct.pack('>H', w) + bytes([1, 1, 0x11, 0])
    sos = b'\xff\xda' + struct.pack('>H', 8) + b'\x01\x00\x00\x00\x3f\x00'
    entropy = b'\x12\x34\xff\x00\x56\x78'   # 0xFF00 是填充字节，扫描时必须跳过，不能当成假的 EOI/标记
    eoi = b'\xff\xd9'
    return soi + sof0 + sos + entropy + eoi


def mp4_box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack('>I', 8 + len(payload)) + btype + payload


def make_tkhd(w, h):
    body = b'\x00' * 4                # version(1)+flags(3)
    body += b'\x00' * 4 * 5           # creation/modification/track_id/reserved/duration
    body += b'\x00' * 8                # reserved
    body += b'\x00' * 2 * 4           # layer/alt_group/volume/reserved
    body += b'\x00' * 36               # matrix
    body += struct.pack('>I', w << 16) + struct.pack('>I', h << 16)
    assert len(body) == 84
    return body


def make_moov(fourcc: bytes, w: int, h: int) -> bytes:
    stsd_payload = b'\x00\x00\x00\x00' + struct.pack('>I', 1) + struct.pack('>I', 16) + fourcc + b'\x00' * 8
    stbl = mp4_box(b'stbl', mp4_box(b'stsd', stsd_payload))
    vmhd = mp4_box(b'vmhd', b'\x00' * 12)
    minf = mp4_box(b'minf', vmhd + stbl)
    mdia = mp4_box(b'mdia', minf)
    trak = mp4_box(b'trak', mp4_box(b'tkhd', make_tkhd(w, h)) + mdia)
    return mp4_box(b'moov', trak)


def make_mp4(fourcc: bytes, w: int, h: int, moov_at_end: bool) -> bytes:
    ftyp = mp4_box(b'ftyp', b'isom' + b'\x00' * 12)
    mdat = mp4_box(b'mdat', b'0' * 4096)
    moov = make_moov(fourcc, w, h)
    return ftyp + mdat + moov if moov_at_end else ftyp + moov + mdat


def load_wallpapers_module():
    spec = importlib.util.spec_from_file_location('wallpapers_test', ROOT / 'team-board' / 'wallpapers.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_fake_steam_library(root: Path):
    """伪造一个 Steam 库：workshop/content/431960/ 下 5 个工程——video / scene / web(+内嵌视频) / 坏 project.json / 无 preview。"""
    content = root / 'steamapps' / 'workshop' / 'content' / '431960'
    content.mkdir(parents=True)

    d = content / '1001'; d.mkdir()
    (d / 'preview.jpg').write_bytes(PNG_1PX)
    (d / 'clip.mp4').write_bytes(b'0' * 4096)   # 4KB 假视频，够测 Range
    (d / 'project.json').write_text(json.dumps({'title': '视频壁纸', 'type': 'video', 'file': 'clip.mp4'}), encoding='utf-8')

    d = content / '1002'; d.mkdir()
    (d / 'preview.jpg').write_bytes(PNG_1PX)
    (d / 'project.json').write_text(json.dumps({'title': '3D 场景', 'type': 'Scene'}), encoding='utf-8')

    d = content / '1003'; d.mkdir()
    (d / 'preview.jpg').write_bytes(PNG_1PX)
    (d / 'video.webm').write_bytes(b'1' * 2048)
    (d / 'project.json').write_text(json.dumps({'title': 'web 壁纸', 'type': 'WEB'}), encoding='utf-8')

    d = content / '1004'; d.mkdir()
    (d / 'preview.jpg').write_bytes(PNG_1PX)
    (d / 'project.json').write_text('{not json', encoding='utf-8')   # 坏 project.json

    d = content / '1005'; d.mkdir()
    (d / 'project.json').write_text(json.dumps({'title': '无预览', 'type': 'scene'}), encoding='utf-8')   # 无 preview

    return root


class DiscoverTest(unittest.TestCase):
    """纯扫描逻辑：不起 HTTP 服务。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='wetest-'))
        make_fake_steam_library(self.tmp)
        import os
        self._old = os.environ.get('WALLPAPER_ENGINE_DIRS')
        self._old_isolate = os.environ.get('WALLPAPER_ENGINE_TEST_ISOLATE')
        os.environ['WALLPAPER_ENGINE_DIRS'] = str(self.tmp)
        os.environ['WALLPAPER_ENGINE_TEST_ISOLATE'] = '1'   # 本机可能真装了 Steam/WE，测试只认伪造库
        self.wp = load_wallpapers_module()
        self.wp._scan_cache = {'sig': None, 'items': None}

    def tearDown(self):
        import os
        if self._old is None:
            os.environ.pop('WALLPAPER_ENGINE_DIRS', None)
        else:
            os.environ['WALLPAPER_ENGINE_DIRS'] = self._old
        if self._old_isolate is None:
            os.environ.pop('WALLPAPER_ENGINE_TEST_ISOLATE', None)
        else:
            os.environ['WALLPAPER_ENGINE_TEST_ISOLATE'] = self._old_isolate
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discovers_valid_projects_and_skips_bad_json(self):
        items = self.wp.discover_wallpapers()
        ids = {it['id'] for it in items}
        self.assertEqual(ids, {'1001', '1002', '1003', '1005'}, '1004 是坏 project.json，应跳过')

    def test_video_item_has_media_and_kind(self):
        it = self.wp.find_item('1001')
        self.assertEqual(it['type'], 'video')
        self.assertIsNotNone(it['_media'])
        self.assertEqual(it['_mediaKind'], 'video')
        self.assertIsNotNone(it['_preview'])

    def test_scene_item_preview_only(self):
        it = self.wp.find_item('1002')
        self.assertEqual(it['type'], 'scene', 'type 值大小写不定，需规范化为小写')
        self.assertIsNone(it['_media'])
        self.assertIsNotNone(it['_preview'])

    def test_web_item_with_embedded_video(self):
        it = self.wp.find_item('1003')
        self.assertEqual(it['type'], 'web')
        self.assertEqual(it['_mediaKind'], 'video', 'web 目录里带 video.webm 时也当视频')

    def test_missing_preview_still_listed(self):
        it = self.wp.find_item('1005')
        self.assertIsNotNone(it)
        self.assertIsNone(it['_preview'])

    def test_public_item_shape(self):
        pub = self.wp.public_item(self.wp.find_item('1001'))
        # '1001' 的 clip.mp4 是 4KB 假数据（不是真 mp4），probe_video 解析不出 moov，codec/width/height 留空
        self.assertEqual(pub, {'id': '1001', 'title': '视频壁纸', 'type': 'video',
                                'preview': '/api/wallpapers/1001/preview', 'media': '/api/wallpapers/1001/media',
                                'mediaKind': 'video', 'codec': '', 'playable': None, 'note': '无法解析视频信息'})

    def test_resolve_media_whitelist_unknown_id(self):
        self.assertIsNone(self.wp.resolve_media('nope', 'media'))
        self.assertIsNone(self.wp.resolve_media('nope', 'preview'))


class HttpTest(unittest.TestCase):
    """起真实 HTTP 服务：/api/wallpapers 列表、Range 取视频、POST /api/backdrop 接壁纸/桌面，v2 字段与旧配置兼容。"""

    @classmethod
    def setUpClass(cls):
        import os
        cls.tmp = Path(tempfile.mkdtemp(prefix='wehttp-'))
        make_fake_steam_library(cls.tmp)
        cls._old_env = os.environ.get('WALLPAPER_ENGINE_DIRS')
        cls._old_isolate = os.environ.get('WALLPAPER_ENGINE_TEST_ISOLATE')
        os.environ['WALLPAPER_ENGINE_DIRS'] = str(cls.tmp)
        os.environ['WALLPAPER_ENGINE_TEST_ISOLATE'] = '1'   # 本机可能真装了 Steam/WE，测试只认伪造库

        cls.home = cls.tmp / 'home'
        (cls.home / '.claude').mkdir(parents=True)
        shutil.copytree(ROOT / 'team-board', cls.home / '.claude' / 'team-board', ignore=shutil.ignore_patterns('bus', 'backdrops'))
        os.environ['HOME'] = str(cls.home)
        os.environ['USERPROFILE'] = str(cls.home)

        spec = importlib.util.spec_from_file_location('team_board_wp_test', cls.home / '.claude' / 'team-board' / 'team_board.py')
        cls.tb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tb)
        cls.tb.BACKDROPS = cls.tmp / 'backdrops'
        cls.tb.Handler.args = cls.tb.argparse.Namespace(session=None, project=None, stale=600)
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), cls.tb.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        import os
        cls.srv.shutdown()
        if cls._old_env is None:
            os.environ.pop('WALLPAPER_ENGINE_DIRS', None)
        else:
            os.environ['WALLPAPER_ENGINE_DIRS'] = cls._old_env
        if cls._old_isolate is None:
            os.environ.pop('WALLPAPER_ENGINE_TEST_ISOLATE', None)
        else:
            os.environ['WALLPAPER_ENGINE_TEST_ISOLATE'] = cls._old_isolate
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, headers=None):
        """返回 (status, http.client.HTTPMessage headers, body bytes)——不能用 `with` 提前返回连接对象，
        退出 with 块时底层 socket 已关闭，之后再 .read() 只会拿到空字节。"""
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def get_json(self, path):
        st, _, body = self.get(path)
        return st, json.loads(body)

    def post(self, path, obj):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                      data=json.dumps(obj).encode('utf-8'), headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_list_wallpapers_around_expected_count(self):
        st, out = self.get_json('/api/wallpapers')
        self.assertEqual(st, 200)
        self.assertTrue(out['found'])
        self.assertEqual(len(out['items']), 4)
        self.assertIn('desktop', out)

    def test_wallpaper_preview_served(self):
        st, _, body = self.get('/api/wallpapers/1002/preview')
        self.assertEqual(st, 200)
        self.assertEqual(body, PNG_1PX)

    def test_wallpaper_unknown_id_404(self):
        st, _, _ = self.get('/api/wallpapers/nope/media')
        self.assertEqual(st, 404)

    def test_range_request_on_video_returns_206(self):
        st, headers, body = self.get('/api/wallpapers/1001/media', headers={'Range': 'bytes=0-1023'})
        self.assertEqual(st, 206)
        self.assertEqual(headers.get('Content-Range'), 'bytes 0-1023/4096')
        self.assertEqual(len(body), 1024)

    def test_full_request_on_video_returns_200(self):
        st, _, body = self.get('/api/wallpapers/1001/media')
        self.assertEqual(st, 200)
        self.assertEqual(len(body), 4096)

    def test_backdrop_video_wallpaper_sets_kind_and_url(self):
        st, out = self.post('/api/backdrop', {'wallpaper': '1001'})
        self.assertEqual(st, 200, out)
        self.assertEqual(out['kind'], 'video')
        self.assertEqual(out['source'], 'wallpaper')
        self.assertTrue(out['enabled'])
        self.assertEqual(out['url'], '/api/wallpapers/1001/media')

    def test_backdrop_scene_wallpaper_copies_preview_as_image(self):
        st, out = self.post('/api/backdrop', {'wallpaper': '1002'})
        self.assertEqual(st, 200, out)
        self.assertEqual(out['kind'], 'image')
        self.assertTrue(out['enabled'])
        self.assertTrue(out['url'].startswith('/backdrops/'))

    def test_backdrop_unknown_wallpaper_400(self):
        st, out = self.post('/api/backdrop', {'wallpaper': 'nope'})
        self.assertEqual(st, 400, out)

    def test_backdrop_no_preview_wallpaper_400(self):
        st, out = self.post('/api/backdrop', {'wallpaper': '1005'})
        self.assertEqual(st, 400, out)

    def test_backdrop_desktop_no_snapshot_available(self):
        # 非 Windows，或没配置：desktop_wallpaper_path 应返回 None → 400
        orig = self.tb._wp().desktop_wallpaper_path
        self.tb._wp().desktop_wallpaper_path = lambda: None
        try:
            st, out = self.post('/api/backdrop', {'desktop': True})
            self.assertEqual(st, 400, out)
        finally:
            self.tb._wp().desktop_wallpaper_path = orig

    def test_backdrop_desktop_snapshot_copied(self):
        fake_desktop = self.tmp / 'desktop.jpg'
        fake_desktop.write_bytes(PNG_1PX)
        orig = self.tb._wp().desktop_wallpaper_path
        self.tb._wp().desktop_wallpaper_path = lambda: fake_desktop
        try:
            st, out = self.post('/api/backdrop', {'desktop': True})
            self.assertEqual(st, 200, out)
            self.assertEqual(out['source'], 'desktop')
            self.assertTrue(out['enabled'])
        finally:
            self.tb._wp().desktop_wallpaper_path = orig

    def test_backdrop_v2_fields_settable_and_clamped(self):
        self.post('/api/backdrop', {'wallpaper': '1002'})
        st, out = self.post('/api/backdrop', {'fit': 'custom', 'scale': 9999, 'x': -5, 'y': 50,
                                                'blur': 100, 'dim': 1, 'saturate': 0, 'panelAlpha': 0,
                                                'mask': 'vignette', 'blend': 'screen', 'area': 'stage'})
        self.assertEqual(st, 200, out)
        self.assertEqual(out['fit'], 'custom')
        self.assertEqual(out['scale'], 400, '越界夹到上限')
        self.assertEqual(out['x'], 0, '越界夹到下限')
        self.assertEqual(out['blur'], 20)
        self.assertEqual(out['dim'], 0.9)
        self.assertEqual(out['panelAlpha'], 0.2)   # v1.4.1：下限由 0.3 放开到 0.2
        self.assertEqual(out['mask'], 'vignette')
        self.assertEqual(out['blend'], 'screen')
        self.assertEqual(out['area'], 'stage')

    def test_backdrop_v2_bad_enum_400(self):
        st, out = self.post('/api/backdrop', {'fit': 'nope'})
        self.assertEqual(st, 400, out)

    def test_backdrop_legacy_config_gets_v2_defaults_and_side_compat(self):
        self.tb.BACKDROPS.mkdir(parents=True, exist_ok=True)
        (self.tb.BACKDROPS / 'backdrop.json').write_text(json.dumps(
            {'file': '', 'credit': '旧版', 'opacity': 0.7, 'side': 'right', 'enabled': False}), encoding='utf-8')
        st, out = self.get_json('/api/backdrop')
        self.assertEqual(st, 200)
        self.assertEqual(out['fit'], 'contain')
        self.assertEqual(out['x'], 100, '旧配置 side=right 兼容映射到 x=100')
        self.assertEqual(out['area'], 'page')
        self.assertEqual(out['panelAlpha'], 1.0)


class SceneExtractTest(unittest.TestCase):
    """extract_scene_image：流式扫描合成 scene.pkg。"""

    def setUp(self):
        self.wp = load_wallpapers_module()
        self.tmp = Path(tempfile.mkdtemp(prefix='pkgtest-'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_picks_larger_jpeg_over_smaller_png(self):
        pkg = self.tmp / 'scene.pkg'
        data = (b'\x00' * 500 + make_png(200, 200) + b'\x11' * 300
                + make_jpeg(800, 450) + b'\x22' * 1000)
        pkg.write_bytes(data)
        result = self.wp.extract_scene_image(pkg)
        self.assertIsNotNone(result)
        img, ext, w, h = result
        self.assertEqual(ext, 'jpg')
        self.assertEqual((w, h), (800, 450))
        self.assertTrue(img.startswith(b'\xff\xd8'))

    def test_no_image_returns_none(self):
        pkg = self.tmp / 'scene.pkg'
        pkg.write_bytes(b'\x00\x01\x02\x03' * 4096)   # 纯噪声，没有 PNG/JPEG 特征
        self.assertIsNone(self.wp.extract_scene_image(pkg))

    def test_below_min_size_ignored(self):
        pkg = self.tmp / 'scene.pkg'
        pkg.write_bytes(make_png(100, 100))   # 小于 640x360 门槛
        self.assertIsNone(self.wp.extract_scene_image(pkg))

    def test_get_scene_full_image_caches(self):
        proj = self.tmp / '9001'
        proj.mkdir()
        (proj / 'project.json').write_text(json.dumps({'title': 't', 'type': 'scene'}), encoding='utf-8')
        (proj / 'scene.pkg').write_bytes(make_jpeg(1000, 700))
        import os
        os.environ['WALLPAPER_ENGINE_DIRS'] = str(self.tmp.parent)  # 不会被扫到，直接用 find_item 的等价物
        self.wp.CACHE_DIR = self.tmp / '.cache'
        it = self.wp.parse_project(proj)
        orig_discover = self.wp.discover_wallpapers
        self.wp.discover_wallpapers = lambda: [it]
        try:
            r1 = self.wp.get_scene_full_image('9001')
            self.assertIsNotNone(r1)
            path1, w, h = r1
            self.assertEqual((w, h), (1000, 700))
            self.assertTrue(path1.is_file())
            mtime1 = path1.stat().st_mtime
            r2 = self.wp.get_scene_full_image('9001')
            self.assertEqual(r2[0], path1)
            self.assertEqual(path1.stat().st_mtime, mtime1, '第二次应直接命中缓存，不重新写文件')
        finally:
            self.wp.discover_wallpapers = orig_discover


class VideoProbeTest(unittest.TestCase):
    """probe_video：mp4 codec / 宽高 / 可播性解析（moov 在头部和尾部都要能找到）。"""

    def setUp(self):
        self.wp = load_wallpapers_module()
        self.tmp = Path(tempfile.mkdtemp(prefix='mp4test-'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_h264_playable(self):
        p = self.tmp / 'a.mp4'
        p.write_bytes(make_mp4(b'avc1', 1920, 1080, moov_at_end=False))
        info = self.wp.probe_video(p)
        self.assertEqual(info['codec'], 'h264')
        self.assertEqual((info['width'], info['height']), (1920, 1080))
        self.assertTrue(info['playable'])

    def test_hevc_not_playable(self):
        p = self.tmp / 'b.mp4'
        p.write_bytes(make_mp4(b'hvc1', 3840, 2160, moov_at_end=True))
        info = self.wp.probe_video(p)
        self.assertEqual(info['codec'], 'hevc')
        self.assertFalse(info['playable'])
        self.assertIn('HEVC', info['note'])

    def test_wide_h264_not_playable(self):
        p = self.tmp / 'c.mp4'
        p.write_bytes(make_mp4(b'avc1', 5000, 2000, moov_at_end=True))
        info = self.wp.probe_video(p)
        self.assertFalse(info['playable'])
        self.assertIn('4096', info['note'])

    def test_moov_at_end_found(self):
        p = self.tmp / 'd.mp4'
        p.write_bytes(make_mp4(b'avc1', 1280, 720, moov_at_end=True))
        info = self.wp.probe_video(p)
        self.assertEqual((info['width'], info['height']), (1280, 720))

    def test_webm_codec(self):
        p = self.tmp / 'e.webm'
        p.write_bytes(b'\x1a\x45\xdf\xa3' + b'\x00' * 100)
        info = self.wp.probe_video(p)
        self.assertEqual(info['codec'], 'webm')
        self.assertTrue(info['playable'])


class GifPreviewHttpTest(unittest.TestCase):
    """GIF 类型的 preview（不少 scene 壁纸的 preview 就是 gif）能被接受并应用为背景。"""

    @classmethod
    def setUpClass(cls):
        import os
        cls.tmp = Path(tempfile.mkdtemp(prefix='wegif-'))
        content = cls.tmp / 'steamapps' / 'workshop' / 'content' / '431960'
        content.mkdir(parents=True)
        d = content / '2001'
        d.mkdir()
        (d / 'preview.gif').write_bytes(GIF_1PX)
        (d / 'project.json').write_text(json.dumps({'title': 'GIF 预览场景', 'type': 'scene'}), encoding='utf-8')

        cls._old_env = os.environ.get('WALLPAPER_ENGINE_DIRS')
        cls._old_isolate = os.environ.get('WALLPAPER_ENGINE_TEST_ISOLATE')
        os.environ['WALLPAPER_ENGINE_DIRS'] = str(cls.tmp)
        os.environ['WALLPAPER_ENGINE_TEST_ISOLATE'] = '1'

        cls.home = cls.tmp / 'home'
        (cls.home / '.claude').mkdir(parents=True)
        shutil.copytree(ROOT / 'team-board', cls.home / '.claude' / 'team-board', ignore=shutil.ignore_patterns('bus', 'backdrops'))
        os.environ['HOME'] = str(cls.home)
        os.environ['USERPROFILE'] = str(cls.home)

        spec = importlib.util.spec_from_file_location('team_board_gif_test', cls.home / '.claude' / 'team-board' / 'team_board.py')
        cls.tb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tb)
        cls.tb.BACKDROPS = cls.tmp / 'backdrops'
        cls.tb.Handler.args = cls.tb.argparse.Namespace(session=None, project=None, stale=600)
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), cls.tb.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        import os
        cls.srv.shutdown()
        if cls._old_env is None:
            os.environ.pop('WALLPAPER_ENGINE_DIRS', None)
        else:
            os.environ['WALLPAPER_ENGINE_DIRS'] = cls._old_env
        if cls._old_isolate is None:
            os.environ.pop('WALLPAPER_ENGINE_TEST_ISOLATE', None)
        else:
            os.environ['WALLPAPER_ENGINE_TEST_ISOLATE'] = cls._old_isolate
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def post(self, path, obj):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                      data=json.dumps(obj).encode('utf-8'), headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_gif_preview_wallpaper_applies(self):
        st, out = self.post('/api/backdrop', {'wallpaper': '2001'})
        self.assertEqual(st, 200, out)
        self.assertTrue(out['enabled'])
        self.assertTrue(out['url'].endswith('.gif?v=' + out['url'].rsplit('v=', 1)[1]) or '.gif' in out['url'])
        self.assertEqual(out.get('quality'), 'preview', '没有 scene.pkg，只能是预览质量')


if __name__ == '__main__':
    unittest.main()
