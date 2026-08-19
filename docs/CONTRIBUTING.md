# Contributing to crosshair-overlay

Thanks for taking an interest in this project. It's a small, focused tool, and this doc is here to save you time before you try to help with anything. So that includes what kind of contributions are welcome, what the project is trying (and not trying) to be, and a few good practices.

## What this project is

crosshair-overlay does one job: put a crosshair overlay on top of your games on Wayland, with a GUI so it’s easy to adjust. It’s meant to be simple and work reliably across many compositors and monitor resolutions.

It is **not** trying to be anything more than that. If a change would meaningfully increase the surface area or complexity of the app to serve a narrow use case, it's probably not a fit here, even if it's well-built.

## What's most useful right now

In rough order of what I actually want:

1. **Bug fixes.** Anything that's broken, crashes, misbehaves or produces a wrong result (bad scaling on import, wrong output selection, socket edge cases, etc). 
2. **Reliability / stability improvements.** Better error handling, more defensive config parsing, fixing race conditions, cleaning up resource leaks. Things that make the existing base more solid, even if nothing is broken.
3. **Portability / install-friendliness.** Anything that makes it easier to get running on more distros or compositors without adding too much new complexity.

## What's out of scope by default

New features are not the priority, and the bar for them is high. This isn't a "no" forever, though. If someone has an idea that genuinely helps with using the crosshair or a good quality of life change, I'm open to hearing about it. But:

- Please open an issue to discuss a new feature **before** putting time into a PR for it. I'd rather tell you early if it's not the direction I want to go than have you write code that doesn't get merged.
- Expect a higher bar for new features than for fixes. "Cool, but does it need to exist" is a real question I'll ask.
- Scope creep dressed up as a bug fix (e.g. "fixing" a limitation by adding a new config surface) will get the same scrutiny as a feature request.

## Before you dig into the code

Read `docs/ARCHITECTURE.md` if you want to better know how crosshair-overlay works. The code itself is commented on fairly heavily around the non-obvious/tricky bits, so between the two you should have what you need for most debugging or feature work.

## Good practices

- Keep PRs focused, as one fix or one improvement per PR is much easier to review and merge than a bundle of unrelated changes.
- If you're fixing a bug, **include a short description of how to reproduce it** (compositor, distro, steps) as that makes it much faster to confirm the fix actually addresses it.
- Match the existing code style, so keep it simple and leave comments explaining what and *why* you did something.
- If your change touches behavior a user would notice (new config key, changed default, changed CLI output), call that out explicitly in the PR description.

## Reporting bugs

Open an issue with:

- What compositor and distro you're on (KDE plasma + CachyOS, Hyprland + Arch, etc.)
- What you expected vs. what happened
- Anything relevant from running `crosshaird.py` or `crosshair-gui.py` directly in a terminal. For the GUI, use
`CROSSHAIR_GUI_DEBUG=1 crosshair-gui`
on the terminal to get more useful debug data.

That's most of it, for now.
