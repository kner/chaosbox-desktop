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


class PosterRefreshTests(unittest.TestCase):
    def setUp(self):
        from app import App
        self.app = App.__new__(App)
        self.app.busy = self.app.search_mode = False
        self.app.t = lambda text, **kw: text.format(**kw)
        self.app.status, self.app.log = Mock(), Mock()
        self.app.poster_preview = Mock()
        self.app.poster_preview.winfo_exists.return_value = True
        self.app.poster_preview.source_paths = [Path('/images/first.jpg'), Path('/images/second.jpg')]
        self.app.poster_preview.profile = SimpleNamespace(images=Path('/images'))
        self.app.poster_preview.settings = object()
        self.app.poster_preview.setup_dir = Path('/setup')
        self.app.values = Mock(side_effect=AssertionError('Refresh must use saved metadata'))

    def test_individual_and_common_save_replace_same_preview(self):
        from app import App
        for count in (1, 2):
            with self.subTest(count=count):
                self.setUp()
                preview = self.app.poster_preview
                originals = list(preview.source_paths[:count])
                saved = [Path('/saved') / path.name for path in originals]
                temporary = Mock(name='temporary')
                self.app.task = lambda work, done: done(work())
                with patch('app.tempfile.TemporaryDirectory', return_value=temporary), \
                     patch('app.core.create_posters', return_value=[Path('/tmp/poster.jpg')]) as render, \
                     patch('app.PosterPreview') as window:
                    temporary.name = '/tmp/render'
                    App.refresh_poster_after_save(self.app, originals, saved)
                self.assertEqual(render.call_args.args[0], saved + originals[count:] if count == 2 else saved + [Path('/images/second.jpg')])
                self.assertEqual(render.call_args.kwargs, {'field_overrides': {}, 'setup_dir': Path('/setup'), 'layout_state': {}})
                preview.replace.assert_called_once()
                window.assert_not_called()
                self.assertIs(self.app.poster_preview, preview)

    def test_closed_preview_is_not_reopened_after_render(self):
        temporary = Mock(name='temporary')
        temporary.name = '/tmp/render'
        def task(work, done):
            result = work()
            self.app.poster_preview = None
            done(result)
        self.app.task = task
        with patch('app.tempfile.TemporaryDirectory', return_value=temporary), \
             patch('app.core.create_posters', return_value=[Path('/tmp/poster.jpg')]), \
             patch('app.PosterPreview') as window:
            self.app.refresh_poster_after_save([Path('/images/first.jpg')], [Path('/saved/first.jpg')])
        window.assert_not_called()
        temporary.cleanup.assert_called_once()

    def test_no_open_preview_does_not_render_on_save(self):
        self.app.poster_preview = None
        self.app.make_poster = Mock()
        self.app.refresh_poster_after_save([], [])
        self.app.make_poster.assert_not_called()

    def test_replacing_pages_preserves_page_and_cleans_old_temporary_files(self):
        previous, temporary = Mock(), Mock()
        preview = SimpleNamespace(temporary=previous, index=3, show=Mock(), save_button=Mock())
        PosterPreview.replace(preview, [Path('/page1.jpg'), Path('/page2.jpg')], temporary, Path('/poster'))
        preview.show.assert_called_once_with(1)
        self.assertIs(preview.temporary, temporary)
        previous.cleanup.assert_called_once()

    def test_individual_mode_renders_whole_selection_with_current_image_edits_only(self):
        self.app.poster_preview = None
        paths = [Path('/images/first.jpg'), Path('/images/second.jpg')]
        self.app.individual_paths = paths
        self.app.media = paths[1:]
        self.app.profile = SimpleNamespace(images=Path('/images'))
        self.app.settings = SimpleNamespace(poster=object(), path=Path('/setup/setup.ini'))
        self.app.media_baseline = {'device': 'A1', 'comment': 'saved'}
        self.app.media_edit_mode = 'replace'
        self.app.values = Mock(return_value={'device': 'B2', 'comment': 'saved'})
        self.app.task = lambda work, done: done(work())
        temporary = Mock()
        temporary.name = '/tmp/render'
        with patch('app.tempfile.TemporaryDirectory', return_value=temporary), \
             patch('app.core.create_posters', return_value=[Path('/tmp/poster.jpg')]) as render, \
             patch('app.PosterPreview') as window:
            self.app.make_poster()
        self.assertEqual(render.call_args.args[0], paths)
        self.assertEqual(render.call_args.kwargs, {'field_overrides': {},
                         'path_overrides': {paths[1]: {'device': 'B2'}}, 'setup_dir': Path('/setup'), 'layout_state': {}})
        self.assertEqual(window.return_value.source_paths, paths)

    def test_individual_save_refresh_passes_previous_layout_and_changed_image(self):
        self.app.individual_paths = list(self.app.poster_preview.source_paths)
        self.app.poster_preview.layout_state = {"pages": []}
        old_layout = self.app.poster_preview.layout_state
        self.app.task = lambda work, done: done(work())
        temporary = Mock()
        temporary.name = '/tmp/render'
        with patch('app.tempfile.TemporaryDirectory', return_value=temporary), \
             patch('app.core.create_posters', return_value=[Path('/tmp/poster.jpg')]) as render:
            self.app.refresh_poster_after_save([Path('/images/second.jpg')], [Path('/saved/second.jpg')])
        self.assertIs(render.call_args.kwargs['previous_layout'], old_layout)
        self.assertEqual(render.call_args.kwargs['changed_indices'], {1})
