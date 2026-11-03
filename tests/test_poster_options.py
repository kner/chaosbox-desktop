from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
import core
import poster_project


class PosterOptionTests(unittest.TestCase):
    def test_options_and_project_roundtrip(self):
        settings = core.poster_settings('[Poster]\nPOSTER-TRANSPARENCY=100\nPOSTER-DATE-ROTATION=90\nPOSTER-FONT-SIZE=10')
        self.assertEqual((settings.transparency, settings.date_rotation, settings.font_size), (100, 90, 10))
        self.assertEqual(poster_project._settings(asdict(settings)), settings)
        self.assertEqual(poster_project._settings(asdict(core.PosterSettings())), core.PosterSettings())
        for value in ('', 'none', '180', 'junk', '-90'):
            self.assertIsNone(core.poster_settings('[Poster]\nPOSTER-DATE-ROTATION='+value).date_rotation)
        for key, value in (('TRANSPARENCY', '101'), ('TRANSPARENCY', '-1'),
                           ('FONT-SIZE', '0'), ('FONT-SIZE', 'nan')):
            with self.assertRaises(ValueError):
                core.poster_settings(f'[Poster]\nPOSTER-{key}={value}')

    def test_initial_transparency_changes_only_transparency(self):
        record = {'device': '2/1/3/90/25/7', 'comment': 'keep'}
        result = core.initial_poster_record(record, core.PosterSettings(transparency=100))
        self.assertEqual(result, {'device': '2/1/3/90/100/7', 'comment': 'keep'})
        self.assertEqual(record['device'], '2/1/3/90/25/7')
        self.assertEqual(core.initial_poster_record(record, core.PosterSettings()), record)
        self.assertEqual(core.poster_transparency({'device': ''}, 100), 100)
        self.assertEqual(core.poster_transparency({'device': '////0'}, 100), 0)

    def test_date_rendering_rotation_and_point_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'IMG_20261201_120000.png'
            Image.new('RGB', (80, 80), 'red').save(path)
            profile = SimpleNamespace(images=root/'JPG')
            outputs = []
            for rotation in (None, 0, 90):
                settings = core.PosterSettings(limit=600, width=100, height=100, cols=1, rows=1,
                                               date_rotation=rotation, font_size=10)
                with patch.object(core, 'selection_creation_time', return_value=datetime(2026, 12, 1).timestamp()), \
                     patch.object(core, 'poster_font', wraps=core.poster_font) as font:
                    output = core.create_poster([path], profile, settings,
                                                metadata=[{'comment': 'Test comment'}])
                    self.assertTrue(any(call.args[0] == round(10 * 25.4 / 72 * 6) for call in font.call_args_list))
                    with Image.open(output) as image:
                        outputs.append(image.copy())
            self.assertNotEqual(outputs[0].tobytes(), outputs[1].tobytes())
            self.assertNotEqual(outputs[1].tobytes(), outputs[2].tobytes())

    def test_date_badge_is_inside_bottom_right_with_translucent_black_and_opaque_text(self):
        for rotation in (0, 90):
            with self.subTest(rotation=rotation):
                photo = Image.new('RGBA', (300, 200), (255, 255, 255, 255))
                core.poster_date_overlay(photo, '1.12.2026', core.poster_font(20), rotation)
                self.assertEqual(photo.getpixel((0, 0)), (255, 255, 255, 255))
                self.assertEqual(photo.getpixel((299, 199)), (153, 153, 153, 255))
                colors = set(photo.getdata())
                self.assertIn((255, 255, 255, 255), colors)
                self.assertTrue(all(pixel[3] == 255 for pixel in colors))
                # Render on transparent photo to distinguish opaque glyphs from background.
                layer = Image.new('RGBA', (300, 200))
                core.poster_date_overlay(layer, '1.12.2026', core.poster_font(20), rotation)
                self.assertEqual(layer.getpixel((299, 199)), (0, 0, 0, 102))
                self.assertIn((255, 255, 255, 255), set(layer.getdata()))

    def test_date_badge_disabled_and_oversized(self):
        photo = Image.new('RGBA', (40, 40), 'red')
        before = photo.tobytes()
        core.poster_date_overlay(photo, '1.12.2026', core.poster_font(20), None)
        self.assertEqual(photo.tobytes(), before)
        with self.assertRaisesRegex(ValueError, 'does not fit inside'):
            core.poster_date_overlay(photo, '1.12.2026', core.poster_font(20), 0)
