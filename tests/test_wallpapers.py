"""
Wallpaper Engine 壁纸发现 + 背景板 v2（尺寸/透明化/壁纸接入）测试。
运行：python -m unittest discover -s tests -v

用 WALLPAPER_ENGINE_DIRS 伪造一个 Steam 库，不碰真实注册表 / Steam 安装。
"""
import importlib.util
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PNG_1PX = b'\x89PNG\r\n\x1a\n' + (13).to_bytes(4, 'big') + b'IHDR' + (2).to_bytes(4, 'big') + (2).to_bytes(4, 'big') + bytes(5) + bytes(4)


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
        self.assertEqual(pub, {'id': '1001', 'title': '视频壁纸', 'type': 'video',
                                'preview': '/api/wallpapers/1001/preview', 'media': '/api/wallpapers/1001/media',
                                'mediaKind': 'video'})

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
        self.assertEqual(out['panelAlpha'], 0.3)
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


if __name__ == '__main__':
    unittest.main()
