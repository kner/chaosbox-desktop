"""Preview lifecycle and exact-output saving without a display server."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from app import PosterPreview


class PosterPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        sources = [root / 'page.jpg', root / 'page_2.jpg']
        for index, source in enumerate(sources):
            source.write_bytes(b'preview JPEG bytes ' + bytes([index]))
        self.app = SimpleNamespace(busy=False, status=Mock())
        self.preview = SimpleNamespace(app=self.app, saving=False, outputs=sources,
                                       directory=root / 'poster', save_button=Mock(), destroy=Mock(),
                                       t=lambda text, **kw: text.format(**kw))
        self.errors = []
        def task(work, done, failed):
            try:
                result = work()
            except Exception as error:
                self.errors.append(error)
                failed(error)
            else:
                done(result)
        self.app.task = task

    def test_save_keeps_exact_bytes_and_preserves_existing_files(self):
        self.preview.directory.mkdir()
        existing = self.preview.directory / 'page.jpg'
        existing.write_bytes(b'existing')
        PosterPreview.save(self.preview)
        self.assertEqual(existing.read_bytes(), b'existing')
        self.assertEqual((self.preview.directory / 'page_1.jpg').read_bytes(), self.preview.outputs[0].read_bytes())
        self.assertEqual((self.preview.directory / 'page_2.jpg').read_bytes(), self.preview.outputs[1].read_bytes())
        self.preview.destroy.assert_called_once()
        self.assertFalse(self.errors)

    def test_failed_save_rolls_back_new_files_and_allows_retry(self):
        with patch('app.core.atomic_write', side_effect=OSError('disk full')), patch('app.messagebox.showerror'):
            PosterPreview.save(self.preview)
        self.assertEqual(list(self.preview.directory.iterdir()), [])
        self.assertFalse(self.preview.saving)
        self.preview.destroy.assert_not_called()
        self.preview.save_button.configure.assert_called_with(state='normal')

    def test_close_discards_preview_without_saving(self):
        PosterPreview.close(self.preview)
        self.preview.destroy.assert_called_once()
        self.assertFalse(self.preview.directory.exists())

    def test_close_waits_for_save(self):
        self.preview.saving = True
        PosterPreview.close(self.preview)
        self.preview.destroy.assert_not_called()
