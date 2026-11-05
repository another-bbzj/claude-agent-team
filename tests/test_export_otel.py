"""OTLP/JSON export copies snapshot fields and drops private text."""
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('export_otel', ROOT / 'tools' / 'export_otel.py')
otel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(otel)

FIXTURE = ROOT / 'examples' / 'otel-snapshot.synthetic.json'
CHECKED = ROOT / 'examples' / 'otel-export.synthetic.json'
SECRET = re.compile(
    r'(?:/Users/|/home/|[A-Za-z]:[\\/]+Users[\\/]|~[/\\]'
    r'|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'
    r'|sk-ant-|sk-[A-Za-z0-9][A-Za-z0-9_\-]{7,}|ghp_|github_pat_|xox[baprs]-'
    r'|AKIA[0-9A-Z]{8,}|BEGIN [A-Z ]*PRIVATE KEY|api[_-]?key\s*[:=]'
    r'|Bearer\s+[A-Za-z0-9._\-]+)',
    re.IGNORECASE)
SECRET_SAMPLES = (
    '/home/synthetic/notes',
    '/Users/synthetic/notes',
    'C:\\Users\\synthetic\\notes',
    'D:/Users/synthetic/notes',
    '~/synthetic/notes',
    'synthetic.person@example.com',
    'sk-ant-api03-synthetic',
    'sk-proj-synthetickey',
    'sk-12345678',
    'ghp_synthetictoken',
    'github_pat_synthetic',
    'xoxb-synthetic',
    'AKIA12345678',
    'api_key=synthetic',
    'Bearer synthetic.token',
)
TRACE_ID = re.compile(r'^[0-9a-f]{32}$')
SPAN_ID = re.compile(r'^[0-9a-f]{16}$')
NANO = re.compile(r'^[0-9]+$')
VALUE_KEYS = {'stringValue', 'boolValue', 'intValue', 'doubleValue'}


def _load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _spans(document):
    resource_spans = document['resourceSpans']
    if len(resource_spans) != 1:
        raise AssertionError('expected one resourceSpans entry')
    scope_spans = resource_spans[0]['scopeSpans']
    if len(scope_spans) != 1:
        raise AssertionError('expected one scopeSpans entry')
    return scope_spans[0]['spans']


def _attr_map(span):
    found = {}
    for item in span.get('attributes', []):
        found[item['key']] = item['value']
    return found


def _string(span, key):
    value = _attr_map(span).get(key)
    if not value:
        return None
    return value.get('stringValue')


def _double(span, key):
    value = _attr_map(span).get(key)
    if not value or 'doubleValue' not in value:
        return None
    return value['doubleValue']


def _resource_map(document):
    found = {}
    for item in document['resourceSpans'][0]['resource']['attributes']:
        found[item['key']] = item['value']
    return found


