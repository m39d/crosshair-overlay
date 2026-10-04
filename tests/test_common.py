import base64
import socket
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import cairo
import crosshair_common as common


class ConfigTests(unittest.TestCase):
    def test_defaults_are_independent(self):
        cfg = common.validate_config({})
        cfg['crosshair']['size'] = 30
        self.assertEqual(common.DEFAULT_CONFIG['crosshair']['size'], 24)

    def test_invalid_config_is_rejected_before_use(self):
        for value in ('large', True, float('nan'), 0, 301):
            with self.subTest(value=value), self.assertRaises(ValueError):
                common.validate_config({'crosshair': {'size': value}})
        for cfg in ({'crosshair': 'oops'}, {'daemon': {'start_visible': 'false'}},
                    {'crosshair': {'opacity': float('inf')}},
                    {'crosshair': {'rel_offset_x': float('nan')}}):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                common.validate_config(cfg)

    def test_unicode_and_quoted_keys_round_trip(self):
        cfg = {'crosshair': {'image': '/tmp/🎯.png'}, 'extra.section': {'a.b': 'hello'}}
        self.assertEqual(tomllib.loads(common.dump_toml(cfg)), cfg)

    def test_resolution_must_be_positive(self):
        for value in ('0x1080', '-2x1080', '1920x0', 'bad', ''):
            self.assertIsNone(common.parse_monitor_res(value))
        self.assertEqual(common.parse_monitor_res('2560x1440'), (2560, 1440))

    def test_failed_atomic_save_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            common.save_config(common.DEFAULT_CONFIG, path)
            previous = path.read_bytes()
            with patch.object(Path, 'replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    common.save_config({'crosshair': {'size': 80}}, path)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_invalid_file_load_has_no_partially_merged_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            path.write_text('[crosshair]\nsize = 80\nopacity = "bad"\n')
            self.assertEqual(common.load_config(path), common.DEFAULT_CONFIG)

    def test_embedded_image_validation_and_containment(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = common.decode_image_bundle({'filename': '../../cross.png',
                'data_base64': base64.b64encode(b'example').decode()}, Path(tmp))
            self.assertEqual(dest.parent, Path(tmp))
            self.assertEqual(dest.read_bytes(), b'example')
            with self.assertRaises(ValueError):
                common.decode_image_bundle({'data_base64': '%%%bad'}, Path(tmp))

    def test_stale_socket_is_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'control.sock'
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(path))
                server.listen(2)
                self.assertTrue(common.daemon_is_running(path))
            self.assertTrue(path.exists())
            self.assertFalse(common.daemon_is_running(path))

    def test_large_center_gap_does_not_draw_reversed_arms(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 24, 24)
        ctx = cairo.Context(surface)
        cfg = dict(common.DEFAULT_CONFIG['crosshair'], gap=60)
        common.render_crosshair(ctx, 24, 24, cfg)
        surface.flush()
        self.assertFalse(any(surface.get_data()))
