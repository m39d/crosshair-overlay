#!/usr/bin/env python3
"""
crosshair_common — shared helpers for crosshaird, crosshairctl, and
crosshair-gui.

Deliberately dependency-light (stdlib + cairo only at import time; GTK
bits are imported lazily inside the functions that need them) so this
module can be imported by the settings GUI without pulling in the
daemon's layer-shell-specific startup behavior (LD_PRELOAD relaunch,
forced GDK_BACKEND=wayland) — that logic only makes sense for the real
overlay surface, not a plain settings window.

Install this file alongside crosshaird.py / crosshairctl.py /
crosshair-gui.py (e.g. in ~/.local/bin/) — Python automatically adds a
script's own directory to sys.path, so no packaging is needed for the
import to work.
"""

import base64
import hashlib
import json
import math
import os
import socket as _socket
import sys
import tomllib
from pathlib import Path

import cairo

CONFIG_DIR = Path.home() / ".config" / "crosshair-overlay"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.toml"
DEFAULT_SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "crosshair-overlay.sock"
IMPORTED_IMAGES_DIR = CONFIG_DIR / "imported-images"

CONTROL_COMMANDS = ("toggle", "show", "hide", "reload", "quit")

DEFAULT_CONFIG = {
    "crosshair": {
        "shape": "cross",       # cross | dot | circle (ignored if image is set)
        "size": 24,             # bounding box, px (also scaled size of a custom image)
        "thickness": 2,
        "gap": 4,
        "color": "#00FF00",
        "opacity": 0.85,
        "output": "",           # empty = compositor default output
        "offset_x": 0,          # px from screen center; 0,0 = dead center
        "offset_y": 0,
        "image": "",            # path to a custom crosshair image; overrides shape
    },
    "daemon": {
        "start_visible": True,
    },
    # GUI-only bookkeeping for the export/import round trip -- crosshaird.py
    # never reads this section (it only ever looks up cfg["crosshair"] and
    # cfg["daemon"] keys), so nothing here can affect the running overlay.
    # Included in DEFAULT_CONFIG (rather than left undocumented) so it's
    # always visibly written out to config.toml, as a discoverable knob:
    # setting keep_rel_offset by hand skips the resolution-mismatch prompt
    # crosshair-gui shows when importing a config exported for a different
    # monitor_res.
    "import": {
        "monitor_res": "",     # "WIDTHxHEIGHT" the offsets below were exported for; "" = n/a
        "keep_rel_offset": "",  # "" (ask) | "raw" (keep exact pixels) | "scaled" (rescale to this monitor)
    },
}


# --- config load/save -------------------------------------------------------

def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    cfg = {section: dict(values) for section, values in DEFAULT_CONFIG.items()}
    if path.exists():
        try:
            with open(path, "rb") as f:
                user_cfg = tomllib.load(f)
            for section, values in user_cfg.items():
                cfg.setdefault(section, {}).update(values)
        except Exception as exc:  # noqa: BLE001 - keep running with defaults
            sys.stderr.write(f"Warning: failed to parse {path}: {exc}\n")
    return cfg


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))  # JSON string syntax is valid TOML basic-string syntax


def parse_monitor_res(value: str):
    """Parse a "WIDTHxHEIGHT" string (as stored in [import] monitor_res)
    into an (int, int) tuple, or None if it's empty or malformed.

    Deliberately lenient about malformed input (returns None rather than
    raising) since this reads a value that may have been hand-edited.
    """
    if not value:
        return None
    try:
        w_str, h_str = str(value).lower().split("x", 1)
        return (int(w_str), int(h_str))
    except (ValueError, AttributeError):
        return None


def format_monitor_res(width: int, height: int) -> str:
    return f"{int(width)}x{int(height)}"


def dump_toml(cfg: dict) -> str:
    """Minimal TOML serializer for our flat [section] key=value structure.

    No external dependency needed (tomllib is read-only in the stdlib);
    this covers exactly the shapes of data we ever write: sections of
    str/int/float/bool values, nothing nested deeper than that.
    """
    lines = []
    for section, values in cfg.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def save_config(cfg: dict, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_toml(cfg))


# --- color/rendering ---------------------------------------------------------

def hex_to_rgba(hex_color: str, alpha: float):
    """Parse a "#RRGGBB" string into a cairo-style (r, g, b, alpha) tuple.

    Falls back to green on anything malformed -- wrong length *or*
    non-hex characters (e.g. a hand-edited config.toml with "#GGGGGG")
    -- rather than letting int(..., 16) raise ValueError out of what's
    normally a GTK draw callback. Confirmed via a real config.toml with
    an invalid color: pre-fix this crashed render_crosshair() with an
    uncaught ValueError every time the preview/overlay redrew; post-fix
    it just renders green, same as any other malformed color value.
    """
    hex_color = (hex_color or "").lstrip("#")
    if len(hex_color) != 6:
        return (0.0, 1.0, 0.0, alpha)
    try:
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
    except ValueError:
        return (0.0, 1.0, 0.0, alpha)
    return (r, g, b, alpha)


