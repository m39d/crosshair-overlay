#!/usr/bin/env python3
"""
crosshaird — native Wayland crosshair overlay daemon.

Target: any Wayland compositor implementing wlr-layer-shell (tested on
KDE Plasma/KWin and Hyprland). Uses the wlr-layer-shell protocol (via
GTK4 + gtk4-layer-shell) so the overlay is a real compositor-managed
surface, not an X11-style always-on-top hack.

Design follows the build guide:
  - layer = overlay, keyboard-interactivity = none (never steals focus)
  - empty input region (click-through: mouse events pass to the game)
  - no in-process global hotkeys; controlled via a Unix-socket channel
    that a KDE custom shortcut (or any script) can hit via crosshairctl
  - config file at ~/.config/crosshair-overlay/config.toml

See README.md for dependency install / build / test instructions.
"""

import argparse
import ctypes.util
import glob
import os
import signal
import socket
import sys
import threading
from pathlib import Path

from crosshair_common import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_SOCKET_PATH,
    load_config,
    load_pixbuf,
    render_crosshair,
)

# --- Fix for a documented gtk4-layer-shell gotcha -------------------------
# If gtk4-layer-shell's .so ends up linked/loaded after libwayland-client,
# layer surface init fails at runtime with warnings like "Failed to
# initialize layer surface ... may have been linked after libwayland" and
# every LayerShell call becomes a no-op — the window silently stays a
# normal top-level window (taskbar entry, focus behavior, needs "keep
# above", wrong positioning). Upstream's own fix is LD_PRELOAD-ing the
# library, but LD_PRELOAD has to be set before the process starts, so we
# detect this case and re-exec ourselves once with it set, rather than
# requiring it to be set by hand every launch (including under systemd/
# autostart where that's awkward).
#
# These hardcoded paths only cover the common FHS/Debian/Arch layouts —
# they miss non-x86_64 multiarch triplets and anything installed to a
# fully custom prefix outside LD_LIBRARY_PATH. Kept only as a
# last-resort fallback below; the linker-cache and LD_LIBRARY_PATH
# lookups cover most other cases (including NixOS) without needing to
# enumerate paths by hand.
_LAYER_SHELL_LIB_CANDIDATES = [
    "/usr/lib/libgtk4-layer-shell.so*",
    "/usr/lib64/libgtk4-layer-shell.so*",
    "/usr/lib/x86_64-linux-gnu/libgtk4-layer-shell.so*",
    "/usr/local/lib/libgtk4-layer-shell.so*",
    "/usr/local/lib64/libgtk4-layer-shell.so*",
]


def _find_layer_shell_lib():
    override = os.environ.get("GTK4_LAYER_SHELL_PATH")
    if override and Path(override).exists():
        return override

    # ctypes.util.find_library asks the platform's own dynamic linker
    # (ldconfig's cache on Linux) where the library lives. This is
    # distro- and architecture-agnostic -- it works the same on Fedora,
    # Debian/arm64, Arch, etc. without us needing to know their layout.
    found = ctypes.util.find_library("gtk4-layer-shell")
    if found:
        # On Linux this is typically a bare soname (e.g.
        # "libgtk4-layer-shell.so.0") rather than an absolute path, since
        # it's read out of ldconfig's cache rather than the filesystem.
        # That's fine for LD_PRELOAD: the dynamic linker resolves it via
        # the same cache/search path it used to report it here.
        return found

    # ldconfig's cache is a dead end on NixOS: packages live under
    # hashed, unpredictable /nix/store/<hash>-.../lib paths and are
    # deliberately never registered globally. What Nix *does* do instead
    # is populate LD_LIBRARY_PATH with the relevant store paths whenever
    # you're inside a nix-shell/flake devShell that depends on the
    # package -- so search those directories too. This also incidentally
    # covers anyone who's manually exported LD_LIBRARY_PATH to point at a
    # from-source build.
    for lib_dir in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep):
        if not lib_dir:
            continue
        matches = sorted(glob.glob(os.path.join(lib_dir, "libgtk4-layer-shell.so*")))
        if matches:
            return matches[0]

    for pattern in _LAYER_SHELL_LIB_CANDIDATES:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


