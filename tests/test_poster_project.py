"""Persistent frozen poster layouts and editable source references."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import core
import poster_project


class PosterProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = SimpleNamespace(images=self.root / 'JPG', id='Photos')
        self.profile.images.mkdir()
        self.paths = []
        for color in ('green', 'red', 'blue'):
            path = self.profile.images / f'{color}.png'
            Image.new('RGB', (80, 120), color).save(path)
            self.paths.append(path)
        self.setup = self.root / 'setup'
        self.setup.mkdir()
        Image.new('RGB', (40, 40), 'purple').save(self.setup / 'background.png')
        self.settings = core.PosterSettings(400, 100, 100, 2, 2, False)
        self.records = [{'device': '2/A1/90/100', 'alias': 't', 'comment': 'First'},
                        {'device': '/A1//50', 'comment': 'Second'}, {'comment': 'Third'}]
        self.state = {}
        with patch('core.read_metadata_batch', return_value=self.records):
            self.outputs = core.create_posters(self.paths, self.profile, self.settings,
                         layout_state=self.state, setup_dir=self.setup)
        self.file = self.root / 'poster' / 'layout.cbposter'

    def save(self):
        poster_project.save_project(self.file, self.paths, self.settings, self.state,
                                    self.outputs, self.setup, self.profile.id)

    def load(self):
        return poster_project.load_project(self.file, self.root / 'restored')

    def test_round_trip_preserves_order_settings_previews_and_layers(self):
        self.save()
        project = self.load()
        self.assertEqual(project['paths'], self.paths)
        self.assertEqual(project['settings'], self.settings)
        self.assertEqual(project['profile_id'], 'Photos')
        self.assertEqual(project['layout']['metadata'], self.records)
        self.assertEqual(project['setup_dir'], self.setup)
        for source, restored in zip(self.outputs, project['outputs']):
            self.assertEqual(source.read_bytes(), restored.read_bytes())
        for before, after in zip(self.state['pages'], project['layout']['pages']):
            self.assertEqual(before['placements'], after['placements'])
            self.assertEqual(before['cells'], after['cells'])
            self.assertEqual(before['base'].tobytes(), after['base'].tobytes())
            for (a, xy_a), (b, xy_b) in zip(before['panels'], after['panels']):
                self.assertEqual(a.tobytes(), b.tobytes())
                self.assertEqual(xy_a, xy_b)

    def test_reloaded_project_supports_individual_edit_and_preserves_others(self):
        self.save()
        project = self.load()
        edited = [dict(record) for record in self.records]
        edited[1]['device'] = '3/B2/90/100'
        state = {}
        # Changes to setup assets must not change a frozen page's background.
        Image.new('RGB', (40, 40), 'yellow').save(self.setup / 'background.png')
        with patch('core.read_metadata_batch', return_value=edited):
            outputs = core.create_posters(project['paths'], self.profile, project['settings'],
                         layout_state=state, previous_layout=project['layout'], changed_indices={1}, setup_dir=self.setup)
        self.assertEqual([item[0] for item in state['pages'][1]['placements']], [0, 2])
        self.assertEqual([item[0] for item in state['pages'][2]['placements']], [1])
        self.assertEqual(state['pages'][1]['base'].tobytes(), project['layout']['pages'][1]['base'].tobytes())
        self.assertIs(state['pages'][1]['panels'][0][0], project['layout']['pages'][1]['panels'][0][0])
        self.assertIs(state['pages'][1]['panels'][1][0], project['layout']['pages'][1]['panels'][2][0])
        poster_project.save_project(self.file, self.paths, self.settings, state,
                    outputs, self.setup, self.profile.id)
        self.assertEqual(self.load()["layout"]["pages"][2]["placements"][0][0], 1)

    def test_reloaded_project_supports_global_edits_without_losing_frozen_cells(self):
        self.save()
        project = self.load()
        state = {}
        with patch('core.read_metadata_batch', return_value=self.records):
            outputs = core.create_posters(project['paths'], self.profile, project['settings'],
                         layout_state=state, previous_layout=project['layout'], changed_indices={0, 1, 2},
                         field_overrides={'comment': 'Changed for every image'}, setup_dir=self.setup)
        self.assertEqual([record['comment'] for record in state['metadata']], ['Changed for every image'] * 3)
        self.assertEqual([page['cells'] for page in state['pages']], [page['cells'] for page in project['layout']['pages']])
        self.assertEqual([page['placements'] for page in state['pages']], [page['placements'] for page in project['layout']['pages']])
        poster_project.save_project(self.file, self.paths, self.settings, state, outputs, self.setup, self.profile.id)
        self.assertEqual([record['comment'] for record in self.load()['layout']['metadata']], ['Changed for every image'] * 3)

    def test_sources_are_relative_and_collection_can_move_together(self):
        self.save()
        data = json.loads(self.file.read_text())
        self.assertEqual(data['sources'], ['../JPG/green.png', '../JPG/red.png', '../JPG/blue.png'])
        moved = self.root / 'moved'
        (moved / 'poster').mkdir(parents=True)
        shutil.copytree(self.profile.images, moved / 'JPG')
        shutil.copy2(self.file, moved / 'poster' / self.file.name)
        project = poster_project.load_project(moved / 'poster' / self.file.name, moved / 'restored')
        self.assertEqual(project['paths'], [moved / 'JPG' / path.name for path in self.paths])

    def test_missing_image_fails_before_writing_preview_files(self):
        self.save()
        self.paths[0].unlink()
        with self.assertRaisesRegex(FileNotFoundError, 'images are missing'):
            self.load()
        self.assertFalse((self.root / 'restored').exists())

    def test_invalid_archive_and_inconsistent_order_are_rejected(self):
        self.file.write_bytes(b'not a project')
        with self.assertRaisesRegex(ValueError, 'Invalid poster project'):
            self.load()
        self.save()
        data = json.loads(self.file.read_text())
        data['pages'][1]['placements'][1][0] = 0
        self.file.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'order or placement'):
            self.load()

    def test_configuration_is_small_json_without_embedded_images(self):
        self.save()
        self.assertFalse(zipfile.is_zipfile(self.file))
        self.assertLess(self.file.stat().st_size, 10000)
        data = json.loads(self.file.read_text())
        self.assertEqual(data['schema'], 3)
        self.assertTrue(all(set(page) == {'title', 'placements', 'cells'} for page in data['pages']))
        self.assertNotIn('panels', self.file.read_text())
        self.assertNotIn('base', self.file.read_text())

    def test_legacy_archive_ignores_embedded_images_and_is_compacted_on_open(self):
        self.save()
        data = json.loads(self.file.read_text())
        data['schema'] = 1
        for page in data['pages']:
            page['panel_positions'] = []
        with zipfile.ZipFile(self.file, 'w') as archive:
            archive.writestr('project.json', json.dumps(data))
            # Deliberately invalid image data: loading must use ../JPG instead.
            archive.writestr('pages/1.jpg', b'not an image' * 100000)
        before = self.file.stat().st_size
        project = self.load()
        self.assertEqual(project['paths'], self.paths)
        self.assertFalse(zipfile.is_zipfile(self.file))
        self.assertLess(self.file.stat().st_size, before / 100)
        self.assertEqual(json.loads(self.file.read_text())['schema'], 3)
        self.assertEqual(project['layout']['pages'][1]['cells'], self.state['pages'][1]['cells'])

    def test_loading_uses_referenced_images_even_if_exported_posters_are_missing(self):
        self.save()
        for path in self.outputs:
            path.unlink()
        Image.new('RGB', (80, 120), 'yellow').save(self.paths[0])
        project = self.load()
        self.assertEqual(project['layout']['pages'][1]['cells'], self.state['pages'][1]['cells'])
        self.assertNotEqual(project['layout']['pages'][1]['panels'][0][0].tobytes(),
                            self.state['pages'][1]['panels'][0][0].tobytes())

    def test_explicit_zorder_survives_reference_only_project_reload(self):
        self.settings = core.PosterSettings(400, 100, 100, 1, 1, True)
        self.records = [{"device": "1/A1/0/0/9"}, {"device": "1/A1/0/0/2"}, {"device": "1/A1/0/0/4"}]
        self.state = {}
        with patch('core.read_metadata_batch', return_value=self.records):
            self.outputs = core.create_posters(self.paths, self.profile, self.settings, layout_state=self.state)
        self.save()
        project = self.load()
        self.assertEqual(project['layout']['metadata'], self.records)
        self.assertEqual([item[0] for item in project['layout']['pages'][0]['placements']], [0, 1, 2])
        with Image.open(project['outputs'][0]) as image:
            r, g, b = image.getpixel((200, 200))
            self.assertGreater(g, 110)
            self.assertLess(max(r, b), 20)

    def test_numeric_xy_project_roundtrip_preserves_negative_and_off_grid_geometry(self):
        self.records = [{'device': '2/-0.5/0.25/90/100/4'}, {'device': '2/100/-100'}, {'device': '2/0/0'}]
        self.state = {}
        with patch('core.read_metadata_batch', return_value=self.records):
            self.outputs = core.create_posters(self.paths, self.profile, self.settings, layout_state=self.state)
        self.save()
        project = self.load()
        self.assertEqual(project['layout']['metadata'], self.records)
        for old, restored in zip(self.state['pages'], project['layout']['pages']):
            self.assertEqual(old['cells'], restored['cells'])
            self.assertEqual(old['placements'], restored['placements'])
        self.assertEqual(project['layout']['pages'][1]['panels'][1][0].getbbox(), None)

    def test_previous_numeric_free_rotation_is_migrated_from_schema_two(self):
        self.save()
        data = json.loads(self.file.read_text())
        data['schema'] = 2
        data['metadata'][0]['device'] = '2//90/100/4'
        self.file.write_text(json.dumps(data))
        project = self.load()
        self.assertEqual(project['layout']['metadata'][0]['device'], '2///90/100/4')
        self.assertEqual(core.poster_angle(project['layout']['metadata'][0]), 90)
        self.assertEqual(core.poster_zorder(project['layout']['metadata'][0]), 4)

    def test_project_cannot_overwrite_a_source_image(self):
        with self.assertRaisesRegex(ValueError, 'source images'):
            poster_project.save_project(self.paths[0], self.paths, self.settings, self.state,
                                        self.outputs, self.setup, self.profile.id)

    def test_export_saves_one_project_named_after_first_poster_with_all_pages(self):
        from app import PosterPreview
        from unittest.mock import Mock
        app = SimpleNamespace(busy=False, status=Mock())
        def task(work, done, failed):
            done(work())
        app.task = task
        preview = SimpleNamespace(app=app, saving=False, directory=self.root / 'exported',
                         source_paths=self.paths, settings=self.settings, layout_state=self.state,
                         outputs=self.outputs, setup_dir=self.setup, profile=self.profile,
                         save_button=Mock(), title=Mock(), t=lambda text, **kw: text.format(**kw))
        PosterPreview.save(preview)
        posters = sorted(preview.directory.glob('*.jpg'))
        self.assertEqual(len(posters), len(self.outputs))
        self.assertEqual(list(preview.directory.glob('*.cbposter')), [preview.project_path])
        self.assertEqual(preview.project_path, posters[0].with_suffix('.cbposter'))
        project = poster_project.load_project(preview.project_path, self.root / 'export-check')
        for number, poster in enumerate(posters):
            self.assertEqual(project['paths'], self.paths)
            self.assertEqual(project['layout']['metadata'], self.records)
            self.assertEqual(project['outputs'][number].read_bytes(), poster.read_bytes())

    def test_export_names_use_actual_category_folders_and_match_configuration(self):
        from app import PosterPreview
        from unittest.mock import Mock
        renamed = []
        for index, path in enumerate(self.paths):
            directory = self.profile.images / ('Reisen' if index < 2 else 'Haus')
            directory.mkdir(exist_ok=True)
            target = directory / path.name
            path.rename(target)
            renamed.append(target)
        self.paths = renamed
        app = SimpleNamespace(busy=False, status=Mock(), task=lambda work, done, failed: done(work()))
        preview = SimpleNamespace(app=app, saving=False, directory=self.root / 'exported',
                         source_paths=self.paths, settings=self.settings, layout_state=self.state,
                         outputs=self.outputs, setup_dir=self.setup, profile=self.profile,
                         save_button=Mock(), title=Mock(), t=lambda text, **kw: text.format(**kw))
        PosterPreview.save(preview)
        self.assertEqual(len(list(preview.directory.glob('*.cbposter'))), 1)
        self.assertTrue(preview.project_path.name.startswith('poster_Reisen_Haus_'))
        data = json.loads(preview.project_path.read_text())
        self.assertEqual(preview.project_path, (preview.directory / data['poster_names'][0]).with_suffix('.cbposter'))
        for poster in preview.directory.glob('*.jpg'):
            self.assertTrue(poster.name.startswith('poster_Reisen_Haus_'), poster.name)
            self.assertIn(poster.name, data['poster_names'])
            self.assertTrue(all(source.startswith('../JPG/') for source in data['sources']))



class PosterProjectLocationTests(unittest.TestCase):
    def test_selection_lists_only_configurations_in_fixed_folder_newest_first(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('first.cbposter', 'latest.CBPOSTER', 'poster.jpg', 'unrelated.txt'):
                (root / name).write_bytes(b'file')
            (root / 'subfolder').mkdir()
            (root / 'subfolder' / 'hidden.cbposter').write_bytes(b'file')
            os.utime(root / 'first.cbposter', ns=(1, 1))
            os.utime(root / 'latest.CBPOSTER', ns=(2, 2))
            self.assertEqual(poster_project.project_files(root), [root / 'latest.CBPOSTER', root / 'first.cbposter'])
            self.assertEqual(poster_project.project_files(root / 'missing'), [])

    def test_category_filename_preserves_unicode_and_uses_folder_not_metadata(self):
        name = poster_project.poster_filename('poster_20261101_120000.jpg',
                        [Path('/box/JPG/Urlaub Österreich/a.jpg'), Path('/box/JPG/Urlaub Österreich/b.jpg')])
        self.assertEqual(name, 'poster_Urlaub Österreich_20261101_120000.jpg')
        self.assertEqual(poster_project.poster_filename('poster_123.jpg', []), 'poster_unassigned_123.jpg')
