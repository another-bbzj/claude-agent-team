"""Loopback startup must not wait for system reverse-DNS resolution."""
import os
import socket
import unittest
from unittest import mock

from test_board import BoardEnv


class LoopbackServerTest(unittest.TestCase):
    def test_server_binds_and_listens_without_reverse_dns(self):
        with mock.patch.dict(os.environ):
            env = BoardEnv()
            try:
                board = env.load()
                # An unavailable resolver must not affect the local-only server.
                with mock.patch.object(socket, 'getfqdn', side_effect=OSError('resolver unavailable')) as dns:
                    with board.LoopbackHTTPServer(('127.0.0.1', 0), board.Handler) as server:
                        self.assertEqual(server.server_name, 'localhost')
                        self.assertEqual(server.server_port, server.server_address[1])
                        self.assertGreater(server.server_port, 0)
                        self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1)
                    dns.assert_not_called()
            finally:
                env.cleanup()


if __name__ == '__main__':
    unittest.main()
