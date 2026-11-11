"""Grafana dashboard queries stay inside the Prometheus exporter contract."""
import copy
import json
import re
import unittest
from pathlib import Path

from test_export_snapshot import exporter

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / 'examples' / 'grafana' / 'claude-agent-team.dashboard.json'
SOURCE = ROOT / 'examples' / 'snapshot.synthetic.json'
OPTIONAL = {
    'claude_agent_team_budget_utilization_ratio',
    'claude_agent_team_test_results',
}
COMPONENT_KINDS = {'input', 'output', 'cache_read', 'cache_write'}
KINDS = COMPONENT_KINDS | {'total'}
SCOPES = {'members', 'lead', 'all'}
_SELECTOR = re.compile(r'^(claude_agent_team_[a-z_]+)(?:\{([^{}]*)\})?$')
_PAIR = re.compile(r'^([a-z_]+)\s*=\s*"([a-z0-9_]+)"$')


def _panels(node):
    if isinstance(node, dict):
        if 'type' in node and 'id' in node:
            yield node
        for value in node.values():
            for panel in _panels(value):
                yield panel
    elif isinstance(node, list):
        for item in node:
            for panel in _panels(item):
                yield panel


def _parse_selector(expr):
    """One gauge selector. Anything else, including rate() or a second metric, fails."""
    match = _SELECTOR.fullmatch(expr.strip())
    if match is None:
        raise AssertionError('not a single gauge selector: %s' % expr)
    body = match.group(2)
    pairs = []
    if body is not None:
        parts = [part.strip() for part in body.split(',')]
        if not parts or any(part == '' for part in parts):
            raise AssertionError('empty label matcher: %s' % expr)
        for part in parts:
            pair = _PAIR.fullmatch(part)
            if pair is None:
                raise AssertionError('label is not a fixed equality: %s' % expr)
            pairs.append((pair.group(1), pair.group(2)))
    return match.group(1), tuple(pairs)


def _labels(pairs):
    labels = {}
    for key, value in pairs:
        if key in labels:
            raise AssertionError('duplicate label %s' % key)
        labels[key] = value
    return labels


def _series_keys(text):
    names = set()
    keys = set()
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        head = line.rsplit(' ', 1)[0]
        name, pairs = _parse_selector(head)
        names.add(name)
        keys.add((name, tuple(sorted(_labels(pairs).items()))))
    return names, keys


def _with_optional(snapshot):
    """Turn on the two families the synthetic fixture omits, without private fields."""
    updated = copy.deepcopy(snapshot)
    updated['budget'] = {'enabled': True, 'ratio': 1.25}
    updated['members'][0]['lastTest'] = {'status': 'unknown'}
    updated['members'][1]['lastTest'] = {'status': 'fail'}
    return updated


