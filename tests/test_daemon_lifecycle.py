"""Daemon lifecycle tests without creating GTK windows or touching user settings."""

import ast
import io
import signal
import sys
import threading
import socket
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


PROJECT_DIR = Path(__file__).resolve().parent.parent
# Exercise the real class definitions without loading layer-shell or re-execing
# the process. CI needs GTK for the GUI suite, but no compositor or layer-shell.
source = PROJECT_DIR / "crosshaird.py"
tree = ast.parse(source.read_text())
names = {"CrosshairApp", "ControlServer", "_daemon_already_running"}
nodes = [node for node in tree.body
         if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
daemon = ModuleType("crosshaird_test")
daemon.__dict__.update(
    Gtk=SimpleNamespace(Application=object), Gdk=Mock(), LayerShell=Mock(),
    GLib=Mock(), Path=Path, socket=socket, threading=threading,
    signal=signal, sys=sys, load_config=Mock(),
)
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), daemon.__dict__)


class WaylandDisplay:
    """Stand in for the backend checked by do_activate()."""


class DaemonActivationTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(
            config_path=Path("/tmp/crosshair-test/config.toml"),
            socket_path=Path("/tmp/crosshair-test/control.sock"),
            window=None,
            control_server=None,
            visible=True,
            quit=Mock(),
            _on_signal=Mock(),
        )
        self.app._swap_window = Mock(side_effect=self.create_window)
        self.display = self.enterContext(patch.object(
            daemon.Gdk.Display, "get_default", return_value=WaylandDisplay()
        ))
        self.supported = self.enterContext(patch.object(
            daemon.LayerShell, "is_supported", return_value=True
        ))
        self.running = self.enterContext(patch.object(
            daemon, "_daemon_already_running", return_value=False
        ))
        self.load_config = self.enterContext(patch.object(
            daemon, "load_config", return_value={"daemon": {"start_visible": True}}
        ))
        self.server = self.enterContext(patch.object(daemon, "ControlServer"))
        self.signals = self.enterContext(patch.object(daemon.GLib, "unix_signal_add"))
        self.enterContext(patch("sys.stderr", new_callable=io.StringIO))

    def create_window(self, _cfg):
        self.app.window = Mock()

    def test_first_activation_starts_daemon(self):
        daemon.CrosshairApp.do_activate(self.app)

        self.load_config.assert_called_once_with(self.app.config_path)
        self.app._swap_window.assert_called_once_with(self.load_config.return_value)
        self.server.assert_called_once_with(self.app.socket_path, self.app)
        self.server.return_value.start.assert_called_once_with()
        self.assertEqual(self.signals.call_count, 2)
        self.assertTrue(self.app.visible)
        self.app.quit.assert_not_called()

    def test_first_activation_respects_start_hidden(self):
        self.load_config.return_value = {"daemon": {"start_visible": False}}

        daemon.CrosshairApp.do_activate(self.app)

        self.assertFalse(self.app.visible)
        self.server.return_value.start.assert_called_once_with()

    def assert_reactivation_preserves_overlay(self, visible):
        self.load_config.return_value = {"daemon": {"start_visible": visible}}
        daemon.CrosshairApp.do_activate(self.app)
        window = self.app.window
        server = self.app.control_server
        # Once started, probing this path would find our own listener.
        self.running.return_value = True

        for _ in range(3):
            daemon.CrosshairApp.do_activate(self.app)

        self.app.quit.assert_not_called()
        self.assertIs(self.app.window, window)
        self.assertIs(self.app.control_server, server)
        self.assertEqual(self.app.visible, visible)
        window.set_visible.assert_not_called()
        window.present.assert_not_called()
        self.display.assert_called_once_with()
        self.supported.assert_called_once_with()
        self.running.assert_called_once_with(self.app.socket_path)
        self.load_config.assert_called_once_with(self.app.config_path)
        self.app._swap_window.assert_called_once()
        self.server.return_value.start.assert_called_once_with()
        self.assertEqual(self.signals.call_count, 2)

    def test_repeated_activation_preserves_visible_overlay(self):
        self.assert_reactivation_preserves_overlay(True)

    def test_repeated_activation_preserves_hidden_overlay(self):
        self.assert_reactivation_preserves_overlay(False)

    def test_first_activation_refuses_another_live_daemon(self):
        self.running.return_value = True

        daemon.CrosshairApp.do_activate(self.app)

        self.app.quit.assert_called_once_with()
        self.app._swap_window.assert_not_called()
        self.server.assert_not_called()
        self.load_config.assert_not_called()

    def test_first_activation_requires_wayland(self):
        self.display.return_value = None

        daemon.CrosshairApp.do_activate(self.app)

        self.app.quit.assert_called_once_with()
        self.server.assert_not_called()

    def test_first_activation_requires_layer_shell(self):
        self.supported.return_value = False

        daemon.CrosshairApp.do_activate(self.app)

        self.app.quit.assert_called_once_with()
        self.server.assert_not_called()

    def test_controls_work_after_reactivation(self):
        daemon.CrosshairApp.do_activate(self.app)
        self.running.return_value = True
        daemon.CrosshairApp.do_activate(self.app)
        self.app.quit.assert_not_called()

        for command, visible in (("hide", False), ("show", True),
                                 ("toggle", False), ("toggle", True)):
            with self.subTest(command=command, visible=visible):
                daemon.CrosshairApp.handle_command(self.app, command)
                self.assertEqual(self.app.visible, visible)
                self.app.window.set_visible.assert_called_with(visible)

        daemon.CrosshairApp.handle_command(self.app, "hide")
        daemon.CrosshairApp.handle_command(self.app, "reload")
        self.assertFalse(self.app.visible)
        self.app._swap_window.assert_called_with(self.load_config.return_value)

        daemon.CrosshairApp.handle_command(self.app, "quit")
        self.server.return_value.stop.assert_called_once_with()
        self.app.quit.assert_called_once_with()


class DaemonSocketTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "control.sock"

    def test_missing_socket_is_not_running(self):
        self.assertFalse(daemon._daemon_already_running(self.path))

    def test_live_socket_is_running_and_remains_available(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
            listener.listen(4)

            self.assertTrue(daemon._daemon_already_running(self.path))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(str(self.path))

    def test_stale_socket_can_be_replaced_and_cleaned_up(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(self.path))
        self.assertTrue(self.path.exists())
        self.assertFalse(daemon._daemon_already_running(self.path))

        server = daemon.ControlServer(self.path, Mock())
        self.addCleanup(server.stop)
        # Real bind/listen operations, without starting a socket worker thread.
        with patch.object(daemon.threading, "Thread"):
            server.start()

        self.assertTrue(daemon._daemon_already_running(self.path))
        server.stop()
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
