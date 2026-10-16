"""Native drop payload handling without opening a graphical window."""
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, media_drop_paths


class MediaDropTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.interpreter = tk.Tcl()
        self.paths = [self.folder / 'one photo.jpg', self.folder / '東京 {two};[exit].PNG']
        for path in self.paths:
            path.write_bytes(b'media')
        self.app = App.__new__(App)
        self.app.root = Mock()
        self.app.root.tk = self.interpreter
        self.app.root.grab_current.return_value = None
        self.app.busy = self.app.search_mode = False
        self.app.error = Mock()
        self.app.open_media = Mock()
        self.app.remember_media_folder = Mock()
        self.app.t = lambda text: text

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, paths):
        return self.interpreter.call('list', *(str(path) for path in paths))

    def test_multiple_files_spaces_unicode_tcl_characters_and_duplicates(self):
        payload = self.payload(self.paths + self.paths)
        self.assertEqual(media_drop_paths(self.interpreter, payload), self.paths)
        self.assertEqual(self.app.drop_media(payload), 'copy')
        self.app.open_media.assert_called_once_with(self.paths)
        self.app.error.assert_not_called()

    def test_ubuntu_tkdnd_utf8_filename_compatibility(self):
        mangled = str(self.paths[1]).encode("utf-8").decode("latin-1")
        self.assertEqual(media_drop_paths(self.interpreter, self.payload([mangled])), self.paths[1:])

    def test_local_file_uri(self):
        payload = self.payload([self.paths[0].as_uri()])
        self.assertEqual(media_drop_paths(self.interpreter, payload), self.paths[:1])

    def test_bad_mixed_batch_is_rejected_before_opening_anything(self):
        wrong = self.folder / 'not-media.json'
        wrong.write_text('{}')
        for path in [wrong, self.folder, self.folder / 'missing.jpg', 'file://other-host/photo.jpg']:
            with self.subTest(path=path):
                self.assertEqual(self.app.drop_media(self.payload([self.paths[0], path])), 'refuse_drop')
        self.app.open_media.assert_not_called()

    def test_busy_search_and_unrelated_modal_refuse_drops(self):
        payload = self.payload(self.paths)
        self.app.busy = True
        self.assertEqual(self.app.drop_media(payload), 'refuse_drop')
        self.app.busy, self.app.search_mode = False, True
        self.assertEqual(self.app.drop_media(payload), 'refuse_drop')
        self.app.search_mode = False
        self.app.root.grab_current.return_value = Mock()
        self.assertEqual(self.app.drop_media(payload), 'refuse_drop')
        self.app.open_media.assert_not_called()

    def test_json_attachment_accepts_one_file_and_closes_gallery(self):
        dialog, select = Mock(), Mock()
        self.assertEqual(self.app.drop_media(self.payload(self.paths), dialog, select), 'refuse_drop')
        dialog.destroy.assert_not_called()
        self.assertEqual(self.app.drop_media(self.payload(self.paths[:1]), dialog, select), 'copy')
        dialog.destroy.assert_called_once()
        select.assert_called_once_with(self.paths[0])
        self.app.open_media.assert_not_called()


if __name__ == '__main__':
    unittest.main()
