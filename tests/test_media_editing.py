"""Correct invalid poster fields and keep unedited batch fields individual."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import App
import core


class MediaEditingTests(unittest.TestCase):
    def app(self):
        app = App.__new__(App)
        app.busy = app.search_mode = False
        app.settings = SimpleNamespace(profiles=[], poster=core.PosterSettings(transparency=100))
        app.profile = SimpleNamespace(labels=core.LABELS)
        app.variables = {field: Mock() for field in core.FIELDS if field != 'comment'}
        app.field_widgets = {'comment': Mock(), 'device': Mock()}
        app.refresh_profile = Mock()
        app.preview_result = Mock(return_value=(None, ''))
        app.preview = app.selected = Mock()
        app.set_preview_path = Mock()
        app.log = Mock()
        app.task = lambda work, done: done(work())
        return app

    def test_invalid_field_three_opens_unchanged_and_can_be_corrected(self):
        for value in ('1/B2/90/50/2/extra/invalid', 'not a page', '1/bad/0/0'):
            with self.subTest(value=value):
                app = self.app()
                original = core.normalized(dict(device=value, comment='keep'))
                with patch('core.read_metadata_batch', return_value=[original]):
                    app.open_media([Path('broken.jpg')])
                app.variables['device'].set.assert_called_with(value)
                self.assertEqual(app.media_records[0], original)
                self.assertEqual(app.media_baseline['device'], value)
                corrected = core.edited_metadata(original, dict(original, device='1/0/0'),
                                                  app.media_baseline, mode='replace')
                self.assertEqual(corrected['device'], '1/0/0')
                self.assertEqual(corrected['comment'], 'keep')

    def test_open_does_not_overwrite_stored_transparency_with_setup_default(self):
        app = self.app()
        record = core.normalized(dict(device='1/0/0/0/25'))
        with patch('core.read_metadata_batch', return_value=[record]):
            app.open_media([Path('image.jpg')])
        self.assertEqual(app.media_records[0]['device'], '1/0/0/0/25')
        app.variables['device'].set.assert_called_with('1/0/0/0/25')

    def test_legacy_coordinates_display_without_becoming_an_edit(self):
        app = self.app()
        record = core.normalized(dict(device='1/B2/90/25/2'))
        with patch('core.read_metadata_batch', return_value=[record]):
            app.open_media([Path('image.jpg')])
        self.assertEqual(app.media_baseline['device'], '1/1/1/90/25/2')
        saved = core.edited_metadata(record, app.media_baseline, app.media_baseline, mode='replace')
        self.assertEqual(saved['device'], record['device'])

    def test_edit_one_batch_field_preserves_every_other_individual_value(self):
        records = [core.normalized(dict(box=f'Box{i}', anzahl=i + 1, device=f'{i+1}/0/0',
                                       alias=str(i), category=f'Category{i}', package=f'Pack{i}',
                                       comment=f'Comment{i}')) for i in range(2)]
        baseline = core.common_metadata(records)
        for changed in core.FIELDS:
            with self.subTest(field=changed):
                values = dict(baseline, **{changed: '7' if changed == 'anzahl' else 'New value'})
                for original in records:
                    result = core.edited_metadata(original, values, baseline, mode='replace',
                                                  differing_fields=core.differing_metadata_fields(records))
                    for field in core.FIELDS:
                        expected = (7 if changed == 'anzahl' else 'NEW VALUE' if changed == 'category'
                                    else 'New value') if field == changed else original[field]
                        self.assertEqual(result[field], expected)

    def test_invalid_poster_fields_block_rendering_for_the_entire_selection(self):
        paths = [Path('valid.jpg'), Path('broken.jpg')]
        for value in ('invalid', '1/B2/90/50/2/extra/invalid', '1/0/0/0/101', '1/0/0/0/0/bad'):
            with self.subTest(value=value), patch('core.read_metadata_batch', return_value=[
                    core.normalized(dict(device='1/0/0')), core.normalized(dict(device=value))]), \
                    patch('core.create_poster') as render:
                with self.assertRaisesRegex(ValueError, 'broken.jpg: .*field 3'):
                    core.create_posters(paths, SimpleNamespace(images=Path('/unused/JPG')), core.PosterSettings())
                render.assert_not_called()


class PosterCancellationTests(unittest.TestCase):
    def app(self):
        app = App.__new__(App)
        app.cancel_poster_button = Mock()
        app.status = Mock()
        app.log = Mock()
        app.error = Mock()
        app.task = Mock()
        return app

    def test_cancel_stops_at_next_progress_update_and_restores_controls(self):
        app = self.app()
        done = Mock()
        def work(progress):
            progress('Page 1/3')
            app.cancel_poster_creation()
            progress('Poster: 2/5')
            self.fail('Rendering continued after cancellation')
        app.poster_task(work, done)
        execute, completed, failed = app.task.call_args.args
        with self.assertRaises(core.Cancelled) as caught:
            execute()
        failed(caught.exception)
        done.assert_not_called()
        app.error.assert_not_called()
        self.assertIsNone(app.poster_cancel)
        app.cancel_poster_button.configure.assert_called_with(state='disabled')
        app.status.set.assert_called_with('Poster creation cancelled.')

    def test_cancel_after_render_shows_completed_result(self):
        app = self.app()
        result = ([Path('preview.jpg')], Mock())
        done = Mock()
        app.poster_task(lambda progress: result, done)
        execute, completed, failed = app.task.call_args.args
        rendered = execute()
        app.cancel_poster_creation()
        completed(rendered)
        result[1].cleanup.assert_not_called()
        done.assert_called_once_with(result)
        app.error.assert_not_called()

    def test_completion_keeps_output_and_errors_remain_visible(self):
        app = self.app()
        result = ([Path('preview.jpg')], Mock())
        done = Mock()
        app.poster_task(lambda progress: result, done)
        execute, completed, failed = app.task.call_args.args
        completed(execute())
        done.assert_called_once_with(result)
        result[1].cleanup.assert_not_called()
        error = ValueError('Invalid poster field')
        app.poster_task(lambda progress: None, done)
        app.task.call_args.args[2](error)
        app.error.assert_called_once_with(error)

    def test_cancel_retains_completed_pages_with_saveable_layout(self):
        from tempfile import TemporaryDirectory
        from PIL import Image
        with TemporaryDirectory() as folder:
            root = Path(folder)
            paths = [root / f'{index}.png' for index in range(3)]
            for path in paths:
                Image.new('RGB', (40, 40), 'red').save(path)
            layout = {}
            def progress(message):
                if message.startswith('Page 2/3 · Poster:'):
                    raise core.Cancelled('stop')
            with self.assertRaises(core.Cancelled) as caught:
                core.create_posters(paths, SimpleNamespace(images=root / 'JPG'),
                                    core.PosterSettings(200, 200, 200, 1, 1, True),
                                    progress, layout_state=layout)
            outputs = caught.exception.outputs
            self.assertEqual(len(outputs), 1)
            self.assertTrue(outputs[0].is_file())
            kept = App.completed_poster_sources(paths, layout)
            self.assertEqual(kept, paths[:1])
            self.assertEqual(len(layout['metadata']), 1)
            self.assertEqual(len(layout['pages']), 1)
            self.assertEqual(layout['pages'][0]['placements'][0][0], 0)