def render_crosshair(ctx, width, height, crosshair_cfg: dict, pixbuf=None, clear=True):
    """Draw the crosshair into a cairo context.

    Shared by the daemon's real overlay surface and the GUI's live
    preview, so the settings window always shows exactly what the
    actual overlay looks like.

    `clear`: if True (the daemon's case), the target is first cleared
    to fully transparent — required so the real overlay has no opaque
    background behind it. The GUI preview paints its own opaque dark
    backdrop first and passes clear=False, since clearing to
    transparent there would just punch a hole showing whatever's
    behind the preview widget instead of a nice contrasting box.
    """
    if clear:
        ctx.save()
        ctx.set_operator(cairo.OPERATOR_CLEAR)
        ctx.paint()
        ctx.restore()

    opacity = float(crosshair_cfg.get("opacity", 0.85))

    if pixbuf is not None:
        import gi

        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk

        img_w, img_h = pixbuf.get_width(), pixbuf.get_height()
        x = (width - img_w) / 2.0
        y = (height - img_h) / 2.0
        Gdk.cairo_set_source_pixbuf(ctx, pixbuf, x, y)
        ctx.paint_with_alpha(opacity)
        return

    shape = crosshair_cfg.get("shape", "cross")
    thickness = float(crosshair_cfg.get("thickness", 2))
    gap = float(crosshair_cfg.get("gap", 4))
    color = hex_to_rgba(crosshair_cfg.get("color", "#00FF00"), opacity)

    ctx.set_source_rgba(*color)
    ctx.set_line_width(thickness)
    cx, cy = width / 2.0, height / 2.0
    half = min(width, height) / 2.0

    if shape == "dot":
        ctx.arc(cx, cy, thickness * 1.5, 0, 2 * math.pi)
        ctx.fill()
    elif shape == "circle":
        ctx.arc(cx, cy, max(1.0, half - thickness), 0, 2 * math.pi)
        ctx.stroke()
    else:  # cross (default)
        ctx.move_to(cx - half, cy)
        ctx.line_to(cx - gap, cy)
        ctx.move_to(cx + gap, cy)
        ctx.line_to(cx + half, cy)
        ctx.move_to(cx, cy - half)
        ctx.line_to(cx, cy - gap)
        ctx.move_to(cx, cy + gap)
        ctx.line_to(cx, cy + half)
        ctx.stroke()


def load_pixbuf(image_path, size, warn=True):
    """Load and scale an image for use as a crosshair.

    Returns a GdkPixbuf.Pixbuf, or None if loading failed (missing
    file, unsupported format, corrupt data, etc).
    """
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    expanded = str(Path(image_path).expanduser())
    size = max(4, int(size))
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(expanded)
        if pixbuf.get_width() != size or pixbuf.get_height() != size:
            pixbuf = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
        return pixbuf
    except Exception as exc:  # noqa: BLE001
        if warn:
            sys.stderr.write(f"Warning: could not load crosshair image '{expanded}': {exc}\n")
        return None


# --- portable config bundles (embeds a custom image as base64) --------------

def encode_image_bundle(image_path: Path) -> dict:
    data = image_path.read_bytes()
    return {
        "filename": image_path.name,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def decode_image_bundle(bundle: dict, dest_dir: Path = IMPORTED_IMAGES_DIR) -> Path:
    """Decode an embedded-image bundle from an imported config.toml.

    `filename` comes from a file the user imported -- possibly shared by
    someone else -- so it's untrusted input, and prior to this function
    a "../"-laden filename here looked like a path-traversal bug: naive
    string-building (dest_dir / f"{digest}-{filename}") plus a Path
    containing "/" characters, and Path.resolve() will happily collapse
    those ".." segments right out of the intended imported-images/ dir.

    In practice it wasn't exploitable, because dest_dir/f"{digest}-..."
    glues the digest onto filename with no separating "/". That turns
    the first path segment into something like "a1b2c3d4-.." -- a
    literal, nonexistent directory name, not the special ".." parent
    reference -- and write_bytes()'s underlying open() call has to
    successfully descend into each real directory component before it
    can act on a ".." after it. Since "a1b2c3d4-.." never exists on
    disk, that descent fails with FileNotFoundError before any real
    traversal occurs; confirmed empirically against this exact code
    with several ".."-based payloads, none of which escaped dest_dir.

    Sanitizing here anyway, as defense-in-depth rather than an active
    fix: today's safety is an accident of *how* the digest and filename
    happen to be concatenated, not something the code asserts on
    purpose. A future change as small as adding a "/" separator for
    readability, switching to os.path.join, or restructuring the digest
    scheme would silently reintroduce a real traversal bug. Path(...)
    .name strips any directory components regardless of how the rest of
    this function evolves, and the containment check is a second,
    independent guard against the same class of bug.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    raw_filename = bundle.get("filename") or "imported-crosshair.png"
    filename = Path(raw_filename).name or "imported-crosshair.png"
    raw = base64.b64decode(bundle["data_base64"])
    digest = hashlib.sha256(raw).hexdigest()[:8]
    dest = (dest_dir / f"{digest}-{filename}").resolve()
    if dest_dir.resolve() not in dest.parents:
        raise ValueError(f"invalid filename in imported bundle: {raw_filename!r}")
    if not dest.exists():
        dest.write_bytes(raw)
    return dest


# --- control socket -----------------------------------------------------------

def send_control_command(command: str, socket_path: Path = DEFAULT_SOCKET_PATH) -> bool:
    """Send a command to a running crosshaird over its Unix control socket.

    Returns True on success, False if the daemon isn't reachable (not
    running, stale socket, etc) — never raises.
    """
    if not socket_path.exists():
        return False
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(socket_path))
            sock.sendall(command.encode("utf-8"))
            reply = sock.recv(16)
            return reply.strip() == b"ok"
    except OSError:
        return False
