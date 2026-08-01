#!/usr/bin/env python3
"""
crosshair-gui — settings window for the crosshair-overlay daemon.

A plain, ordinary desktop window (not a layer-shell surface — no need
for one here). It edits ~/.config/crosshair-overlay/config.toml and
tells a running crosshaird to reload after every change, so the
overlay updates live as you drag a slider or type a number. The
overlay is started automatically when this window opens (if it isn't
already running); the bottom-right button then lets you stop/restart
it explicitly.

Layout, top to bottom:
  1. Live preview of the current crosshair
  2. Shape picker: Cross / Dot / Circle / Custom Image...
  3. Controls that apply to both vector shapes and custom images:
     size, opacity, offset X/Y, output monitor -- each a slider paired
     with a type-able number box sharing one Gtk.Adjustment
  4. Controls that only apply to vector shapes (color, thickness, gap)
     — grayed out when a custom image is selected
  5. Status text, then Import/Export/Start-Stop-Overlay buttons

Export bundles a custom image as base64 directly inside the exported
.toml file, so the exported config is fully self-contained and
portable to another machine without separately copying the image file.
"""

import argparse
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

DEBUG = os.environ.get("CROSSHAIR_GUI_DEBUG") == "1"


def _debug(msg: str) -> None:
    """Diagnostic logging, only when CROSSHAIR_GUI_DEBUG=1 is set in the
    environment. Useful if state ever gets out of sync between self.cfg
    and the displayed controls -- silent by default, safe to leave in."""
    if DEBUG:
        sys.stderr.write(f"[crosshair-gui debug] {msg}\n")
        sys.stderr.flush()


from crosshair_common import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_CONFIG_PATH,
    DEFAULT_SOCKET_PATH,
    decode_image_bundle,
    dump_toml,
    encode_image_bundle,
    format_monitor_res,
    load_config,
    load_pixbuf,
    parse_monitor_res,
    render_crosshair,
    save_config,
    send_control_command,
)

APPLY_DEBOUNCE_MS = 150       # avoid flooding disk writes/socket calls while dragging a slider
STATUS_POLL_INTERVAL_MS = 3000  # notice if the daemon was started/stopped externally
PREVIEW_BOX_SIZE = 220


