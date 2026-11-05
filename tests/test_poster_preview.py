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
                                       directory=root / 'poster', save_button=Mock(), destroy=Mock(), title=Mock(),
                                       source_paths=sources, settings=object(), layout_state={}, setup_dir=root,
                                       profile=SimpleNamespace(id='Photos'),
                                       t=lambda text, **kw: text.format(**kw))
        writer = patch('app.poster_project.save_project', side_effect=lambda path, *args: Path(path).write_bytes(b'project snapshot'))
        self.project_writer = writer.start()
        self.addCleanup(writer.stop)
        names = patch('app.poster_project.poster_filename', side_effect=lambda name, paths: name)
        names.start()
        self.addCleanup(names.stop)
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
        self.preview.destroy.assert_not_called()
        self.preview.save_button.configure.assert_called_with(state="normal")
        self.assertFalse(self.errors)
        self.assertTrue(self.preview.frozen)
        self.assertEqual(self.preview.project_path, self.preview.directory / 'page_1.cbposter')
        self.assertEqual((self.preview.directory / 'page_1.cbposter').read_bytes(), b'project snapshot')
        self.assertFalse((self.preview.directory / 'page_2.cbposter').exists())
        self.assertEqual(len(list(self.preview.directory.glob("*.cbposter"))), 1)
        self.project_writer.assert_called_once()

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

    def test_destroy_releases_buffers_even_when_window_is_still_referenced(self):
        import weakref
        from concurrent.futures import Future
        from PIL import Image
        base, panel, thumbnail = [Image.new('RGBA', (32, 32)) for _ in range(3)]
        references = [weakref.ref(image) for image in (base, panel, thumbnail)]
        self.preview.layout_state = {'pages': [{'base': base, 'panels': [(panel, (0, 0))]}]}
        self.preview.viewer = SimpleNamespace(original=thumbnail, photo=Mock())
        self.preview.page_future = Future()
        self.preview.page_future.set_result(thumbnail)
        self.preview.page_worker = Mock()
        self.preview.page_poll = 'pending'
        self.preview.after_cancel = Mock()
        self.preview.temporary = Mock()
        self.app.poster_preview = self.preview
        del base, panel, thumbnail
        # Child-widget destruction must leave the active layout intact.
        PosterPreview.cleanup(self.preview, SimpleNamespace(widget=object()))
        self.assertTrue(all(reference() is not None for reference in references))
        PosterPreview.cleanup(self.preview, SimpleNamespace(widget=self.preview))
        self.assertTrue(all(reference() is None for reference in references))
        self.assertIsNone(self.preview.viewer.photo)
        self.assertIsNone(self.preview.page_future)
        self.assertIsNone(self.app.poster_preview)
        self.assertEqual(self.preview.source_paths, [])
        self.preview.after_cancel.assert_called_once_with('pending')
        self.preview.page_worker.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        self.preview.temporary.cleanup.assert_called_once()

    def test_close_waits_for_save(self):
        self.preview.saving = True
        PosterPreview.close(self.preview)
        self.preview.destroy.assert_not_called()

    def test_configuration_failure_rolls_back_both_posters_and_projects(self):
        self.project_writer.side_effect = OSError('configuration failed')
        with patch('app.messagebox.showerror'):
            PosterPreview.save(self.preview)
        self.assertEqual(list(self.preview.directory.iterdir()), [])
        self.assertFalse(self.preview.saving)
        self.preview.destroy.assert_not_called()

    def test_existing_configuration_is_preserved_and_new_pair_uses_same_basename(self):
        self.preview.directory.mkdir()
        existing = self.preview.directory / 'page.cbposter'
        existing.write_bytes(b'older project')
        PosterPreview.save(self.preview)
        self.assertEqual(existing.read_bytes(), b'older project')
        self.assertFalse((self.preview.directory / 'page.jpg').exists())
        self.assertTrue((self.preview.directory / 'page_1.jpg').is_file())
        self.assertTrue((self.preview.directory / 'page_1.cbposter').is_file())


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
        self.app.poster_preview.profile = SimpleNamespace(images=Path('/images'), id='Photos')
        self.app.poster_preview.settings = object()
        self.app.poster_preview.setup_dir = Path('/setup')
        self.app.profile = self.app.poster_preview.profile
        self.app.values = Mock(side_effect=AssertionError('Refresh must use saved metadata'))

    def test_setup_change_rebuilds_using_project_metadata_and_new_settings(self):
        preview = self.app.poster_preview
        preview.saving = False
        preview.project_path = Path('/poster/project.cbposter')
        records = [{'comment': 'Saved first'}, {'comment': 'Saved second'}]
        preview.layout_state = {'metadata': records}
        settings = object()
        self.app.task = lambda work, done, failed=None: done(work())
        temporary = Mock(name='temporary')
        temporary.name = '/tmp/render'
        with patch('app.tempfile.TemporaryDirectory', return_value=temporary), \
             patch('app.core.create_posters', return_value=[Path('/tmp/new.jpg')]) as render:
            self.app.apply_poster_setup(preview, settings)
        self.assertIs(render.call_args.args[2], settings)
        self.assertEqual(render.call_args.kwargs['path_overrides'], dict(zip(preview.source_paths, records)))
        self.assertNotIn('previous_layout', render.call_args.kwargs)
        self.assertIs(preview.settings, settings)
        preview.replace.assert_called_once()

    def test_setup_without_loaded_project_does_nothing(self):
        preview = self.app.poster_preview
        preview.saving = False
        preview.project_path = None
        self.app.task = Mock()
        self.app.apply_poster_setup(preview, object())
        self.app.task.assert_not_called()

    def test_individual_and_common_save_replace_same_preview(self):
        from app import App
        for count in (1, 2):
            with self.subTest(count=count):
                self.setUp()
                preview = self.app.poster_preview
                originals = list(preview.source_paths[:count])
                saved = [Path('/saved') / path.name for path in originals]
                temporary = Mock(name='temporary')
                self.app.task = lambda work, done, failed=None: done(work())
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
        def task(work, done, failed=None):
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
        self.app.profile = SimpleNamespace(images=Path('/images'), id='Photos')
        self.app.settings = SimpleNamespace(poster=object(), path=Path('/setup/setup.ini'))
        self.app.media_baseline = {'device': 'A1', 'comment': 'saved'}
        self.app.media_edit_mode = 'replace'
        self.app.values = Mock(return_value={'device': 'B2', 'comment': 'saved'})
        self.app.task = lambda work, done, failed=None: done(work())
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
        self.app.task = lambda work, done, failed=None: done(work())
        temporary = Mock()
        temporary.name = '/tmp/render'
        with patch('app.tempfile.TemporaryDirectory', return_value=temporary), \
             patch('app.core.create_posters', return_value=[Path('/tmp/poster.jpg')]) as render:
            self.app.refresh_poster_after_save([Path('/images/second.jpg')], [Path('/saved/second.jpg')])
        self.assertIs(render.call_args.kwargs['previous_layout'], old_layout)
        self.assertEqual(render.call_args.kwargs['changed_indices'], {1})

    def test_frozen_project_common_edits_keep_project_settings_and_geometry(self):
        self.app.poster_preview.frozen = True
        self.app.poster_preview.layout_state = {'pages': []}
        old_layout = self.app.poster_preview.layout_state
        self.app.media = list(self.app.poster_preview.source_paths)
        self.app.individual_paths = None
        self.app.profile = self.app.poster_preview.profile
        self.app.settings = SimpleNamespace(poster=object(), path=Path('/different/setup.ini'))
        self.app.media_baseline = {'comment': 'Old'}
        self.app.values = Mock(return_value={'comment': 'New'})
        self.app.task = lambda work, done, failed=None: done(work())
        temporary = Mock()
        temporary.name = '/tmp/render'
        with patch('app.tempfile.TemporaryDirectory', return_value=temporary), \
             patch('app.core.create_posters', return_value=[Path('/tmp/poster.jpg')]) as render:
            self.app.make_poster()
        self.assertIs(render.call_args.kwargs['previous_layout'], old_layout)
        self.assertEqual(render.call_args.kwargs['changed_indices'], {0, 1})
        self.assertEqual(render.call_args.kwargs['field_overrides'], {'comment': 'New'})
        self.assertIs(render.call_args.args[2], self.app.poster_preview.settings)
        self.assertEqual(render.call_args.kwargs['setup_dir'], Path('/setup'))

    def test_individual_navigation_uses_project_metadata(self):
        self.app.individual_paths = list(self.app.poster_preview.source_paths)
        self.app.individual_index = 0
        self.app.poster_preview.frozen = True
        records = [{'device': '/A1/90/100'}, {'device': '/B1//50'}]
        self.app.poster_preview.layout_state = {'metadata': records}
        self.app.preview_result = Mock(return_value=(None, ''))
        results = []
        self.app.task = lambda work, done, failed=None: results.append(work())
        with patch('app.core.read_metadata_batch', side_effect=AssertionError('Must use project snapshot')):
            self.app.navigate_preview(1)
        self.assertEqual(results[0][0], [records[1]])

    def test_edit_project_can_switch_between_individual_and_common_selection(self):
        self.app.settings = SimpleNamespace(profile=Mock(return_value=self.app.poster_preview.profile))
        self.app.root, self.app.refresh_profile, self.app.open_media = Mock(), Mock(), Mock()
        self.app.poster_preview.saving = False
        records = [{'comment': 'First'}, {'comment': 'Second'}]
        self.app.poster_preview.layout_state = {'metadata': records}
        paths = list(self.app.poster_preview.source_paths)
        self.app.edit_poster_project(self.app.poster_preview, individual=True)
        self.assertEqual(self.app.individual_paths, paths)
        self.app.open_media.assert_called_with(paths[:1], individual=True, project_records=dict(zip(paths, records)))
        self.app.edit_poster_project(self.app.poster_preview, individual=False)
        self.app.open_media.assert_called_with(paths, project_records=dict(zip(paths, records)))

    def test_load_project_reuses_preview_and_restores_editable_selection(self):
        from app import App
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'poster'
            directory.mkdir()
            selected = directory / 'page.cbposter'
            selected.write_bytes(b'poster')
            config = selected
            config.write_bytes(b'configuration')
            preview = self.app.poster_preview
            self.app.root = Mock()
            profile = SimpleNamespace(id='Photos', images=Path(folder) / 'JPG')
            self.app.profile = profile
            self.app.settings = SimpleNamespace(profiles=[profile], profile=Mock())
            self.app.task = lambda work, done, failed=None: done(work())
            project = dict(paths=list(preview.source_paths), layout={'metadata': [{}, {}]},
                           outputs=[Path('/tmp/restored.jpg')], settings=object(), setup_dir=Path('/frozen/setup'),
                           project_path=config, profile_id='Photos', poster_names=[selected.with_suffix('.jpg').name])
            temporary = Mock()
            temporary.name = '/tmp/render'
            with patch('app.PosterOpenDialog', return_value=SimpleNamespace(result=selected)) as dialog, \
                 patch('app.tempfile.TemporaryDirectory', return_value=temporary), \
                 patch('app.poster_project.load_project', return_value=project) as load, \
                 patch('app.PosterPreview') as window:
                App.load_poster_project(self.app)
            dialog.assert_called_once_with(self.app.root, directory, [selected], self.app.t)
            load.assert_called_once_with(config, Path('/tmp/render/poster'))
            preview.replace.assert_called_once_with(project['outputs'], temporary, directory)
            self.assertTrue(preview.frozen)
            self.assertEqual(preview.source_paths, project['paths'])
            self.assertIs(preview.layout_state, project['layout'])
            self.assertIs(preview.settings, project['settings'])
            self.app.settings.profile.assert_not_called()
            window.assert_not_called()

    def test_unavailable_project_collection_preserves_open_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            selected = Path(folder) / 'page.cbposter'
            selected.write_bytes(b'poster')
            selected.with_suffix('.cbposter').write_bytes(b'configuration')
            self.app.root = Mock()
            self.app.settings = SimpleNamespace(profiles=[])
            self.app.task = lambda work, done, failed=None: done(work())
            temporary = Mock()
            temporary.name = '/tmp/render'
            with patch('app.poster_project.project_files', return_value=[selected]), \
                 patch('app.PosterOpenDialog', return_value=SimpleNamespace(result=selected)), \
                 patch('app.tempfile.TemporaryDirectory', return_value=temporary), \
                 patch('app.poster_project.load_project', return_value={'profile_id': 'Missing', 'poster_names': [selected.name]}):
                with self.assertRaisesRegex(ValueError, 'collection is unavailable'):
                    self.app.load_poster_project()
            temporary.cleanup.assert_called_once()
            self.app.poster_preview.replace.assert_not_called()

    def test_no_configurations_reports_fixed_folder_without_starting_load(self):
        self.app.root = Mock()
        self.app.task = Mock()
        with patch('app.poster_project.project_files', return_value=[]) as find, \
             patch('app.messagebox.showinfo') as info, patch('app.PosterOpenDialog') as dialog:
            self.app.load_poster_project()
        find.assert_called_once_with(Path('/poster'))
        self.app.task.assert_not_called()
        dialog.assert_not_called()
        info.assert_called_once()

    def test_cancel_poster_selection_keeps_existing_preview(self):
        self.app.root, self.app.task = Mock(), Mock()
        with patch('app.poster_project.project_files', return_value=[Path('/poster/a.cbposter')]), \
             patch('app.PosterOpenDialog', return_value=SimpleNamespace(result=None)):
            self.app.load_poster_project()
        self.app.task.assert_not_called()
        self.app.poster_preview.replace.assert_not_called()