if "_CROSSHAIRD_RELAUNCHED" not in os.environ:
    lib_path = _find_layer_shell_lib()
    if lib_path:
        env = os.environ.copy()
        existing_preload = env.get("LD_PRELOAD", "")
        env["LD_PRELOAD"] = (
            f"{lib_path}:{existing_preload}" if existing_preload else lib_path
        )
        env["_CROSSHAIRD_RELAUNCHED"] = "1"
        os.execvpe(sys.executable, [sys.executable] + sys.argv, env)
    else:
        sys.stderr.write(
            "Warning: could not auto-locate libgtk4-layer-shell.so to "
            "LD_PRELOAD it.\n"
            "If you see 'Failed to initialize layer surface ... linked "
            "after libwayland' warnings,\n"
            "find it with `pacman -Ql gtk4-layer-shell | grep '\\.so'` and "
            "either set\n"
            "GTK4_LAYER_SHELL_PATH=/path/to/libgtk4-layer-shell.so, or add "
            "its directory\nto the glob list near the top of this file.\n"
        )
# ---------------------------------------------------------------------------

# Must happen before GTK opens a display connection. Without this, GTK's
# default backend order can silently pick X11/XWayland instead of native
# Wayland depending on how the process was launched (terminal, dock,
# systemd unit, etc). On X11 there is no layer-shell protocol, so
# gtk4-layer-shell's init_for_window() degrades *silently* to a normal
# top-level window instead of erroring — which looks exactly like:
# taskbar entry, focus-dependent appearance, needing "keep above" from
# the WM, and not staying above fullscreen games. Forcing wayland here
# turns that silent fallback into a loud, fixable startup error instead.
os.environ.setdefault("GDK_BACKEND", "wayland")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402
import cairo  # noqa: E402

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402
except (ValueError, ImportError) as exc:
    sys.stderr.write(
        "Fatal: gtk4-layer-shell GObject introspection typelib not found.\n"
        "Install gtk4-layer-shell first (see README.md, 'Dependencies').\n"
        f"Underlying error: {exc}\n"
    )
    sys.exit(1)


