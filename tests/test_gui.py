import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('crosshair_gui', ROOT / 'crosshair-gui.py')
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Gtk.init()
        cls.app = Gtk.Application(application_id='io.github.crosshair.tests',
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.toml'
        self.socket = Path(self.tmp.name) / 'control.sock'
        self.send = patch.object(gui, 'send_control_command', return_value=False)
        self.send.start()
        self.addCleanup(self.send.stop)
        with patch.object(gui.CrosshairSettingsWindow, '_start_overlay'):
            self.window = gui.CrosshairSettingsWindow(self.app, self.path, self.socket)
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        self.window._on_close_request()
        self.window.destroy()

    def test_close_flushes_last_slider_change(self):
        self.window.size_adj.set_value(42)
        self.assertFalse(self.path.exists())
        self.window._on_close_request()
        self.assertEqual(gui.load_config(self.path)['crosshair']['size'], 42)
        self.assertIsNone(self.window._apply_source_id)

    def test_selected_shape_cannot_be_deselected(self):
        btn = self.window._shape_buttons['cross']
        btn.set_active(False)
        self.assertTrue(btn.get_active())
        self.window._shape_buttons['circle'].set_active(True)
        self.assertFalse(btn.get_active())
        self.assertEqual(self.window._current_mode, 'circle')
        self.assertFalse(self.window.gap_row.get_sensitive())

    def test_cancel_image_replacement_keeps_selection(self):
        self.window._current_mode = 'image'
        for key in self.window._shape_buttons:
            self.window._set_toggle_silently(key, key == 'image')
        with patch.object(gui.Gtk.FileChooserNative, 'new') as new:
            self.window._shape_buttons['image'].set_active(False)
            new.assert_called_once()
        dialog = Mock()
        self.window._on_image_chosen(dialog, Gtk.ResponseType.CANCEL,
                                     self.window._shape_buttons['image'])
        self.assertTrue(self.window._shape_buttons['image'].get_active())

    def test_invalid_import_leaves_settings_and_disk_untouched(self):
        gui.save_config(self.window.cfg, self.path)
        before = self.path.read_bytes()
        old_cfg = copy.deepcopy(self.window.cfg)
        for content in ('[crosshair]\nsize="oops"', 'crosshair="bad"',
                        '[crosshair]\nsize=30\n[image_data]\ndata_base64="%%%"',
                        '[crosshair]\nimage="/missing/crosshair.png"'):
            imported = Path(self.tmp.name) / 'import.toml'
            imported.write_text(content)
            self.window._import_bundle(imported)
            self.assertEqual(self.window.cfg, old_cfg)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertIn('Could not import', self.window.applied_label.get_label())

    def test_valid_portable_import_resolves_and_saves_offsets(self):
        imported = Path(self.tmp.name) / 'import.toml'
        imported.write_text('[crosshair]\nsize=30\nrel_offset_x=10\nrel_offset_y=-5\n')
        with patch.object(self.window, '_get_monitor_geometry', return_value=(1920, 1080)):
            self.window._import_bundle(imported)
        c = gui.load_config(self.path)['crosshair']
        self.assertEqual((c['offset_x'], c['offset_y']), (955, 520))
        self.assertNotIn('rel_offset_x', c)
        self.assertEqual(self.window.size_adj.get_value(), 30)

    def test_start_passes_custom_paths_and_saves_first(self):
        with patch.object(gui.subprocess, 'Popen') as popen:
            self.window._start_overlay()
            args = popen.call_args.args[0]
            self.assertEqual(args[-4:], ['--config', str(self.path), '--socket', str(self.socket)])
            self.assertTrue(self.path.exists())
            self.window._start_overlay()
            popen.assert_called_once()

    def test_failed_image_export_does_not_create_incomplete_bundle(self):
        self.window.cfg['crosshair']['image'] = '/missing/image.png'
        dest = Path(self.tmp.name) / 'export.toml'
        dialog = Mock()
        dialog.get_file.return_value.get_path.return_value = str(dest)
        self.window._on_export_response(dialog, Gtk.ResponseType.ACCEPT)
        self.assertFalse(dest.exists())
        self.assertIn('Could not embed image', self.window.applied_label.get_label())

    def test_failed_save_is_not_reported_as_successful_import(self):
        imported = Path(self.tmp.name) / 'import.toml'
        imported.write_text('[crosshair]\nsize=40\n')
        with patch.object(gui, 'save_config', side_effect=OSError('disk full')):
            self.window._import_bundle(imported)
        self.assertIn('Could not save config', self.window.applied_label.get_label())

    def test_embedded_image_export_import_round_trip(self):
        import cairo
        image_path = Path(self.tmp.name) / '🎯.png'
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 24, 24)
        surface.write_to_png(str(image_path))
        self.window.cfg['crosshair']['image'] = str(image_path)
        dest = Path(self.tmp.name) / 'export.toml'
        dialog = Mock()
        dialog.get_file.return_value.get_path.return_value = str(dest)
        self.window._on_export_response(dialog, Gtk.ResponseType.ACCEPT)
        self.assertTrue(dest.exists())
        self.assertNotIn(str(image_path), dest.read_text())
        decode = gui.decode_image_bundle
        with patch.object(gui, 'decode_image_bundle',
                          side_effect=lambda bundle: decode(bundle, Path(self.tmp.name) / 'images')):
            self.window._import_bundle(dest)
        self.assertEqual(self.window._current_mode, 'image')
        self.assertIsNotNone(self.window._preview_pixbuf)
        self.assertEqual(Path(self.window.cfg['crosshair']['image']).read_bytes(), image_path.read_bytes())