class LargePosterPreviewTests(unittest.TestCase):
    def test_preview_is_bounded_and_original_file_is_unchanged(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'large.jpg'
            Image.new('RGB', (4000, 2000), 'red').save(path)
            original = path.read_bytes()
            image = PosterPreview.load_page(path)
            self.assertEqual(image.size, (1600, 800))
            self.assertEqual(path.read_bytes(), original)

    def test_show_queues_loading_without_waiting_for_image(self):
        preview = SimpleNamespace(outputs=[Path('large.jpg')], page_future=None,
                                  page_worker=Mock(), page_poll=None, after=Mock(),
                                  load_page=PosterPreview.load_page, poll_page=Mock(),
                                  page=Mock(), previous=Mock(), next=Mock(),
                                  t=lambda text, **kw: text.format(**kw))
        PosterPreview.show(preview, 0)
        preview.page_worker.submit.assert_called_once_with(PosterPreview.load_page, Path('large.jpg'))
        preview.page_future.result.assert_not_called()
        preview.after.assert_called_once_with(40, preview.poll_page)

    def test_canvas_destruction_does_not_call_chooser_tooltip(self):
        from app import ImageCanvas
        canvas = ImageCanvas.__new__(ImageCanvas)
        canvas.pending = None
        with patch('app.tk.Canvas.destroy') as destroy:
            ImageCanvas.destroy(canvas)
        destroy.assert_called_once()