class CrosshairWindow(Gtk.Window):
    _CSS = b"""
    window.crosshair-overlay-window {
        background-color: transparent;
    }
    drawingarea.crosshair-overlay-canvas {
        background-color: transparent;
    }
    /* GTK4 still renders a window drop-shadow via the 'decoration' CSS
       node even when set_decorated(False) removes the titlebar. That
       shadow has a fixed blur radius and sits relative to the window's
       edges, so it doesn't scale with content but does shift position
       whenever the window size changes -- exactly the faint gray blobs
       at the corners of the overlay. Kill it explicitly. */
    window.crosshair-overlay-window,
    window.crosshair-overlay-window decoration {
        box-shadow: none;
        border: none;
        outline: none;
        margin: 0;
        padding: 0;
    }
    """

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.set_decorated(False)
        self.set_resizable(False)
        self.add_css_class("crosshair-overlay-window")
        self._install_transparent_css()

        LayerShell.init_for_window(self)
        LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self, "crosshair-overlay")

        # No edge anchors -> gtk4-layer-shell centers the surface on the
        # target output. This is the "size + position directly" approach
        # the guide calls out in section 2's protocol table, rather than
        # anchoring to all four edges.
        for edge in (
            LayerShell.Edge.TOP,
            LayerShell.Edge.BOTTOM,
            LayerShell.Edge.LEFT,
            LayerShell.Edge.RIGHT,
        ):
            LayerShell.set_anchor(self, edge, False)

        # Don't reserve screen space like a panel would.
        LayerShell.set_exclusive_zone(self, -1)

        # Critical: never take keyboard focus from the game underneath.
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)

        offset_x = int(cfg["crosshair"].get("offset_x", 0))
        offset_y = int(cfg["crosshair"].get("offset_y", 0))
        if offset_x or offset_y:
            # For a non-centered position, anchor top-left and use margins.
            LayerShell.set_anchor(self, LayerShell.Edge.LEFT, True)
            LayerShell.set_anchor(self, LayerShell.Edge.TOP, True)
            LayerShell.set_margin(self, LayerShell.Edge.LEFT, offset_x)
            LayerShell.set_margin(self, LayerShell.Edge.TOP, offset_y)

        output_name = cfg["crosshair"].get("output", "")
        if output_name:
            self._bind_output(output_name)

        size = max(4, int(cfg["crosshair"].get("size", 24)))
        self.set_default_size(size, size)

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.add_css_class("crosshair-overlay-canvas")
        self.drawing_area.set_content_width(size)
        self.drawing_area.set_content_height(size)
        self.drawing_area.set_draw_func(self._draw, None)
        self.set_child(self.drawing_area)

        self.pixbuf = None
        self._load_image(cfg)

        self.connect("realize", self._on_realize)

    def _install_transparent_css(self):
        # Without this, GTK4 paints the theme's default window background
        # (typically an opaque dark/black square under KDE's dark theme)
        # *before* our draw_func runs, which is exactly the black box
        # visible behind the vector crosshair. This makes both the window
        # and the drawing area's background fully transparent so only the
        # crosshair itself is opaque.
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(self._CSS)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _load_image(self, cfg: dict):
        """Load+scale a custom crosshair image if `image` is set in config.

        Any raster format GdkPixbuf supports works (PNG, including
        transparency, is the recommended choice for a crosshair).
        """
        image_path = cfg["crosshair"].get("image", "")
        if not image_path:
            self.pixbuf = None
            return
        size = max(4, int(cfg["crosshair"].get("size", 24)))
        self.pixbuf = load_pixbuf(image_path, size)

    def _bind_output(self, output_name: str):
        display = Gdk.Display.get_default()
        if display is None:
            return
        monitors = display.get_monitors()
        for i in range(monitors.get_n_items()):
            monitor = monitors.get_item(i)
            connector = monitor.get_connector() or ""
            if connector == output_name:
                LayerShell.set_monitor(self, monitor)
                return
        sys.stderr.write(
            f"Warning: output '{output_name}' not found (list output/connector "
            "names with `kscreen-doctor -o` on KDE Plasma, or `hyprctl monitors` "
            "on Hyprland); using compositor default.\n"
        )

    def _on_realize(self, *_args):
        # Click-through: an empty input region means the surface receives
        # no pointer events at all, so clicks fall through to whatever is
        # underneath. This mirrors the wl_surface.set_input_region call
        # described in the guide's section 2.
        surface = self.get_surface()
        if surface is None:
            return
        surface.set_input_region(cairo.Region())

    def _draw(self, _area, ctx, width, height, _data):
        render_crosshair(ctx, width, height, self.cfg["crosshair"], self.pixbuf)


class ControlServer:
    """Unix-socket control channel: toggle / show / hide / reload / quit.

    This exists instead of an in-process global hotkey because Wayland
    compositors don't let arbitrary clients grab global keys (guide,
    section 3.2). Bind a KDE custom shortcut to `crosshairctl toggle`.
    """

    def __init__(self, socket_path: Path, app: "CrosshairApp"):
        self.socket_path = socket_path
        self.app = app
        self._sock = None
        self._thread = None
        self._running = False

    def start(self):
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.socket_path))
        self._sock.listen(4)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            with conn:
                data = conn.recv(64).decode("utf-8", "ignore").strip()
                if data:
                    GLib.idle_add(self.app.handle_command, data)
                    try:
                        conn.sendall(b"ok\n")
                    except OSError:
                        pass

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass


def _daemon_already_running(socket_path: Path) -> bool:
    """True if another crosshaird is live and listening on socket_path.

    A stale socket file left behind by a crashed/killed process fails
    to connect (ECONNREFUSED) and is safe to unlink and reuse; only a
    socket with an actual listener on the other end counts as "already
    running". Without this check, starting a second instance (e.g. one
    launched manually plus one from the GUI's "Start Overlay" button)
    silently steals the control socket out from under the first one,
    leaving that first instance's window stuck on screen with no way
    to reach it via crosshairctl/reload/quit -- which looks exactly
    like "closing/hiding the overlay does nothing".
    """
    if not socket_path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(socket_path))
        return True
    except OSError:
        return False