def _assert_shape(testcase, document):
    testcase.assertEqual(set(document), {'resourceSpans'})
    testcase.assertNotIn('resourceLogs', document)
    spans = _spans(document)
    scope = document['resourceSpans'][0]['scopeSpans'][0]['scope']
    testcase.assertEqual(scope, {'name': 'team-board.export_otel', 'version': '1'})
    ids = []
    trace_ids = set()
    for span in spans:
        testcase.assertRegex(span['traceId'], TRACE_ID)
        testcase.assertRegex(span['spanId'], SPAN_ID)
        testcase.assertNotEqual(span['traceId'], '0' * 32)
        testcase.assertNotEqual(span['spanId'], '0' * 16)
        testcase.assertEqual(span['kind'], 1)
        testcase.assertIsInstance(span['kind'], int)
        testcase.assertRegex(span['startTimeUnixNano'], NANO)
        testcase.assertRegex(span['endTimeUnixNano'], NANO)
        start = int(span['startTimeUnixNano'])
        end = int(span['endTimeUnixNano'])
        testcase.assertGreaterEqual(end, start)
        testcase.assertLessEqual(end, 2 ** 64 - 1)
        testcase.assertIsInstance(span['name'], str)
        testcase.assertTrue(span['name'])
        if 'parentSpanId' in span:
            testcase.assertRegex(span['parentSpanId'], SPAN_ID)
        if 'status' in span:
            testcase.assertEqual(set(span['status']), {'code'})
            testcase.assertIn(span['status']['code'], (1, 2))
        for item in span.get('attributes', []):
            testcase.assertEqual(set(item['value']), set(item['value'].keys()) & VALUE_KEYS)
            testcase.assertEqual(len(item['value']), 1)
            if 'intValue' in item['value']:
                testcase.assertRegex(item['value']['intValue'], r'^[0-9]+$')
            if 'doubleValue' in item['value']:
                testcase.assertNotIsInstance(item['value']['doubleValue'], bool)
                testcase.assertGreaterEqual(item['value']['doubleValue'], 0)
        ids.append(span['spanId'])
        trace_ids.add(span['traceId'])
    testcase.assertEqual(len(ids), len(set(ids)))
    if spans:
        testcase.assertEqual(len(trace_ids), 1)
    for span in spans:
        parent = span.get('parentSpanId')
        if parent is not None:
            testcase.assertIn(parent, ids)
            testcase.assertNotEqual(parent, span['spanId'])
    for item in document['resourceSpans'][0]['resource']['attributes']:
        testcase.assertEqual(len(item['value']), 1)
        testcase.assertTrue(set(item['value']) <= VALUE_KEYS)


class OtelFixtureTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = _load(FIXTURE)

    def test_fixture_and_checked_in_export_have_no_secret_material(self):
        self.assertTrue(FIXTURE.is_file())
        self.assertTrue(CHECKED.is_file())
        for sample in SECRET_SAMPLES:
            self.assertIsNotNone(SECRET.search(sample), sample)
        for path in (FIXTURE, CHECKED):
            text = path.read_text(encoding='utf-8')
            self.assertIsNone(SECRET.search(text), path.name)
            self.assertNotIn('synthetic-session', text if path == CHECKED else 'ok')

    def test_checked_in_example_matches_the_renderer(self):
        rendered = otel.render_otlp(self.snapshot)
        self.assertEqual(rendered, CHECKED.read_text(encoding='utf-8'))
        self.assertTrue(rendered.endswith('\n'))
        self.assertNotIn('\r\n', rendered)

    def test_fixture_copies_present_numbers_and_skips_the_open_member(self):
        blob = json.dumps(self.snapshot, sort_keys=True)
        with mock.patch('time.time', side_effect=AssertionError('clock')):
            document = otel.build_otlp(self.snapshot)
            again = otel.build_otlp(self.snapshot)
        self.assertEqual(document, again)
        self.assertEqual(blob, json.dumps(self.snapshot, sort_keys=True))
        _assert_shape(self, document)
        spans = _spans(document)
        self.assertEqual(
            [span['name'] for span in spans],
            ['合成队长', '合成甲', 'Read · notes', 'report', 'Survey the module', 'PostToolUse Read'])
        lead, actor, event, comm, task, hook = spans
        self.assertEqual(_string(lead, 'team.actor.kind'), 'lead')
        self.assertEqual(_double(lead, 'team.cost.usd'), 0.25)
        self.assertEqual(_attr_map(lead)['team.cost.unpriced']['intValue'], '1')
        self.assertIs(_attr_map(lead)['team.cost.partial']['boolValue'], True)
        self.assertEqual(_attr_map(lead)['team.tokens.total']['intValue'], '7')
        self.assertEqual(_attr_map(lead)['team.usage.input']['intValue'], '3')
        self.assertEqual(lead['status'], {'code': 1})
        self.assertNotIn('parentSpanId', lead)
        self.assertEqual(_string(actor, 'team.actor.kind'), 'member')
        self.assertEqual(_string(actor, 'team.dept'), 'dev')
        self.assertEqual(_string(actor, 'team.role'), '合成研发')
        self.assertEqual(_double(actor, 'team.cost.usd'), 0.5)
        self.assertEqual(_attr_map(actor)['team.cost.unpriced']['intValue'], '0')
        self.assertNotIn('team.cost.partial', _attr_map(actor))
        self.assertEqual(_double(actor, 'team.cache_hit'), 0.5)
        self.assertEqual(_attr_map(actor)['team.tokens.output']['intValue'], '10')
        self.assertEqual(actor['startTimeUnixNano'], '1700000000000000000')
        self.assertEqual(actor['endTimeUnixNano'], '1700000010000000000')
        self.assertEqual(event['parentSpanId'], actor['spanId'])
        self.assertEqual(event['startTimeUnixNano'], '1700000002000000000')
        self.assertEqual(event['endTimeUnixNano'], '1700000002015000000')
        self.assertEqual(event['status'], {'code': 2})
        self.assertEqual(_string(event, 'team.test.status'), 'fail')
        self.assertEqual(_attr_map(event)['team.test.passed']['intValue'], '2')
        self.assertEqual(_attr_map(event)['team.test.failed']['intValue'], '1')
        self.assertNotIn('team.test.errors', _attr_map(event))
        self.assertNotIn('team.test.exit_code', _attr_map(event))
        self.assertIs(_attr_map(event)['team.test.truncated']['boolValue'], False)
        self.assertNotIn('team.cost.usd', _attr_map(event))
        self.assertEqual(comm['parentSpanId'], actor['spanId'])
        self.assertEqual(_string(comm, 'team.comm.text'), '合成回报：检查通过。')
        self.assertNotIn('status', comm)
        self.assertEqual(task['parentSpanId'], actor['spanId'])
        self.assertEqual(task['startTimeUnixNano'], task['endTimeUnixNano'])
        self.assertIs(_attr_map(task)['team.task.blocked']['boolValue'], False)
        self.assertNotIn('parentSpanId', hook)
        self.assertEqual(_string(hook, 'team.hook.event'), 'PostToolUse')
        self.assertEqual(_string(hook, 'team.tool.name'), 'Read')
        resource = _resource_map(document)
        self.assertEqual(resource['service.name']['stringValue'], 'claude-agent-team')
        self.assertEqual(resource['team.member_count']['intValue'], '2')
        self.assertEqual(resource['team.totals.cost']['doubleValue'], 2.0)
        self.assertEqual(resource['team.totals.members_cost']['doubleValue'], 1.75)
        self.assertEqual(resource['team.totals.output_tokens']['intValue'], '14')
        self.assertEqual(resource['team.usage.members.output']['intValue'], '10')
        self.assertEqual(resource['team.usage.lead.cache_hit']['doubleValue'], 0)
        self.assertEqual(resource['team.snapshot.generated_at_unix_nano']['stringValue'],
                         '1700000000000000000')
        self.assertNotIn('team.budget.utilization_ratio', resource)
        rendered = json.dumps(document, ensure_ascii=False)
        for hidden in (
                '合成乙', '1.25', 'synthetic-session', 'toolu_synthetic_omit',
                'not-a-member', 'This description stays out of the export.'):
            self.assertNotIn(hidden, rendered)

    def test_span_cap_keeps_the_first_actor_and_counts_the_rest(self):
        document = otel.build_otlp(self.snapshot, max_spans=1)
        spans = _spans(document)
        self.assertEqual([span['name'] for span in spans], ['合成队长'])
        self.assertEqual(_resource_map(document)['team.export.omitted_spans']['intValue'], '5')
        empty = otel.build_otlp(self.snapshot, max_spans=0)
        self.assertEqual(_spans(empty), [])
        self.assertEqual(_resource_map(empty)['team.export.omitted_spans']['intValue'], '6')

    def test_negative_span_limit_is_rejected_without_source_text(self):
        with self.assertRaises(otel.ExportError) as raised:
            otel.build_otlp({'version': 'SECRET'}, max_spans=-1)
        self.assertNotIn('SECRET', str(raised.exception))


