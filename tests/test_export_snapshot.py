"""Exporter privacy, board compatibility and offline/loopback CLI regressions."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.request
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path
from unittest import mock

from test_board import BoardEnv

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('snapshot_exporter', ROOT / 'tools' / 'export_snapshot.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class ExportMetricsTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = json.loads((ROOT / 'examples' / 'snapshot.synthetic.json').read_text(encoding='utf-8'))

    def test_real_board_snapshot_is_supported(self):
        with mock.patch.dict(os.environ):
            env = BoardEnv()
            try:
                board = env.load()
                snapshot = board.build_snapshot(board.argparse.Namespace(session=None, project=None, stale=600))
                output = exporter.export_metrics(snapshot)
                self.assertEqual(output['memberCount'], len(snapshot['members']))
                self.assertEqual(output['totals']['allUsage']['total'], snapshot['totals']['allUsage']['total'])
                self.assertEqual(output['totals']['cost'], snapshot['totals']['cost'])
            finally:
                env.cleanup()

    def test_unknown_fields_and_private_values_never_escape(self):
        expected = exporter.export_metrics(self.snapshot)
        self.snapshot['session'] = {'id': 'SECRET_SESSION', 'cwd': 'SECRET_PATH'}
        self.snapshot['comms'] = [{'text': 'SECRET_MESSAGE'}]
        self.snapshot['members'][0].update(name='SECRET_NAME', prompt='SECRET_PROMPT')
        self.snapshot['totals'].update(modelMix={'SECRET_MODEL': 3}, unpricedModels=['SECRET_MODEL'], error='SECRET_ERROR')
        self.snapshot['totals']['allUsage']['nested'] = {'token': 'SECRET_TOKEN'}
        result = exporter.export_metrics(self.snapshot)
        self.assertEqual(result, expected)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(set(result), {'schemaVersion', 'memberCount', 'totals'})

    def test_bad_shape_and_numbers_fail_with_safe_diagnostic(self):
        bad = [None, [], {}, {'totals': {}, 'members': 'SECRET'}]
        for value in (True, None, 'SECRET', -1, float('nan'), float('inf')):
            item = copy.deepcopy(self.snapshot)
            item['totals']['cost'] = value
            bad.append(item)
        for field, value in (('in', True), ('out', 1.5), ('cacheHit', 1.1), ('total', -1)):
            item = copy.deepcopy(self.snapshot)
            item['totals']['allUsage'][field] = value
            bad.append(item)
        for item in bad:
            with self.subTest(item=item), self.assertRaises(exporter.ExportError) as raised:
                exporter.export_metrics(item)
            self.assertNotIn('SECRET', str(raised.exception))

    def test_zero_members_and_zero_usage_are_valid(self):
        self.snapshot['members'] = []
        self.assertEqual(exporter.export_metrics(self.snapshot)['memberCount'], 0)


class ReadAndCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='snapshot-export-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = ROOT / 'examples' / 'snapshot.synthetic.json'

    def test_cli_stdout_and_output_file_are_valid_json(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(exporter.main(['--input', str(self.source)]), 0)
        metrics = json.loads(output.getvalue())
        target = self.root / 'metrics.json'
        self.assertEqual(exporter.main(['--input', str(self.source), '--output', str(target)]), 0)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), metrics)

    def test_cli_input_failure_does_not_echo_payload_or_traceback(self):
        source = self.root / 'bad.json'
        for content in (b'{"SECRET_PAYLOAD":', b'\xff', b'null', b'{"SECRET": NaN}'):
            source.write_bytes(content)
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(exporter.main(['--input', str(source)]), 1)
            self.assertIn('export failed:', error.getvalue())
            self.assertNotIn('SECRET', error.getvalue())
            self.assertNotIn('Traceback', error.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(exporter.main(['--input', str(self.root / 'missing.json')]), 1)
        self.assertIn('cannot read snapshot', error.getvalue())

    def test_source_cannot_be_overwritten(self):
        source = self.root / 'source.json'
        source.write_bytes(self.source.read_bytes())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(exporter.main(['--input', str(source), '--output', str(source)]), 1)
        self.assertEqual(source.read_bytes(), self.source.read_bytes())

    def test_hardlink_output_cannot_overwrite_source(self):
        source, target = self.root / 'source.json', self.root / 'alias.json'
        source.write_bytes(self.source.read_bytes())
        try:
            os.link(str(source), str(target))
        except OSError:
            self.skipTest('hardlinks unavailable on this filesystem')
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(exporter.main(['--input', str(source), '--output', str(target)]), 1)
        self.assertIn('output must differ', error.getvalue())
        self.assertEqual(source.read_bytes(), self.source.read_bytes())

    def test_file_identity_failure_stops_before_write_and_is_sanitized(self):
        target = self.root / 'existing.json'
        target.write_bytes(b'keep existing output')
        with mock.patch.object(Path, 'samefile', side_effect=OSError('SECRET_PATH')), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(exporter.main(['--input', str(self.source), '--output', str(target)]), 1)
        self.assertEqual(target.read_bytes(), b'keep existing output')
        self.assertNotIn('SECRET', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())

    def test_invalid_cli_source_selection_and_ports(self):
        for args in ([], ['--port', '0'], ['--port', '65536'], ['--port', 'not-a-port'],
                     ['--input', str(self.source), '--port', '7788']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                exporter.main(args)
            self.assertEqual(raised.exception.code, 2)

    def test_bound_applies_to_file_and_http_streams(self):
        with mock.patch.object(exporter, 'MAX_BYTES', 3):
            with self.assertRaisesRegex(exporter.ExportError, 'limit'):
                exporter.read_snapshot(self.source)
            response = io.BytesIO(b'1234')
            with mock.patch.object(exporter.urllib.request, 'build_opener') as build:
                build.return_value.open.return_value = response
                with self.assertRaisesRegex(exporter.ExportError, 'limit'):
                    exporter.read_snapshot(port=7788)

    def test_local_http_bypasses_proxies_and_uses_fixed_endpoint_and_timeout(self):
        response = io.BytesIO(self.source.read_bytes())
        with mock.patch.object(exporter.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = response
            result = exporter.read_snapshot(port=9000)
        handlers = build.call_args.args
        self.assertIsInstance(handlers[0], urllib.request.ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], exporter.NoRedirect)
        build.return_value.open.assert_called_once_with('http://127.0.0.1:9000/snapshot.json', timeout=5)
        self.assertIsInstance(result['totals'], dict)

    def test_redirects_rejected_including_local_destinations(self):
        for target in ('https://example.invalid/private', 'http://127.0.0.1:7788/other'):
            with self.subTest(target=target), self.assertRaises(exporter.ExportError):
                exporter.NoRedirect().redirect_request(None, None, 302, 'Moved', {}, target)

    def test_http_errors_are_sanitized_and_ports_validated(self):
        with mock.patch.object(exporter.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = OSError('SECRET')
            with self.assertRaises(exporter.ExportError) as raised:
                exporter.read_snapshot(port=7788)
        self.assertNotIn('SECRET', str(raised.exception))
        for port in (0, 65536, True, '7788'):
            with self.subTest(port=port), self.assertRaises(exporter.ExportError):
                exporter.read_snapshot(port=port)

    def test_http_protocol_errors_are_sanitized_by_cli(self):
        for exception in (BadStatusLine('SECRET_RESPONSE'), IncompleteRead(b'SECRET_RESPONSE', 100)):
            for location in ('open', 'read'):
                with self.subTest(exception=type(exception).__name__, location=location), \
                        mock.patch.object(exporter.urllib.request, 'build_opener') as build, \
                        contextlib.redirect_stderr(io.StringIO()) as error:
                    if location == 'open':
                        build.return_value.open.side_effect = exception
                    else:
                        build.return_value.open.return_value.__enter__.return_value.read.side_effect = exception
                    self.assertEqual(exporter.main(['--port', '7788']), 1)
                self.assertIn('cannot read local board', error.getvalue())
                self.assertNotIn('SECRET', error.getvalue())
                self.assertNotIn('Traceback', error.getvalue())


if __name__ == '__main__':
    unittest.main()