class CrosshairSettingsWindow(Gtk.ApplicationWindow):
    def __init__(self, app, config_path: Path, socket_path: Path):
        super().__init__(application=app, title="Crosshair Overlay Settings")
        self.config_path = config_path
        self.socket_path = socket_path
        # Matches Icon=crosshair-overlay in crosshair-overlay.desktop
        # (whose StartupWMClass also matches this app's application-id
        # below, so KDE can tie a running window back to that entry).
        # This used to be hardcoded to "preferences-system" -- a generic
        # icon-theme fallback from before a real .desktop file/icon
        # existed, back when an icon-less toplevel made KDE/Wayland show
        # the Wayland project's own logo instead. Now that a proper icon
        # exists, naming it directly here shows the real crosshair icon
        # instead of that generic placeholder.
        #
        # For KDE to actually resolve the name "crosshair-overlay" to
        # crosshair-overlay.svg, the file needs to be installed under an
        # icon theme directory, e.g.:
        #   ~/.local/share/icons/hicolor/scalable/apps/crosshair-overlay.svg
        # (or the system-wide /usr/share/icons/... equivalent if this is
        # packaged) -- just having the .svg sitting next to the scripts
        # is not enough for icon-name lookup to find it.
        self.set_icon_name("crosshair-overlay")
        # This was previously (400, 680), which was never the real
        # opening size -- gtk_window_set_default_size() is only a
        # request, and GTK silently ignores it whenever the content's
        # actual minimum size is larger, falling back to that instead.
        # This window's content (all the control rows, unwrapped
        # section headers, etc.) has a minimum footprint closer to
        # 512x850, which is what was actually showing up regardless of
        # what was requested here. Matching that here so the code
        # states the size it really opens at, rather than one that was
        # always being overridden.
        self.set_default_size(512, 850)

        self._loading = True         # suppress apply-on-change while we set initial widget state
        self._apply_source_id = None
        self._current_mode = "cross"  # "cross" | "dot" | "circle" | "image"
        self._preview_pixbuf = None
        self._output_values = [""]

        # The GUI's Offset X/Y sliders show a value *relative to dead
        # center* (0 = centered), which is translated to/from the raw
        # offset_x/offset_y written to config.toml. The daemon itself
        # only understands raw top-left layer-shell margins (see
        # crosshaird.py/README "output/offset settings don't seem to do
        # anything") and is deliberately left alone -- this translation
        # lives entirely here. These two hold the GUI-side relative
        # values; self.cfg["crosshair"]["offset_x"/"offset_y"] always
        # holds the raw, daemon-facing values derived from them.
        self._rel_offset_x = 0.0
        self._rel_offset_y = 0.0

        # Handler ids for every value-changed-style signal we connect,
        # keyed by the widget/adjustment object. Populated via
        # _connect_signal() below. Lets _populate_from_cfg() truly
        # silence a control while setting it programmatically (via
        # handler_block/unblock), the same hard guarantee already used
        # for the shape toggle buttons -- rather than relying only on
        # the self._loading flag, which offers no protection if
        # something raises partway through populating and never gets
        # reset, or if a handler somehow fires out of the order we
        # expect.
        self._signal_handlers = {}

        self.cfg = load_config(config_path)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)

        # Previously `root` was set directly as the window's child at a
        # fixed 400x680 default size. On a short monitor (< ~1080p
        # vertical, or a scaled-up UI) or a compositor where the window
        # can't easily be dragged, the content simply ran off the bottom
        # of the screen with no way to reach the Import/Export/Start
        # buttons -- the README's "Known issues" workaround (lower UI
        # scale, or move the window with a keyboard shortcut) was a
        # workaround for exactly this. A ScrolledWindow lets GTK shrink
        # the actual window below the content's natural height and
        # scroll to reach whatever doesn't fit, instead of requiring the
        # full content height to be visible at once.
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # Without this, GTK sizes the ScrolledWindow (and so the window)
        # to fit its child's full natural size before ever considering
        # shrinking it -- the same clipping problem all over again, just
        # one layer up. This is what actually allows "smaller than the
        # content" to be a valid window size.
        scroller.set_propagate_natural_height(False)
        scroller.set_vexpand(True)
        scroller.set_child(root)
        self.set_child(scroller)
        # Kept so _disable_scroll()'s controllers can drive the window's
        # own scrolling by hand -- see the comment there for why this is
        # necessary rather than just letting the scroll event bubble.
        self.scroller = scroller

        root.append(self._build_preview())
        root.append(self._build_shape_picker())
        root.append(Gtk.Separator())
        root.append(self._build_common_controls())
        root.append(Gtk.Separator())
        root.append(self._build_shape_only_controls())
        root.append(Gtk.Separator())
        root.append(self._build_status_label())
        root.append(self._build_bottom_row())

        self._loading = True
        try:
            self._populate_from_cfg()
        finally:
            self._loading = False

        # _get_monitor_geometry()'s "automatic" fallback asks which
        # monitor *this window* is on, which GTK can't answer until
        # we're realized -- at __init__ time (above) it fell back to
        # monitor 0. Once realize fires, recompute the offset sliders
        # against the real answer so a saved non-zero offset displays
        # correctly instead of whatever monitor-0's resolution implied.
        self.connect("realize", self._on_realize_refresh_offsets)

        self._refresh_status()
        if not self.socket_path.exists():
            # Quality-of-life: start the overlay automatically when you
            # open settings, rather than making "why isn't anything
            # showing" the first thing a new user has to debug. The
            # button remains available to stop/restart it explicitly.
            self._start_overlay()
        GLib.timeout_add(STATUS_POLL_INTERVAL_MS, self._on_status_poll)

    # -- UI construction ------------------------------------------------

    def _build_preview(self):
        frame = Gtk.Frame()
        frame.add_css_class("preview-frame")
        self.preview_area = Gtk.DrawingArea()
        self.preview_area.set_content_width(PREVIEW_BOX_SIZE)
        self.preview_area.set_content_height(PREVIEW_BOX_SIZE)
        self.preview_area.add_css_class("preview-canvas")
        self.preview_area.set_draw_func(self._draw_preview, None)
        frame.set_child(self.preview_area)

        css = Gtk.CssProvider()
        css.load_from_data(
            b".preview-canvas { background-color: #1e1e1e; }"
            b".preview-frame { border-radius: 6px; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_halign(Gtk.Align.CENTER)
        box.append(frame)
        caption = Gtk.Label(label="Preview")
        caption.add_css_class("dim-label")
        caption.set_wrap(True)
        caption.set_justify(Gtk.Justification.CENTER)
        box.append(caption)
        return box

    def _build_shape_picker(self):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.set_homogeneous(True)
        row.add_css_class("linked")
        row.set_halign(Gtk.Align.CENTER)

        self._shape_buttons = {}
        self._toggle_handlers = {}
        for shape, label in (("cross", "Cross"), ("dot", "Dot"), ("circle", "Circle")):
            btn = Gtk.ToggleButton(label=label)
            handler_id = btn.connect("toggled", self._on_shape_toggled, shape)
            self._shape_buttons[shape] = btn
            self._toggle_handlers[shape] = handler_id
            row.append(btn)

        image_btn = Gtk.ToggleButton(label="Custom Image…")
        handler_id = image_btn.connect("toggled", self._on_image_toggled)
        self._shape_buttons["image"] = image_btn
        self._toggle_handlers["image"] = handler_id
        row.append(image_btn)

        return row

    def _labeled_row(self, label_text, widget):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        label = Gtk.Label(label=label_text)
        label.set_halign(Gtk.Align.START)
        label.set_size_request(90, -1)
        row.append(label)
        widget.set_hexpand(True)
        row.append(widget)
        return row

    def _disable_scroll(self, widget):
        """Stop the mouse scroll wheel from changing `widget`'s value,
        while still letting the settings window scroll underneath it.

        GtkScale and GtkSpinButton both treat a scroll event over them
        as an increment/decrement by default. That's a mild convenience
        for a lone slider, but a bad surprise here: nudging a value by
        one scroll click while just trying to move the mouse past it
        is exactly the kind of "why did this change" bug reports come
        from. A capture-phase scroll controller intercepts the event
        before the widget's own built-in scroll handling ever runs.

        Simply returning True from that controller stops the value from
        changing, but it also marks the event fully handled, so it never
        reaches the outer ScrolledWindow's own bubble-phase scroll
        controller further up the widget tree -- the window just does
        nothing instead of scrolling. There's no GTK4 flag to say
        "handled by me, but still bubble it up", so _on_control_scroll
        below drives self.scroller's vertical adjustment by hand to get
        the same effect: the control's value stays put, and the window
        still scrolls.
        """
        controller = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("scroll", self._on_control_scroll)
        widget.add_controller(controller)

    def _on_control_scroll(self, _controller, _dx, dy):
        """Manually scroll the settings window in place of the slider/spin
        button under the cursor eating the event. See _disable_scroll for
        why this can't just be left to normal event propagation."""
        vadj = self.scroller.get_vadjustment()
        if vadj is not None:
            step = vadj.get_step_increment() or 20
            new_value = vadj.get_value() + dy * step
            upper = max(vadj.get_lower(), vadj.get_upper() - vadj.get_page_size())
            vadj.set_value(max(vadj.get_lower(), min(new_value, upper)))
        return True

    def _build_adjustable_row(self, label_text, lower, upper, step, digits=0, page_increment=None, subtitle=None):
        """A slider + a type-able number box, sharing one Gtk.Adjustment.

        Both widgets read/write the same Adjustment, so dragging the
        slider and typing a number always agree — no separate syncing
        code needed, and no risk of the two getting out of step.
        Returns (row_widget, adjustment); connect to
        `adjustment.connect("value-changed", ...)` once for both.

        `subtitle`, if given, is rendered as a small grey caption under
        the main label (e.g. clarifying what "0" means for a control).
        """
        adjustment = Gtk.Adjustment(
            value=lower,
            lower=lower,
            upper=upper,
            step_increment=step,
            page_increment=page_increment or max(step * 10, step),
        )
        scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adjustment)
        scale.set_draw_value(False)
        scale.set_digits(digits)
        scale.set_hexpand(True)
        self._disable_scroll(scale)

        spin = Gtk.SpinButton(adjustment=adjustment, digits=digits, climb_rate=step)
        spin.set_width_chars(6)
        self._disable_scroll(spin)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if subtitle:
            label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            label_box.set_size_request(90, -1)
            label = Gtk.Label(label=label_text)
            label.set_halign(Gtk.Align.START)
            label_box.append(label)
            sub = Gtk.Label(label=subtitle)
            sub.set_halign(Gtk.Align.START)
            sub.add_css_class("caption")
            sub.add_css_class("dim-label")
            label_box.append(sub)
            row.append(label_box)
        else:
            label = Gtk.Label(label=label_text)
            label.set_halign(Gtk.Align.START)
            label.set_size_request(90, -1)
            row.append(label)
        row.append(scale)
        row.append(spin)
        return row, adjustment

    def _build_common_controls(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(Gtk.Label(label="<b>Size, position &amp; opacity</b>", use_markup=True, halign=Gtk.Align.START))

        row, self.size_adj = self._build_adjustable_row("Size (px)", 4, 300, 1, digits=0)
        self._connect_signal(self.size_adj, "value-changed", self._on_size_changed)
        box.append(row)

        row, self.opacity_adj = self._build_adjustable_row("Opacity", 0.0, 1.0, 0.01, digits=2)
        self._connect_signal(self.opacity_adj, "value-changed", self._on_opacity_changed)
        box.append(row)

        row, self.offset_x_adj = self._build_adjustable_row(
            "Offset X", -3000, 3000, 1, digits=0, subtitle="(relative to center)"
        )
        self._connect_signal(self.offset_x_adj, "value-changed", self._on_offset_x_changed)
        box.append(row)

        row, self.offset_y_adj = self._build_adjustable_row(
            "Offset Y", -3000, 3000, 1, digits=0, subtitle="(relative to center)"
        )
        self._connect_signal(self.offset_y_adj, "value-changed", self._on_offset_y_changed)
        box.append(row)

        self.output_dropdown = Gtk.DropDown(model=self._build_output_model())
        self._connect_signal(self.output_dropdown, "notify::selected", self._on_output_changed)
        box.append(self._labeled_row("Output", self.output_dropdown))
        return box

    def _build_output_model(self):
        names = ["Automatic (default)"]
        self._output_values = [""]
        display = Gdk.Display.get_default()
        if display is not None:
            monitors = display.get_monitors()
            for i in range(monitors.get_n_items()):
                monitor = monitors.get_item(i)
                connector = monitor.get_connector() or f"output-{i}"
                model = monitor.get_model() or ""
                label = f"{connector} ({model})" if model else connector
                names.append(label)
                self._output_values.append(connector)
        return Gtk.StringList.new(names)

    def _build_shape_only_controls(self):
        self.shape_only_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.shape_only_box.append(
            Gtk.Label(
                label="<b>Shape appearance</b> (vector shapes only)",
                use_markup=True,
                halign=Gtk.Align.START,
            )
        )

        self.color_button = Gtk.ColorButton()
        self.color_button.set_use_alpha(False)
        self.color_button.connect("color-set", self._on_color_changed)
        self.shape_only_box.append(self._labeled_row("Color", self.color_button))

        row, self.thickness_adj = self._build_adjustable_row("Thickness", 1, 20, 1, digits=0)
        self._connect_signal(self.thickness_adj, "value-changed", self._on_thickness_changed)
        self.shape_only_box.append(row)

        row, self.gap_adj = self._build_adjustable_row("Center gap", 0, 60, 1, digits=0)
        self._connect_signal(self.gap_adj, "value-changed", self._on_gap_changed)
        self.shape_only_box.append(row)

        return self.shape_only_box

    def _build_status_label(self):
        self.applied_label = Gtk.Label(label="")
        self.applied_label.add_css_class("dim-label")
        self.applied_label.set_halign(Gtk.Align.START)
        return self.applied_label

    def _build_bottom_row(self):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_halign(Gtk.Align.END)

        import_btn = Gtk.Button(label="Import Config…")
        import_btn.connect("clicked", self._on_import_clicked)
        row.append(import_btn)

        export_btn = Gtk.Button(label="Export Config…")
        export_btn.connect("clicked", self._on_export_clicked)
        row.append(export_btn)

        # Rightmost/last on purpose: this is the button most likely to
        # need attention (starting/stopping the overlay), so it sits in
        # the bottom-right corner where it's easy to find on sight.
        self.status_button = Gtk.Button()
        self.status_button.connect("clicked", self._on_status_clicked)
        row.append(self.status_button)
        return row

    # -- populate widgets from self.cfg --------------------------------
    #
    # Every control here is set via _set_widget_silently()/_set_toggle_
    # silently(), which block that control's own signal handler for the
    # duration of the call. This is a hard guarantee that programmatic
    # updates (initial load, or Import Config…) can never be mistaken
    # for a user edit and echoed back into self.cfg -- self._loading is
    # kept as a secondary guard (e.g. for the image-picker dialog) but
    # is no longer what's protecting the numeric controls.

    def _populate_from_cfg(self):
        c = self.cfg["crosshair"]
        self._set_widget_silently(self.size_adj, lambda: self.size_adj.set_value(float(c.get("size", 24))))
        self._set_widget_silently(
            self.opacity_adj, lambda: self.opacity_adj.set_value(float(c.get("opacity", 0.85)))
        )
        self._set_widget_silently(
            self.thickness_adj, lambda: self.thickness_adj.set_value(float(c.get("thickness", 2)))
        )
        self._set_widget_silently(self.gap_adj, lambda: self.gap_adj.set_value(float(c.get("gap", 4))))

        rgba = Gdk.RGBA()
        rgba.parse(c.get("color", "#00FF00"))
        self.color_button.set_rgba(rgba)  # programmatic set_rgba() never emits "color-set"

        # Output must be selected *before* computing relative offsets --
        # the centering formula depends on which monitor's resolution
        # we're measuring against.
        self._select_output_from_cfg()

        # No-ops unless cfg["crosshair"] has rel_offset_x/rel_offset_y --
        # i.e. an Import Config… (or a hand-pasted [crosshair]/[import]
        # section) that hasn't been resolved into offset_x/offset_y for
        # this machine yet. See its docstring for the raw-vs-scaled
        # resolution-mismatch prompt this can trigger.
        self._resolve_imported_offsets()

        rel_x, rel_y = self._relative_offsets_from_raw()
        self._rel_offset_x, self._rel_offset_y = rel_x, rel_y
        self._set_widget_silently(self.offset_x_adj, lambda: self.offset_x_adj.set_value(rel_x))
        self._set_widget_silently(self.offset_y_adj, lambda: self.offset_y_adj.set_value(rel_y))

        image_path = c.get("image", "")
        shape = c.get("shape", "cross")
        for key in ("cross", "dot", "circle", "image"):
            self._set_toggle_silently(key, False)

        if image_path:
            self._current_mode = "image"
            self._set_toggle_silently("image", True)
        else:
            self._current_mode = shape if shape in ("cross", "dot", "circle") else "cross"
            self._set_toggle_silently(self._current_mode, True)

        self._update_sensitivity()
        self._refresh_preview()

    def _connect_signal(self, obj, signal_name, handler):
        """connect() + remember the handler id so we can block/unblock it later."""
        handler_id = obj.connect(signal_name, handler)
        self._signal_handlers[obj] = handler_id
        return handler_id

    def _set_widget_silently(self, obj, setter):
        """Run `setter()` (a zero-arg callable that mutates `obj`) with
        obj's tracked signal handler blocked, so the corresponding
        _on_*_changed callback cannot fire -- a hard guarantee, unlike
        checking self._loading inside the handler, which only works if
        nothing goes wrong between setting the flag and clearing it.
        """
        handler_id = self._signal_handlers.get(obj)
        if handler_id is not None:
            obj.handler_block(handler_id)
        try:
            setter()
        finally:
            if handler_id is not None:
                obj.handler_unblock(handler_id)

    def _set_toggle_silently(self, key, active):
        btn = self._shape_buttons[key]
        handler_id = self._toggle_handlers[key]
        btn.handler_block(handler_id)
        btn.set_active(active)
        btn.handler_unblock(handler_id)

    def _select_output_from_cfg(self):
        value = self.cfg["crosshair"].get("output", "")
        try:
            idx = self._output_values.index(value)
        except ValueError:
            idx = 0
        self._set_widget_silently(self.output_dropdown, lambda: self.output_dropdown.set_selected(idx))

    def _update_sensitivity(self):
        self.shape_only_box.set_sensitive(self._current_mode != "image")

    # -- center-relative offset math ------------------------------------
    #
    # crosshaird.py only understands raw offset_x/offset_y as a
    # top-left layer-shell margin. Left alone, offset 0 on one axis
    # means "pin that edge to the literal screen edge", not "stay
    # centered" -- which is why leaving Offset Y at 0 while touching
    # Offset X used to snap Y to the top. We keep that raw behavior in
    # the config file/daemon (deliberately not touched), and instead
    # have the GUI present + edit a center-relative value, translating
    # it to the raw margin any time something the formula depends on
    # changes (the relative value itself, crosshair size, or target
    # output/monitor resolution).

    def _get_monitor_geometry(self):
        """(width, height) of the currently-selected output, in px.

        Returns None if no display/monitor info is available at all.
        """
        display = Gdk.Display.get_default()
        if display is None:
            return None
        monitors = display.get_monitors()
        if monitors.get_n_items() == 0:
            return None

        idx = self.output_dropdown.get_selected() if hasattr(self, "output_dropdown") else 0
        connector = self._output_values[idx] if 0 <= idx < len(self._output_values) else ""

        target = None
        if connector:
            for i in range(monitors.get_n_items()):
                m = monitors.get_item(i)
                if (m.get_connector() or "") == connector:
                    target = m
                    break

        if target is None:
            # "Automatic" (or a named output that isn't currently
            # connected): crosshaird.py's own automatic case never
            # calls LayerShell.set_monitor() at all (see _bind_output),
            # so the surface just lands on whatever the compositor
            # treats as its default/focused output. GTK4 has no public
            # "primary output" query on Wayland, so we can't ask that
            # question directly -- but this settings window is itself
            # an ordinary toplevel, and the compositor puts *it*
            # somewhere too, almost always the same
            # currently-focused/default output the overlay will end up
            # on. So: ask which monitor our own surface is on and use
            # that as the stand-in for "automatic".
            surface = self.get_surface() if hasattr(self, "get_surface") else None
            if surface is not None:
                target = display.get_monitor_at_surface(surface)

        if target is None:
            # Window not realized yet (e.g. called before the first
            # "realize" signal), or get_monitor_at_surface came back
            # empty -- fall back to the first monitor GTK reports.
            # Good enough to avoid crashing; the geometry gets
            # recomputed (and corrected) as soon as the window is
            # shown, via the "realize"/"map" handlers.
            target = monitors.get_item(0)

        rect = target.get_geometry()
        return rect.width, rect.height

    def _relative_offsets_from_raw(self):
        """Reverse the centering formula to get (rel_x, rel_y) for the sliders.

        crosshaird.py treats raw offset_x == 0 AND offset_y == 0 as a
        distinct special case (see its `if offset_x or offset_y:` check):
        when *both* are exactly zero it sets no anchors at all and lets
        the compositor auto-center the surface, rather than anchoring
        top-left with a computed margin. That auto-centered position is
        the true center regardless of what (monitor - size) / 2 works
        out to, so it must map to relative (0, 0) directly -- running
        the general margin-reversal formula on raw (0, 0) would produce
        a large, wrong-looking relative value (e.g. -948) instead, and
        that wrong value would then drift further any time size/output
        changed recomputed offsets from it.
        """
        raw_x = int(self.cfg["crosshair"].get("offset_x", 0))
        raw_y = int(self.cfg["crosshair"].get("offset_y", 0))
        if raw_x == 0 and raw_y == 0:
            return 0.0, 0.0
        geo = self._get_monitor_geometry()
        if geo is None:
            return float(raw_x), float(raw_y)
        mon_w, mon_h = geo
        size = max(4, int(self.cfg["crosshair"].get("size", 24)))
        rel_x = raw_x - (mon_w - size) / 2.0
        rel_y = raw_y - (mon_h - size) / 2.0
        return rel_x, rel_y

    def _apply_relative_offsets(self):
        """Write raw offset_x/offset_y into cfg from self._rel_offset_x/y.

        offset = (monitor_dimension - crosshair_size) / 2 + relative_value

        so relative 0 always means dead-center regardless of monitor
        resolution or crosshair size, and changing size/output/either
        axis recomputes *both* raw values together -- which is also
        what stops the untouched axis from drifting to an edge.

        Exception: relative (0, 0) exactly is written as raw (0, 0)
        rather than the computed margin -- this is crosshaird.py's own
        "no anchors, let the compositor auto-center" special case (see
        _relative_offsets_from_raw's docstring), and using it directly
        both matches the daemon's own centering exactly and sidesteps
        any imprecision in our monitor-geometry guess (e.g. the
        "Automatic" output fallback) for the single most common case:
        dead center.
        """
        if self._rel_offset_x == 0 and self._rel_offset_y == 0:
            self.cfg["crosshair"]["offset_x"] = 0
            self.cfg["crosshair"]["offset_y"] = 0
            return
        size = max(4, int(self.cfg["crosshair"].get("size", 24)))
        geo = self._get_monitor_geometry()
        if geo is None:
            # No monitor info available (e.g. no display connection at
            # config-edit time) -- fall back to raw == relative, same
            # as the daemon's literal top-left-margin behavior.
            self.cfg["crosshair"]["offset_x"] = int(round(self._rel_offset_x))
            self.cfg["crosshair"]["offset_y"] = int(round(self._rel_offset_y))
            return
        mon_w, mon_h = geo
        self.cfg["crosshair"]["offset_x"] = int(round((mon_w - size) / 2.0 + self._rel_offset_x))
        self.cfg["crosshair"]["offset_y"] = int(round((mon_h - size) / 2.0 + self._rel_offset_y))

    # -- resolving a portable (rel_offset_x/y) import into raw offsets --

    def _resolve_imported_offsets(self):
        """Consume cfg["crosshair"]'s rel_offset_x/rel_offset_y (present
        only right after an Import Config…, or a profile hand-pasted
        straight into config.toml) and turn them into this machine's raw
        offset_x/offset_y, same as the normal saved format.

        If the config also recorded a different [import] monitor_res
        than what's currently selected, and hasn't already recorded a
        keep_rel_offset choice, this prompts once (async -- GTK4 has no
        blocking dialog) asking whether to keep the exact pixel offset
        or scale it to the new resolution. Either way, an offset is
        applied immediately using the best answer available so far (the
        unscaled "raw" reading, corrected afterwards if the prompt comes
        back "scaled") -- nothing is left half-applied while the prompt
        is pending.

        Caveat shared with _get_monitor_geometry()'s "Automatic" output
        case: at first-launch time (before this window's "realize"
        fires), the "current resolution" this compares against may
        still be the monitor-0 fallback rather than this window's real
        monitor -- same limitation _on_realize_refresh_offsets already
        works around for the *display*, just not re-run here.
        """
        c = self.cfg["crosshair"]
        if "rel_offset_x" not in c and "rel_offset_y" not in c:
            return

        rel_x = float(c.pop("rel_offset_x", 0))
        rel_y = float(c.pop("rel_offset_y", 0))

        imp = self.cfg.setdefault("import", dict(DEFAULT_CONFIG["import"]))
        recorded_res = parse_monitor_res(imp.get("monitor_res", ""))
        current_res = self._get_monitor_geometry()
        keep_mode = imp.get("keep_rel_offset", "")

        needs_prompt = (
            recorded_res is not None
            and current_res is not None
            and recorded_res != current_res
            and keep_mode not in ("raw", "scaled")
        )

        if keep_mode == "scaled" and recorded_res and current_res:
            rel_x, rel_y = self._scale_rel_offsets(rel_x, rel_y, recorded_res, current_res)

        self._rel_offset_x, self._rel_offset_y = rel_x, rel_y
        self._apply_relative_offsets()

        # Resolved into offset_x/offset_y above -- nothing left for a
        # future load to resolve, so there's nothing left worth
        # remembering here either.
        self.cfg["import"]["monitor_res"] = ""
        self.cfg["import"]["keep_rel_offset"] = ""

        # Persist now rather than waiting for some unrelated edit to
        # trigger a save -- otherwise a hand-pasted rel_offset_x/y that
        # doesn't need the prompt below (matching/missing monitor_res)
        # would silently re-run this same resolution on every launch
        # until something else happens to save the file.
        self._apply_now()

        if needs_prompt:
            self._prompt_offset_scaling(rel_x, rel_y, recorded_res, current_res)

    @staticmethod
    def _scale_rel_offsets(rel_x, rel_y, recorded_res, current_res):
        rw, rh = recorded_res
        cw, ch = current_res
        return rel_x * (cw / rw), rel_y * (ch / rh)

    def _prompt_offset_scaling(self, rel_x, rel_y, recorded_res, current_res):
        """Ask whether to keep the imported offset's exact pixel value or
        scale it proportionally to this machine's resolution.

        A plain Gtk.Window built by hand rather than Gtk.MessageDialog:
        MessageDialog always puts its buttons in an action area glued to
        the very bottom, with no supported way to put anything after
        them -- but the config-file note here is meant to sit below the
        buttons, not above them alongside the question, so the fixed
        MessageDialog layout doesn't fit.

        `rel_x`/`rel_y` are the *unscaled* relative offsets (already
        applied as a starting point by the caller); this only decides
        whether to leave them as-is or replace them with a scaled
        version once the user answers.
        """
        window = Gtk.Window(transient_for=self, modal=True, title="Different Monitor Resolution")
        window.set_resizable(False)
        # Same fallback-icon fix as the main settings window; dialog-question
        # also happens to be a semantically fitting stock icon for this.
        window.set_icon_name("dialog-question")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        recorded_str = format_monitor_res(*recorded_res)
        current_str = format_monitor_res(*current_res)
        question = Gtk.Label(
            label=(
                f"The current monitor resolution ({current_str}) differs from "
                f"the monitor resolution ({recorded_str}) this config file was "
                "created for.\nWould you rather:"
            )
        )
        question.set_wrap(True)
        question.set_max_width_chars(46)
        question.set_xalign(0)
        box.append(question)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, homogeneous=True)

        keep_btn = Gtk.Button()
        keep_label = Gtk.Label(label="Keep the raw offset")
        keep_label.set_wrap(True)
        keep_label.set_justify(Gtk.Justification.CENTER)
        keep_btn.set_child(keep_label)

        scale_btn = Gtk.Button()
        scale_label = Gtk.Label(label="Try to scale the offset to the current resolution")
        scale_label.set_wrap(True)
        scale_label.set_justify(Gtk.Justification.CENTER)
        scale_btn.set_child(scale_label)

        button_row.append(keep_btn)
        button_row.append(scale_btn)
        box.append(button_row)

        note = Gtk.Label()
        note.set_markup(
            "<small><i>You can change 'keep_rel_offset raw/scaled' in the "
            "config file to skip this message</i></small>"
        )
        note.add_css_class("dim-label")
        note.set_wrap(True)
        note.set_max_width_chars(46)
        note.set_xalign(0)
        box.append(note)

        window.set_child(box)

        def finish(scaled):
            window.destroy()
            self._on_offset_scaling_response(scaled, rel_x, rel_y, recorded_res, current_res)

        keep_btn.connect("clicked", lambda *_a: finish(False))
        scale_btn.connect("clicked", lambda *_a: finish(True))
        # Closing the window (titlebar X, Escape, etc.) without picking
        # either button leaves the interim "raw" apply from
        # _resolve_imported_offsets in place -- same as explicitly
        # choosing "Keep the raw offset".
        window.connect("close-request", lambda *_a: finish(False) or True)

        self._active_dialog = window
        window.present()

    def _on_offset_scaling_response(self, scaled: bool, rel_x, rel_y, recorded_res, current_res):
        self._active_dialog = None

        if scaled:
            rel_x, rel_y = self._scale_rel_offsets(rel_x, rel_y, recorded_res, current_res)

        self._rel_offset_x, self._rel_offset_y = rel_x, rel_y
        self._apply_relative_offsets()
        self._set_widget_silently(self.offset_x_adj, lambda: self.offset_x_adj.set_value(rel_x))
        self._set_widget_silently(self.offset_y_adj, lambda: self.offset_y_adj.set_value(rel_y))
        self._refresh_preview()
        # Persist right away -- otherwise closing the window immediately
        # after answering, without touching anything else, would lose
        # the corrected offset (the interim "raw" apply from
        # _resolve_imported_offsets was already saved, but this
        # "scaled" correction, if chosen, hasn't been yet).
        self._apply_now()

    def _on_realize_refresh_offsets(self, *_args):
        """Re-derive the relative-offset sliders now that this window's
        own monitor (our stand-in for "automatic") is knowable.

        Purely a display refresh: it doesn't touch self.cfg, so it
        can't overwrite a good saved config even if something about
        this guess is still off.
        """
        rel_x, rel_y = self._relative_offsets_from_raw()
        self._rel_offset_x, self._rel_offset_y = rel_x, rel_y
        self._set_widget_silently(self.offset_x_adj, lambda: self.offset_x_adj.set_value(rel_x))
        self._set_widget_silently(self.offset_y_adj, lambda: self.offset_y_adj.set_value(rel_y))

    # -- change handlers --------------------------------------------------

    def _on_shape_toggled(self, btn, shape):
        if self._loading or not btn.get_active():
            return
        for key, other in self._shape_buttons.items():
            if other is not btn:
                self._set_toggle_silently(key, False)
        self.cfg["crosshair"]["shape"] = shape
        self.cfg["crosshair"]["image"] = ""
        self._current_mode = shape
        self._update_sensitivity()
        self._refresh_preview()
        self._schedule_apply()

    def _on_image_toggled(self, btn):
        if self._loading or not btn.get_active():
            return
        dialog = Gtk.FileChooserNative.new(
            "Choose Crosshair Image", self, Gtk.FileChooserAction.OPEN, "_Open", "_Cancel"
        )
        img_filter = Gtk.FileFilter()
        img_filter.set_name("Images")
        img_filter.add_mime_type("image/png")
        img_filter.add_mime_type("image/jpeg")
        img_filter.add_mime_type("image/bmp")
        dialog.add_filter(img_filter)
        dialog.connect("response", self._on_image_chosen, btn)
        # Keep a reference so the native dialog isn't garbage-collected mid-flight.
        self._active_dialog = dialog
        dialog.show()

    def _on_image_chosen(self, dialog, response, btn):
        self._active_dialog = None
        if response == Gtk.ResponseType.ACCEPT:
            gfile = dialog.get_file()
            path = gfile.get_path() if gfile else None
            if path:
                for key, other in self._shape_buttons.items():
                    if other is not btn:
                        self._set_toggle_silently(key, False)
                self.cfg["crosshair"]["image"] = path
                self._current_mode = "image"
                self._update_sensitivity()
                self._refresh_preview()
                self._schedule_apply()
        else:
            self._set_toggle_silently("image", False)
        dialog.destroy()

    def _on_size_changed(self, adjustment):
        if self._loading:
            _debug("_on_size_changed: skipped (self._loading=True)")
            return
        self.cfg["crosshair"]["size"] = int(adjustment.get_value())
        self._apply_relative_offsets()
        self._refresh_preview()
        _debug(f"_on_size_changed: -> {self.cfg['crosshair']}")
        self._schedule_apply()

    def _on_opacity_changed(self, adjustment):
        if self._loading:
            _debug("_on_opacity_changed: skipped (self._loading=True)")
            return
        self.cfg["crosshair"]["opacity"] = round(adjustment.get_value(), 2)
        self._refresh_preview()
        _debug(f"_on_opacity_changed: -> {self.cfg['crosshair']}")
        self._schedule_apply()

    def _on_offset_x_changed(self, adjustment):
        if self._loading:
            _debug("_on_offset_x_changed: skipped (self._loading=True)")
            return
        self._rel_offset_x = adjustment.get_value()
        self._apply_relative_offsets()
        _debug(f"_on_offset_x_changed: rel_x={self._rel_offset_x} -> {self.cfg['crosshair']}")
        self._schedule_apply()

    def _on_offset_y_changed(self, adjustment):
        if self._loading:
            _debug("_on_offset_y_changed: skipped (self._loading=True)")
            return
        self._rel_offset_y = adjustment.get_value()
        self._apply_relative_offsets()
        _debug(f"_on_offset_y_changed: rel_y={self._rel_offset_y} -> {self.cfg['crosshair']}")
        self._schedule_apply()

    def _on_output_changed(self, dropdown, _pspec):
        if self._loading:
            _debug("_on_output_changed: skipped (self._loading=True)")
            return
        idx = dropdown.get_selected()
        if 0 <= idx < len(self._output_values):
            self.cfg["crosshair"]["output"] = self._output_values[idx]
            # Different output likely means a different resolution --
            # recompute raw offsets so the crosshair stays at the same
            # *relative* position (e.g. still dead-center) on the newly
            # selected monitor instead of keeping the old monitor's
            # raw margins.
            self._apply_relative_offsets()
            self._schedule_apply()

    def _on_color_changed(self, btn):
        if self._loading:
            _debug("_on_color_changed: skipped (self._loading=True)")
            return
        rgba = btn.get_rgba()
        hex_color = "#{:02X}{:02X}{:02X}".format(
            int(round(rgba.red * 255)), int(round(rgba.green * 255)), int(round(rgba.blue * 255))
        )
        self.cfg["crosshair"]["color"] = hex_color
        self._refresh_preview()
        _debug(f"_on_color_changed: -> {self.cfg['crosshair']}")
        self._schedule_apply()

    def _on_thickness_changed(self, adjustment):
        if self._loading:
            _debug("_on_thickness_changed: skipped (self._loading=True)")
            return
        self.cfg["crosshair"]["thickness"] = int(adjustment.get_value())
        self._refresh_preview()
        _debug(f"_on_thickness_changed: -> {self.cfg['crosshair']}")
        self._schedule_apply()

    def _on_gap_changed(self, adjustment):
        if self._loading:
            _debug("_on_gap_changed: skipped (self._loading=True)")
            return
        self.cfg["crosshair"]["gap"] = int(adjustment.get_value())
        self._refresh_preview()
        _debug(f"_on_gap_changed: -> {self.cfg['crosshair']}")
        self._schedule_apply()

    # -- apply / status ----------------------------------------------------

    def _schedule_apply(self):
        _debug(f"_schedule_apply: queued (source id was {self._apply_source_id})")
        if self._apply_source_id is not None:
            GLib.source_remove(self._apply_source_id)
        self._apply_source_id = GLib.timeout_add(APPLY_DEBOUNCE_MS, self._apply_now)

    def _apply_now(self):
        self._apply_source_id = None
        _debug(f"_apply_now: saving self.cfg={self.cfg}")
        try:
            save_config(self.cfg, self.config_path)
        except Exception as exc:  # noqa: BLE001
            self._set_status_text(f"⚠ Could not save config: {exc}")
            return GLib.SOURCE_REMOVE
        ok = send_control_command("reload", self.socket_path)
        if ok:
            self._set_status_text("● Applied")
        else:
            self._set_status_text("○ Overlay not running — saved to config only")
        self._refresh_status()
        return GLib.SOURCE_REMOVE

    def _set_status_text(self, text):
        self.applied_label.set_label(text)

    def _refresh_status(self):
        # This button controls the daemon's lifecycle explicitly, on
        # purpose separate from this window's own lifecycle: closing
        # the GUI (X button, Ctrl+C, etc) only ever affects this GUI
        # process, never the overlay daemon, which runs detached so it
        # doesn't vanish mid-game just because you closed settings.
        running = self.socket_path.exists()
        if running:
            self.status_button.set_label("■ Stop Overlay")
            self.status_button.remove_css_class("suggested-action")
            self.status_button.add_css_class("destructive-action")
        else:
            self.status_button.set_label("▶ Start Overlay")
            self.status_button.remove_css_class("destructive-action")
            self.status_button.add_css_class("suggested-action")
        self.status_button.set_sensitive(True)

    def _on_status_poll(self):
        self._refresh_status()
        return GLib.SOURCE_CONTINUE

    def _on_status_clicked(self, _btn):
        if self.socket_path.exists():
            self._stop_overlay()
        else:
            self._start_overlay()

    def _stop_overlay(self):
        ok = send_control_command("quit", self.socket_path)
        self._set_status_text("Overlay stopped" if ok else "⚠ Could not stop overlay (already gone?)")
        GLib.timeout_add(300, self._delayed_status_refresh)

    def _start_overlay(self):
        daemon_path = Path(__file__).resolve().parent / "crosshaird.py"
        if not daemon_path.exists():
            self._set_status_text("⚠ Could not find crosshaird.py next to this script")
            return
        try:
            subprocess.Popen(
                [sys.executable, str(daemon_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._set_status_text(f"⚠ Could not start overlay: {exc}")
            return
        self._set_status_text("Starting…")
        GLib.timeout_add(1200, self._after_start_check)

    def _delayed_status_refresh(self):
        self._refresh_status()
        return GLib.SOURCE_REMOVE

    def _after_start_check(self):
        self._refresh_status()
        self._apply_now()
        return GLib.SOURCE_REMOVE

    # -- preview -------------------------------------------------------

    def _refresh_preview(self):
        crosshair_cfg = self.cfg["crosshair"]
        image_path = crosshair_cfg.get("image", "")
        if image_path:
            size = max(4, int(crosshair_cfg.get("size", 24)))
            self._preview_pixbuf = load_pixbuf(image_path, size, warn=False)
        else:
            self._preview_pixbuf = None
        self.preview_area.queue_draw()

    def _draw_preview(self, _area, ctx, width, height, _data):
        # Opaque dark backdrop so the crosshair (and its opacity) is
        # readable regardless of desktop theme. render_crosshair's own
        # clear-to-transparent step is for the real overlay window only;
        # skip it here or it would punch a transparent hole through this
        # backdrop on every redraw.
        ctx.set_source_rgb(0x1e / 255, 0x1e / 255, 0x1e / 255)
        ctx.paint()

        crosshair_cfg = self.cfg["crosshair"]
        size = max(4, int(crosshair_cfg.get("size", 24)))
        ox = (width - size) / 2.0
        oy = (height - size) / 2.0
        ctx.save()
        ctx.translate(ox, oy)
        render_crosshair(ctx, size, size, crosshair_cfg, self._preview_pixbuf, clear=False)
        ctx.restore()

    # -- import / export ----------------------------------------------

    def _on_export_clicked(self, _btn):
        dialog = Gtk.FileChooserNative.new(
            "Export Crosshair Config", self, Gtk.FileChooserAction.SAVE, "_Save", "_Cancel"
        )
        dialog.set_current_name("crosshair-config.toml")
        dialog.connect("response", self._on_export_response)
        self._active_dialog = dialog
        dialog.show()

    def _on_export_response(self, dialog, response):
        self._active_dialog = None
        if response == Gtk.ResponseType.ACCEPT:
            gfile = dialog.get_file()
            path = Path(gfile.get_path()) if gfile and gfile.get_path() else None
            if path:
                # Raw offset_x/offset_y are baked-in pixel margins for
                # *this* machine's monitor -- exporting them as-is is
                # what breaks on a friend's different resolution. Swap
                # them for the portable rel_offset_x/y (already tracked
                # in self._rel_offset_x/y) plus the monitor_res they
                # were computed against, so the importing machine can
                # re-derive correct raw offsets for its own screen. The
                # raw values are kept too, but only as comments -- see
                # _insert_offset_comments.
                crosshair_export = dict(self.cfg["crosshair"])
                raw_x = crosshair_export.pop("offset_x", 0)
                raw_y = crosshair_export.pop("offset_y", 0)
                crosshair_export["rel_offset_x"] = int(round(self._rel_offset_x))
                crosshair_export["rel_offset_y"] = int(round(self._rel_offset_y))
                # A specific output/connector name (e.g. "DP-2") is tied
                # to this machine's setup and almost never matches
                # anyone else's -- worse, it'd silently pin the imported
                # crosshair to whatever *that* connector happens to be
                # on the importing machine, if it exists at all. Left
                # out entirely (not even as a comment) rather than
                # exported: anyone who wants it for their own personal
                # reuse can add `output = "..."` back by hand.
                crosshair_export.pop("output", None)

                geo = self._get_monitor_geometry()
                monitor_res = format_monitor_res(*geo) if geo else ""

                bundle = {
                    "crosshair": crosshair_export,
                    "daemon": dict(self.cfg["daemon"]),
                    "import": {
                        "monitor_res": monitor_res,
                        # Deliberately left blank: this machine's own
                        # raw/scaled preference wouldn't mean anything
                        # on a different screen -- the importing side
                        # decides for itself, via the prompt.
                        "keep_rel_offset": "",
                    },
                }
                image_path = self.cfg["crosshair"].get("image", "")
                if image_path and Path(image_path).expanduser().exists():
                    try:
                        bundle["image_data"] = encode_image_bundle(Path(image_path).expanduser())
                    except Exception as exc:  # noqa: BLE001
                        self._set_status_text(f"⚠ Could not embed image: {exc}")
                try:
                    text = self._insert_offset_comments(dump_toml(bundle), raw_x, raw_y, monitor_res)
                    path.write_text(text)
                    self._set_status_text(f"Exported to {path.name}")
                except Exception as exc:  # noqa: BLE001
                    self._set_status_text(f"⚠ Export failed: {exc}")
        dialog.destroy()

    @staticmethod
    def _insert_offset_comments(text: str, raw_x, raw_y, monitor_res: str) -> str:
        """Splice commented-out raw offset_x/offset_y lines back into an
        exported config, right above the rel_offset_x/y lines that
        replace them as the live values.

        These comments are never read back by anything -- tomllib
        ignores comment lines entirely, and crosshaird.py only ever
        looks up offset_x/offset_y as live keys -- they're kept purely
        so someone running crosshaird directly off an exported file,
        without ever going through crosshair-gui's Import button, can
        still see (and manually uncomment/restore) the exact pixel
        values the config was originally created with.
        """
        res_note = f" (on a {monitor_res} display)" if monitor_res else ""
        out = []
        for line in text.split("\n"):
            if line.startswith("rel_offset_x = "):
                out.append(f"#offset_x = {raw_x}  # original offset{res_note}, see rel_offset_x below")
            elif line.startswith("rel_offset_y = "):
                out.append(f"#offset_y = {raw_y}  # original offset{res_note}, see rel_offset_y below")
            out.append(line)
        return "\n".join(out)

    def _on_import_clicked(self, _btn):
        dialog = Gtk.FileChooserNative.new(
            "Import Crosshair Config", self, Gtk.FileChooserAction.OPEN, "_Open", "_Cancel"
        )
        toml_filter = Gtk.FileFilter()
        toml_filter.set_name("Crosshair config (*.toml)")
        toml_filter.add_pattern("*.toml")
        dialog.add_filter(toml_filter)
        dialog.connect("response", self._on_import_response)
        self._active_dialog = dialog
        dialog.show()

    def _on_import_response(self, dialog, response):
        self._active_dialog = None
        if response == Gtk.ResponseType.ACCEPT:
            gfile = dialog.get_file()
            path = Path(gfile.get_path()) if gfile and gfile.get_path() else None
            if path:
                self._import_bundle(path)
        dialog.destroy()

    def _import_bundle(self, path: Path):
        try:
            with open(path, "rb") as f:
                bundle = tomllib.load(f)
        except Exception as exc:  # noqa: BLE001
            self._set_status_text(f"⚠ Could not read {path.name}: {exc}")
            return

        _debug(f"import: parsed bundle keys={list(bundle.keys())}")
        _debug(f"import: bundle['crosshair']={bundle.get('crosshair')}")

        crosshair = dict(DEFAULT_CONFIG["crosshair"])
        crosshair.update(bundle.get("crosshair", {}))

        image_data = bundle.get("image_data")
        if image_data and "data_base64" in image_data:
            try:
                dest = decode_image_bundle(image_data)
                crosshair["image"] = str(dest)
            except Exception as exc:  # noqa: BLE001
                self._set_status_text(f"⚠ Could not decode embedded image: {exc}")

        _debug(f"import: merged crosshair dict to apply={crosshair}")
        _debug(f"import: id(self.cfg)={id(self.cfg)} id(old crosshair dict)={id(self.cfg['crosshair'])}")

        self.cfg["crosshair"] = crosshair
        daemon_cfg = dict(DEFAULT_CONFIG["daemon"])
        daemon_cfg.update(bundle.get("daemon", {}))
        self.cfg["daemon"] = daemon_cfg

        # Carries monitor_res/keep_rel_offset for _resolve_imported_offsets
        # (run below, inside _populate_from_cfg) to act on. Old-style
        # exports with no [import] section just merge in as all-blank,
        # same effect as no rel_offset_x/y being present at all.
        import_cfg = dict(DEFAULT_CONFIG["import"])
        import_cfg.update(bundle.get("import", {}))
        self.cfg["import"] = import_cfg

        _debug(f"import: id(new self.cfg['crosshair'])={id(self.cfg['crosshair'])} value={self.cfg['crosshair']}")

        self._loading = True
        try:
            self._populate_from_cfg()
        except Exception as exc:  # noqa: BLE001
            # Every individual widget update above is signal-blocked
            # (see _set_widget_silently), so a failure partway through
            # can't leave a control silently out of sync with self.cfg
            # -- but surface it instead of failing silently, and make
            # sure _loading is never left stuck True (which would
            # otherwise make every future slider/dropdown edit a no-op).
            self._set_status_text(f"⚠ Imported {path.name} but couldn't refresh the GUI: {exc}")
            self._loading = False
            return
        self._loading = False

        _debug(
            "import: post-populate widget values: "
            f"size={self.size_adj.get_value()} opacity={self.opacity_adj.get_value()} "
            f"offset_x(rel)={self.offset_x_adj.get_value()} offset_y(rel)={self.offset_y_adj.get_value()} "
            f"thickness={self.thickness_adj.get_value()} gap={self.gap_adj.get_value()}"
        )
        _debug(f"import: self.cfg['crosshair'] after populate={self.cfg['crosshair']}")

        # Discard any apply that was already debounced from an edit
        # made just before importing -- otherwise it fires ~150ms from
        # now using self.cfg, which by then is fine anyway, but there's
        # no reason to leave a stray timer pointed at a window that's
        # about to save/reload again immediately below.
        if self._apply_source_id is not None:
            GLib.source_remove(self._apply_source_id)
            self._apply_source_id = None

        self._apply_now()
        self._set_status_text(f"Imported {path.name}")


class CrosshairGuiApp(Gtk.Application):
    def __init__(self, config_path: Path, socket_path: Path):
        super().__init__(application_id="io.github.crosshair-overlay.settings")
        self.config_path = config_path
        self.socket_path = socket_path
        self.window = None

    def do_activate(self):
        if self.window is None:
            self.window = CrosshairSettingsWindow(self, self.config_path, self.socket_path)
        self.window.present()


def main():
    parser = argparse.ArgumentParser(description="Settings GUI for the crosshair-overlay daemon")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    args = parser.parse_args()

    app = CrosshairGuiApp(args.config, args.socket)
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main())
