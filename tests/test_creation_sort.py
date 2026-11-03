from datetime import datetime
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

import core


class CreationSortTests(unittest.TestCase):
    def test_varied_filenames(self):
        examples = {
            'IMG_20240102_030405.jpg': '2024-01-02T03:04:05',
            '2024-01-02_03-04-05.png': '2024-01-02T03:04:05',
            'Screenshot_2024.1.2 03.04.05.jpg': '2024-01-02T03:04:05',
            'VID20240102030405.mp4': '2024-01-02T03:04:05',
            'IMG-20240102-WA0001.jpg': '2024-01-02T00:00:00',
            'photo_02-01-2024.jpg': '2024-01-02T00:00:00',
            'photo_12-31-2024.jpg': '2024-12-31T00:00:00',
            '2 January 2024.jpg': '2024-01-02T00:00:00',
            'Jan 2, 2024.jpg': '2024-01-02T00:00:00',
            '2024_Jan_2.jpg': '2024-01-02T00:00:00',
            'copy_20230230_20240229.jpg': '2024-02-29T00:00:00',
        }
        for name, expected in examples.items():
            with self.subTest(name=name):
                self.assertEqual(core.filename_creation_date(name).isoformat(), expected)
        for name in ('IMG_123456.jpg', '20230229.jpg', '1202401029.jpg', 'holiday_2024.jpg'):
            with self.subTest(name=name):
                self.assertIsNone(core.filename_creation_date(name))

    def test_filename_priority_discrepancy_and_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'IMG_20240102_030405.jpg'
            path.touch()
            named = datetime(2024, 1, 2, 3, 4, 5).timestamp()
            os.utime(path, (named + 3600, named + 3600))
            with patch.object(core, 'open_image_metadata'), patch.object(core, 'image_capture_tags') as tags:
                tags.return_value = {'DateTimeOriginal': '2024:01:02 02:04:05'}
                self.assertEqual(core.selection_creation_time(path), named)
                self.assertEqual(core.sort_media_for_selection([path]).creation_dates, {path: '2.1.2024'})
                tags.return_value = {'DateTimeOriginal': '2020:01:01 00:00:00'}
                self.assertEqual(core.selection_creation_time(path), datetime(2020, 1, 1).timestamp())
                selection = core.sort_media_for_selection([path])
                self.assertEqual(selection, [path])
                self.assertEqual(selection.creation_dates, {path: '1.1.2020'})
                tags.return_value = {'DateTimeOriginal': '2025:01:01 00:00:00'}
                self.assertEqual(core.selection_creation_time(path), named)
                older = datetime(2019, 1, 1).timestamp()
                os.utime(path, (older, older))
                self.assertEqual(core.selection_creation_time(path), older)
            with patch.object(core, 'open_image_metadata', side_effect=ValueError('bad metadata')):
                self.assertEqual(core.selection_creation_time(path), older)
                plain = path.rename(Path(folder)/'undated.jpg')
                self.assertEqual(core.selection_creation_time(plain), older)

    def test_metadata_timezone_and_newest_first(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder)/name for name in ('b.jpg', 'a.jpg', 'old.jpg')]
            for path in paths:
                path.touch()
            with patch.object(core, 'open_image_metadata'), patch.object(core, 'image_capture_tags',
                    return_value={'DateTimeOriginal': '2024:01:02 03:04:05', 'OffsetTimeOriginal': '+02:00'}):
                self.assertEqual(core.selection_creation_time(paths[0]),
                                 datetime.fromisoformat('2024-01-02T03:04:05+02:00').timestamp())
            with patch.object(core, 'selection_creation_time', side_effect=[20, 20, 10]):
                self.assertEqual(core.sort_media_for_selection(paths), [paths[1], paths[0], paths[2]])
