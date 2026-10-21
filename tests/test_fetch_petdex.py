"""fetch_petdex.py：清单缓存、slug 选择和安装失败。不访问 petdex.dev。"""
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fetch_petdex_under_test', ROOT / 'team-board' / 'fetch_petdex.py')
fp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fp)


def pet(slug, name=None, **extra):
    item = {
        'slug': slug,
        'displayName': slug if name is None else name,
        'zipUrl': 'https://example.invalid/' + slug + '.zip',
        'submittedBy': 'ada',
        'spriteVersionNumber': 2,
        'kind': 'object',
    }
    item.update(extra)
    return item


class FakeBody:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class PickTest(unittest.TestCase):
    def test_exact_slug_beats_prefix_and_ignores_case(self):
        pets = [pet('boba-mini', 'Mini'), pet('boba', 'Boba Tea')]
        self.assertEqual(fp.pick(pets, ['boba'])[0]['slug'], 'boba')
        self.assertEqual(fp.pick(pets, ['BOBA'])[0]['slug'], 'boba')
        self.assertEqual(fp.pick(pets, ['boba-'])[0]['slug'], 'boba-mini')

    def test_display_name_and_dedup(self):
        pets = [pet('x', 'Milk Frog'), pet('boba', 'Boba Tea')]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            found = fp.pick(pets, ['frog', 'missing', 'boba'], search='boba')
        self.assertEqual([p['slug'] for p in found], ['x', 'boba'])
        self.assertIn('找不到', buf.getvalue())
        self.assertIn('missing', buf.getvalue())

    def test_search_matches_slug_or_display_name(self):
        pets = [pet('boba', 'Boba Tea'), pet('cat', 'Cat')]
        self.assertEqual([p['slug'] for p in fp.pick(pets, search='TEA')], ['boba'])
        self.assertEqual([p['slug'] for p in fp.pick(pets, search='cat')], ['cat'])


class FetchAndManifestTest(unittest.TestCase):
    def test_import_survives_a_console_that_cannot_reconfigure(self):
        class Broken:
            encoding = 'utf-8'

            def reconfigure(self, **kwargs):
                raise OSError('console')

        spec_broken = importlib.util.spec_from_file_location(
            'fetch_petdex_broken_console', ROOT / 'team-board' / 'fetch_petdex.py')
        module = importlib.util.module_from_spec(spec_broken)
        with mock.patch('sys.stdout', Broken()), mock.patch('sys.stderr', Broken()):
            spec_broken.loader.exec_module(module)
        self.assertTrue(module.STARTER)

    def test_cli_without_arguments_prints_help(self):
        result = subprocess.run([sys.executable, str(ROOT / 'team-board' / 'fetch_petdex.py')],
                                capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--starter', result.stdout)

    def test_fetch_sends_user_agent(self):
        with mock.patch.object(fp.urllib.request, 'urlopen', return_value=FakeBody(b'body')) as urlopen:
            self.assertEqual(fp.fetch('https://example.invalid/manifest', timeout=7), b'body')
        request = urlopen.call_args.args[0]
        self.assertIn('claude-agent-team', request.get_header('User-agent'))
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 7)

    def test_network_failure_uses_cache_then_exits_without_one(self):
        with tempfile.TemporaryDirectory(prefix='petdex-') as tmp:
            root = Path(tmp)
            payload = json.dumps({'pets': [pet('boba')]}).encode()
            with mock.patch.object(fp, 'HERE', root), mock.patch.object(fp, 'fetch', return_value=payload):
                first = fp.manifest()
            self.assertEqual(first[0]['slug'], 'boba')
            self.assertEqual((root / '.petdex-manifest.json').read_bytes(), payload)
            buf = io.StringIO()
            with mock.patch.object(fp, 'HERE', root), \
                    mock.patch.object(fp, 'fetch', side_effect=urllib.error.URLError('offline')), \
                    contextlib.redirect_stdout(buf):
                second = fp.manifest()
            self.assertEqual(second, first)
            self.assertIn('缓存', buf.getvalue())

            empty = root / 'empty'
            empty.mkdir()
            with mock.patch.object(fp, 'HERE', empty), \
                    mock.patch.object(fp, 'fetch', side_effect=urllib.error.URLError('offline')):
                with self.assertRaises(SystemExit) as raised:
                    fp.manifest()
            self.assertIn('拿不到 petdex', str(raised.exception))


