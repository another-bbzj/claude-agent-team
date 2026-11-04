"""预算护栏：没设预算、接近、超支、未定价下限、分成员/部门上限、非法配置。"""
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_budget():
    spec = importlib.util.spec_from_file_location('budget_under_test', ROOT / 'team-board' / 'budget.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


budget = load_budget()


def person(pid, name, agent_type, dept, cost, family, **extra):
    row = {
        'id': pid, 'name': name, 'agentType': agent_type, 'dept': dept,
        'cost': cost, 'modelFamily': family, 'model': 'claude-' + family if family else '',
        'definedModel': extra.pop('definedModel', family or 'inherit'),
        'dispatchModel': extra.pop('dispatchModel', ''),
    }
    row.update(extra)
    return row


def lead(cost=0.0, family='', model=''):
    return {'name': '队长', 'dept': 'hq', 'cost': cost, 'modelFamily': family, 'model': model}


def picture(members, lead_row=None, totals=None, departments=None):
    lead_row = lead() if lead_row is None else lead_row
    if departments is None:
        departments = [
            {'id': 'hq', 'name': '指挥部'},
            {'id': 'dev', 'name': '研发部'},
            {'id': 'docs', 'name': '文档部'},
            {'id': 'qa', 'name': '质量部'},
            {'id': 'rnd', 'name': '研究部'},
        ]
    if totals is None:
        spent = 0.0
        models = []
        unpriced = 0
        for row in [lead_row] + list(members):
            if row.get('cost') is None:
                unpriced += 1
                if row.get('model'):
                    models.append(row['model'])
            elif isinstance(row.get('cost'), (int, float)) and not isinstance(row.get('cost'), bool):
                spent += row['cost']
        totals = {'cost': round(spent, 4), 'unpriced': unpriced, 'unpricedModels': models}
    return {'lead': lead_row, 'members': list(members), 'departments': departments, 'totals': totals}


def status_of(snap, kind, ident=None):
    rows = [row for row in snap['scopes'] if row['kind'] == kind and (ident is None or row['id'] == ident or row.get('capKey') == ident)]
    return rows


class BudgetStatusTest(unittest.TestCase):
    def test_unset_budget_is_off_and_adds_no_alarm_fields(self):
        snap = picture([person('a', '阿服', 'backend-dev', 'dev', 1.2, 'opus')])
        for raw in (None, {}):
            got = budget.budget_status(snap, None, raw)
            self.assertFalse(got['enabled'])
            self.assertEqual(got['status'], 'off')
            self.assertEqual(got['error'], '')
            self.assertEqual(got['scopes'], [])
            self.assertEqual(got['suggestions'], [])
            self.assertIsNone(got['topSpender'])
            self.assertFalse(got['lowerBound'])
            self.assertEqual(got['lowerBoundLabel'], '')
        held = {'totals': snap['totals'], 'members': snap['members']}
        budget.attach_budget(held, None, None)
        self.assertEqual(held['budget']['status'], 'off')
        self.assertEqual(held['totals'], snap['totals'])

    def test_warn_at_default_and_custom_ratio(self):
        members = [person('a', '阿服', 'backend-dev', 'dev', 0.8, 'sonnet')]
        got = budget.budget_status(picture(members), None, {'usd': 1})
        self.assertTrue(got['enabled'])
        self.assertEqual(got['status'], 'warn')
        self.assertEqual(got['limit'], 1)
        self.assertAlmostEqual(got['spent'], 0.8)
        self.assertAlmostEqual(got['ratio'], 0.8)
        self.assertEqual(status_of(got, 'session')[0]['status'], 'warn')
        self.assertEqual(got['suggestions'], [])

        under = budget.budget_status(picture([person('a', '阿服', 'backend-dev', 'dev', 0.79, 'sonnet')]), None, {'usd': 1})
        self.assertEqual(under['status'], 'ok')

        custom = budget.budget_status(picture(members), None, {'usd': 1, 'warnRatio': 0.5})
        self.assertEqual(custom['status'], 'warn')
        self.assertEqual(custom['warnRatio'], 0.5)
        quiet = budget.budget_status(picture([person('a', '阿服', 'backend-dev', 'dev', 0.4, 'sonnet')]), None, {'usd': 1, 'warnRatio': 0.5})
        self.assertEqual(quiet['status'], 'ok')

        edge = budget.budget_status(picture([person('a', '阿服', 'backend-dev', 'dev', 0.99, 'sonnet')]), None, {'usd': 1, 'warnRatio': 1})
        self.assertEqual(edge['status'], 'ok')
        exact = budget.budget_status(picture([person('a', '阿服', 'backend-dev', 'dev', 1, 'sonnet')]), None, {'usd': 1, 'warnRatio': 1})
        self.assertEqual(exact['status'], 'over')

    def test_over_session_budget_names_top_spender(self):
        members = [
            person('a', '阿服', 'backend-dev', 'dev', 1.25, 'opus'),
            person('b', '文文', 'docs-writer', 'docs', 0.4, 'haiku'),
        ]
        got = budget.budget_status(picture(members, lead(0.2, 'opus', 'claude-opus')), None, {'usd': 1})
        self.assertEqual(got['status'], 'over')
        self.assertGreaterEqual(got['spent'], got['limit'])
        self.assertEqual(got['topSpender']['name'], '阿服')
        self.assertEqual(got['topSpender']['modelFamily'], 'opus')
        self.assertAlmostEqual(got['topSpender']['cost'], 1.25)
        self.assertFalse(got['lowerBound'])

    def test_top_spender_tie_uses_name_and_skips_unpriced(self):
        members = [
            person('b', '乙', 'backend-dev', 'dev', 2, 'sonnet'),
            person('a', '甲', 'frontend-dev', 'dev', 2, 'sonnet'),
            person('c', '丙', 'researcher', 'rnd', None, 'yi', model='yi-lightning'),
        ]
        got = budget.budget_status(picture(members, lead(2, 'opus', 'claude-opus')), None, {'usd': 100})
        # 金额相同按名字的码位：乙在「队长」和「甲」之前。未定价的丙不参与。
        self.assertEqual(got['topSpender']['name'], '乙')
        self.assertNotEqual(got['topSpender']['id'], 'c')
        only_members = budget.budget_status(picture(members, lead(0)), None, {'usd': 100})
        self.assertEqual(only_members['topSpender']['name'], '乙')
        none_priced = picture([person('c', '丙', 'researcher', 'rnd', None, 'yi', model='yi-lightning')], lead_row={'name': '队长', 'dept': 'hq', 'cost': None, 'model': 'missing-model', 'modelFamily': ''})
        none_priced['totals'] = {'cost': 0, 'unpriced': 2, 'unpricedModels': ['yi-lightning', 'missing-model']}
        empty = budget.budget_status(none_priced, None, {'usd': 10})
        self.assertIsNone(empty['topSpender'])
        self.assertTrue(empty['lowerBound'])

    def test_unpriced_is_a_lower_bound_and_does_not_invent_a_price(self):
        members = [
            person('a', '阿服', 'backend-dev', 'dev', 0.6, 'sonnet'),
            person('b', '乙', 'researcher', 'rnd', None, 'yi', model='yi-lightning'),
        ]
        snap = picture(members, lead(0))
        got = budget.budget_status(snap, None, {'usd': 10})
        self.assertEqual(got['status'], 'ok')
        self.assertTrue(got['lowerBound'])
        self.assertIn('成本为下限', got['lowerBoundLabel'])
        self.assertIn('yi-lightning', got['lowerBoundLabel'])
        self.assertAlmostEqual(got['spent'], 0.6)
        self.assertEqual(got['unpriced'], 1)
        blob = json.dumps(got, ensure_ascii=False)
        self.assertNotIn('9.99', blob)
        self.assertNotIn('999', blob)
        self.assertIn('yi-lightning', got['unpricedModels'])

        over = budget.budget_status(picture(members, lead(0.5, 'opus', 'claude-opus')), None, {'usd': 1})
        self.assertEqual(over['status'], 'over')
        self.assertTrue(over['lowerBound'])
        self.assertIn('成本为下限', over['lowerBoundLabel'])
        self.assertAlmostEqual(over['spent'], 1.1)

    def test_member_cap_is_per_person_and_can_trip_while_session_is_under(self):
        members = [
            person('a1', '阿服', 'backend-dev', 'dev', 3, 'sonnet'),
            person('a2', '阿服·2', 'backend-dev', 'dev', 3, 'sonnet'),
        ]
        got = budget.budget_status(picture(members, lead(0)), None, {'usd': 10, 'members': {'backend-dev': 2}})
        self.assertEqual(got['status'], 'over')
        self.assertEqual(status_of(got, 'session')[0]['status'], 'ok')
        rows = status_of(got, 'member', 'backend-dev')
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['status'] == 'over' and row['spent'] == 3 for row in rows))
        by_id = budget.budget_status(picture(members, lead(0)), None, {'members': {'a1': 1}})
        self.assertEqual(status_of(by_id, 'member', 'a1')[0]['status'], 'over')
        self.assertEqual(status_of(by_id, 'member', 'a2'), [])
        absent = budget.budget_status(picture([], lead(0)), None, {'members': {'qa-tester': 1}})
        row = status_of(absent, 'member', 'qa-tester')[0]
        self.assertFalse(row['present'])
        self.assertEqual(row['status'], 'ok')
        self.assertEqual(row['spent'], 0)

    def test_department_cap_sums_members_and_hq_includes_the_lead_once(self):
        members = [
            person('a', '阿服', 'backend-dev', 'dev', 1, 'sonnet'),
            person('b', '小界', 'frontend-dev', 'dev', 2, 'sonnet'),
        ]
        got = budget.budget_status(picture(members, lead(0.5, 'opus', 'claude-opus')), None, {'usd': 100, 'departments': {'dev': 2.5}})
        dev = status_of(got, 'department', 'dev')[0]
        self.assertEqual(dev['name'], '研发部')
        self.assertAlmostEqual(dev['spent'], 3)
        self.assertEqual(dev['status'], 'over')
        self.assertEqual(status_of(got, 'session')[0]['status'], 'ok')
        self.assertEqual(got['status'], 'over')

        hq = budget.budget_status(picture([], lead(5, 'opus', 'claude-opus')), None, {'departments': {'hq': 4}})
        row = status_of(hq, 'department', 'hq')[0]
        self.assertAlmostEqual(row['spent'], 5)
        self.assertEqual(row['status'], 'over')
        self.assertEqual(hq['status'], 'over')

    def test_invalid_config_does_not_enable_the_guard(self):
        snap = picture([person('a', '阿服', 'backend-dev', 'dev', 1, 'sonnet')])
        cases = [
            ([], 'budget 必须是对象'),
            ({'usd': 0}, 'budget.usd'),
            ({'usd': -2}, 'budget.usd'),
            ({'usd': True}, 'budget.usd'),
            ({'usd': '1'}, 'budget.usd'),
            ({'usd': float('nan')}, 'budget.usd'),
            ({'usd': float('inf')}, 'budget.usd'),
            ({'usd': 1, 'warnRatio': 0}, 'warnRatio'),
            ({'usd': 1, 'warnRatio': 1.5}, 'warnRatio'),
            ({'usd': 1, 'warnRatio': True}, 'warnRatio'),
            ({'members': []}, 'budget.members'),
            ({'members': {'backend-dev': 0}}, 'backend-dev'),
            ({'members': {'': 1}}, '键不能为空'),
            ({'departments': 'dev'}, 'budget.departments'),
            ({'departments': {'dev': -1}}, 'dev'),
        ]
        for raw, needle in cases:
            with self.subTest(raw=raw):
                got = budget.budget_status(snap, None, raw)
                self.assertFalse(got['enabled'])
                self.assertEqual(got['status'], 'invalid')
                self.assertIn(needle, got['error'])
                self.assertEqual(got['scopes'], [])
                self.assertEqual(got['suggestions'], [])

    def test_cli_overrides_file_and_a_bad_flag_stays_invalid(self):
        snap = picture([person('a', '阿服', 'backend-dev', 'dev', 1, 'sonnet')])
        cli = budget.cli_from_args(Namespace(budget=0.5, budget_warn=None, budget_member=None, budget_dept=None))
        got = budget.budget_status(snap, cli, {'usd': 10, 'warnRatio': 0.8})
        self.assertEqual(got['limit'], 0.5)
        self.assertEqual(got['status'], 'over')
        broken = budget.cli_from_args(Namespace(budget=None, budget_warn=None, budget_member=['backend-dev=nope'], budget_dept=None))
        bad = budget.budget_status(snap, broken, {'usd': 10})
        self.assertEqual(bad['status'], 'invalid')
        self.assertIn('backend-dev', bad['error'])
        only_cli = budget.budget_status(snap, cli, ['not-an-object'])
        self.assertEqual(only_cli['status'], 'over')
        self.assertEqual(only_cli['limit'], 0.5)

    def test_downgrade_follows_role_tiers(self):
        members = [
            person('a', '阿服', 'backend-dev', 'dev', 4, 'opus', definedModel='opus', dispatchModel='opus'),
            person('d', '文文', 'docs-writer', 'docs', 2, 'sonnet'),
            person('r', '老审', 'code-reviewer', 'qa', 3, 'opus'),
            person('q', '测测', 'qa-tester', 'qa', 1, 'sonnet'),
            person('h', '探探', 'researcher', 'rnd', 0.5, 'haiku'),
            person('f', '临时', 'general-purpose', 'docs', 1.5, 'fable'),
            person('g', '发发', 'release-ops', 'ops', 0.2, 'haiku'),
        ]
        got = budget.budget_status(picture(members, lead(0.1, 'opus', 'claude-opus')), None, {'usd': 100})
        by_type = {item['agentType']: item for item in got['suggestions']}
        self.assertEqual(by_type['backend-dev']['recommended'], 'sonnet')
        self.assertIn('派工把 model 指定成了 opus', by_type['backend-dev']['action'])
        self.assertIn('下次派工把 model 设为 sonnet', by_type['backend-dev']['action'])
        self.assertIn('改回 sonnet', by_type['backend-dev']['action'])
        self.assertEqual(by_type['docs-writer']['recommended'], 'haiku')
        self.assertIn('设为 haiku', by_type['docs-writer']['action'])
        self.assertEqual(by_type['general-purpose']['recommended'], 'haiku')
        self.assertIn('fable', by_type['general-purpose']['action'])
        self.assertNotIn('code-reviewer', by_type)
        self.assertNotIn('qa-tester', by_type)
        self.assertNotIn('researcher', by_type)
        self.assertNotIn('release-ops', by_type)
        self.assertEqual([item['name'] for item in got['suggestions'][:2]], ['阿服', '文文'])

    def test_page_and_protocol_mention_the_guard(self):
        html = (ROOT / 'team-board' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="budget-guard"', html)
        self.assertIn('function renderBudget', html)
        self.assertIn('成本为下限', html)
        self.assertIn('budget-guard:start', html)
        skill = (ROOT / 'skills' / 'agent-team' / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('budget.suggestions', skill)
        self.assertIn('budget.status', skill)
        shipped = json.loads((ROOT / 'team-board' / 'team.json').read_text(encoding='utf-8'))
        self.assertNotIn('budget', shipped)


class BudgetMergeTest(unittest.TestCase):
    def test_merge_keeps_user_budget_and_existing_price_rules(self):
        import install
        src = ROOT / 'team-board' / 'team.json'
        shipped = json.loads(src.read_text(encoding='utf-8'))
        self.assertNotIn('budget', shipped)
        tmp = Path(tempfile.mkdtemp(prefix='budget-merge-'))
        try:
            dst = tmp / 'team.json'
            old = json.loads(src.read_text(encoding='utf-8'))
            old['budget'] = {'usd': 3, 'warnRatio': 0.7, 'members': {'backend-dev': 1}}
            old['pricing_usd_per_mtok'] = dict(old['pricing_usd_per_mtok'])
            old['pricing_usd_per_mtok']['sonnet'] = {'in': 9, 'out': 9, 'cache_read': 9, 'cache_write': 9}
            old['pricing_usd_per_mtok']['my-model'] = {'in': 1, 'out': 2, 'cache_read': 0.1, 'cache_write': 1}
            old['agents'] = dict(old['agents'])
            old['agents']['my-designer'] = {'dept': 'design', 'avatar': 'ui-designer', 'name': '小设', 'role': '设计师'}
            dst.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')
            manifest = {'_team_json_pricing': shipped['pricing_usd_per_mtok']}
            install.merge_team_json(src, dst, manifest, {})
            merged = json.loads(dst.read_text(encoding='utf-8'))
            self.assertEqual(merged['budget']['usd'], 3)
            self.assertEqual(merged['budget']['members']['backend-dev'], 1)
            self.assertEqual(merged['pricing_usd_per_mtok']['sonnet']['in'], 9)
            self.assertEqual(merged['pricing_usd_per_mtok']['my-model']['in'], 1)
            self.assertIn('opus', merged['pricing_usd_per_mtok'])
            self.assertEqual(merged['agents']['my-designer']['name'], '小设')
            self.assertEqual(merged['agents']['backend-dev']['name'], '阿服')

            plain = tmp / 'plain.json'
            plain.write_text(src.read_text(encoding='utf-8'), encoding='utf-8')
            install.merge_team_json(src, plain, manifest, {})
            self.assertNotIn('budget', json.loads(plain.read_text(encoding='utf-8')))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BoardBudgetWiringTest(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get('HOME')
        self._profile = os.environ.get('USERPROFILE')
        self.tmp = Path(tempfile.mkdtemp(prefix='budget-board-'))
        self.home = self.tmp / 'home'
        board = self.home / '.claude' / 'team-board'
        shutil.copytree(ROOT / 'team-board', board, ignore=shutil.ignore_patterns('bus', 'backdrops', '__pycache__'))
        (self.home / '.claude' / 'projects').mkdir(parents=True)
        os.environ['HOME'] = str(self.home)
        os.environ['USERPROFILE'] = str(self.home)
        os.environ['CODEX_HOME'] = str(self.tmp / 'codex')
        os.environ['PETDEX_HOME'] = str(self.tmp / 'petdex')
        spec = importlib.util.spec_from_file_location('team_board_budget', board / 'team_board.py')
        self.tb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tb)
        self.board = board

    def tearDown(self):
        if self._home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self._home
        if self._profile is None:
            os.environ.pop('USERPROFILE', None)
        else:
            os.environ['USERPROFILE'] = self._profile
        shutil.rmtree(self.tmp, ignore_errors=True)

    def snap(self, **kw):
        ns = Namespace(session=None, project=None, stale=600, budget=kw.get('budget'),
                       budget_warn=kw.get('budget_warn'), budget_member=kw.get('budget_member'),
                       budget_dept=kw.get('budget_dept'))
        return self.tb.build_snapshot(ns)

    def test_snapshot_stays_off_until_team_json_or_cli_sets_a_budget(self):
        quiet = self.snap()
        self.assertEqual(quiet['budget']['status'], 'off')
        self.assertFalse(quiet['budget']['enabled'])
        self.assertIn('members', quiet)
        path = self.board / 'team.json'
        cfg = json.loads(path.read_text(encoding='utf-8'))
        cfg['budget'] = {'usd': 1, 'warnRatio': 0.8}
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')
        armed = self.snap()
        self.assertTrue(armed['budget']['enabled'])
        self.assertEqual(armed['budget']['limit'], 1)
        self.assertEqual(armed['budget']['status'], 'ok')
        overridden = self.snap(budget_member=['nope=0'])
        self.assertEqual(overridden['budget']['status'], 'invalid')
        self.assertIn('nope', overridden['budget']['error'])


class DemoBudgetTest(unittest.TestCase):
    def test_synthetic_demo_can_trigger_a_red_guard(self):
        spec = importlib.util.spec_from_file_location('demo_budget_test', ROOT / 'tools' / 'demo_board.py')
        demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo)
        source = json.loads(demo.FIXTURE.read_text(encoding='utf-8'))
        self.assertNotIn('budget', source)
        quiet = demo.snapshot_at(source, source['generatedAt'])
        budget.attach_budget(quiet, None, None)
        self.assertEqual(quiet['budget']['status'], 'off')
        alert = demo.snapshot_at(source, source['generatedAt'])
        config = budget.over_budget_demo(alert)
        budget.attach_budget(alert, None, config)
        self.assertEqual(alert['budget']['status'], 'over')
        self.assertFalse(alert['budget']['lowerBound'])
        self.assertGreaterEqual(alert['budget']['spent'], alert['budget']['limit'])
        docs = next(item for item in alert['budget']['suggestions'] if item['agentType'] == 'docs-writer')
        self.assertEqual(docs['recommended'], 'haiku')
        self.assertIn('设为 haiku', docs['action'])
        self.assertEqual(alert['budget']['topSpender']['name'], '演示前端')

    def test_demo_http_budget_query_is_opt_in(self):
        spec = importlib.util.spec_from_file_location('demo_budget_http', ROOT / 'tools' / 'demo_board.py')
        demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo)
        server = demo.DemoServer(0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def get(path):
                with urllib.request.urlopen('http://127.0.0.1:{}{}'.format(server.server_port, path), timeout=3) as resp:
                    return json.loads(resp.read().decode('utf-8'))
            self.assertEqual(get('/snapshot.json')['budget']['status'], 'off')
            over = get('/snapshot.json?budget=over')
            self.assertEqual(over['budget']['status'], 'over')
            self.assertIn('成本为下限', (ROOT / 'team-board' / 'index.html').read_text(encoding='utf-8'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
