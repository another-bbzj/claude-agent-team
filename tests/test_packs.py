"""
游戏角色包（SPEC §9 / 工单 14）后端测试：不联网，monkeypatch fetch_petdex 的下载函数。
复用 test_board.py 的 BoardEnv（临时 HOME + 拷贝一份 team-board/）。
运行：python -m unittest discover -s tests -v
"""
import importlib.util
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_board import BoardEnv, webp_sheet  # noqa: E402

# 用一个 4 只的假角色包（覆盖 lead / 常驻 type / 不分配三种 role），不用真的 20 只，跑得快
FAKE_PACK = [
    {'slug': 'amiya', 'name': '阿米娅', 'franchise': '明日方舟', 'role': 'lead'},
    {'slug': 'furina-2', 'name': '芙宁娜', 'franchise': '原神', 'role': 'team-architect'},
    {'slug': 'firefly-2', 'name': '流萤', 'franchise': '崩坏：星穹铁道', 'role': 'backend-dev'},
    {'slug': 'jinx', 'name': '金克丝', 'franchise': '英雄联盟', 'role': None},
]


def fake_manifest(strict=True):
    return [{'slug': it['slug'], 'displayName': it['slug'], 'zipUrl': f'https://petdex.dev/z/{it["slug"]}.zip',
             'submittedBy': 'tester'} for it in FAKE_PACK]


def fake_fetch_ok(url, timeout=60):
    return webp_sheet(1536, 1872)


