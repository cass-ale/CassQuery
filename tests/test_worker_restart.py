import importlib.util
from pathlib import Path
import socket
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import worker


class WorkerRestartTests(unittest.TestCase):
    def test_connected_websocket_listener_can_restart_on_same_pair(self):
        http, server = worker.bind_servers(53000)
        port = http.server_port
        client = socket.create_connection(('127.0.0.1', port + 1000))
        peer, _ = server.accept()
        peer.sendall(b'connection exercised')
        peer.shutdown(socket.SHUT_WR)
        client.recv(100)
        peer.close()
        client.close()
        server.close()
        http.server_close()
        restarted_http, restarted_ws = worker.bind_servers(port)
        try:
            self.assertEqual(restarted_http.server_port, port)
            self.assertEqual(restarted_ws.getsockname()[1], port + 1000)
        finally:
            restarted_http.server_close()
            restarted_ws.close()