class InstallTest(unittest.TestCase):
    def test_install_records_source_credit_and_force(self):
        meta = {'displayName': 'Boba Tea'}
        saved = {}

        def save_pet(got, ext, img, overwrite=False, source='', credit=''):
            saved.update(meta=got, ext=ext, img=img, overwrite=overwrite, source=source, credit=credit)
            return {'id': got['id'], 'width': 1536, 'height': 1872}

        item = pet('Boba', 'Boba Tea')
        buf = io.StringIO()
        with mock.patch.object(fp, 'fetch', return_value=b'img') as fetch, \
                mock.patch.object(fp.tb, 'parse_pet_package', return_value=(meta, 'webp', b'x' * 2048)), \
                mock.patch.object(fp.tb, 'save_pet', side_effect=save_pet), \
                contextlib.redirect_stdout(buf):
            self.assertTrue(fp.install(item, force=True))
        fetch.assert_called_once_with(item['zipUrl'])
        self.assertEqual(saved['meta']['id'], 'boba')
        self.assertTrue(saved['overwrite'])
        self.assertEqual(saved['source'], 'https://petdex.dev/pets/Boba')
        self.assertIn('ada', saved['credit'])
        self.assertIn('petdex', saved['credit'])
        self.assertIn('boba', buf.getvalue())

    def test_spritesheet_url_is_used_when_zip_is_absent(self):
        item = pet('mini')
        del item['zipUrl']
        item['spritesheetUrl'] = 'https://example.invalid/mini.webp'
        with mock.patch.object(fp, 'fetch', return_value=b'img') as fetch, \
                mock.patch.object(fp.tb, 'parse_pet_package', return_value=({'displayName': 'Mini'}, 'webp', b'xx')), \
                mock.patch.object(fp.tb, 'save_pet', return_value={'id': 'mini', 'width': 1, 'height': 1}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(fp.install(item))
        fetch.assert_called_once_with('https://example.invalid/mini.webp')

    def test_existing_pet_is_skipped_without_force(self):
        buf = io.StringIO()
        with mock.patch.object(fp, 'fetch', return_value=b'img'), \
                mock.patch.object(fp.tb, 'parse_pet_package', return_value=({'id': 'boba'}, 'webp', b'x')), \
                mock.patch.object(fp.tb, 'save_pet', side_effect=ValueError('形象 boba 已存在；勾选覆盖或换个标识')), \
                contextlib.redirect_stdout(buf):
            self.assertFalse(fp.install(pet('boba')))
        self.assertIn('已有', buf.getvalue())
        self.assertIn('--force', buf.getvalue())

    def test_bad_package_and_download_errors_are_reported(self):
        buf = io.StringIO()
        with mock.patch.object(fp, 'fetch', return_value=b'not-an-image'), contextlib.redirect_stdout(buf):
            self.assertFalse(fp.install(pet('nope')))
        self.assertIn('跳过', buf.getvalue())
        self.assertNotIn('已有', buf.getvalue())

        buf = io.StringIO()
        with mock.patch.object(fp, 'fetch', side_effect=OSError('disk')), contextlib.redirect_stdout(buf):
            self.assertFalse(fp.install(pet('nope')))
        text = buf.getvalue()
        self.assertIn('下载失败', text)
        self.assertIn('OSError', text)

        # 没有下载地址时 urllib 抛出 ValueError，install 目前把它算进「跳过」而不是「下载失败」。
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertFalse(fp.install({'slug': 'nope', 'displayName': 'Nope'}))
        self.assertIn('跳过', buf.getvalue())
        self.assertNotIn('下载失败', buf.getvalue())


class MainTest(unittest.TestCase):
    def test_no_arguments_prints_help_without_network(self):
        manifest = mock.Mock()
        buf = io.StringIO()
        with mock.patch.object(fp, 'manifest', manifest), \
                mock.patch.object(fp.sys, 'argv', ['fetch_petdex.py']), \
                contextlib.redirect_stdout(buf):
            self.assertIsNone(fp.main())
        manifest.assert_not_called()
        self.assertIn('--starter', buf.getvalue())

    def test_search_flag_without_a_term_prints_help(self):
        manifest = mock.Mock()
        with mock.patch.object(fp, 'manifest', manifest), \
                mock.patch.object(fp.sys, 'argv', ['fetch_petdex.py', '--search']), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(fp.main())
        manifest.assert_not_called()

    def test_list_does_not_download(self):
        pets = [pet('robot-1', 'Robot'), pet('cat', 'Cat')]
        buf = io.StringIO()
        install = mock.Mock()
        with mock.patch.object(fp, 'manifest', return_value=pets), \
                mock.patch.object(fp, 'install', install), \
                mock.patch.object(fp.sys, 'argv', ['fetch_petdex.py', '--list', '--search', 'robot']), \
                contextlib.redirect_stdout(buf):
            fp.main()
        install.assert_not_called()
        self.assertIn('robot-1', buf.getvalue())
        self.assertIn('共 1 只', buf.getvalue())
        self.assertNotIn('cat', buf.getvalue().split('共')[0])

    def test_broad_search_is_not_downloaded(self):
        pets = [pet('cat-%02d' % i, 'Cat') for i in range(41)]
        buf = io.StringIO()
        install = mock.Mock()
        with mock.patch.object(fp, 'manifest', return_value=pets), \
                mock.patch.object(fp, 'install', install), \
                mock.patch.object(fp.sys, 'argv', ['fetch_petdex.py', '--search', 'cat']), \
                contextlib.redirect_stdout(buf):
            fp.main()
        install.assert_not_called()
        self.assertIn('太多', buf.getvalue())

    def test_slug_plus_search_dedups_before_install(self):
        pets = [pet('boba', 'Boba Tea'), pet('cat', 'Cat')]
        install = mock.Mock(return_value=True)
        with mock.patch.object(fp, 'manifest', return_value=pets), \
                mock.patch.object(fp, 'install', install), \
                mock.patch.object(fp.sys, 'argv', ['fetch_petdex.py', '--force', 'boba', '--search', 'boba']), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            fp.main()
        self.assertEqual([call.args[0]['slug'] for call in install.call_args_list], ['boba'])
        self.assertTrue(install.call_args.args[1])
        self.assertIn('完成：1 只', buf.getvalue())

    def test_starter_requests_the_curated_slugs(self):
        pets = [pet(slug) for slug in fp.STARTER]
        install = mock.Mock(return_value=True)
        with mock.patch.object(fp, 'manifest', return_value=pets), \
                mock.patch.object(fp, 'install', install), \
                mock.patch.object(fp.sys, 'argv', ['fetch_petdex.py', '--starter']), \
                contextlib.redirect_stdout(io.StringIO()):
            fp.main()
        self.assertEqual([call.args[0]['slug'] for call in install.call_args_list], list(fp.STARTER))

    def test_no_match_exits(self):
        with mock.patch.object(fp, 'manifest', return_value=[pet('boba')]), \
                mock.patch.object(fp.sys, 'argv', ['fetch_petdex.py', 'missing-creature']), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                fp.main()
        self.assertIn('没有匹配', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
