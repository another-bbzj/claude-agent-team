"""Synthetic magic fixtures exercise local media validation, not browser decoding."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PNG = b'\x89PNG\r\n\x1a\n' + b'synthetic-image-header-only'
MP4 = (24).to_bytes(4, 'big') + b'ftyp' + b'isom' + bytes(4) + b'isommp42'
WEBM = b'\x1aE\xdf\xa3' + b'\x84webm'  # magic-only fixture, not playable media


def load_importer():
    spec = importlib.util.spec_from_file_location('wallpaper_import_test', ROOT / 'team-board' / 'wallpaper_import.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WallpaperImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wallpaper-import-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'backdrops'
        self.root.mkdir()
        self.old_image = self.root / 'custom.png'
        self.old_image.write_bytes(PNG + b'previous')
        self.config = self.root / 'backdrop.json'
        self.config.write_text('{"file":"custom.png","opacity":0.6,"enabled":true}', encoding='utf-8')
        self.before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        self.importer = load_importer()

    def encoded(self, data):
        return base64.b64encode(data).decode('ascii')

    def upload(self, data, **extra):
        return self.importer.import_wallpaper(dict(data=self.encoded(data), **extra), self.root)

    def assert_previous_intact(self):
        for name, content in self.before.items():
            self.assertEqual((self.root / name).read_bytes(), content)

    def assert_no_new_files(self):
        self.assertEqual({path.name for path in self.root.iterdir()}, set(self.before))
        self.assert_previous_intact()

    def project(self, **metadata):
        directory = self.base / 'project'
        directory.mkdir(exist_ok=True)
        (directory / 'project.json').write_text(json.dumps(metadata), encoding='utf-8')
        return directory

    def test_image_upload_uses_generated_name_and_does_not_change_configuration(self):
        result = self.upload(PNG, name='../../outside.exe')
        cfg = result['config']
        self.assertEqual(cfg['kind'], 'image')
        self.assertRegex(cfg['file'], r'^imported-[0-9a-f]{24}\.png$')
        self.assertEqual((self.root / cfg['file']).read_bytes(), PNG)
        self.assertEqual(cfg['source'], 'upload')
        self.assertEqual(cfg['wallpaperId'], '')
        self.assert_previous_intact()

    def test_video_uploads_are_copied_without_transcoding(self):
        for data, extension in ((MP4, 'mp4'), (WEBM, 'webm')):
            with self.subTest(extension=extension):
                result = self.upload(data, name='movie.' + extension)
                self.assertEqual(result['config']['kind'], 'video')
                self.assertEqual(result['config']['credit'], 'movie.' + extension)
                self.assertTrue(result['config']['enabled'])
                self.assertTrue(result['config']['file'].endswith('.' + extension))
                self.assertEqual((self.root / result['config']['file']).read_bytes(), data)
                self.assertIn('浏览器', result['note'])
        self.assert_previous_intact()

    def test_valid_data_uri_and_mime_mismatch(self):
        value = 'data:video/mp4;base64,' + self.encoded(MP4)
        self.assertEqual(self.importer.import_wallpaper({'data': value}, self.root)['config']['kind'], 'video')
        with self.assertRaisesRegex(ValueError, 'MIME'):
            self.importer.import_wallpaper({'data': 'data:image/png;base64,' + self.encoded(MP4)}, self.root)
        self.assert_previous_intact()

    def test_invalid_and_unsupported_uploads_leave_state_untouched(self):
        for data in ('not valid!%%', 'AAAA\n', '', '你好', 'data:image/png,hello',
                     'data:text/html;base64,PHNjcmlwdD4=', self.encoded(b'<html>not media</html>')):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.importer.import_wallpaper({'data': data}, self.root)
            self.assert_no_new_files()

    def test_truncated_and_nonvideo_ftyp_are_rejected(self):
        invalid = (b'\x1aE\xdf\xa3', b'0000ftypisom', (100).to_bytes(4, 'big') + MP4[4:],
                   (16).to_bytes(4, 'big') + b'ftypavif' + bytes(4),
                   (15).to_bytes(4, 'big') + b'ftypisom' + bytes(4))
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.upload(data)
        self.assert_no_new_files()

    def test_size_limits_before_publication(self):
        with mock.patch.object(self.importer, 'MAX_IMAGE_BYTES', len(PNG) - 1):
            with self.assertRaisesRegex(ValueError, '15 MB'):
                self.upload(PNG)
        with mock.patch.object(self.importer, 'MAX_VIDEO_BYTES', len(MP4) - 1):
            with self.assertRaisesRegex(ValueError, '50 MB'):
                self.upload(MP4)
        self.assert_no_new_files()

    def test_real_oversize_file_is_rejected_before_read(self):
        path = self.base / 'large.mp4'
        with path.open('wb') as handle:
            handle.write(MP4)
            handle.truncate(50 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, '50 MB'):
            self.importer.import_wallpaper({'path': str(path)}, self.root)
        self.assert_no_new_files()

    def test_path_image_and_video(self):
        for filename, data, kind in (('picture.png', PNG, 'image'), ('movie.mp4', MP4, 'video')):
            path = self.base / filename
            path.write_bytes(data)
            result = self.importer.import_wallpaper({'path': str(path)}, self.root)
            self.assertEqual(result['config']['source'], 'path')
            self.assertEqual(result['config']['title'], filename)
            self.assertEqual(result['config']['kind'], kind)
        self.assert_previous_intact()

    def test_image_and_video_project(self):
        for kind, name, data in (('image', 'still.png', PNG), ('video', 'movie.webm', WEBM)):
            directory = self.project(type=kind, title='Synthetic project', file=name)
            (directory / name).write_bytes(data)
            result = self.importer.import_wallpaper({'path': str(directory)}, self.root)
            self.assertEqual(result['config']['title'], 'Synthetic project')
            self.assertEqual(result['config']['kind'], kind)
        self.assert_previous_intact()

    def test_scene_and_web_only_use_preview(self):
        for kind in ('scene', 'web'):
            directory = self.project(type=kind, file='index.html', preview='poster.png')
            (directory / 'index.html').write_text('<script>throw Error("never execute")</script>', encoding='utf-8')
            (directory / 'poster.png').write_bytes(PNG)
            result = self.importer.import_wallpaper({'path': str(directory)}, self.root)
            self.assertEqual(result['config']['kind'], 'image')
            self.assertEqual((self.root / result['config']['file']).read_bytes(), PNG)
            self.assertIn('预览', result['note'])
            self.assertIn('不会执行', result['note'])
        self.assert_previous_intact()

    def test_default_preview_names(self):
        directory = self.project(type='scene')
        (directory / 'preview.gif').write_bytes(b'GIF89a' + b'synthetic-only')
        result = self.importer.import_wallpaper({'path': str(directory)}, self.root)
        self.assertTrue(result['config']['file'].endswith('.gif'))

    def test_missing_preview_and_unknown_project_fail(self):
        for metadata in ({'type': 'web'}, {'type': 'unknown'}, {'type': 'image'}, {'type': 'video'}):
            directory = self.project(**metadata)
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.importer.import_wallpaper({'path': str(directory)}, self.root)
        self.assert_no_new_files()

    def test_project_type_must_match_media(self):
        directory = self.project(type='video', file='movie.mp4')
        (directory / 'movie.mp4').write_bytes(PNG)
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.importer.import_wallpaper({'path': str(directory)}, self.root)
        self.assert_no_new_files()

    def test_project_traversal_remote_and_absolute_paths_rejected(self):
        (self.base / 'outside.png').write_bytes(PNG)
        for reference in ('../outside.png', '..\\outside.png', 'https://example.invalid/image.png',
                          str(self.base / 'outside.png'), 'C:\\outside.png', '//host/share/image.png'):
            directory = self.project(type='image', file=reference)
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                self.importer.import_wallpaper({'path': str(directory)}, self.root)
        directory = self.project(type='scene', preview='../outside.png')
        with self.assertRaises(ValueError):
            self.importer.import_wallpaper({'path': str(directory)}, self.root)
        self.assert_no_new_files()

    def test_project_bad_json_and_nonobject_rejected(self):
        directory = self.project(type='image')
        for data in (b'{broken', b'[]', b'\xff'):
            (directory / 'project.json').write_bytes(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.importer.import_wallpaper({'path': str(directory)}, self.root)
        self.assert_no_new_files()

    def test_reference_to_directory_is_not_media(self):
        directory = self.project(type='image', file='not-a-file')
        (directory / 'not-a-file').mkdir()
        with self.assertRaises(ValueError):
            self.importer.import_wallpaper({'path': str(directory)}, self.root)
        with self.assertRaisesRegex(ValueError, '普通文件'):
            self.importer._read_file(directory, 100)
        self.assert_no_new_files()

    def test_remote_input_and_ambiguous_request_rejected(self):
        for value in ({'path': 'https://example.invalid/movie.mp4'}, {'path': '//host/share/movie.mp4'},
                      {'path': ''}, {'path': 7}, {'data': self.encoded(PNG), 'path': 'other'}, {}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.importer.import_wallpaper(value, self.root)
        self.assert_no_new_files()

    def test_reimport_same_bytes_is_idempotent(self):
        first = self.upload(MP4)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        second = self.upload(MP4)
        self.assertEqual(first['config']['file'], second['config']['file'])
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_preexisting_hash_destination_is_never_overwritten(self):
        name = 'imported-' + hashlib.sha256(MP4).hexdigest()[:24] + '.mp4'
        (self.root / name).write_bytes(b'previous unrelated contents')
        with self.assertRaisesRegex(ValueError, '未覆盖'):
            self.upload(MP4)
        self.assertEqual((self.root / name).read_bytes(), b'previous unrelated contents')
        self.assert_previous_intact()

    def test_failed_atomic_publication_cleans_temporary_files(self):
        for data in (PNG, MP4):
            operation = 'rename' if self.importer.os.name == 'nt' else 'link'
            with self.subTest(data=data), mock.patch.object(self.importer.os, operation, side_effect=OSError('synthetic failure')):
                with self.assertRaises(OSError):
                    self.upload(data)
            self.assert_no_new_files()

    def test_symbolic_link_media_and_metadata_rejected(self):
        target = self.base / 'outside.png'
        target.write_bytes(PNG)
        directory = self.project(type='image', file='linked.png')
        try:
            (directory / 'linked.png').symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation unavailable in this environment')
        with self.assertRaises(ValueError):
            self.importer.import_wallpaper({'path': str(directory)}, self.root)
        with self.assertRaises(ValueError):
            self.importer.import_wallpaper({'path': str(directory / 'linked.png')}, self.root)
        self.assert_no_new_files()

    def test_windows_junction_attribute_is_rejected(self):
        path = self.base / 'junction'
        info = mock.Mock(st_mode=0o40755, st_file_attributes=0x400)
        with mock.patch.object(Path, 'lstat', return_value=info):
            with self.assertRaisesRegex(ValueError, '目录联接'):
                self.importer._plain_path(path)


if __name__ == '__main__':
    unittest.main()
