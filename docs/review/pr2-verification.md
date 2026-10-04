# PR #2 verification record — 2026-10-04

Inspected application/test source:
`71af5bcd2c22710613cefd34afb215303136ff2f`.
This follow-up changes documentation only; native desktop verification is
still pending. The PR was open and draft with no submitted reviews or inline
review threads at inspection time. The checked-in review and test guide is
`docs/review/settings-review.md`.

| Check | Evidence / result |
| --- | --- |
| GitHub Actions regression job | [Run 37016118206](https://github.com/m39d/crosshair-overlay/actions/runs/37016118206), job `regression` / 110867280544: completed, success. Retrieved job log explicitly reports **Ran 19 tests / OK** under Xvfb. |
| Local Python compilation | PASS: `/usr/bin/python3 -m py_compile` for all four application modules and all three test modules. |
| Local PR whitespace check | PASS: `git diff --check 42a30e052fb1b06ec99ad3579d2935f5402ac9da HEAD`; documentation diff also checked. |
| Local full regression attempt | **INCOMPLETE / ENVIRONMENT BLOCKED**: 19 tests attempted, eight passed and 11 errored; exit 1. Do not report a local 19-test pass. |
| Native KDE/Hyprland smoke matrix | **NOT RUN**. No local Wayland session/display, compositor or game; this environment cannot establish click-through, fullscreen stacking, focus or physical output placement. |
| Visual inspection | The original PR reports Adwaita Dark inspection on virtual X11. No new local visual inspection was performed in this follow-up, and the original inspection does not establish KDE/Breeze or layer-shell behavior. |

## Local regression attempt

Ubuntu 24.04, Python 3.12.3, GTK 4.14.5, with the CI's GTK/Cairo/Xvfb
dependencies installed. Ran the guide's unchanged command:

```sh
xvfb-run -a env GDK_BACKEND=x11 GSK_RENDERER=cairo GTK_A11Y=none /usr/bin/python3 -m unittest discover -s tests -v
```

Eight existing common/config/rendering tests passed: independent defaults,
embedded-image validation/containment, preserving the previous file on failed
atomic save, invalid config rejection, invalid-file fallback, excessive-gap
rendering, positive monitor resolution validation, and Unicode/key round trip.

The stale-socket and control-server tests error at creation of an `AF_UNIX`
socket with `PermissionError: [Errno 1] Operation not permitted`. All nine GUI
tests error during window construction because GTK cannot initialize a
display connection. The runtime blocks Unix-domain sockets, including the
virtual X11 display connection. No test assertions establish a new application
regression from these environment errors. The tests were not modified, mocked
further or marked skipped to manufacture a passing suite.

## Prepared desktop verification

[wayland-smoke-test.md](wayland-smoke-test.md) maps the original five guide
steps into eight result rows with prerequisites, disposable settings/socket
paths, diagnostics, pass criteria, evidence and cleanup. The original gate
says KDE Wayland **or** Hyprland; verifying both requires actual results for
both. Unavailable second-monitor/mixed-scale coverage remains untested.

Keep PR #2 draft until real desktop observations satisfy the gate. The
unchanged Automatic-output geometry guess is documented separately from the
explicit-output placement checks. No native desktop pass, merge, release or
AUR publication is established by this record.