class CrosshairApp(Gtk.Application):
    def __init__(self, config_path: Path, socket_path: Path):
        super().__init__(application_id="io.github.crosshair-overlay")
        self.config_path = config_path
        self.socket_path = socket_path
        self.window: CrosshairWindow | None = None
        self.control_server: ControlServer | None = None
        self.visible = True

    def do_activate(self):
        display = Gdk.Display.get_default()
        backend = type(display).__name__ if display else "none"
        if "Wayland" not in backend:
            sys.stderr.write(
                f"Fatal: connected to display backend '{backend}', not Wayland.\n"
                "The overlay needs a native Wayland connection for layer-shell to\n"
                "work; on X11/XWayland it silently falls back to a normal window\n"
                "(which is what you'd see as: shows in the taskbar, needs "
                "'keep above',\ndoesn't stay over fullscreen apps). "
                "Check: echo $XDG_SESSION_TYPE (expect 'wayland'),\n"
                "and try running with `GDK_BACKEND=wayland ./crosshaird.py` "
                "explicitly to surface\nthe real connection error if there is one.\n"
            )
            self.quit()
            return

        if not LayerShell.is_supported():
            sys.stderr.write(
                "Fatal: compositor does not advertise wlr-layer-shell "
                "(zwlr_layer_shell_v1).\n"
                "Verify with: wayland-info | grep layer_shell\n"
                "This should be present by default on recent KDE Plasma/KWin "
                "and Hyprland sessions;\nif it's missing, update your "
                "compositor or check its config for anything disabling "
                "layer-shell.\n"
            )
            self.quit()
            return

        if _daemon_already_running(self.socket_path):
            sys.stderr.write(
                f"Fatal: crosshaird already appears to be running (control "
                f"socket {self.socket_path} is live).\n"
                "Starting a second instance would silently steal the control "
                "socket, leaving the first\ninstance's window stuck on screen "
                "with no way to hide/reload/quit it via crosshairctl --\n"
                "which is exactly the 'nothing responds' symptom this check "
                "prevents. Use crosshairctl to\ncontrol the existing instance "
                "instead. If you're sure nothing is actually running (e.g. a\n"
                "previous instance crashed without cleaning up), remove the "
                f"stale socket and try again:\n  rm {self.socket_path}\n"
            )
            self.quit()
            return

        cfg = load_config(self.config_path)
        self.visible = bool(cfg["daemon"].get("start_visible", True))
        self._swap_window(cfg)

        self.control_server = ControlServer(self.socket_path, self)
        self.control_server.start()

        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal, sig)

    def _swap_window(self, cfg: dict):
        """(Re)create the overlay window from cfg.

        Layer-shell properties like output/anchor/margin can only be
        set before a surface is realized -- gtk4-layer-shell (and the
        underlying wlr-layer-shell protocol) don't support reassigning
        them on a live surface. So "reload" doesn't try to mutate the
        existing window in place; it builds a fresh one from the new
        config and swaps it in, which guarantees every setting
        (including output and offset, not just size/color/image) is
        freshly and correctly applied every time.
        """
        old_window = self.window
        self.window = CrosshairWindow(cfg)
        self.add_window(self.window)
        if self.visible:
            self.window.present()
        if old_window is not None:
            self.remove_window(old_window)
            old_window.destroy()

    def _on_signal(self, _sig):
        self.handle_command("quit")
        return GLib.SOURCE_REMOVE

    def handle_command(self, command: str):
        command = command.strip().lower()
        if command == "toggle":
            self.visible = not self.visible
            self.window.set_visible(self.visible)
        elif command == "show":
            self.visible = True
            self.window.set_visible(True)
        elif command == "hide":
            self.visible = False
            self.window.set_visible(False)
        elif command == "reload":
            cfg = load_config(self.config_path)
            self._swap_window(cfg)
        elif command == "quit":
            if self.control_server:
                self.control_server.stop()
            self.quit()
        else:
            sys.stderr.write(f"Unknown command: {command}\n")
        return GLib.SOURCE_REMOVE


def main():
    parser = argparse.ArgumentParser(description="Native Wayland crosshair overlay daemon")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    args = parser.parse_args()

    app = CrosshairApp(args.config, args.socket)
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main())