class OtelSelectionTest(unittest.TestCase):
    def test_open_actor_cost_is_not_copied_onto_a_new_span(self):
        document = otel.build_otlp({
            'members': [{
                'name': '甲',
                'status': 'running',
                'startedAt': 1700000000,
                'endedAt': None,
                'cost': None,
                'totalTokens': 3,
            }],
        })
        self.assertEqual(_spans(document), [])
        rendered = json.dumps(document)
        self.assertNotIn('team.cost.usd', rendered)
        self.assertNotIn('team.tokens.total', rendered)

    def test_null_cost_stays_absent_and_rows_are_not_summed(self):
        document = otel.build_otlp({
            'members': [
                {
                    'name': '甲',
                    'status': 'completed',
                    'startedAt': 1700000000,
                    'endedAt': 1700000010,
                    'cost': None,
                    'totalTokens': 3,
                },
                {
                    'name': '乙',
                    'status': 'completed',
                    'startedAt': 1700000000,
                    'endedAt': 1700000020,
                    'cost': 1.5,
                    'totalTokens': 4,
                },
            ],
        })
        spans = _spans(document)
        self.assertEqual([span['name'] for span in spans], ['甲', '乙'])
        self.assertNotIn('team.cost.usd', _attr_map(spans[0]))
        self.assertEqual(_double(spans[1], 'team.cost.usd'), 1.5)
        self.assertNotIn('team.totals.cost', _resource_map(document))
        rendered = json.dumps(document)
        self.assertNotIn('4.5', rendered)
        self.assertEqual(rendered.count('1.5'), 1)

    def test_invalid_cost_and_reversed_interval_are_omitted(self):
        document = otel.build_otlp({
            'members': [{
                'name': '甲',
                'status': 'completed',
                'startedAt': 1700000020,
                'endedAt': 1700000010,
                'cost': 'SECRET',
                'totalTokens': True,
            }],
        })
        self.assertEqual(_spans(document), [])
        self.assertNotIn('SECRET', json.dumps(document))

    def test_feed_wins_over_timeline_and_an_empty_feed_does_not_fall_back(self):
        timeline = {
            'name': '甲',
            'startedAt': 1700000000,
            'endedAt': 1700000050,
            'status': 'completed',
            'timeline': [{
                'name': 'Bash',
                'label': 'TIMELINE_ONLY',
                'ts': 1700000010,
                'ms': False,
                'done': True,
                'error': False,
            }],
        }
        from_timeline = otel.build_otlp({'members': [timeline]})
        spans = _spans(from_timeline)
        self.assertEqual([span['name'] for span in spans], ['甲', 'TIMELINE_ONLY'])
        self.assertEqual(spans[1]['parentSpanId'], spans[0]['spanId'])
        self.assertEqual(spans[1]['startTimeUnixNano'], spans[1]['endTimeUnixNano'])
        self.assertEqual(_string(spans[1], 'team.tool.name'), 'Bash')
        self.assertEqual(spans[1]['status'], {'code': 1})
        empty_feed = otel.build_otlp({'members': [timeline], 'feed': []})
        self.assertEqual([span['name'] for span in _spans(empty_feed)], ['甲'])

    def test_merged_hooks_are_skipped_and_ids_stay_out(self):
        snapshot = {
            'members': [{
                'id': 'synthetic-member-1',
                'name': '甲',
                'startedAt': 1700000000,
                'endedAt': 1700000100,
                'status': 'completed',
            }],
            'live': {
                'merged': False,
                'events': [{
                    'ts': 1700000005,
                    'event': 'PreToolUse',
                    'tool_name': 'Read',
                    'session_id': 'synthetic-session',
                    'tool_use_id': 'toolu_should_not_export',
                    'agent_id': 'synthetic-member-1',
                }],
            },
        }
        document = otel.build_otlp(snapshot)
        hook = _spans(document)[1]
        self.assertEqual(hook['name'], 'PreToolUse Read')
        self.assertEqual(_string(hook, 'team.actor'), '甲')
        self.assertEqual(hook['parentSpanId'], _spans(document)[0]['spanId'])
        rendered = json.dumps(document)
        self.assertNotIn('synthetic-session', rendered)
        self.assertNotIn('synthetic-member-1', rendered)
        self.assertNotIn('toolu_should_not_export', rendered)
        snapshot['live']['merged'] = True
        merged = otel.build_otlp(snapshot)
        self.assertEqual([span['name'] for span in _spans(merged)], ['甲'])

    def test_unknown_test_does_not_become_ok_and_budget_ratio_is_copied(self):
        document = otel.build_otlp({
            'budget': {'enabled': True, 'ratio': 1.5, 'lowerBoundLabel': 'SECRET'},
            'feed': [{
                'ts': 1700000000,
                'label': 'npm test',
                'done': True,
                'error': False,
                'test': {
                    'framework': 'sk-12345678',
                    'status': 'unknown',
                    'passed': None,
                    'truncated': True,
                },
            }],
        })
        span = _spans(document)[0]
        self.assertNotIn('status', span)
        self.assertNotIn('team.test.framework', _attr_map(span))
        self.assertEqual(_string(span, 'team.test.status'), 'unknown')
        self.assertIs(_attr_map(span)['team.test.truncated']['boolValue'], True)
        self.assertEqual(_resource_map(document)['team.budget.utilization_ratio']['doubleValue'], 1.5)
        self.assertNotIn('SECRET', json.dumps(document))
        disabled = otel.build_otlp({'budget': {'enabled': False, 'ratio': 0.2}})
        self.assertNotIn('team.budget.utilization_ratio', _resource_map(disabled))
        invalid = otel.build_otlp({'budget': {'enabled': True, 'ratio': 'SECRET'}})
        self.assertNotIn('team.budget.utilization_ratio', _resource_map(invalid))
        self.assertNotIn('SECRET', json.dumps(invalid))

    def test_aggregate_snapshot_has_totals_and_no_invented_spans(self):
        document = otel.build_otlp(_load(ROOT / 'examples' / 'snapshot.synthetic.json'))
        self.assertEqual(_spans(document), [])
        resource = _resource_map(document)
        self.assertEqual(resource['team.member_count']['intValue'], '2')
        self.assertEqual(resource['team.totals.cost']['doubleValue'], 0.012)
        self.assertEqual(resource['team.totals.members_cost']['doubleValue'], 0.008)
        self.assertEqual(resource['team.usage.all.total']['intValue'], '750')
        self.assertNotIn('service.version', resource)
        self.assertNotIn('team.snapshot.demo', resource)
        rendered = json.dumps(document)
        self.assertNotIn('completed', rendered)
        self.assertNotIn('running', rendered)

    def test_non_object_snapshot_is_rejected_without_its_value(self):
        with self.assertRaises(otel.ExportError) as raised:
            otel.build_otlp(['SECRET'])
        self.assertIn('JSON object', str(raised.exception))
        self.assertNotIn('SECRET', str(raised.exception))