class GrafanaDashboardTest(unittest.TestCase):
    def setUp(self):
        self.dashboard = json.loads(DASHBOARD.read_text(encoding='utf-8'))
        self.snapshot = json.loads(SOURCE.read_text(encoding='utf-8'))
        self.panels = [panel for panel in _panels(self.dashboard['panels'])]
        self.exprs = []
        for panel in self.panels:
            for item in panel.get('targets') or []:
                self.exprs.append(item['expr'])

    def test_dashboard_is_an_importable_prometheus_model(self):
        self.assertEqual(self.dashboard['title'], 'Claude Agent Team')
        self.assertEqual(self.dashboard['uid'], 'claude-agent-team')
        self.assertIsInstance(self.dashboard['schemaVersion'], int)
        self.assertIsNone(self.dashboard['id'])
        inputs = self.dashboard['__inputs']
        self.assertEqual(
            [(item['name'], item['type'], item['pluginId']) for item in inputs],
            [('DS_PROMETHEUS', 'datasource', 'prometheus')])
        variables = self.dashboard['templating']['list']
        self.assertEqual(
            [(item['name'], item['type'], item['query']) for item in variables],
            [('DS_PROMETHEUS', 'datasource', 'prometheus')])
        ids = [panel['id'] for panel in self.panels]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(self.exprs)
        for panel in self.panels:
            targets = panel.get('targets') or []
            if panel['type'] in ('row', 'text'):
                self.assertEqual(targets, [])
                continue
            self.assertTrue(targets, panel.get('title'))
            self.assertEqual(panel['datasource'], {'type': 'prometheus', 'uid': '${DS_PROMETHEUS}'})
            for item in targets:
                self.assertEqual(item['datasource'], {'type': 'prometheus', 'uid': '${DS_PROMETHEUS}'})
            if panel['type'] == 'timeseries':
                self.assertEqual(panel['fieldConfig']['defaults']['custom']['stacking']['mode'], 'none')
            self.assertEqual(panel.get('transformations') or [], [])

    def test_every_query_matches_a_series_the_exporter_emits(self):
        plain = exporter.render_prometheus(self.snapshot)
        triggered_snapshot = _with_optional(self.snapshot)
        triggered = exporter.render_prometheus(triggered_snapshot)
        plain_names, plain_keys = _series_keys(plain)
        triggered_names, triggered_keys = _series_keys(triggered)
        self.assertTrue(OPTIONAL.isdisjoint(plain_names))
        self.assertTrue(OPTIONAL <= triggered_names)
        self.assertIn('claude_agent_team_budget_utilization_ratio{scope="session"} 1.25\n', triggered)
        self.assertIn('claude_agent_team_test_results{status="pass"} 0\n', triggered)
        self.assertIn('claude_agent_team_test_results{status="fail"} 1\n', triggered)
        self.assertIn('claude_agent_team_test_results{status="unknown"} 1\n', triggered)
        self.assertNotIn('rate(', plain)
        self.assertNotIn('increase(', plain)
        for expr in self.exprs:
            lowered = expr.lower()
            self.assertNotIn('rate(', lowered, expr)
            self.assertNotIn('increase(', lowered, expr)
            name, pairs = _parse_selector(expr)
            labels = _labels(pairs)
            self._assert_fixed_labels(name, labels, expr)
            key = (name, tuple(sorted(labels.items())))
            if name in OPTIONAL:
                self.assertNotIn(name, plain_names, expr)
                self.assertIn(key, triggered_keys, expr)
            else:
                self.assertIn(key, plain_keys, expr)
            self.assertIn(key, triggered_keys, expr)

    def test_panels_cover_the_exported_families_without_overlapping_sums(self):
        parsed = [_parse_selector(expr) for expr in self.exprs]
        names = {name for name, _pairs in parsed}
        self.assertEqual(names, {
            'claude_agent_team_members',
            'claude_agent_team_estimated_cost_usd',
            'claude_agent_team_unpriced_entries',
            'claude_agent_team_tokens',
            'claude_agent_team_cache_hit_ratio',
            'claude_agent_team_budget_utilization_ratio',
            'claude_agent_team_test_results',
        })
        label_sets = {}
        for name, pairs in parsed:
            label_sets.setdefault(name, set()).add(tuple(sorted(_labels(pairs).items())))
        self.assertEqual(label_sets['claude_agent_team_members'], {()})
        self.assertEqual(label_sets['claude_agent_team_unpriced_entries'], {()})
        self.assertEqual(label_sets['claude_agent_team_estimated_cost_usd'], {
            (('scope', 'all'),),
            (('scope', 'members'),),
        })
        self.assertEqual(label_sets['claude_agent_team_cache_hit_ratio'], {
            (('scope', 'all'),),
            (('scope', 'members'),),
            (('scope', 'lead'),),
        })
        self.assertEqual(label_sets['claude_agent_team_budget_utilization_ratio'], {
            (('scope', 'session'),),
        })
        self.assertEqual(label_sets['claude_agent_team_test_results'], {
            (('status', 'pass'),),
            (('status', 'fail'),),
            (('status', 'unknown'),),
        })
        token_labels = {
            (dict(pairs)['scope'], dict(pairs)['kind'])
            for pairs in label_sets['claude_agent_team_tokens']
        }
        self.assertEqual(token_labels, {(scope, kind) for scope in SCOPES for kind in KINDS})
        for panel in self.panels:
            kinds = set()
            for item in panel.get('targets') or []:
                name, pairs = _parse_selector(item['expr'])
                if name != 'claude_agent_team_tokens':
                    continue
                kinds.add(_labels(pairs)['kind'])
            if 'total' in kinds:
                self.assertEqual(kinds, {'total'}, panel.get('title'))
            self.assertFalse(kinds & COMPONENT_KINDS and 'total' in kinds, panel.get('title'))
        text = '\n'.join(
            panel.get('options', {}).get('content', '')
            for panel in self.panels if panel['type'] == 'text')
        self.assertIn('部门', text)
        self.assertNotIn('dept=', text)

    def test_docs_link_the_dashboard(self):
        integrations = (ROOT / 'docs' / 'INTEGRATIONS.md').read_text(encoding='utf-8')
        self.assertIn('## Grafana', integrations)
        for relative in (
            'examples/grafana/claude-agent-team.dashboard.json',
            'examples/grafana/textfile-refresh.example.md',
            'examples/grafana/provisioning/datasources/prometheus.yaml',
            'examples/grafana/provisioning/dashboards/claude-agent-team.yaml',
        ):
            self.assertIn(relative, integrations)
            self.assertTrue((ROOT / relative).is_file(), relative)
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        english = (ROOT / 'README.en.md').read_text(encoding='utf-8')
        self.assertIn('docs/INTEGRATIONS.md#grafana', readme)
        self.assertIn('docs/INTEGRATIONS.md#grafana', english)

    def _assert_fixed_labels(self, name, labels, expr):
        if name == 'claude_agent_team_members' or name == 'claude_agent_team_unpriced_entries':
            self.assertEqual(labels, {}, expr)
        elif name == 'claude_agent_team_estimated_cost_usd':
            self.assertEqual(set(labels), {'scope'}, expr)
            self.assertIn(labels['scope'], {'all', 'members'}, expr)
        elif name == 'claude_agent_team_tokens':
            self.assertEqual(set(labels), {'scope', 'kind'}, expr)
            self.assertIn(labels['scope'], SCOPES, expr)
            self.assertIn(labels['kind'], KINDS, expr)
        elif name == 'claude_agent_team_cache_hit_ratio':
            self.assertEqual(set(labels), {'scope'}, expr)
            self.assertIn(labels['scope'], SCOPES, expr)
        elif name == 'claude_agent_team_budget_utilization_ratio':
            self.assertEqual(labels, {'scope': 'session'}, expr)
        elif name == 'claude_agent_team_test_results':
            self.assertEqual(set(labels), {'status'}, expr)
            self.assertIn(labels['status'], {'pass', 'fail', 'unknown'}, expr)
        else:
            self.fail('unexpected metric %s in %s' % (name, expr))
