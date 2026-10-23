"""Prometheus textfile format, privacy and failure-safe publication."""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_doctor import SnapshotServer
from test_export_snapshot import exporter

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'examples' / 'snapshot.synthetic.json'
GOLDEN = ROOT / 'examples' / 'prometheus.synthetic.prom'


class PrometheusFormatTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = json.loads(SOURCE.read_text(encoding='utf-8'))

    def test_synthetic_example_and_metric_contract(self):
        result = exporter.render_prometheus(self.snapshot)
        self.assertEqual(result, GOLDEN.read_text(encoding='utf-8'))
        lines = result.splitlines()
        self.assertTrue(result.endswith('\n'))
        self.assertNotIn('\r', result)
        types = [line for line in lines if line.startswith('# TYPE ')]
        self.assertEqual(len(types), 5)
        self.assertTrue(all(line.endswith(' gauge') for line in types))
        samples = [line for line in lines if not line.startswith('#')]
        self.assertEqual(len(samples), 22)
        self.assertEqual(len(samples), len({line.rsplit(' ', 1)[0] for line in samples}))
        for sample in samples:
            self.assertRegex(sample, r'^claude_agent_team_[a-z_]+(?:\{[^}]+\})? [0-9.eE+\-]+$')
        self.assertIn('claude_agent_team_tokens{scope="all",kind="total"} 750', samples)
        self.assertIn('claude_agent_team_tokens{scope="members",kind="output"} 200', samples)

    def test_private_fields_cannot_add_names_labels_or_help(self):
        expected = exporter.render_prometheus(self.snapshot)
        self.snapshot['session'] = {'id': 'PRIVATE_SESSION', 'cwd': 'PRIVATE_PATH'}
        self.snapshot['comms'] = [{'text': 'PRIVATE_MESSAGE\nmalicious_metric 1'}]
        self.snapshot['members'][0].update(name='PRIVATE_NAME', prompt='PRIVATE_PROMPT')
        self.snapshot['totals']['modelMix'] = {'PRIVATE_MODEL': 1}
        self.snapshot['totals']['usage']['PRIVATE_FIELD'] = 'injected_label="value"'
        self.assertEqual(exporter.render_prometheus(self.snapshot), expected)

    def test_invalid_numbers_are_rejected_before_rendering(self):
        for value in (True, -1, float('nan'), float('inf'), 'PRIVATE_VALUE'):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['totals']['cost'] = value
            with self.subTest(value=value), self.assertRaises(exporter.ExportError) as error:
                exporter.render_prometheus(snapshot)
            self.assertNotIn('PRIVATE_VALUE', str(error.exception))

    def test_usage_and_cost_can_decrease_between_snapshots(self):
        # Selected-session snapshots are gauges, not process-lifetime counters.
        self.snapshot['members'] = []
        self.snapshot['totals'].update(cost=0, membersCost=0, unpriced=2)
        for group in ('usage', 'leadUsage', 'allUsage'):
            self.snapshot['totals'][group] = dict.fromkeys(self.snapshot['totals'][group], 0)
        result = exporter.render_prometheus(self.snapshot)
        self.assertIn('claude_agent_team_members 0\n', result)
        self.assertIn('claude_agent_team_estimated_cost_usd{scope="all"} 0\n', result)
        self.assertIn('claude_agent_team_unpriced_entries 2\n', result)
        self.assertNotIn(' counter', result)

    def test_unbounded_json_integers_cannot_overflow_prometheus_samples(self):
        self.snapshot['totals']['allUsage']['total'] = 10 ** 400
        # JSON can represent this integer, but Prometheus samples are float64.
        self.assertEqual(exporter.export_metrics(self.snapshot)['totals']['allUsage']['total'], 10 ** 400)
        with self.assertRaisesRegex(exporter.ExportError, 'finite numeric range'):
            exporter.render_prometheus(self.snapshot)


class PrometheusCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='prometheus-export-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'team.prom'

    def args(self, *extra):
        return ['--input', str(SOURCE), '--format', 'prometheus'] + list(extra)

    def test_cli_subprocess_stdout_and_file(self):
        command = [sys.executable, str(ROOT / 'tools' / 'export_snapshot.py')] + self.args()
        result = subprocess.run(command, capture_output=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, GOLDEN.read_bytes())
        self.assertEqual(result.stderr, b'')
        self.assertEqual(exporter.main(self.args('--output', str(self.target))), 0)
        self.assertEqual(self.target.read_bytes(), result.stdout)

    def test_live_input_bypasses_proxy_and_matches_offline_output(self):
        server = SnapshotServer(SOURCE.read_bytes())
        self.addCleanup(server.close)
        with mock.patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:9', 'HTTP_PROXY': 'http://127.0.0.1:9'}), \
                mock.patch.object(exporter.urllib.request, 'proxy_bypass', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(exporter.main(['--port', str(server.port), '--format', 'prometheus']), 0)
        self.assertEqual(output.getvalue(), GOLDEN.read_text(encoding='utf-8'))

    def test_invalid_input_preserves_previous_textfile_and_reports_safe_error(self):
        source = self.root / 'bad.json'
        source.write_text('{"PRIVATE_PAYLOAD":', encoding='utf-8')
        self.target.write_text('previous_metric 1\n', encoding='utf-8')
        with contextlib.redirect_stderr(io.StringIO()) as error:
            result = exporter.main(['--input', str(source), '--format', 'prometheus',
                                    '--output', str(self.target)])
        self.assertEqual(result, 1)
        self.assertEqual(self.target.read_text(encoding='utf-8'), 'previous_metric 1\n')
        self.assertNotIn('PRIVATE', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())

    def test_replacement_failure_preserves_previous_output_for_both_formats(self):
        for format_name in ('json', 'prometheus'):
            self.target.write_text('keep previous output', encoding='utf-8')
            with self.subTest(format=format_name), \
                    mock.patch.object(exporter.os, 'replace', side_effect=OSError('PRIVATE_PATH')), \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(exporter.main(['--input', str(SOURCE), '--format', format_name,
                                                '--output', str(self.target)]), 1)
            self.assertEqual(self.target.read_text(encoding='utf-8'), 'keep previous output')
            self.assertEqual(list(self.root.iterdir()), [self.target])
            self.assertIn('cannot write metrics output', error.getvalue())
            self.assertNotIn('PRIVATE', error.getvalue())

    def test_publication_is_atomic_and_temporary_file_is_not_a_prom_file(self):
        self.target.write_text('previous_metric 1\n', encoding='utf-8')
        replace = os.replace

        def publish(source, target):
            self.assertNotEqual(Path(source).suffix, '.prom')
            self.assertEqual(Path(source).parent, self.target.parent)
            self.assertEqual(Path(source).read_bytes(), GOLDEN.read_bytes())
            self.assertEqual(self.target.read_text(encoding='utf-8'), 'previous_metric 1\n')
            replace(source, target)

        with mock.patch.object(exporter.os, 'replace', side_effect=publish) as publish_mock:
            self.assertEqual(exporter.main(self.args('--output', str(self.target))), 0)
        publish_mock.assert_called_once()
        self.assertEqual(list(self.root.iterdir()), [self.target])
        self.assertEqual(self.target.read_bytes(), GOLDEN.read_bytes())

    def test_source_and_hardlink_alias_cannot_be_overwritten(self):
        source = self.root / 'source.json'
        source.write_bytes(SOURCE.read_bytes())
        alias = self.root / 'alias.prom'
        try:
            os.link(str(source), str(alias))
        except OSError:
            self.skipTest('hardlinks unavailable on this filesystem')
        for target in (source, alias):
            with self.subTest(target=target), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(exporter.main(['--input', str(source), '--format', 'prometheus',
                                                '--output', str(target)]), 1)
            self.assertEqual(source.read_bytes(), SOURCE.read_bytes())

    def test_missing_output_directory_does_not_create_output(self):
        target = self.root / 'missing' / 'team.prom'
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(exporter.main(self.args('--output', str(target))), 1)
        self.assertFalse(target.exists())
        self.assertNotIn('Traceback', error.getvalue())

    @unittest.skipIf(os.name == 'nt', 'POSIX file mode semantics')
    def test_existing_file_permissions_are_preserved(self):
        self.target.write_text('previous_metric 1\n', encoding='utf-8')
        self.target.chmod(0o640)
        self.assertEqual(exporter.main(self.args('--output', str(self.target))), 0)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)

    def test_unknown_format_is_an_argument_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            exporter.main(['--input', str(SOURCE), '--format', 'unknown'])
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