class OtelPrivacyTest(unittest.TestCase):
    def test_planted_secrets_are_redacted_before_the_file_is_written(self):
        snapshot = {
            'version': 'sk-12345678',
            'members': [{
                'name': '/home/ada/a',
                'dept': 'sk-proj-synthetickey',
                'startedAt': 1700000000,
                'endedAt': 1700000010,
                'cost': 0.5,
                'status': 'completed',
                'prompt': 'PRIVATE_OTLP_MARKER sk-ant-api03-TAILSHOULDNOTREMAIN9',
                'description': 'DESCRIPTION_ONLY_MARKER',
            }],
            'feed': [{
                'ts': 1700000002,
                'name': 'sk-proj-synthetickey',
                'label': 'Read /Users/ada/b and C:\\Users\\ada\\c and D:/Users/ada/d and ~/ada/e',
                'done': True,
                'error': True,
            }],
            'comms': [{
                'ts': 1700000003,
                'kind': 'report',
                'fromName': 'ada.person@example.com',
                'toName': '合成甲',
                'text': 'send synthetic.person@example.com sk-proj-synthetickey ghp_synthetictoken',
            }],
            'officialTeam': {'tasks': [{
                'subject': 'See /home/ada/a',
                'description': 'DESCRIPTION_ONLY_MARKER sk-ant-api03-TAILSHOULDNOTREMAIN9',
                'stateSince': 1700000004,
            }]},
            'live': {'events': [{
                'ts': 1700000005,
                'event': 'Stop',
                'session_id': 'synthetic-session',
                'tool_use_id': 'toolu_should_not_export',
            }]},
        }
        text = otel.render_otlp(snapshot)
        self.assertIsNone(SECRET.search(text))
        for hidden in (
                'PRIVATE_OTLP_MARKER', 'TAILSHOULDNOTREMAIN9', 'DESCRIPTION_ONLY_MARKER',
                '/home/ada', '/Users/ada', 'Users\\ada', 'synthetic.person',
                'ada.person', 'sk-proj', 'sk-ant', 'sk-12345678', 'ghp_',
                'synthetic-session', 'toolu_should_not_export', '~/ada'):
            self.assertNotIn(hidden, text)
        self.assertIn('[path]', text)
        self.assertIn('[email]', text)
        self.assertIn('[redacted]', text)
        document = json.loads(text)
        names = [span['name'] for span in _spans(document)]
        self.assertIn('member', names)
        self.assertNotIn('/home/ada/a', names)
        actor = _spans(document)[0]
        self.assertEqual(_double(actor, 'team.cost.usd'), 0.5)
        self.assertEqual(_string(actor, 'team.actor.kind'), 'member')
        self.assertNotIn('team.actor', _attr_map(actor))


class OtelDemoTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = _load(ROOT / 'examples' / 'board-demo.synthetic.json')

    def test_demo_keeps_closed_member_cost_and_drops_private_fields(self):
        blob = json.dumps(self.snapshot, sort_keys=True)
        document = otel.build_otlp(self.snapshot)
        self.assertEqual(blob, json.dumps(self.snapshot, sort_keys=True))
        _assert_shape(self, document)
        text = otel.render_otlp(self.snapshot)
        self.assertIsNone(SECRET.search(text))
        for hidden in (
                'synthetic-session', 'synthetic-member', 'synthetic-official', 'tool-demo',
                'toolu_', '__lead', 'session-syntheti', 'wt-synthetic', 'huhu-plan',
                'synthetic-model', '检查界面和团队协作流程', '合成演示：展示团队成员和指标',
                'Official files have no status clock', '0.6684', '0.005'):
            self.assertNotIn(hidden, text)
        self.assertIn('示例验收记录已回报', text)
        self.assertIn('0.7084', text)
        frontend = None
        for member in self.snapshot['members']:
            if member['name'] == '演示前端':
                frontend = member
        actor = None
        read_event = None
        npm_event = None
        for span in _spans(document):
            if _string(span, 'team.actor.kind') == 'member' and _string(span, 'team.actor') == '演示前端':
                actor = span
            if span['name'] == 'Read · synthetic example' and _string(span, 'team.actor') == '演示前端':
                read_event = span
            if span['name'] == 'Bash · npm test' and _string(span, 'team.actor') == '演示前端':
                npm_event = span
        self.assertIsNotNone(actor)
        self.assertEqual(_double(actor, 'team.cost.usd'), frontend['cost'])
        self.assertEqual(actor['startTimeUnixNano'], str(frontend['startedAt'] * 1000000000))
        self.assertEqual(actor['endTimeUnixNano'], str(frontend['endedAt'] * 1000000000))
        self.assertEqual(read_event['parentSpanId'], actor['spanId'])
        self.assertNotIn('parentSpanId', npm_event)
        kinds = [
            span for span in _spans(document)
            if _string(span, 'team.actor.kind') == 'member' and _string(span, 'team.actor') == '演示后端'
        ]
        self.assertEqual(kinds, [])
        self.assertEqual(
            _resource_map(document)['team.totals.cost']['doubleValue'],
            self.snapshot['totals']['cost'])
        self.assertEqual(
            _resource_map(document)['team.usage.all.total']['intValue'],
            str(self.snapshot['totals']['allUsage']['total']))


