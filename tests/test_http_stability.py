"""Regression tests for HTTP input validation and invalid config reloads."""
import io
import json
import os
import unittest
from email.message import Message
from unittest.mock import Mock, patch

from test_board import BoardEnv


class HttpInputStabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environ = patch.dict(os.environ)
        cls.environ.start()
        cls.env = BoardEnv()
        cls.tb = cls.env.load()

    @classmethod
    def tearDownClass(cls):
        cls.env.cleanup()
        cls.environ.stop()

    def request(self, body, path='/api/update/apply', length=None):
        handler = self.tb.Handler.__new__(self.tb.Handler)
        handler.path = path
        handler.headers = Message()
        handler.headers['Content-Length'] = str(len(body) if length is None else length)
        handler.rfile = io.BytesIO(body)
        handler._json = Mock()
        with patch.object(self.tb, 'reload_team_cfg') as reload_cfg, \
                patch.object(self.tb, 'update_apply', return_value={'ok': True}) as apply:
            handler.do_POST()
        return handler._json, reload_cfg, apply

    def test_non_object_json_is_rejected_before_any_side_effect(self):
        for path in ('/api/agents', '/api/bus/send', '/api/update/apply', '/api/shutdown'):
            for value in (None, [], [1], 'text', 3, True):
                with self.subTest(path=path, payload=value), patch.object(self.tb.threading, 'Timer') as timer:
                    response, reload_cfg, apply = self.request(json.dumps(value).encode('utf-8'), path)
                    self.assertEqual(response.call_args[0][1], 400)
                    self.assertIn('error', response.call_args[0][0])
                    reload_cfg.assert_not_called()
                    apply.assert_not_called()
                    timer.assert_not_called()

    def test_truncated_json_object_is_rejected(self):
        response, reload_cfg, apply = self.request(b'{}', length=10)
        self.assertEqual(response.call_args[0][1], 400)
        reload_cfg.assert_not_called()
        apply.assert_not_called()

    def test_valid_object_and_empty_body_keep_existing_behavior(self):
        for body in (b'{}', b''):
            with self.subTest(body=body):
                response, reload_cfg, apply = self.request(body)
                reload_cfg.assert_called_once_with()
                apply.assert_called_once_with()
                response.assert_called_once_with({'ok': True})

    def _post_cross_origin(self, body, length=None, origin='http://evil.example'):
        events = []

        class RecordingReader(io.BytesIO):
            def read(self, n=-1):
                chunk = super().read(n)
                events.append(('read', n, len(chunk)))
                return chunk

        class RecordingWriter(io.BytesIO):
            def write(self, data):
                events.append(('write', len(data)))
                return super().write(data)

            def flush(self):
                events.append(('flush',))
                return super().flush()

        handler = self.tb.Handler.__new__(self.tb.Handler)
        handler.path = '/api/bus/send'
        handler.headers = Message()
        handler.headers['Content-Length'] = str(len(body) if length is None else length)
        handler.headers['Origin'] = origin
        handler.rfile = RecordingReader(body)
        handler.wfile = RecordingWriter()
        handler.request_version = 'HTTP/1.1'
        handler.requestline = 'POST /api/bus/send HTTP/1.1'
        handler.do_POST()
        return handler, events

    def test_cross_origin_drains_body_before_403_and_flushes(self):
        body = b'{"from":"user","to":"lead","text":"origin"}'
        handler, events = self._post_cross_origin(body)
        raw = handler.wfile.getvalue()
        kinds = [event[0] for event in events]
        self.assertIn(b' 403 ', raw)
        self.assertIn('拒绝跨站请求'.encode('utf-8'), raw)
        self.assertLess(kinds.index('read'), kinds.index('write'))
        self.assertLess(kinds.index('write'), kinds.index('flush'))
        self.assertEqual(sum(event[2] for event in events if event[0] == 'read'), len(body))
        self.assertFalse(any(event[0] == 'read' and event[1] < 0 for event in events))

    def test_cross_origin_skips_negative_or_huge_content_length(self):
        body = b'{"a":1}'
        for length in (-1, self.tb.PET_MAX_BYTES * 2 + 1, 'nope'):
            with self.subTest(length=length):
                handler, events = self._post_cross_origin(body, length=length)
                self.assertIn(b' 403 ', handler.wfile.getvalue())
                self.assertFalse(any(event[0] == 'read' for event in events))
                self.assertIn('flush', [event[0] for event in events])
                self.assertEqual(handler.rfile.read(), body)

    def test_invalid_reload_preserves_last_valid_config(self):
        tb = self.tb
        config = json.loads(json.dumps(tb.TEAM_CFG))
        last_mtime = tb._team_cfg_mtime
        path = tb.HERE / 'team.json'
        original = path.read_bytes()
        try:
            for invalid in ('{', 'null', '[]', '"text"'):
                with self.subTest(config=invalid):
                    path.write_text(invalid, encoding='utf-8')
                    tb._team_cfg_mtime = -1
                    tb.reload_team_cfg()
                    self.assertEqual(tb.TEAM_CFG, config)
                    self.assertEqual(tb._team_cfg_mtime, -1)
            updated = dict(config, maintenance_test=True)
            path.write_text(json.dumps(updated), encoding='utf-8')
            tb.reload_team_cfg()
            self.assertEqual(tb.TEAM_CFG, updated)
        finally:
            path.write_bytes(original)
            tb.TEAM_CFG.clear()
            tb.TEAM_CFG.update(config)
            tb._team_cfg_mtime = last_mtime


if __name__ == '__main__':
    unittest.main()
