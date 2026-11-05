"""Date-aware poster ordering and source/project identity regressions."""
from datetime import date
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

import core
import poster_project
from app import App


class PosterOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = SimpleNamespace(images=self.root/'JPG', id='Photos')
        self.settings = core.PosterSettings(900, 100, 100, 3, 3, False)
        self.paths, self.records = [], []
        for index, portrait in enumerate((False, True, True, False, True)):
            path = self.root/f'{index}.png'
            Image.new('RGB', (100, 180) if portrait else (180, 100),
                      ('red', 'green', 'blue', 'yellow', 'purple')[index]).save(path)
            self.paths.append(path)
            self.records.append({'created': '2024-01-31', 'comment': f'Photo {index}', 'box': 'Trip'})

    def test_landscape_lookahead_fits_five_photos_instead_of_four(self):
        original = core.poster_photo_pages(self.paths, self.records, self.settings)
        order, pages = core.poster_selection_order(self.paths, self.records, self.settings)
        self.assertEqual([len(page) for page in original], [4, 1])
        self.assertEqual(order, [0, 1, 3, 2, 4])
        self.assertEqual([len(page) for page in pages], [5])
        self.assertEqual(sorted(order), list(range(5)))
        occupied = set()
        for index, row, col, height, width in pages[0]:
            cells = core.poster_occupied(row, col, height, width, 3, 3)
            self.assertFalse(occupied & cells)
            occupied.update(cells)

    def test_month_window_crosses_month_and_year_boundaries(self):
        for first, second, expected in ((date(2024, 1, 31), date(2024, 2, 29), True),
                                        (date(2024, 1, 31), date(2024, 3, 1), False),
                                        (date(2023, 12, 31), date(2024, 1, 31), True),
                                        (date(2024, 2, 1), date(2024, 3, 2), False)):
            with self.subTest(first=first, second=second):
                self.assertEqual(core.poster_dates_within_month(first, second), expected)
                self.assertEqual(core.poster_dates_within_month(second, first), expected)
        self.records[3]['created'] = '2024-03-02'
        order, _ = core.poster_selection_order(self.paths, self.records, self.settings)
        self.assertEqual(order, list(range(5)))

    def test_unknown_dates_do_not_trigger_reordering(self):
        records = [dict(record, created='unknown') for record in self.records]
        order, _ = core.poster_selection_order(self.paths, records, self.settings)
        self.assertEqual(order, list(range(5)))

    def test_fixed_and_manual_layouts_preserve_selection(self):
        order, _ = core.poster_selection_order(self.paths, self.records,
                                               core.PosterSettings(900, 100, 100, 3, 3, True))
        self.assertEqual(order, list(range(5)))
        for override in ({'device': '1'}, {'device': '/0/0'}, {'alias': 'false'}, {'anzahl': '2'}):
            with self.subTest(override=override):
                records = [dict(record) for record in self.records]
                records[0].update(override)
                order, _ = core.poster_selection_order(self.paths, records, self.settings)
                self.assertEqual(order, list(range(5)))

    def test_candidate_cannot_make_pagination_worse(self):
        portraits = [True, True, True, False, False, False]
        paths = self.paths + [self.paths[3]]
        records = self.records + [self.records[3]]
        with patch('core.poster_photo_geometry', return_value=(portraits, [.5 if p else 2 for p in portraits])):
            order, pages = core.poster_selection_order(paths, records, self.settings)
        self.assertEqual(order, list(range(6)))
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(pages[0]), 6)

    def test_render_numbering_refresh_and_project_round_trip_follow_new_order(self):
        records = dict(zip(self.paths, self.records))
        read = lambda paths, *args, **kwargs: [dict(records[path]) for path in paths]
        state = {}
        setup = self.root/'setup'
        setup.mkdir()
        originals = {path: path.read_bytes() for path in self.paths}
        with patch('core.read_metadata_batch', side_effect=read), \
                patch('core.create_poster', wraps=core.create_poster) as render:
            outputs = core.create_posters(self.paths, self.profile, self.settings, layout_state=state,
                                           setup_dir=setup)
        ordered = [self.paths[index] for index in (0, 1, 3, 2, 4)]
        self.assertEqual(state['source_paths'], ordered)
        self.assertEqual(state['metadata'], [records[path] for path in ordered])
        self.assertEqual(render.call_args.kwargs['selection_numbers'], [1, 2, 3, 4, 5])
        project_file = self.root/'trip.cbposter'
        poster_project.save_project(project_file, ordered, self.settings, state, outputs, setup, 'Photos')
        project = poster_project.load_project(project_file, self.root/'restored')
        self.assertEqual(project['paths'], ordered)
        self.assertEqual(project['layout']['metadata'], state['metadata'])
        self.assertEqual(project['outputs'][0].read_bytes(), outputs[0].read_bytes())
        refreshed = {}
        with patch('core.read_metadata_batch', side_effect=read), \
                patch('core.poster_selection_order', side_effect=AssertionError('frozen layout reordered')):
            core.create_posters(ordered, self.profile, self.settings, layout_state=refreshed,
                                previous_layout=state, changed_indices={1})
        self.assertEqual(refreshed['source_paths'], ordered)
        self.assertEqual(refreshed['pages'][0]['placements'], state['pages'][0]['placements'])
        self.assertEqual({path: path.read_bytes() for path in self.paths}, originals)

    def test_partial_preview_uses_reordered_sources(self):
        # Completed page indices refer to the optimized order, including moved photos.
        ordered = [self.paths[index] for index in (0, 1, 3, 2, 4)]
        layout = {'source_paths': ordered, 'metadata': list(range(5)), 'fixed_modes': [False]*5,
                  'pages': [{'placements': [(0, 0, 0, 1, 1), (2, 0, 1, 1, 1)]}]}
        completed = App.completed_poster_sources(self.paths, layout)
        self.assertEqual(completed, [self.paths[0], self.paths[3]])
        self.assertEqual(layout['source_paths'], completed)
        self.assertEqual(layout['metadata'], [0, 2])
        self.assertEqual([item[0] for item in layout['pages'][0]['placements']], [0, 1])