class OtelCliTest(unittest.TestCase):
    def test_demo_writes_utf8_lf_and_does_not_open_a_url(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch('sys.stdout', stdout), mock.patch('sys.stderr', stderr):
            with mock.patch('urllib.request.urlopen', side_effect=AssertionError('network')):
                code = otel.main(['--demo'])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), '')
        text = stdout.getvalue()
        self.assertIn('演示前端', text)
        self.assertNotIn('synthetic-session', text)
        self.assertTrue(text.endswith('\n'))

        class Out(object):
            def __init__(self):
                self.buffer = io.BytesIO()

        out = Out()
        with mock.patch('sys.stdout', out), mock.patch('sys.stderr', io.StringIO()):
            code = otel.main(['--input', str(FIXTURE)])
        self.assertEqual(code, 0)
        body = out.buffer.getvalue()
        self.assertTrue(body.endswith(b'\n'))
        self.assertNotIn(b'\r\n', body)
        self.assertEqual(body.decode('utf-8'), otel.render_otlp(_load(FIXTURE)))

    def test_output_file_and_same_file_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'traces.json'
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch('sys.stdout', stdout), mock.patch('sys.stderr', stderr):
                code = otel.main(['--input', str(FIXTURE), '--output', str(dest)])
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(dest.read_text(encoding='utf-8'), otel.render_otlp(_load(FIXTURE)))
            before = dest.read_text(encoding='utf-8')
            stderr = io.StringIO()
            with mock.patch('sys.stderr', stderr):
                code = otel.main(['--input', str(dest), '--output', str(dest)])
            self.assertEqual(code, 1)
            self.assertIn('output must differ', stderr.getvalue())
            self.assertEqual(dest.read_text(encoding='utf-8'), before)
            missing = Path(tmp) / 'missing' / 'traces.json'
            stderr = io.StringIO()
            with mock.patch('sys.stderr', stderr):
                code = otel.main(['--demo', '--output', str(missing)])
            self.assertEqual(code, 1)
            self.assertIn('cannot write', stderr.getvalue())
            self.assertFalse(missing.exists())

    def test_invalid_json_does_not_echo_the_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'snap.json'
            path.write_text('{"SECRET":', encoding='utf-8')
            stderr = io.StringIO()
            with mock.patch('sys.stderr', stderr):
                code = otel.main(['--input', str(path)])
            self.assertEqual(code, 1)
            self.assertIn('otel export failed', stderr.getvalue())
            self.assertNotIn('SECRET', stderr.getvalue())

    def test_selectors_require_port_and_are_forwarded(self):
        with self.assertRaises(SystemExit) as raised:
            with mock.patch('sys.stderr', io.StringIO()):
                otel.main(['--demo', '--session', 'abc'])
        self.assertEqual(raised.exception.code, 2)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'traces.json'
            with mock.patch.object(otel, 'read_snapshot', return_value={'demo': False}) as read:
                with mock.patch('sys.stderr', io.StringIO()):
                    code = otel.main([
                        '--port', '7788', '--project', 'C--demo', '--output', str(dest)])
            self.assertEqual(code, 0)
            read.assert_called_once_with(None, 7788, session=None, project='C--demo')
            self.assertIn('team.snapshot.demo', dest.read_text(encoding='utf-8'))
