"""Exercise the real control server without executing Wayland startup/re-exec."""
import ast
import socket
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

source = Path(__file__).resolve().parents[1] / 'crosshaird.py'
tree = ast.parse(source.read_text())
server_node = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == 'ControlServer')
namespace = {'socket': socket, 'threading': threading, 'Path': Path, 'GLib': Mock()}
exec(compile(ast.Module(body=[server_node], type_ignores=[]), str(source), 'exec'), namespace)
ControlServer = namespace['ControlServer']


class ControlServerTests(unittest.TestCase):
    def test_idle_and_unknown_clients_do_not_break_later_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'control.sock'
            app = Mock()
            server = ControlServer(path, app)
            server.start()
            try:
                with socket.socket(socket.AF_UNIX) as idle:
                    idle.connect(str(path))
                    with socket.socket(socket.AF_UNIX) as client:
                        client.settimeout(2)
                        client.connect(str(path))
                        client.sendall(b'not-a-command')
                        self.assertEqual(client.recv(16), b'error\n')
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(2)
                    client.connect(str(path))
                    client.sendall(b'reload')
                    self.assertEqual(client.recv(16), b'ok\n')
                namespace['GLib'].idle_add.assert_called_with(app.handle_command, 'reload')
            finally:
                server.stop()
