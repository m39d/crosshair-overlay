#!/usr/bin/env python3
"""crosshairctl — control CLI for the crosshair-overlay daemon (crosshaird).

Bind this to a KDE custom shortcut (System Settings -> Shortcuts ->
Custom Shortcuts) rather than trying to register a global hotkey inside
the daemon itself — Wayland compositors don't allow arbitrary clients
to grab global keys, so this is the sanctioned path (see guide, 3.2).

Example custom-shortcut command:
    /home/you/.local/bin/crosshairctl.py toggle
"""

import argparse
import sys
from pathlib import Path

from crosshair_common import CONTROL_COMMANDS, DEFAULT_SOCKET_PATH, send_control_command


def main():
    parser = argparse.ArgumentParser(description="Control the crosshair-overlay daemon")
    parser.add_argument("command", choices=CONTROL_COMMANDS)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    args = parser.parse_args()

    ok = send_control_command(args.command, args.socket)
    if not ok:
        sys.stderr.write(
            f"Error: could not reach crosshaird via {args.socket}\n"
            "Is it running? Start it with `crosshaird`, or open "
            "`crosshair-gui` which starts it automatically.\n"
        )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
