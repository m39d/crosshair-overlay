# Architecture

**This is a high-level summary of how crosshair-overlay works, put together by an AI model to map out what we had in mind when building this. Could be useful if you are trying to debug something, or just out of curiosity. If you want something more specific, all the code is pretty throughly commented with all the details you would need on how and why something was made the way it is.**

## The four files

- `crosshaird.py`. The daemon. This is the actual overlay window.
- `crosshair_common.py`. Config loading/saving, the drawing code, image handling. Used by both the daemon and the GUI.
- `crosshairctl.py`. A CLI that sends one command to the running daemon over a socket.
- `crosshair-gui.py`. The settings window.

`crosshair_common.py` only imports GTK bits inside the functions that need them, not at module load time. That's what lets `crosshair-gui.py` import it without pulling in the daemon's Wayland-specific startup behavior (see below), since a settings window doesn't need any of that.

## Why the daemon is a separate process from the GUI

The overlay has to survive closing the settings window. If you're mid-game and want to nudge the color, you shouldn't lose your crosshair by closing the panel you used to nudge it. So `crosshaird.py` runs as its own process, started once (usually by opening the GUI) and left running. `crosshair-gui.py` talks to it over a Unix socket instead of holding it in-process.

That socket also solves a second problem: Wayland compositors don't let arbitrary applications register global keyboard shortcuts. So there's no in-process hotkey handler. Instead you bind a key in your compositor or DE to run `crosshairctl toggle`, and `crosshairctl` just writes that word to the socket. The daemon's `ControlServer` reads it and calls the matching method (`toggle`, `show`, `hide`, `reload`, `quit`).

`reload` is the one worth understanding: it doesn't mutate the live window. `wlr-layer-shell` doesn't support changing anchor, output, or margin on a surface after it's realized, so instead the daemon builds a brand new `CrosshairWindow` from the current config and swaps it in, destroying the old one. That guarantees every setting gets applied fresh on every reload, not just the ones layer-shell would let you change live.

## Making the overlay actually behave like an overlay

Three properties define whether this thing works as a crosshair and not just an annoying floating window.

It has to stay on top of a fullscreen game. `crosshaird.py` uses `gtk4-layer-shell` to turn the GTK window into a real `wlr-layer-shell` surface on the `OVERLAY` layer, which is a compositor-level concept, not a window manager hint like "always on top." That's the difference between this and the X11-era hacks that just set a window flag and hope.

It has to never take keyboard focus. Layer-shell surfaces set `keyboard-interactivity`, and this one sets it to `NONE`. If it didn't, opening the overlay could steal keystrokes from the game running underneath it.

It has to be click-through. On realize, the code sets an empty input region on the surface (`surface.set_input_region(cairo.Region())`), so the surface receives no pointer events at all and clicks pass straight through to whatever's under it.

None of this works over XWayland. `zwlr_layer_shell_v1` is a native Wayland protocol, so `crosshaird.py` forces `GDK_BACKEND=wayland` before GTK opens a display connection, and checks the actual display backend at startup, failing loudly if it's not Wayland rather than silently falling back to a normal top-level window.

## The LD_PRELOAD relaunch

There's a known `gtk4-layer-shell` issue: if its shared library ends up loaded after `libwayland-client`, every layer-shell call becomes a no-op and you get a normal window with no error. Upstream's fix is to `LD_PRELOAD` the library, but that has to be set before the process starts, which is awkward for something launched from a desktop entry or systemd unit.

`crosshaird.py` works around this by finding the library itself (checking a manual override, then `ctypes.util.find_library` via the linker cache, then `LD_LIBRARY_PATH` for Nix-style setups, then a short list of common FHS paths as a last resort) and re-executing itself once with `LD_PRELOAD` set, using an environment variable (`_CROSSHAIRD_RELAUNCHED`) to make sure it only does this once.

## Drawing

`render_crosshair()` in `crosshair_common.py` is the one function that draws a crosshair, and both the daemon's live overlay and the GUI's preview panel call it. That's deliberate: the preview should always show exactly what the real overlay looks like, so there's exactly one place the drawing logic can drift from what's on screen.

It takes a cairo context and either a shape (cross, dot, circle, drawn as vectors) or a `GdkPixbuf` (a custom image, scaled to the configured size). A `clear` flag controls whether it clears the canvas to transparent first. The daemon needs that, since the real overlay surface has to have no background showing through. The GUI preview passes `clear=False` and paints its own dark backdrop instead, since clearing to transparent there would just punch a hole showing whatever's behind the preview widget.

## Config

Lives at `~/.config/crosshair-overlay/config.toml`, read with the standard library's `tomllib`. Writing it back out uses a small hand-rolled serializer instead of a dependency, since `tomllib` is read-only and the only structures ever written are flat sections of strings, numbers, and booleans.

`hex_to_rgba()` falls back to green on anything that doesn't parse as a 6-digit hex color, rather than raising, since this can get called from a GTK draw callback where an uncaught exception would break redraws.

## The offset math

This is the least obvious part of the GUI, so it's worth spelling out separately from the code comments.

The daemon only understands a raw top-left layer-shell margin for `offset_x`/`offset_y`. Margin 0 doesn't mean centered, it means pinned to the literal edge of the screen, which is why the daemon has a special case: if both offsets are exactly 0, it sets no anchors at all and lets the compositor center the surface itself.

Users don't think in top-left margins, they think "move it 10px right of center." So the GUI shows offset sliders as relative-to-center values and translates them to raw margins behind the scenes, using `offset = (monitor_dimension - crosshair_size) / 2 + relative_value`. That translation gets recomputed whenever anything it depends on changes: the relative value itself, the crosshair size, or the selected output, since a different monitor means a different resolution and the same raw margin would land in a different relative spot.

The one exception is relative `(0, 0)`, which the GUI writes as raw `(0, 0)` directly rather than the computed margin, matching the daemon's own auto-center special case exactly and avoiding compounding any error in the monitor-geometry guess for what's the most common case anyway.

Export/import has to deal with the same problem across machines. Exporting swaps the raw offsets for the relative ones, plus the monitor resolution they were computed against. Importing onto a different resolution triggers a prompt asking whether to keep the exact pixel offset or scale it proportionally to the new screen.

## Import/export bundles

A config exported from the GUI is a self-contained `.toml` file. If a custom image is set, `encode_image_bundle()` reads it and stores it as base64 directly in the file, so sharing a look is one file, not a `.toml` plus a separate PNG someone has to remember to also send.

Importing decodes that image back to disk under `~/.config/crosshair-overlay/imported-images/`, prefixed with a hash of its contents. The filename inside the bundle came from whoever exported it, so `decode_image_bundle()` treats it as untrusted and strips it down to just the base name before using it, then checks the resolved destination path is still inside the imports directory before writing. Belt and suspenders: the code comments go through why the naive version wasn't actually exploitable in practice, but the sanitization is kept anyway since that safety was closer to an accident of implementation than something the code enforced on purpose.

## Packaging

The AUR package installs the four Python files to `/usr/lib/crosshair-overlay/` rather than `/usr/bin`, then writes thin shell wrappers in `/usr/bin` (`crosshaird`, `crosshair-gui`, `crosshairctl`, plus a `crosshair-overlay` alias for the GUI) that each exec the real script by its full path. That full-path exec matters: Python puts the invoked script's own directory on `sys.path`, and a symlink in `/usr/bin` pointing at the real file wouldn't reliably resolve to the same directory, which would break the `import crosshair_common` at the top of each script.
