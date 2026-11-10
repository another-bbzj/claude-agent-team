"""Synthetic mixed-source snapshots and imported media HTTP integration."""
import argparse
import base64
import json
import os
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from test_board import BoardEnv, jl, png_sheet


ROOT_ID = '00000000-0000-4000-8000-000000000001'
CHILD_ID = '00000000-0000-4000-8000-000000000002'
STAMP = '2026-11-01T02:00:00Z'


def row(kind, payload):
    return {'timestamp': STAMP, 'type': kind, 'payload': payload}


class CodexBoardTest(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict(os.environ)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.fixture = BoardEnv()
        self.addCleanup(self.fixture.cleanup)
        self.board = self.fixture.load()
        self.board.TEAM_CFG['pricing_usd_per_mtok'] = {}
        self.sessions = self.board.CODEX_SESSIONS / '2026/11/01'
        self.sessions.mkdir(parents=True)
        self.args = argparse.Namespace(session=None, project=None, stale=600, source='all')
        self.write_rollout(ROOT_ID, 'gpt-fixture-main', None, 100, 60, 20)
        self.write_rollout(CHILD_ID, 'gpt-fixture-worker', ROOT_ID, 50, 20, 5)

    def write_rollout(self, identity, model, parent, inputs, cached, outputs):
        meta = {'id': identity, 'cwd': str(self.fixture.tmp / 'synthetic-repo'), 'source': 'vscode'}
        if parent:
            meta['source'] = {'subagent': {'thread_spawn': {'parent_thread_id': parent, 'agent_role': 'backend-dev',
                                                          'agent_nickname': 'Synthetic Worker', 'agent_path': '/root/worker'}}}
        usage = {'input_tokens': inputs, 'cached_input_tokens': cached, 'output_tokens': outputs,
                 'total_tokens': inputs + outputs}
        path = self.sessions / ('rollout-' + identity + '.jsonl')
        path.write_text(jl(
            row('session_meta', meta),
            row('turn_context', {'model': model, 'effort': 'medium'}),
            row('event_msg', {'type': 'task_started'}),
            row('response_item', {'type': 'message', 'role': 'developer',
                                  'content': [{'type': 'input_text', 'text': 'INTERNAL_DO_NOT_DISPLAY'}]}),
            row('response_item', {'type': 'message', 'role': 'user',
                                  'content': [{'type': 'input_text', 'text': 'Synthetic user question'}]}),
            row('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                                  'content': [{'type': 'output_text', 'text': 'Synthetic visible answer'}]}),
            row('event_msg', {'type': 'token_count', 'info': {'total_token_usage': usage}}),
            row('token_usage_record', {'thread_token_usage': usage}),
            row('event_msg', {'type': 'task_complete'}),
        ), encoding='utf-8')

    def test_codex_discovery_and_snapshot_actual_models(self):
        self.args.source = 'codex'
        result = self.board.build_snapshot(self.args)
        self.assertEqual(result['source'], 'codex')
        self.assertEqual(result['lead']['name'], 'Codex')
        self.assertEqual(result['lead']['model'], 'gpt-fixture-main')
        self.assertEqual(len(result['members']), 1)
        self.assertEqual(result['members'][0]['model'], 'gpt-fixture-worker')
        self.assertEqual(result['members'][0]['definedModel'], '')
        self.assertEqual(result['totals']['allUsage']['total'], 175)
        self.assertIsNone(result['lead']['cost'])
        self.assertFalse(result['officialTeam']['available'])
        self.assertNotIn('INTERNAL_DO_NOT_DISPLAY', json.dumps(result['conversation']))
        self.assertEqual([r['role'] for r in result['conversation'] if r['agentId'] == '__lead'], ['user', 'assistant'])

    def test_claude_source_and_provider_qualified_selection(self):
        self.args.source = 'claude'
        result = self.board.build_snapshot(self.args)
        self.assertEqual(result['lead']['name'], 'Claude')
        self.assertTrue(all(s['source'] == 'claude' for s in result['sessions']))
        self.assertTrue(result['conversation'])
        self.args.session = 'codex:' + ROOT_ID
        result = self.board.build_snapshot(self.args)
        self.assertEqual(result['session']['id'], ROOT_ID)
        self.assertEqual(result['lead']['name'], 'Codex')

    def test_codex_snapshot_contains_recorded_participant_messages(self):
        for sender, target, text in ((ROOT_ID, CHILD_ID, 'Review interface'), (CHILD_ID, ROOT_ID, 'Interface checked')):
            path = self.sessions / ('rollout-' + sender + '.jsonl')
            with path.open('a', encoding='utf-8') as stream:
                stream.write(jl(row('response_item', {'type': 'function_call', 'name': 'send_input',
                    'call_id': 'send-' + sender, 'arguments': json.dumps({'id': target, 'message': text})})))
        self.args.source = 'codex'
        result = self.board.build_snapshot(self.args)
        messages = [m for m in result['comms'] if m.get('source') == 'codex']
        self.assertEqual(len(messages), 2)
        self.assertEqual({(m['from'], m['to']) for m in messages}, {('__lead', CHILD_ID), (CHILD_ID, '__lead')})
        self.assertTrue(all(m['delivery'] == 'unconfirmed' for m in messages))

    def test_codex_only_home_and_empty_filter(self):
        self.board.PROJECTS = self.fixture.tmp / 'no-claude-home'
        self.args.source = 'codex'
        self.assertIsNotNone(self.board.build_snapshot(self.args)['session'])
        self.args.source = 'claude'
        self.assertIsNone(self.board.build_snapshot(self.args)['session'])

    def start_http(self):
        self.board.Handler.args = self.args
        self.board.Handler._cache = (0, None)
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.board.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), worker.join(timeout=3)))
        return 'http://127.0.0.1:' + str(server.server_port)

    def test_http_source_queries_do_not_reuse_wrong_provider_cache(self):
        url = self.start_http()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for source in ('claude', 'codex', 'claude'):
            with opener.open(url + '/snapshot.json?source=' + source, timeout=5) as response:
                result = json.load(response)
            self.assertEqual(result['source'], source)
        with self.assertRaises(urllib.error.HTTPError) as error:
            opener.open(url + '/snapshot.json?source=unsupported', timeout=5)
        self.assertEqual(error.exception.code, 400)

    def test_import_video_serve_range_clear_and_failed_import_preserves_config(self):
        video = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42'
        first = self.board.write_backdrop({'data': base64.b64encode(video).decode(), 'name': 'synthetic.mp4'})
        self.assertEqual(first['kind'], 'video')
        self.assertTrue(first['enabled'])
        config = (self.board.BACKDROPS / 'backdrop.json').read_bytes()
        with self.assertRaises(ValueError):
            self.board.write_backdrop({'data': base64.b64encode(b'not a video').decode(), 'name': 'bad.mp4'})
        self.assertEqual(config, (self.board.BACKDROPS / 'backdrop.json').read_bytes())
        url = self.start_http()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(url + first['url'], headers={'Range': 'bytes=0-7'})
        with opener.open(request, timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), video[:8])
        request = urllib.request.Request(url + first['url'], method='HEAD')
        with opener.open(request, timeout=5) as response:
            self.assertEqual(response.headers['Content-Length'], str(len(video)))
        self.board.write_backdrop({'clear': True})
        self.assertFalse(self.board.backdrop_state()['enabled'])

    def test_import_project_preview_and_invalid_settings_are_nonmutating(self):
        folder = self.fixture.tmp / 'synthetic-wallpaper'
        folder.mkdir()
        (folder / 'project.json').write_text(json.dumps({'type': 'scene', 'title': 'Synthetic Scene'}))
        (folder / 'preview.png').write_bytes(png_sheet(20, 20))
        result = self.board.write_backdrop({'path': str(folder)})
        self.assertEqual(result['kind'], 'image')
        self.assertTrue(result['note'])
        before = (self.board.BACKDROPS / 'backdrop.json').read_bytes()
        with self.assertRaises(ValueError):
            self.board.write_backdrop({'path': str(folder), 'fit': 'bad'})
        self.assertEqual(before, (self.board.BACKDROPS / 'backdrop.json').read_bytes())

    def test_variant_rollout_fills_task_board_without_private_tool_text(self):
        source = Path(__file__).resolve().parents[1] / 'examples/codex-sessions.synthetic/variants'
        for path in source.glob('*.jsonl'):
            shutil.copy2(str(path), str(self.sessions / path.name))
        self.args.source = 'codex'
        self.args.session = 'variant-root'
        result = self.board.build_snapshot(self.args)
        self.assertEqual(result['session']['id'], 'variant-root')
        official = result['officialTeam']
        self.assertTrue(official['available'])
        self.assertEqual(official['teamName'], 'codex')
        self.assertEqual(official['lead'], 'Codex')
        self.assertIn('不是 Claude 官方任务目录', ' '.join(official['warnings']))
        self.assertEqual([task['subject'] for task in official['tasks']], [
            'Record the synthetic plan', 'Map the synthetic tool',
            'Check the synthetic plan', 'Leave the synthetic note',
        ])
        self.assertIsNone(official['tasks'][0].get('owner'))
        self.assertEqual(official['tasks'][2]['ownerMatch'], 'Synthetic Checker')
        tool_names = [row['name'] for row in result['lead']['timeline'] if row.get('kind') == 'tool']
        self.assertEqual(tool_names, ['update_plan', 'shell', 'exec_command', 'apply_patch', 'web_search', 'docs/search'])
        labels = [row['label'] for row in result['feed']]
        self.assertEqual(labels.count('python --version'), 2)
        self.assertIn('update_plan', labels)
        self.assertIn('docs/search', labels)
        self.assertTrue(any(label.startswith('apply_patch') for label in labels))
        self.assertIsNone(result['lead']['cost'])
        blob = json.dumps(result)
        for marker in ('SHELL_OUTPUT_SHOULD_NOT_SURFACE', 'DIFF_SHOULD_NOT_SURFACE',
                       'ARG_SHOULD_NOT_SURFACE', 'gAAAAA-synthetic-ciphertext'):
            self.assertNotIn(marker, blob)
        self.args.source = 'claude'
        self.args.session = None
        claude = self.board.build_snapshot(self.args)
        self.assertEqual(claude['lead']['name'], 'Claude')
        self.assertNotEqual(claude['officialTeam'].get('teamName'), 'codex')

    def test_priced_codex_model_estimates_cost(self):
        self.board.TEAM_CFG['pricing_usd_per_mtok'] = {
            'gpt-5': {'in': 1.25, 'out': 10, 'cache_read': 0.125, 'cache_write': 1.25},
        }
        identity = '00000000-0000-4000-8000-0000000000aa'
        usage = {'input_tokens': 800000, 'cached_input_tokens': 0, 'output_tokens': 0}
        path = self.sessions / ('rollout-' + identity + '.jsonl')
        path.write_text(jl(
            row('session_meta', {'id': identity, 'cwd': 'demo-project', 'source': 'cli'}),
            row('turn_context', {'model': 'gpt-5'}),
            row('token_usage_record', {'thread_token_usage': usage}),
        ), encoding='utf-8')
        self.args.source = 'codex'
        self.args.session = identity
        result = self.board.build_snapshot(self.args)
        self.assertEqual(result['lead']['model'], 'gpt-5')
        self.assertEqual(result['lead']['usage']['in'], 800000)
        self.assertEqual(result['lead']['cost'], 1.0)
        self.assertFalse(result['officialTeam']['available'])


if __name__ == '__main__':
    unittest.main()