def load_fetch_petdex(tb_module):
    """把 fetch_petdex.py 挂到 sys.modules['fetch_petdex']，让 team_board.py 里懒加载的
    `import fetch_petdex` 复用同一份；再把它内部的 `tb` 强制指到测试用的 team_board 实例，
    避免它自己 `import team_board as tb` 又实例化出第二份（HERE/SPRITES 对不上，见踩坑记录）。"""
    spec = importlib.util.spec_from_file_location('fetch_petdex', tb_module.HERE / 'fetch_petdex.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules['fetch_petdex'] = mod
    spec.loader.exec_module(mod)
    mod.tb = tb_module
    return mod


class PackBase(unittest.TestCase):
    """给 fetch_petdex 换上假的 PACKS / manifest / fetch，避免真的联网。每个子类独立 BoardEnv。"""

    @classmethod
    def setUpClass(cls):
        cls.env = BoardEnv()
        cls.tb = cls.env.load()
        cls.fp = load_fetch_petdex(cls.tb)
        cls.fp.PACKS = {'games': FAKE_PACK}
        cls.fp.PACK_NAMES = {'games': '游戏角色包'}

    @classmethod
    def tearDownClass(cls):
        cls.env.cleanup()

    def setUp(self):
        self.fp.manifest = fake_manifest
        self.fp.fetch = fake_fetch_ok
        for it in FAKE_PACK:  # 每个测试前清空，互不干扰
            pid = self.tb.slugify_pet_id(it['slug'])
            for ext in ('webp', 'png'):
                (self.tb.SPRITES / f'{pid}.{ext}').unlink(missing_ok=True)
            self.tb.pet_meta_path(pid).unlink(missing_ok=True)
        self.tb.TEAM_CFG.pop('lead_pet', None)
        for atype in ('team-architect', 'backend-dev'):
            self.tb.TEAM_CFG['agents'][atype].pop('pet', None)
        self.tb.save_team_cfg()


class PackDownloadTest(PackBase):
    def test_pack_list(self):
        g = next(p for p in self.tb.pet_pack_list() if p['id'] == 'games')
        self.assertEqual(g['count'], 4)
        self.assertEqual(g['installed'], 0)
        self.assertEqual(g['roles'], {'lead': 'amiya', 'team-architect': 'furina-2', 'backend-dev': 'firefly-2'})

    def test_full_pack_download_and_assign(self):
        out = self.tb.install_pet_pack('games', assign=True)
        self.assertEqual(sorted(out['installed']), ['amiya', 'firefly-2', 'furina-2', 'jinx'])
        self.assertEqual(out['failed'], [])
        self.assertEqual(out['assigned'], {'lead': 'amiya', 'team-architect': 'furina-2', 'backend-dev': 'firefly-2'})
        self.assertEqual(self.tb.TEAM_CFG['lead_pet'], 'amiya')
        self.assertEqual(self.tb.TEAM_CFG['agents']['backend-dev']['pet'], 'firefly-2')
        info = self.tb.pet_info()
        self.assertEqual(info['amiya']['displayName'], '阿米娅')
        meta = json.loads(self.tb.pet_meta_path('amiya').read_text(encoding='utf-8'))
        self.assertEqual(meta['franchise'], '明日方舟')
        self.assertIn('petdex.dev', meta['source'])
        self.assertIn('petdex', meta['credit'])
        g = next(p for p in self.tb.pet_pack_list() if p['id'] == 'games')
        self.assertEqual(g['installed'], 4)

    def test_skip_existing_not_overwritten(self):
        self.tb.install_pet_pack('games')
        original = (self.tb.SPRITES / 'amiya.webp').read_bytes()

        def different_fetch(url, timeout=60):
            return webp_sheet(1536, 2288)  # 假装远端图变了：不该覆盖本地已有的
        self.fp.fetch = different_fetch
        out = self.tb.install_pet_pack('games')
        self.assertEqual(sorted(out['installed']), ['amiya', 'firefly-2', 'furina-2', 'jinx'])  # 已存在也算成功
        self.assertEqual(out['failed'], [])
        self.assertEqual((self.tb.SPRITES / 'amiya.webp').read_bytes(), original)  # 没被覆盖

    def test_single_failure_isolated(self):
        def flaky_fetch(url, timeout=60):
            if 'jinx' in url:
                raise RuntimeError('boom')
            return webp_sheet(1536, 1872)
        self.fp.fetch = flaky_fetch
        out = self.tb.install_pet_pack('games')
        self.assertEqual(sorted(out['installed']), ['amiya', 'firefly-2', 'furina-2'])
        self.assertEqual(len(out['failed']), 1)
        self.assertEqual(out['failed'][0]['slug'], 'jinx')
        self.assertIn('boom', out['failed'][0]['error'])

    def test_only_unassigned_roles_then_assign_all(self):
        self.tb.install_pet_pack('games')
        self.tb.TEAM_CFG['agents']['backend-dev']['pet'] = 'kit'  # 用户自己换过形象
        self.tb.save_team_cfg()
        assigned = self.tb.assign_pack_roles('games')
        self.assertNotIn('backend-dev', assigned)
        self.assertEqual(self.tb.TEAM_CFG['agents']['backend-dev']['pet'], 'kit')
        self.assertEqual(assigned.get('lead'), 'amiya')  # 没改过的岗位照常分配
        assigned = self.tb.assign_pack_roles('games', force_all=True)
        self.assertEqual(assigned['backend-dev'], 'firefly-2')
        self.assertEqual(self.tb.TEAM_CFG['agents']['backend-dev']['pet'], 'firefly-2')

    def test_pack_reassign_is_idempotent(self):
        self.tb.install_pet_pack('games', assign=True)
        assigned = self.tb.assign_pack_roles('games')  # 再跑一次（同一只），不该被当成"用户改过"而跳过
        self.assertEqual(assigned.get('lead'), 'amiya')

    def test_unknown_pack_raises(self):
        with self.assertRaises(ValueError):
            self.tb.install_pet_pack('does-not-exist')

    def test_network_totally_down_raises_pack_error(self):
        def dead_manifest(strict=True):
            raise RuntimeError('拿不到 petdex 清单（模拟）')
        self.fp.manifest = dead_manifest
        with self.assertRaises(self.tb.PetPackError):
            self.tb.install_pet_pack('games')


class PackHttpTest(PackBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tb.Handler.args = cls.tb.argparse.Namespace(session=None, project=None, stale=600)
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), cls.tb.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        super().tearDownClass()

    def get(self, path):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}') as r:
            return r.status, json.loads(r.read())

    def post(self, path, obj):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=json.dumps(obj).encode('utf-8'),
                                      headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_get_packs(self):
        st, body = self.get('/api/pets/packs')
        self.assertEqual(st, 200)
        g = next(p for p in body['packs'] if p['id'] == 'games')
        self.assertEqual(g['count'], 4)

    def test_post_pack_download_and_assign(self):
        st, r = self.post('/api/pets/pack', {'id': 'games', 'assign': True})
        self.assertEqual(st, 200, r)
        self.assertTrue(r['ok'])
        self.assertEqual(sorted(r['installed']), ['amiya', 'firefly-2', 'furina-2', 'jinx'])
        self.assertEqual(r['assigned']['lead'], 'amiya')
        self.assertEqual(self.get('/api/pets')[1]['petInfo']['amiya']['displayName'], '阿米娅')

    def test_post_pack_unknown_id_400(self):
        st, r = self.post('/api/pets/pack', {'id': 'nope'})
        self.assertEqual(st, 400)
        self.assertIn('error', r)

    def test_post_pack_network_down_502(self):
        def dead_manifest(strict=True):
            raise RuntimeError('拿不到 petdex 清单（模拟）')
        self.fp.manifest = dead_manifest
        st, r = self.post('/api/pets/pack', {'id': 'games'})
        self.assertEqual(st, 502)
        self.assertIn('error', r)
        self.fp.manifest = fake_manifest


if __name__ == '__main__':
    unittest.main()
