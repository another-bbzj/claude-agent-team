"""Stall-alert ages: idle / error / stalled only, and only with a known clock."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_stall():
    import importlib.util
    spec = importlib.util.spec_from_file_location('stall_alerts_under_test', ROOT / 'team-board' / 'stall_alerts.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stall = load_stall()


def member(**kwargs):
    base = {'id': 'm', 'name': '合成成员', 'status': 'stalled'}
    base.update(kwargs)
    return base


class IdleAgeTest(unittest.TestCase):
    def test_last_activity_is_preferred_over_an_older_event(self):
        seconds, basis, anchor = stall.member_idle_age(
            member(lastActivityAt=1000, timeline=[{'ts': 100}], endedAt=50, startedAt=1), 1600)
        self.assertEqual((seconds, basis, anchor), (600, 'lastActivityAt', 1000))

    def test_explicit_idle_seconds_win_and_anchor_stays_put(self):
        seconds, basis, anchor = stall.member_idle_age(
            member(idleSeconds=90, lastActivityAt=1), 1000)
        self.assertEqual((seconds, basis, anchor), (90, 'idleSeconds', 910))

    def test_missing_activity_uses_the_newest_event_not_the_start(self):
        seconds, basis, anchor = stall.member_idle_age(
            member(lastActivityAt=None, startedAt=10, endedAt=400, timeline=[{'ts': 100}, {'kind': 'note'}, {'ts': 250}]),
            1000)
        self.assertEqual((seconds, basis, anchor), (600, 'lastEvent', 400))

    def test_no_event_time_is_unknown(self):
        self.assertEqual(stall.member_idle_age(member(lastActivityAt=None, startedAt=10, timeline=[]), 1000), (None, None, None))
        self.assertEqual(stall.member_idle_age(member(lastActivityAt=True, idleSeconds=False, timeline=[{'ts': '100'}]), 1000), (None, None, None))

    def test_future_activity_is_not_a_long_idle(self):
        seconds, basis, _anchor = stall.member_idle_age(member(lastActivityAt=5000), 1000)
        self.assertEqual((seconds, basis), (0.0, 'lastActivityAt'))

    def test_threshold_defaults_and_clamps(self):
        self.assertEqual(stall.normalize_threshold(None), 600)
        self.assertEqual(stall.normalize_threshold('600'), 600)
        self.assertEqual(stall.normalize_threshold(True), 600)
        self.assertEqual(stall.normalize_threshold(1), 60)
        self.assertEqual(stall.normalize_threshold(10 ** 9), 86400)


class CandidateTest(unittest.TestCase):
    def test_only_idle_error_and_stalled_with_a_known_age(self):
        now = 2000
        members = [
            member(id='run', status='running', lastActivityAt=1),
            member(id='done', status='completed', lastActivityAt=1),
            member(id='fresh-error', status='error', lastActivityAt=1900, errorReason='token 上限'),
            member(id='old-error', status='error', lastActivityAt=1000, errorReason='x' * 200),
            member(id='idle-unknown', name='官方队友', status='idle', lastActivityAt=None, timeline=[]),
            member(id='idle-old', status='idle', lastActivityAt=1500),
            member(id='stall', status='stalled', lastActivityAt=1000),
            'not-a-member',
            {'status': 'stalled', 'lastActivityAt': 1000},
        ]
        found = stall.stall_candidates(members, now)
        self.assertEqual([item['id'] for item in found], ['', 'old-error', 'stall', 'idle-old', 'fresh-error'])
        self.assertEqual(found[0]['name'], '成员')
        self.assertEqual(found[1]['errorReason'], 'x' * 120)
        self.assertEqual(found[1]['idleSeconds'], 1000)
        active = stall.active_stall_alerts(found, 600)
        self.assertEqual([item['id'] for item in active], ['', 'old-error', 'stall'])
        self.assertNotIn('fresh-error', [item['id'] for item in active])
        self.assertNotIn('idle-unknown', [item['id'] for item in found])

    def test_attach_records_the_default_and_leaves_members_alone(self):
        snapshot = {'generatedAt': 1900, 'members': [member(id='a', status='stalled', lastActivityAt=1000)]}
        stall.attach_stall_alerts(snapshot)
        self.assertEqual(snapshot['stallAlerts']['thresholdSec'], 600)
        self.assertEqual(snapshot['stallAlerts']['defaultThresholdSec'], 600)
        self.assertEqual(snapshot['stallAlerts']['candidates'][0]['idleSeconds'], 900)
        self.assertEqual(snapshot['members'][0]['name'], '合成成员')
        bad = ['nope']
        self.assertIs(stall.attach_stall_alerts(bad), bad)


class BrowserParityTest(unittest.TestCase):
    def test_page_function_matches_python_on_shared_cases(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        html = (ROOT / 'team-board' / 'index.html').read_text(encoding='utf-8')
        start = html.index('/* stall-alerts:start */')
        end = html.index('/* stall-alerts:end */')
        cases = [
            member(status='stalled', lastActivityAt=1000, timeline=[{'ts': 100}], startedAt=1),
            member(status='error', idleSeconds=90, lastActivityAt=1, errorReason='上限'),
            member(status='idle', lastActivityAt=None, startedAt=10, endedAt=400, timeline=[{'ts': 250}]),
            member(status='idle', lastActivityAt=None, startedAt=10, timeline=[]),
            member(status='running', lastActivityAt=1),
            member(status='stalled', lastActivityAt=5000),
            {'status': 'stalled', 'lastActivityAt': 1000, 'idleSeconds': True},
        ]
        now = 1600
        expected = [stall.stall_candidates([item], now) for item in cases]
        script = html[start:end] + '\nconst cases = %s;\nconst now = %s;\nprocess.stdout.write(JSON.stringify(cases.map(item => stallCandidatesFromMembers([item], now))));\n' % (
            json.dumps(cases, ensure_ascii=False), now)
        completed = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)

        def canon(value):
            if isinstance(value, float) and value == int(value):
                return int(value)
            if isinstance(value, list):
                return [canon(item) for item in value]
            if isinstance(value, dict):
                return {key: canon(item) for key, item in value.items()}
            return value

        self.assertEqual(canon(json.loads(completed.stdout)), canon(expected))
