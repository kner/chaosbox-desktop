"""Poster optimization regressions: image geometry, cache invalidation and memory."""
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageOps, ImageStat, JpegImagePlugin

import core
import markdown_render


class PosterPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = SimpleNamespace(images=self.root / 'JPG')
        self.settings = core.PosterSettings(900, 100, 100, 2, 2, True, date_rotation=0)
        self.paths = []
        for index, color in enumerate(('red', 'blue', 'green')):
            path = self.root / f'{index}.png'
            Image.new('RGB', (120, 80), color).save(path)
            self.paths.append(path)

    def test_layout_reads_exif_orientation_without_decoding_jpeg(self):
        path = self.root / 'portrait.jpg'
        exif = Image.Exif()
        exif[274] = 6
        Image.new('RGB', (600, 300)).save(path, exif=exif)
        with patch.object(JpegImagePlugin.JpegImageFile, 'load', side_effect=AssertionError('pixel decode')):
            pages = core.poster_photo_pages([path], [{}], core.PosterSettings(cols=2, rows=2, fixed=False))
        self.assertEqual(pages, [[(0, 0, 0, 2, 1)]])

    def test_jpeg_downsampling_keeps_exact_geometry_and_exif_orientation(self):
        original = Image.linear_gradient('L').resize((2003, 1007)).convert('RGB')
        transpose = ImageOps.exif_transpose
        for orientation in range(1, 9):
            with self.subTest(orientation=orientation):
                path = self.root / f'orientation-{orientation}.jpg'
                exif = Image.Exif()
                exif[274] = orientation
                original.save(path, exif=exif, quality=95)
                with Image.open(path) as source:
                    expected = ImageOps.contain(transpose(source).convert('RGBA'), (140, 110), Image.Resampling.LANCZOS)
                decoded_sizes = []
                def observe(source):
                    decoded_sizes.append(source.size)
                    return transpose(source)
                with patch('core.ImageOps.exif_transpose', side_effect=observe):
                    actual = core.poster_load_photo(path, (140, 110))
                self.assertEqual(actual.size, expected.size)
                self.assertLess(decoded_sizes[0][0], original.width)
                difference = ImageChops.difference(actual.convert('RGB'), expected.convert('RGB'))
                self.assertLess(max(ImageStat.Stat(difference).mean), 1.5)

    def test_refresh_reuses_metadata_dates_and_captions_and_detects_replaced_file(self):
        initial, refreshed, replaced = {}, {}, {}
        with patch('core.open_image_metadata', wraps=core.open_image_metadata) as reader, \
                patch('core.poster_date', return_value='1.1.2020') as dates, \
                patch('markdown_render.layout', wraps=markdown_render.layout) as captions:
            core.create_posters(self.paths, self.profile, self.settings, layout_state=initial)
            self.assertEqual(reader.call_count, 3)
            self.assertEqual(dates.call_count, 3)
            self.assertGreater(captions.call_count, 0)
            reader.reset_mock(); dates.reset_mock(); captions.reset_mock()
            core.create_posters(self.paths, self.profile, self.settings, layout_state=refreshed,
                                previous_layout=initial, changed_indices={0})
            reader.assert_not_called()
            dates.assert_not_called()
            captions.assert_not_called()
            self.assertIs(initial['pages'][0]['panels'][1][0], refreshed['pages'][0]['panels'][1][0])
            before = self.paths[0].stat()
            replacement = self.root / 'replacement.png'
            Image.new('RGB', (120, 80), 'yellow').save(replacement)
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(replacement, self.paths[0])
            core.create_posters(self.paths, self.profile, self.settings, layout_state=replaced,
                                previous_layout=refreshed, changed_indices={0})
            self.assertEqual(reader.call_count, 1)
            self.assertEqual(dates.call_count, 1)
            self.assertNotEqual(refreshed['pages'][0]['panels'][0][0].tobytes(),
                                replaced['pages'][0]['panels'][0][0].tobytes())

    def test_caption_cache_respects_width_font_and_height_constraints(self):
        cache = {}
        for width, height, size in ((160, 200, 18), (50, 200, 18), (160, 200, 12), (160, 25, 18)):
            args = (['**Caption** with several words'], width, height, size, 8)
            expected, expected_height = markdown_render.fit(*args)
            actual, actual_height = markdown_render.fit(*args, cache=cache)
            self.assertEqual(actual_height, expected_height)
            self.assertEqual(actual[0].plain_lines, expected[0].plain_lines)
        with self.assertRaises(ValueError):
            markdown_render.fit(['**Caption** with several words'], 160, 0, 18, 8, cache=cache)

    def test_temporary_full_page_allocations_do_not_grow_with_photo_count(self):
        settings = core.PosterSettings(900, 100, 100, 3, 3, True)
        with patch('core.Image.new', wraps=Image.new) as allocate:
            core.create_poster(self.paths * 3, self.profile, settings, metadata=[{}] * 9)
        full_pages = [call for call in allocate.call_args_list if call.args[1] == (900, 900)]
        self.assertLessEqual(len(full_pages), 3)


if __name__ == '__main__':
    unittest.main()
