# PR #2: native Wayland smoke test

This is the executable checklist for the five manual checks in
[settings-review.md](settings-review.md). A virtual X11 display can run the
regression suite, but cannot establish any of the compositor results below.
Every desktop result is **NOT RUN** until a tester records observations.

## Scope and prerequisites

The original merge gate asks for **KDE Plasma/KWin Wayland or Hyprland**.
One actual target session satisfies that wording; test both to substantiate
both supported compositor configurations. Record the compositor/version,
distro, GTK version, theme, game/version and whether the game uses native
Wayland or XWayland. The overlay itself must always use native Wayland.

- Use a terminal inside the logged-in desktop, as the desktop user. SSH,
  root, an X11 login, a browser screenshot and Xvfb are insufficient.
- Python 3.11+, PyGObject, pycairo including the GI Cairo bridge, GTK4,
  GdkPixbuf and gtk4-layer-shell (shared library **and** Gtk4LayerShell 1.0
  introspection typelib). These are the dependencies in README/PKGBUILD;
  the headless CI dependencies alone do not include layer-shell.
- The connected compositor must advertise `zwlr_layer_shell_v1`. Its
  availability is checked by the actual daemon on activation.
- At least one monitor and a fullscreen game with an observable click/input
  response. Two monitors with different logical dimensions are needed to
  establish cross-monitor placement. Mixed scale factors are useful added
  coverage; missing hardware means that coverage remains untested.
- Close any other settings window and stop the installed overlay first:
  `crosshairctl quit` (or the installed `crosshairctl.py quit`). An
  unreachable socket is acceptable only after confirming no daemon is
  running. If an autostart/user service relaunches it, pause that service
  for the test and restore it afterward. Do not run two daemons: the
  application ID is shared even when socket paths differ.

Fetch the current PR head into a separate checkout/worktree, without
overwriting an existing branch or dirty working tree. For example, from an
existing clone:

```sh
git fetch origin pull/2/head
git worktree add --detach ../crosshair-overlay-pr2-smoke FETCH_HEAD
cd ../crosshair-overlay-pr2-smoke
git rev-parse HEAD
git status --short
```

Record that SHA; results belong to that commit. The inspection on
2026-10-04 used `71af5bcd2c22710613cefd34afb215303136ff2f`.

Run these prerequisite checks **on the desktop**, before launching:

```sh
printf 'session=%s desktop=%s wayland=%s\n' "$XDG_SESSION_TYPE" "$XDG_CURRENT_DESKTOP" "$WAYLAND_DISPLAY"
test "$XDG_SESSION_TYPE" = wayland
test -n "$WAYLAND_DISPLAY"
/usr/bin/python3 - <<'PY'
import sys, ctypes.util, gi, cairo
assert sys.version_info >= (3, 11)
gi.require_version('Gtk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('Gtk4LayerShell', '1.0')
from gi.repository import Gtk, GdkPixbuf, Gtk4LayerShell
print('Python:', sys.version.split()[0])
print('GTK:', Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version())
print('layer-shell library:', ctypes.util.find_library('gtk4-layer-shell'))
print('GI imports OK; compositor behavior still requires the smoke test')
PY
```

If the library lookup returns `None`, check the daemon's search paths and
`GTK4_LAYER_SHELL_PATH` override described in ARCHITECTURE.md. Do not bypass
its preload/re-exec logic. Do not carry `GDK_BACKEND=x11` from regression
testing into the desktop test.

Capture output names, physical modes, logical dimensions and scale factors:
`kscreen-doctor -o` on KDE, or `hyprctl monitors` on Hyprland. If installed,
`wayland-info` provides a protocol listing; look for `zwlr_layer_shell_v1`.
The import probe above checks packages, not display connectivity or protocol
support. A successful overlay launch is still required.

## Isolated test settings and commands

Use these variables in the same terminal for all commands. The GUI
automatically launches this checkout's daemon and passes the custom paths.

```sh
SMOKE_DIR=$(mktemp -d /tmp/crosshair-pr2.XXXXXX)
SMOKE_CONFIG="$SMOKE_DIR/config.toml"
SMOKE_SOCKET="$SMOKE_DIR/control.sock"
export SMOKE_DIR SMOKE_CONFIG SMOKE_SOCKET
env GDK_BACKEND=wayland CROSSHAIR_GUI_DEBUG=1 /usr/bin/python3 crosshair-gui.py \
  --config "$SMOKE_CONFIG" --socket "$SMOKE_SOCKET" \
  >"$SMOKE_DIR/gui.log" 2>&1 &
```

This isolates config and socket from the installed overlay. Imported images
still go to `~/.config/crosshair-overlay/imported-images/`: that location is
hardcoded and is **not** isolated by `--config` or `XDG_CONFIG_HOME`. Use a
disposable test PNG and note any imported file created. Keep all export files
inside `$SMOKE_DIR`.

For the lifecycle/input cases, run each command separately and observe the
result before the next command. CLI exit 0 means the command was sent, not
that its visible effect has been verified:

```sh
/usr/bin/python3 crosshairctl.py hide --socket "$SMOKE_SOCKET"
/usr/bin/python3 crosshairctl.py show --socket "$SMOKE_SOCKET"
/usr/bin/python3 crosshairctl.py toggle --socket "$SMOKE_SOCKET"
/usr/bin/python3 crosshairctl.py toggle --socket "$SMOKE_SOCKET"
/usr/bin/python3 crosshairctl.py reload --socket "$SMOKE_SOCKET"
```

If GUI startup fails, its child stderr is discarded. Click Stop if possible,
close settings, and confirm the test socket is no longer live before running
the daemon in a foreground terminal for useful diagnostics:

```sh
env GDK_BACKEND=wayland /usr/bin/python3 crosshaird.py \
  --config "$SMOKE_CONFIG" --socket "$SMOKE_SOCKET" \
  2>&1 | tee "$SMOKE_DIR/daemon.log"
```

Stop that diagnostic daemon with the test CLI `quit` from another terminal
before retrying auto-start. Supply the same paths in that terminal.

## Matrix and pass criteria

These rows split the original five steps into observable results. The
suggested sizes, offsets and cycle count make repetition consistent; they
are test inputs added here, not additional promises in the original guide.

| ID / original step | Actions | Required observable result | KDE Wayland | Hyprland |
| --- | --- | --- | --- | --- |
| W1 / 2: desktop layout | Inspect normal and reduced-height settings windows in the desktop theme; select cross, dot, circle and custom image; test size 24 and 300. | Shared label column and slider alignment; no clipped controls/status; scroll reaches bottom; no Preview caption; large preview fits; gap enabled only for cross; selected shape stays selected. | NOT RUN | NOT RUN |
| W2 / 2: image/bundle | Pick disposable PNG A, click Custom Image again and cancel, replace with PNG B; export; rename the source PNG B temporarily; import the export. Restore the fixture afterward. | Cancel retains A; replacement displays B; embedded export imports without the original source path and produces the same image/appearance. | NOT RUN | NOT RUN |
| W3 / 3: close/save | Change a slider and immediately close via the window close control; reopen using the same paths. | Final value survives; overlay stays alive when settings closes; reopening does not create a second overlay. The 150 ms debounce edge is also covered by the automated regression. | NOT RUN | NOT RUN |
| W4 / 1,4: lifecycle/control | Observe initial auto-start; Stop/Start three times; attempt a second Start during startup; run hide, show, toggle twice and reload separately. Close/reopen settings while running; quit via test CLI. | One overlay; startup button disabled while pending; status settles correctly; each CLI action has its expected visible effect; closing settings leaves daemon running; Stop/quit removes overlay/socket listener; restart works. Reopening settings while stopped intentionally auto-starts it. | NOT RUN | NOT RUN |
| W5 / 4: input/focus | Click directly through both drawn pixels and transparent parts onto an observable underlying control; type into the already-focused app. Repeat over a fullscreen game, after reload/size/output changes, and after hide/show. | Underlying app/game receives clicks and keyboard input; overlay never takes focus or becomes an ordinary taskbar window. Pointer movement alone or a screenshot is insufficient evidence. | NOT RUN | NOT RUN |
| W6 / 4: fullscreen stack | Enter game fullscreen, interact, Alt-Tab away/back, reload, hide/show and restart overlay while fullscreen. Record the game's display mode/backend. | Crosshair remains above the fullscreen game whenever shown, disappears when hidden, and returns after show/restart without a black rectangle, shadow, duplicate surface or focus theft. | NOT RUN | NOT RUN |
| W7 / 5: centered/output | Test Automatic at offsets (0,0), then explicitly select each available output at (0,0); change size 24 to 80 and back. | One centered crosshair on the chosen explicit output; center stays fixed as size changes. Record Automatic's actual output rather than assuming it follows the settings window. | NOT RUN | NOT RUN |
| W8 / 5: nonzero placement | On each explicitly selected output, test relative (20,0), (0,-20), (20,-20), (-20,20); change size 24 to 80; switch between outputs of different logical dimensions. | Center moves right for positive X/down for positive Y; untouched axis stays centered; relative displacement survives size and output changes. Raw margins recompute using the chosen output's logical dimensions. | NOT RUN | NOT RUN |

For explicit output logical geometry `(W,H)`, size `s` and GUI-relative
offset `(dx,dy)`, expect raw margins
`round((W-s)/2 + dx)` and `round((H-s)/2 + dy)`. At relative `(0,0)`, expect
raw `(0,0)` instead: the compositor centers the unanchored surface. Compare
logical coordinates, accounting for output scaling when measuring screenshots.
For example, `1920x1080`, size 24, relative `(20,-20)` writes `(968,508)`;
size 80 writes `(940,480)`, leaving the crosshair center unchanged.

Automatic with nonzero offsets on differently sized monitors is a known,
unchanged geometry guess: the GUI uses its own monitor as a proxy for the
compositor-selected output. Observe and report it separately; use explicit
output selection for the required reliable-offset cases. Do not label that
existing limitation as a new passing multi-monitor guarantee.

Run W5/W6 on the intended game first. A second game/backend, mixed-DPI layout
and monitor hotplug are useful extra coverage, but not required by the
original five-step guide. Report those separately. Missing second monitor
or compositor is **NOT RUN (unavailable)**, not PASS.

## Evidence and cleanup

Copy the matrix into a results file with the actual SHA and environment.
For every row record PASS/FAIL/NOT RUN, observations, and evidence filenames.
Use screenshots for visual appearance/placement and a short recording or
written interaction observations for input, focus, stacking and lifecycle.
Record warnings and observed vs expected coordinates for failures. A native
desktop run remains pending until those observations exist.

Finish by closing settings first (to prevent accidental auto-start), then:

```sh
/usr/bin/python3 crosshairctl.py quit --socket "$SMOKE_SOCKET"
```

Confirm no test overlay remains and the socket listener is gone. Retain
`$SMOKE_DIR` logs/config/export/evidence until reviewed. If desired, remove
only the imported disposable image identified during W2; do not delete the
whole imports directory. Restore any installed overlay/service stopped for
the test. None of these steps installs, merges or releases the PR.
