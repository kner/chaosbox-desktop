import sys
import tempfile

from PIL import Image
import core
from pathlib import Path
from unittest.mock import Mock, patch
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App

class MediaDeletionTests(unittest.TestCase):
    def app(self):
        app = App.__new__(App)
        app.busy = False
        app.profile = Mock(images=Path('/dataset/JPG'))
        app.settings = Mock()
        app.log = Mock()
        app.t = lambda text, **kw: text.format(**kw)
        app.clear = Mock()
        app.choose_media = Mock()
        app.status = Mock()
        app.error = Mock()
        app.task = Mock()
        return app

    def test_png_deletion_preserves_recovery_copy_and_sync_tombstone(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'JPG' / 'rea' / 'Unbenannt.png'
            path.parent.mkdir(parents=True)
            Image.new('RGB', (8, 8), 'red').save(path)
            original = path.read_bytes()
            core.delete_entry(path)
            self.assertFalse(path.exists())
            store = core.sync_store(core.profile_for_path(path))
            heads = [op for versions in store.heads().values() for op in versions
                     if op['kind'] == 'media']
            self.assertEqual(len(heads), 1)
            self.assertEqual(heads[0]['action'], 'delete')
            self.assertEqual(heads[0]['path'], 'rea/Unbenannt.png')
            copies = list((store.root / 'trash').glob('*/Unbenannt.png'))
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].read_bytes(), original)

    def test_index_refresh_reads_existing_png_as_media(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'Unbenannt.png'
            Image.new('RGB', (8, 8), 'red').save(path)
            with patch('core.load_index', return_value=[]), patch('core.write_index') as write:
                entries = core.update_index(Mock(), [path])
            self.assertEqual(len(entries), 1)
            self.assertTrue(entries[0].media)
            write.assert_called_once()

    def test_cancel(self):
        app, dialog = self.app(), Mock()
        with patch('app.messagebox.askyesno', return_value=False):
            app.delete_media_selection([Path('/dataset/JPG/a.jpg')], dialog)
        dialog.destroy.assert_not_called()
        app.task.assert_not_called()

    def test_external_file_refused(self):
        app = self.app()
        with patch('app.messagebox.showerror') as error:
            app.delete_media_selection([Path('/outside/a.jpg')], Mock())
        error.assert_called_once()
        app.task.assert_not_called()

    def test_batch_updates_index_and_refreshes(self):
        app, dialog = self.app(), Mock()
        paths = [Path('/dataset/JPG/a.jpg'), Path('/dataset/JPG/b.mp4')]
        with patch('app.messagebox.askyesno', return_value=True):
            app.delete_media_selection(paths, dialog)
        work, done, failed = app.task.call_args.args
        with patch('core.delete_entry') as delete, patch('core.update_index') as index:
            work()
        self.assertEqual([c.args[0] for c in delete.call_args_list], paths)
        index.assert_called_once_with(app.settings, paths, app.log)
        done(None)
        app.clear.assert_called_once()
        app.choose_media.assert_called_once()
        dialog.destroy.assert_called_once()

    def test_partial_failure_updates_index_and_refreshes(self):
        app = self.app()
        with patch('app.messagebox.askyesno', return_value=True):
            app.delete_media_selection([Path('/dataset/JPG/a.jpg'), Path('/dataset/JPG/b.mp4')], Mock())
        work, done, failed = app.task.call_args.args
        with patch('core.delete_entry', side_effect=[None, OSError('disk error')]), patch('core.update_index') as index:
            with self.assertRaises(OSError):
                work()
        index.assert_called_once()
        failed('disk error')
        app.clear.assert_called_once()
        app.choose_media.assert_called_once()
        app.error.assert_called_once_with('disk error')

if __name__ == "__main__":
    unittest.main()
