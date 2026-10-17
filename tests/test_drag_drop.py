"""Native drop payload handling without opening a graphical window."""
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch
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
        self.app.t = lambda text, **values: text.format(**values)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, paths):
        return self.interpreter.call('list', *(str(path) for path in paths))

    def test_multiple_files_spaces_unicode_tcl_characters_and_duplicates(self):
        payload = self.payload(self.paths + self.paths)
        self.assertEqual(media_drop_paths(self.interpreter, payload), self.paths)
        self.assertEqual(self.app.drop_media(payload), 'copy')
        self.app.open_media.assert_called_once_with(self.paths, preserve_fields=True)
        self.app.error.assert_not_called()

    def test_ubuntu_tkdnd_utf8_filename_compatibility(self):
        mangled = str(self.paths[1]).encode("utf-8").decode("latin-1")
        self.assertEqual(media_drop_paths(self.interpreter, self.payload([mangled])), self.paths[1:])

    def test_local_file_uri(self):
        payload = self.payload([self.paths[0].as_uri()])
        self.assertEqual(media_drop_paths(self.interpreter, payload), self.paths[:1])

    def test_x11_reads_fresh_selection_for_consecutive_external_drops(self):
        interpreter = self.interpreter
        interpreter.eval('''
            namespace eval ::tkdnd::xdnd {
                variable _drop_target .
                variable _common_drag_source_types {text/uri-list}
                proc _normalise_data {type data} {return $data}
                proc _selection_get {args} {error "stale native reader used"}
                proc _GetDroppedData {time} {_selection_get}
            }
            proc tk {args} {return x11}
            proc selection {args} {return $::fresh_selection}
            proc winfo {args} {error "external source"}
            proc package {args} {return 2.6}
        ''')
        self.app.dnd_available = False
        self.app.preview = self.app.open_media_button = Mock()
        self.app.register_media_drop = Mock()
        self.app.setup_media_drop()
        for path in self.paths:
            interpreter.setvar('fresh_selection', str(path))
            data = interpreter.call('::tkdnd::xdnd::_GetDroppedData', 0)
            self.assertEqual(self.app.drop_media(self.payload([data])), 'copy')
            self.app.open_media.assert_called_with([path], preserve_fields=True)

    def test_external_uri_selection_as_bytes_or_text(self):
        interpreter = self.interpreter
        # Exercise the installed Ubuntu decoder, including its broken byte-list
        # conversion, and XdndLeave arriving during selection retrieval.
        library = Path('/usr/lib/tkdnd2.6/tkdnd_unix.tcl')
        if not library.is_file():
            self.skipTest('Ubuntu tkdnd 2.6 is not installed')
        interpreter.eval('proc package {args} {return 2.6}; proc bind {args} {}')
        interpreter.call('source', str(library.with_name('tkdnd.tcl')))
        interpreter.call('namespace', 'eval', '::tkdnd', ('source', str(library)))
        interpreter.eval('''
            set ::tkdnd::xdnd::_drop_target .
            set ::tkdnd::xdnd::_common_drag_source_types {text/uri-list}
            proc tk {args} {return x11}
            proc package {args} {return 2.6}
            proc selection {args} {
                ::tkdnd::xdnd::_HandleXdndLeave
                return $::fresh_selection
            }
        ''')
        self.app.preview = self.app.open_media_button = Mock()
        self.app.register_media_drop = Mock()
        self.app.setup_media_drop()
        for path in self.paths:
            uri = path.as_uri() + '\r\n'
            for payload in (uri, tuple(uri.encode('utf-8')),
                            ' '.join(hex(byte) for byte in uri.encode('utf-8'))):
                with self.subTest(path=path, payload_type=type(payload).__name__):
                    interpreter.setvar('fresh_selection', payload)
                    data = interpreter.call('::tkdnd::xdnd::_GetDroppedData', 0)
                    self.assertEqual(interpreter.getvar('::tkdnd::xdnd::_drop_target'), '.')
                    self.assertEqual(interpreter.splitlist(
                        interpreter.getvar('::tkdnd::xdnd::_common_drag_source_types')),
                        ('text/uri-list',))
                    self.assertEqual(self.app.drop_media(data), 'copy')
                    self.app.open_media.assert_called_with([path], preserve_fields=True)

    def test_open_dropped_media_keeps_all_fields_and_comment(self):
        self.app.fill = Mock()
        self.app.preview = Mock()
        self.app.preview_result = Mock(return_value=(None, ''))
        self.app.field_widgets = {'device': Mock()}
        self.app.selected = Mock()
        self.app.set_preview_path = Mock()
        self.app.created = 'existing creation date'
        self.app.task = lambda work, done: done(work())
        originals = [{'device': 'image metadata'}, {'device': 'different metadata'}]
        with patch('app.core.read_metadata_batch', return_value=originals), \
             patch('app.MetadataModeDialog') as dialog:
            App.open_media(self.app, self.paths, preserve_fields=True)
        self.app.fill.assert_not_called()
        dialog.assert_not_called()
        self.assertEqual(self.app.created, 'existing creation date')
        self.assertEqual(self.app.media, self.paths)
        self.assertEqual(self.app.media_records, originals)
        self.assertEqual(self.app.media_edit_mode, 'replace')
        self.app.preview.set_image.assert_called_once_with(None)

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
