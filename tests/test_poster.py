"""Batch editing and poster output regressions."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from app import App, MediaGrid


class BatchMetadataTests(unittest.TestCase):
    def test_explicit_extend_and_replace_modes(self):
        originals = [core.normalized(dict(box=box, anzahl=count, comment=comment, device="shared"))
                     for box, count, comment in [("A", 2, "first"), ("B", 4, "second")]]
        baseline = core.common_metadata(originals)
        differing = core.differing_metadata_fields(originals)
        self.assertEqual(set(differing), {"box", "anzahl", "comment"})
        values = dict(baseline, box="new", anzahl="3", comment="added", device="extra")
        for original in originals:
            extended = core.edited_metadata(original, values, baseline, mode="extend")
            self.assertEqual(extended["box"], original["box"] + ", new")
            self.assertEqual(extended["comment"], original["comment"] + "\nadded")
            self.assertEqual(extended["device"], "shared, extra")
            self.assertEqual(extended["anzahl"], original["anzahl"] + 3)
            replaced = core.edited_metadata(original, values, baseline, mode="replace", differing_fields=differing)
            self.assertEqual([replaced[field] for field in ("box", "anzahl", "comment", "device")],
                             ["new", 3, "added", "extra"])
            cleared = core.edited_metadata(original, baseline, baseline, mode="replace", differing_fields=differing)
            self.assertEqual([cleared[field] for field in ("box", "anzahl", "comment", "device")],
                             ["", 0, "", "shared"])
            kept = core.edited_metadata(original, dict(baseline, device=""), baseline, mode="extend")
            self.assertEqual(kept["device"], "shared")
            self.assertEqual(kept["comment"], original["comment"])

    def test_common_values_and_append_preserve_each_record(self):
        originals = [core.normalized(dict(box=box, anzahl=count, comment=comment,
                                         device="shared", created="original", extra="keep"))
                     for box, count, comment in [("Box1", 2, "first"), ("Box2", 4, "second")]]
        baseline = core.common_metadata(originals)
        self.assertEqual(baseline["box"], "")
        self.assertEqual(baseline["device"], "shared")
        values = dict(baseline, box="Box3", comment="added", anzahl="3", device="replaced")
        for original in originals:
            changed = core.edited_metadata(original, values, baseline)
            self.assertEqual(changed["box"], original["box"] + ", Box3")
            self.assertEqual(changed["comment"], original["comment"] + "\nadded")
            self.assertEqual(changed["anzahl"], original["anzahl"] + 3)
            self.assertEqual(changed["device"], "replaced")
            self.assertEqual(changed["created"], "original")
            self.assertEqual(changed["extra"], "keep")
            unchanged = core.edited_metadata(original, baseline, baseline)
            for field in core.FIELDS:
                self.assertEqual(unchanged[field], original[field])
        updated = [core.edited_metadata(record, values, baseline) for record in originals]
        next_baseline = core.common_metadata(updated)
        for record in updated:
            again = core.edited_metadata(record, next_baseline, next_baseline)
            for field in core.FIELDS:
                self.assertEqual(again[field], record[field])

    def test_selection_order_survives_removal_range_and_select_all(self):
        grid = MediaGrid.__new__(MediaGrid)
        grid.paths = [Path(str(i)) for i in range(5)]
        grid.selected = {}
        grid.anchor = None
        grid.columns, grid.cell_width, grid.cell_height = 5, 10, 10
        grid.canvas = Mock()
        grid.canvas.canvasy.side_effect = lambda y: y
        grid.changed = grid.redraw = Mock()
        def click(index, shift=False):
            grid.click(SimpleNamespace(x=index * 10, y=0, state=int(shift)))
        click(3)
        click(1)
        click(3)
        click(3)
        click(4, True)
        self.assertEqual(grid.selection(), [Path('1'), Path('3'), Path('4')])
        grid.select_all()
        self.assertEqual(grid.selection(), [Path(str(i)) for i in (1, 3, 4, 0, 2)])
        grid.clear_selection()
        self.assertEqual(grid.selection(), [])


class PosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = SimpleNamespace(images=self.root / "JPG")
        self.paths = []
        for color in ("red", "blue", "green"):
            path = self.root / (color + ".png")
            Image.new("RGB", (40, 40), color).save(path)
            self.paths.append(path)

    def test_size_dpi_order_and_distinct_outputs(self):
        settings = core.PosterSettings(600, 200, 100, 2, 2, True)
        path = core.create_poster(self.paths[::-1], self.profile, settings)
        self.assertEqual(path.parent, self.root / "poster")
        with Image.open(path) as image:
            self.assertEqual(image.size, (300, 600))
            self.assertAlmostEqual(image.info['dpi'][0], 76, delta=1)
            self.assertGreater(image.getpixel((75, 150))[1], 100)
            self.assertGreater(image.getpixel((225, 150))[2], 240)
            self.assertGreater(image.getpixel((75, 450))[0], 240)
            self.assertLess(max(abs(a - b) for a, b in zip(image.getpixel((225, 450)), (155, 177, 149))), 4)
        second = core.create_poster(self.paths, self.profile, settings)
        self.assertNotEqual(path, second)

    def test_aspect_ratio_and_orientation_are_preserved(self):
        path = self.root / "wide.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (80, 40), "red").save(path, exif=exif)
        output = core.create_poster([path], self.profile,
                                    core.PosterSettings(400, 100, 100, 1, 1, True))
        with Image.open(output) as image:
            # EXIF rotation makes this a 1:2 portrait, centered with white sides.
            pixels = [(x, y) for y in range(image.height) for x in range(image.width)
                      if (lambda rgb: rgb[0] > 200 and rgb[1] < 30)(image.getpixel((x, y)))]
            left, right = min(x for x, y in pixels), max(x for x, y in pixels)
            top, bottom = min(y for x, y in pixels), max(y for x, y in pixels)
            self.assertAlmostEqual((right - left + 1) / (bottom - top + 1), .5, delta=.02)
            self.assertGreaterEqual(left, 60)
            self.assertLessEqual(right, 380)
            self.assertGreaterEqual(top, 20)
            self.assertLessEqual(bottom, 380)

    def test_title_comments_margins_and_even_spacing(self):
        metadata = [core.normalized({"category": "Werkzeug", "comment": "Beschreibung " + str(i)})
                    for i in range(3)]
        with patch("core.read_metadata", side_effect=metadata) as read, \
             patch("core.poster_draw_text", wraps=core.poster_draw_text) as draw_text, \
             patch("core.markdown_render.draw_poster", wraps=core.markdown_render.draw_poster) as captions:
            output = core.create_poster(self.paths, self.profile,
                                        core.PosterSettings(1000, 100, 200, 3, 1, True), title="Werkzeug")
        self.assertEqual([call.args[0] for call in read.call_args_list], self.paths)
        self.assertEqual(draw_text.call_args_list[0].args[1], ["Werkzeug"])
        for index, call in enumerate(captions.call_args_list):
            self.assertEqual(" ".join(call.args[1].plain_lines), "Beschreibung " + str(index))
        with Image.open(output) as image:
            # Leave a pixel tolerance at JPEG block boundaries.
            for box in [(0, 0, 73, 500), (977, 0, 1000, 500),
                        (0, 0, 1000, 23), (0, 477, 1000, 500)]:
                for extrema, expected in zip(image.crop(box).getextrema(), (155, 177, 149)):
                    self.assertLess(max(abs(value - expected) for value in extrema), 12)
            centers = []
            for channel in (0, 2, 1):
                points = [(x, y) for y in range(25, 475) for x in range(75, 975)
                          if image.getpixel((x, y))[channel] > 100
                          and all(image.getpixel((x, y))[other] < 35 for other in range(3) if other != channel)]
                centers.append((min(x for x, y in points) + max(x for x, y in points)) / 2)
            self.assertAlmostEqual(centers[1] - centers[0], centers[2] - centers[1], delta=2)

    def test_explicit_positions_overlap_in_selection_order(self):
        for value, position, angle in (("A1/0", (0, 0), 0), ("b3", (2, 1), 0),
                                       ("B2/90", (1, 1), 90)):
            self.assertEqual(core.poster_position({"device": value}), position)
            self.assertEqual(core.poster_angle({"device": value}), angle)
        settings = core.PosterSettings(600, 100, 100, 3, 4, True)
        records = [{"device": "B2"}] * 3
        self.assertEqual(core.poster_photo_pages(self.paths, records, settings),
                         [[(i, 1, 1, 1, 1) for i in range(3)]])
        output = core.create_poster(self.paths, self.profile, settings, metadata=records)
        with Image.open(output) as image:
            self.assertEqual(image.size, (600, 600))
            pixels = list(image.getdata())
            self.assertTrue(any(g > 90 and r < 30 and b < 30 for r, g, b in pixels))
            self.assertFalse(any(b > 150 and r < 30 and g < 30 for r, g, b in pixels))
        self.assertEqual(core.poster_photo_pages(self.paths[:1], [{"device": "1/3/0"}], settings),
                         [[(0, 0, 3, 1, 1)]])

    def test_position_overflow_is_cropped_with_frame_inside(self):
        settings = core.PosterSettings(600, 100, 100, 2, 2, False,
                                       frames="000000", image_background_color="ffffff")
        Image.new("RGB", (40, 100), "red").save(self.paths[0])
        records = [{"device": "B2"}]
        self.assertEqual(core.poster_photo_pages(self.paths[:1], records, settings),
                         [[(0, 1, 1, 2, 1)]])
        output = core.create_poster(self.paths[:1], self.profile, settings, metadata=records)
        with Image.open(output) as image:
            x = 450
            bottom = 600 - round(settings.margin_bottom * 6)
            self.assertLess(max(image.getpixel((x, bottom - 1))), 40)
            self.assertGreater(min(image.getpixel((x, bottom - 8))), 220)
            self.assertGreater(image.getpixel((x, bottom - 35))[0], 180)

    def test_position_rotation_transparency_syntax(self):
        examples = [("A2/90/100", ((1, 0), 90, 100)),
                    ("A1//0", ((0, 0), 0, 0)),
                    ("///90/100", (None, 90, 100)),
                    ("////50", (None, 0, 50)), ("B3", ((2, 1), 0, 0)),
                    ("///90", (None, 90, 0)), ("A1/", ((0, 0), 0, 0))]
        for value, expected in examples:
            self.assertEqual(core.poster_transform({"device": value}), expected)
        for value in ("A1//-1", "A1//101", "A1//nan", "A1//bad", "A1/0/0/0/0", "A0//0"):
            with self.assertRaisesRegex(ValueError, "field 3"):
                core.poster_transform({"device": value})

    def test_transparent_panel_reveals_previous_image_without_fading_photo(self):
        Image.new("RGB", (100, 100), "red").save(self.paths[0])
        Image.new("RGB", (20, 100), "blue").save(self.paths[1])
        settings = core.PosterSettings(600, 100, 100, 1, 1, True,
                                       frames="00ff00", image_background_color="ffffff")
        for transparency, expected in ((0, (255, 255, 255)), (50, (255, 128, 128)), (100, (255, 0, 0))):
            with self.subTest(transparency=transparency):
                output = core.create_poster(self.paths[:2], self.profile, settings,
                            metadata=[{"device": "A1//0"}, {"device": f"A1//{transparency}"}])
                with Image.open(output) as image:
                    actual = image.getpixel((150, 300))
                    self.assertTrue(all(abs(a - b) < 12 for a, b in zip(actual, expected)), actual)
                    r, g, b = image.getpixel((300, 300))
                    self.assertGreater(b, 230)
                    self.assertLess(max(r, g), 20)
        settings = core.PosterSettings(600, 100, 100, 1, 1, True,
                                       frames="00ff00", image_background_color="ffffff", background_color="ff0000")
        output = core.create_poster(self.paths[1:2], self.profile, settings,
                                    metadata=[{"device": "///90/100"}])
        with Image.open(output) as image:
            # Transparent padding and frame reveal the red poster background.
            for point in ((76, 300), (90, 300), (300, 100)):
                r, g, b = image.getpixel(point)
                self.assertGreater(r, 230)
                self.assertLess(max(g, b), 25)

    def test_page_position_rotation_transparency_syntax(self):
        examples = [("3", 3, (None, 0, 0)),
                    ("2/A1/90/100", 2, ((0, 0), 90, 100)),
                    ("2/A1", 2, ((0, 0), 0, 0)),
                    ("2///90", 2, (None, 90, 0)),
                    ("/A1/90/100", None, ((0, 0), 90, 100)),
                    ("A1/90/100", None, ((0, 0), 90, 100)),
                    ("///90/100", None, (None, 90, 100))]
        for value, page, transform in examples:
            with self.subTest(value=value):
                self.assertEqual(core.poster_page({"device": value}), page)
                self.assertEqual(core.poster_transform({"device": value}), transform)
        for value in ("0/A1/90/100", "-2/A1/90/100", "1.5/A1/90/100", "2/Z0/90/100", "/bad/100"):
            with self.assertRaisesRegex(ValueError, "field 3"):
                core.poster_transform({"device": value})

    def test_page_start_is_inherited_and_can_return_to_an_earlier_page(self):
        pages = core.poster_pages([False] * 6, 2, 1, fixed=True,
                                  page_numbers=[None, 3, None, None, 2, None])
        self.assertEqual(pages, [[(0, 0, 0, 1, 1)],
                                 [(4, 0, 0, 1, 1), (5, 0, 1, 1, 1)],
                                 [(1, 0, 0, 1, 1), (2, 0, 1, 1, 1)],
                                 [(3, 0, 0, 1, 1)]])

    def test_fixed_switch_is_case_insensitive_and_empty_inherits(self):
        for truth in ("1", "true", "t", " TRUE ", "T"):
            records = [{}, {"alias": truth}, {}, {"alias": "false"}, {}, {"alias": "1"}]
            self.assertEqual(core.poster_layout_modes(records, False),
                             [False, True, True, False, False, True])
        self.assertEqual(core.poster_layout_modes([{}, {"alias": "0"}, {}], True), [True, False, False])
        pages = core.poster_pages([True] * 3, 2, 2, zooms=[2, 2, 2],
                                 fixed_modes=[True, True, False])
        self.assertEqual(pages[0], [(0, 0, 0, 1, 1), (1, 0, 1, 1, 1)])
        self.assertEqual(pages[1], [(2, 0, 0, 2, 2)])

    def test_explicit_page_overrides_flexible_portrait_spans(self):
        self.assertEqual(core.poster_pages([True] * 3, 2, 2,
                         page_numbers=[None, None, 1]),
                         [[(0, 0, 0, 1, 1), (1, 0, 1, 1, 1), (2, 1, 0, 1, 1)]])

    def test_forced_page_reconsiders_earlier_deferred_images_after_zoom_reflow(self):
        pages = core.poster_pages([False] * 3, 2, 2, zooms=[4, 1, 1],
                                  page_numbers=[None, None, 1])
        self.assertEqual(len(pages), 1)
        self.assertEqual([item[0] for item in pages[0]], [0, 1, 2])
        self.assertEqual(len({(item[1], item[2]) for item in pages[0]}), 3)

    def test_forced_page_over_capacity_uses_selection_order_overlap(self):
        pages = core.poster_pages([False] * 3, 2, 1, fixed=True,
                                  page_numbers=[None, None, 1])
        self.assertEqual(pages, [[(0, 0, 0, 1, 1), (1, 0, 1, 1, 1), (2, 0, 0, 1, 1)]])
        self.assertEqual(core.poster_pages([True], 2, 1, page_numbers=[1]), [[(0, 0, 0, 1, 1)]])

    def test_individual_page_override_recalculates_flexible_destination(self):
        settings = core.PosterSettings(600, 100, 100, 2, 2, False)
        for path in self.paths:
            Image.new("RGB", (40, 100), "red").save(path)
        initial, updated = {}, {}
        records = [{}, {}, {}]
        with patch("core.read_metadata_batch", return_value=records):
            core.create_posters(self.paths, self.profile, settings, layout_state=initial)
        self.assertEqual(len(initial["pages"]), 2)
        records[2] = {"device": "1"}
        with patch("core.read_metadata_batch", return_value=records):
            outputs = core.create_posters(self.paths, self.profile, settings, layout_state=updated,
                                         previous_layout=initial, changed_indices={2})
        self.assertEqual(len(outputs), 1)
        self.assertEqual([item[0] for item in updated["pages"][0]["placements"]], [0, 1, 2])
        self.assertTrue(all(item[3:] == (1, 1) for item in updated["pages"][0]["placements"]))
        self.assertIsNot(initial["pages"][0]["panels"][0][0], updated["pages"][0]["panels"][0][0])

    def test_saved_page_override_reconsiders_images_on_previous_overflow_page(self):
        settings = core.PosterSettings(600, 100, 100, 2, 2, False)
        records = [{"anzahl": "Z=4"}, {}, {}]
        initial, updated = {}, {}
        with patch("core.read_metadata_batch", return_value=records):
            core.create_posters(self.paths, self.profile, settings, layout_state=initial)
        self.assertEqual(len(initial["pages"]), 2)
        records[2] = {"device": "1"}
        with patch("core.read_metadata_batch", return_value=records):
            outputs = core.create_posters(self.paths, self.profile, settings, layout_state=updated,
                                         previous_layout=initial, changed_indices={2})
        self.assertEqual(len(outputs), 1)
        self.assertEqual([item[0] for item in updated["pages"][0]["placements"]], [0, 1, 2])

    def test_page_three_output_includes_blank_pages(self):
        settings = core.PosterSettings(600, 100, 100, 2, 2, True)
        state = {}
        records = [{"device": "3/A1/90/100", "alias": "1"}, {}]
        with patch("core.read_metadata_batch", return_value=records):
            outputs = core.create_posters(self.paths[:2], self.profile, settings, layout_state=state)
        self.assertEqual(len(outputs), 3)
        self.assertEqual([len(page["placements"]) for page in state["pages"]], [0, 0, 2])
        self.assertEqual([item[0] for item in state["pages"][2]["placements"]], [0, 1])
        for output in outputs:
            with Image.open(output) as image:
                self.assertEqual(image.size, (600, 600))
        self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())

    def test_individual_page_move_keeps_other_panels_and_z_order(self):
        settings = core.PosterSettings(600, 100, 100, 2, 2, True)
        records = [{"device": "A1"}, {"device": "A1"}, {"device": "3/A1"}]
        initial, updated = {}, {}
        with patch("core.read_metadata_batch", return_value=records):
            core.create_posters(self.paths, self.profile, settings, layout_state=initial)
        edited = [dict(record) for record in records]
        edited[1]["device"] = "3/A1/90/100"
        with patch("core.read_metadata_batch", return_value=edited):
            outputs = core.create_posters(self.paths, self.profile, settings, layout_state=updated,
                                         previous_layout=initial, changed_indices={1})
        self.assertEqual(len(outputs), 3)
        self.assertEqual([item[0] for item in updated["pages"][0]["placements"]], [0])
        self.assertEqual([item[0] for item in updated["pages"][2]["placements"]], [1, 2])
        self.assertIs(updated["pages"][0]["panels"][0][0], initial["pages"][0]["panels"][0][0])
        self.assertIs(updated["pages"][2]["panels"][1][0], initial["pages"][2]["panels"][0][0])

    def test_mixed_modes_keep_fixed_cells_on_the_configured_grid(self):
        settings = core.PosterSettings(600, 100, 100, 3, 2, False)
        records = [{"alias": "true"}, {"alias": "false"}, {}]
        state = {}
        with patch("core.read_metadata_batch", return_value=records):
            core.create_posters(self.paths, self.profile, settings, layout_state=state)
        fixed_cell = state["pages"][0]["cells"][0]
        self.assertEqual(state["fixed_modes"], [True, False, False])
        self.assertLess(fixed_cell[2] - fixed_cell[0], 200)
        self.assertLess(fixed_cell[3] - fixed_cell[1], 300)
        self.assertGreater(state["pages"][0]["cells"][2][3], fixed_cell[3])

    def test_numeric_xy_coordinates_are_zero_based_and_unbounded(self):
        for value, position in (("1/0/0/90/100/4", (0, 0)), ("1/-0.5/1.25", (1.25, -0.5)),
                                ("1/50/-20", (-20, 50)), ("/0/0", (0, 0)), ("1//", None)):
            self.assertEqual(core.poster_position({"device": value}), position)
        self.assertEqual(core.poster_angle({"device": "///90"}), 90)
        self.assertEqual(core.poster_zorder({"device": "1/0/0/90/100/4"}), 4)
        for value in ("1/nan/0", "1/0/inf", "1/a/0", "1/0/0/0/0/0/0"):
            with self.assertRaisesRegex(ValueError, "field 3"):
                core.poster_transform({"device": value})
        self.assertEqual(core.poster_coordinate_text("1/B2/90/100/4"), "1/1/1/90/100/4")

    def test_negative_xy_is_cropped_and_fully_outside_coordinates_are_valid(self):
        settings = core.PosterSettings(600, 100, 100, 2, 2, True,
                                       frames="000000", image_background_color="ffffff")
        state = {}
        records = [{"device": "1/-0.5/-0.5/0/0/4"}, {"device": "1/100/-100/0/0/5"}]
        with patch("core.read_metadata_batch", return_value=records):
            outputs = core.create_posters(self.paths[:2], self.profile, settings, layout_state=state)
        self.assertLess(state["pages"][0]["cells"][0][0], 0)
        self.assertEqual(state["pages"][0]["placements"][0][1:3], (-.5, -.5))
        with Image.open(outputs[0]) as image:
            self.assertTrue(any(r > 150 and g < 30 and b < 30 for r, g, b in image.getdata()))
            self.assertFalse(any(b > 150 and r < 30 and g < 30 for r, g, b in image.getdata()))
            left = round(settings.margin_left * 6)
            top = round(settings.margin_top * 6)
            self.assertLess(max(image.getpixel((left, top))), 40)
        panel, position = state["pages"][0]["panels"][1]
        self.assertEqual(panel.getbbox(), None)

    def test_explicit_zorder_parsing_and_overlapping_render(self):
        self.assertEqual(core.poster_transform({"device": "1/A2/90/100/4"}), ((1, 0), 90, 100))
        self.assertEqual(core.poster_zorder({"device": "1/A2/90/100/4"}), 4)
        self.assertEqual(core.poster_zorder({"device": "1/A2/90/100/"}, 7), 7)
        self.assertEqual(core.poster_zorder({"device": "/////-2,5"}), -2.5)
        for value in ("/////bad", "/////inf", "/////nan", "1/A1/0/0/1/2"):
            with self.assertRaisesRegex(ValueError, "field 3"):
                core.poster_transform({"device": value})
        settings = core.PosterSettings(400, 100, 100, 1, 1, True)
        for z, expected in ((4, (255, 0, 0)), (1, (0, 0, 255)), (2, (0, 0, 255))):
            output = core.create_poster(self.paths[:2], self.profile, settings,
                     metadata=[{"device": f"1/A1/0/0/{z}"}, {"device": "1/A1/0/0/2"}])
            with Image.open(output) as image:
                self.assertTrue(all(abs(a - b) < 10 for a, b in zip(image.getpixel((200, 200)), expected)))

    def test_zorder_edit_changes_stacking_without_reordering_cached_panels(self):
        settings = core.PosterSettings(400, 100, 100, 1, 1, True)
        records = [{"device": "1/A1/0/0/4"}, {"device": "1/A1/0/0/2"}]
        initial, updated = {}, {}
        with patch("core.read_metadata_batch", return_value=records):
            core.create_posters(self.paths[:2], self.profile, settings, layout_state=initial)
        records[0] = {"device": "1/A1/0/0/1"}
        with patch("core.read_metadata_batch", return_value=records):
            outputs = core.create_posters(self.paths[:2], self.profile, settings, layout_state=updated,
                                         previous_layout=initial, changed_indices={0})
        self.assertIs(updated["pages"][0]["panels"][1][0], initial["pages"][0]["panels"][1][0])
        self.assertEqual(updated["pages"][0]["cells"], initial["pages"][0]["cells"])
        with Image.open(outputs[0]) as image:
            self.assertGreater(image.getpixel((200, 200))[2], 240)

    def test_angle_parsing_and_rotated_layout(self):
        for value, expected in (("", 0), ("///90", 90), ("///-90", 270), ("///360", 0), ("///22,5", 22.5)):
            self.assertEqual(core.poster_angle({"device": value}), expected)
        for value in ("bad", "nan", "inf"):
            with self.assertRaisesRegex(ValueError, "field 3"):
                core.poster_angle({"device": value})
        settings = core.PosterSettings(800, 200, 200, 2, 2, False)
        Image.new("RGB", (200, 100), "red").save(self.paths[0])
        page = core.poster_photo_pages(self.paths[:1], [{"device": "///90"}], settings)
        self.assertEqual(page, [[(0, 0, 0, 2, 1)]])

    def test_photo_and_caption_rotate_together(self):
        Image.new("RGB", (200, 100), "red").save(self.paths[0])
        before = self.paths[0].read_bytes()
        def caption_marker(draw, document, left, top, width):
            draw.rectangle((left, top, left + width - 1, top + document.height - 1), fill="blue")
        for angle in (90, -90):
            with patch("core.markdown_render.draw_poster", side_effect=caption_marker):
                output = core.create_poster(self.paths[:1], self.profile,
                    core.PosterSettings(600, 200, 200, 1, 1, True),
                    metadata=[{"device": f"///{angle}", "comment": "Caption"}])
            with Image.open(output) as image:
                centers = []
                for channel in (0, 2):
                    points = [(x, y) for y in range(image.height) for x in range(image.width)
                              if image.getpixel((x, y))[channel] > 180
                              and image.getpixel((x, y))[2 - channel] < 60]
                    self.assertTrue(points)
                    centers.append(sum(x for x, y in points) / len(points))
                # A caption below the image moves left clockwise, right counterclockwise.
                self.assertEqual(centers[1] < centers[0], angle == 90)
        self.assertEqual(self.paths[0].read_bytes(), before)

    def test_only_comment_is_caption(self):
        record = {"box": "Title", "comment": "Original", "category": "Category"}
        self.assertEqual(core.poster_caption(record), "Original")
        self.assertEqual(core.poster_caption({"box": "Title"}), "")
        before = [path.read_bytes() for path in self.paths]
        with patch("core.read_metadata_batch", return_value=[record] * 3), \
             patch("core.poster_draw_text", wraps=core.poster_draw_text) as draw, \
             patch("core.markdown_render.draw_poster", wraps=core.markdown_render.draw_poster) as captions:
            core.create_posters(self.paths, self.profile, core.PosterSettings(1000, 200, 200, 3, 1))
        self.assertEqual(draw.call_args_list[0].args[1], ["Title"])
        self.assertEqual(captions.call_args_list[0].args[1].plain_lines, ["Original"])
        self.assertEqual([path.read_bytes() for path in self.paths], before)

    def test_markdown_caption_is_rendered_without_changing_source(self):
        source = "# Heading\n\n**Bold** and *italic*\n\n- `Code`\n- [Link](https://example.org)"
        record = {"box": "Literal **box**", "comment": source}
        before = self.paths[0].read_bytes()
        with patch("core.markdown_render.draw_poster", wraps=core.markdown_render.draw_poster) as draw:
            output = core.create_poster(self.paths[:1], self.profile,
                                        core.PosterSettings(1200, 200, 150, 1, 1), metadata=[record])
        layout = draw.call_args.args[1]
        self.assertEqual(layout.plain_lines, ["Heading", "Bold and italic", "Code", "Link"])
        self.assertGreater(layout.lines[0].height, layout.lines[1].height)
        self.assertTrue(layout.lines[1].runs[0].span.bold)
        self.assertEqual(layout.lines[2].prefix, "•")
        self.assertTrue(layout.lines[2].runs[0].span.code)
        self.assertEqual(record["comment"], source)
        self.assertEqual(self.paths[0].read_bytes(), before)
        with Image.open(output) as image:
            self.assertEqual(image.size, (900, 1200))

    def test_portrait_spans_and_deferral(self):
        self.assertEqual(core.poster_pages([False, False, True, False, True], 2, 2),
                         [[(0, 0, 0, 1, 1), (1, 0, 1, 1, 1), (3, 1, 0, 1, 1)],
                          [(2, 0, 0, 2, 1), (4, 0, 1, 2, 1)]])
        cells = core.poster_grid_cells([(0, 0, 2, 1), (0, 1, 1, 1)], 2, 2, (0, 0, 200, 200), 10)
        self.assertEqual(cells, [(0, 0, 95, 200), (105, 0, 200, 95)])
        with self.assertRaisesRegex(ValueError, "at least 2"):
            core.poster_pages([True], 2, 1)

    def test_flexible_grid_expands_partial_rows_and_portrait_neighbors(self):
        bounds = (0, 0, 200, 200)
        self.assertEqual(core.poster_flexible_cells([(0, 0, 1, 1)], bounds, 10), [bounds])
        self.assertEqual(core.poster_flexible_cells([(0, 0, 2, 1)], bounds, 10), [bounds])
        self.assertEqual(core.poster_flexible_cells([(0, 0, 1, 1), (0, 1, 1, 1), (1, 0, 1, 1)], bounds, 10),
                         [(0, 0, 95, 95), (105, 0, 200, 95), (0, 105, 200, 200)])
        self.assertEqual(core.poster_flexible_cells([(0, 0, 2, 1), (0, 1, 1, 1)], bounds, 10),
                         [(0, 0, 95, 200), (105, 0, 200, 200)])

    def test_flexible_final_poster_single_photo_uses_whole_area(self):
        for fixed in (True, False):
            with self.subTest(fixed=fixed), patch("core.read_metadata_batch", return_value=[{"box": "Title"}] * 5):
                outputs = core.create_posters([self.paths[0]] * 5, self.profile,
                                             core.PosterSettings(400, 100, 100, 2, 2, fixed))
            self.assertEqual(len(outputs), 2)
            with Image.open(outputs[-1]) as image:
                pixel = image.getpixel((300, 280))
                if fixed:
                    self.assertLess(max(abs(a - b) for a, b in zip(pixel, (155, 177, 149))), 4)
                else:
                    self.assertGreater(pixel[0], 240)
                    self.assertLess(pixel[1], 20)

    def test_portrait_export_uses_first_photo_of_each_page(self):
        portrait = self.root / "portrait.png"
        Image.new("RGB", (40, 80), "yellow").save(portrait)
        paths = self.paths[:2] + [portrait, self.paths[2]]
        records = [{"box": str(i), "comment": "Photo " + str(i)} for i in range(4)]
        with patch("core.read_metadata_batch", return_value=records), \
             patch("core.create_poster", wraps=core.create_poster) as create:
            outputs = core.create_posters(paths, self.profile, core.PosterSettings(600, 200, 100, 2, 2, False))
        self.assertEqual(len(outputs), 2)
        self.assertEqual(create.call_args_list[0].args[0], [paths[0], paths[1], paths[3]])
        self.assertEqual(create.call_args_list[1].args[0], [portrait])
        self.assertEqual([call.kwargs["title"] for call in create.call_args_list], ["0", "2"])
        self.assertEqual(create.call_args_list[1].kwargs["placements"], [(0, 0, 2, 1)])
        self.assertEqual(create.call_args_list[1].kwargs["metadata"][0]["comment"], "Photo 2")

    def test_fixed_portraits_use_one_cell_and_allow_single_row(self):
        portrait = self.root / "portrait.png"
        Image.new("RGB", (40, 80), "yellow").save(portrait)
        settings = core.PosterSettings(600, 100, 200, 2, 1, True)
        with patch("core.read_metadata_batch", return_value=[{"box": str(i)} for i in range(3)]), \
             patch("core.create_poster", wraps=core.create_poster) as create:
            outputs = core.create_posters([portrait] * 3, self.profile, settings)
        self.assertEqual(len(outputs), 2)
        self.assertEqual(create.call_args_list[0].kwargs["placements"], [(0, 0, 1, 1), (0, 1, 1, 1)])
        self.assertEqual(create.call_args_list[1].kwargs["placements"], [(0, 0, 1, 1)])
        self.assertEqual([call.kwargs["title"] for call in create.call_args_list], ["0", "2"])
        self.assertTrue(core.create_poster([portrait] * 2, self.profile, settings).is_file())

    def test_zoom_parsing_and_grid_reservations(self):
        for value in ("", "0", "Z=0", "1", "0.5"):
            self.assertEqual(core.poster_zoom({"anzahl": value}), 1)
        self.assertEqual(core.poster_zoom({"anzahl": "Z=2,5"}), 2.5)
        for value in ("bad", "-1", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.poster_zoom({"anzahl": value})
        pages = core.poster_pages([False] * 4, 3, 2, zooms=[3, 1, 1, 1],
                                  aspect_ratios=[3, 1, 1, 1])
        self.assertEqual(pages, [[(0, 0, 0, 1, 3), (1, 1, 0, 1, 1),
                                  (2, 1, 1, 1, 1), (3, 1, 2, 1, 1)]])
        pages = core.poster_pages([True, False], 2, 2, zooms=[2, 1])
        self.assertEqual(pages, [[(0, 0, 0, 2, 2)], [(1, 0, 0, 1, 1)]])
        self.assertEqual(core.poster_pages([False], 2, 2, zooms=[5]),
                         [[(0, 0, 0, 2, 2)]])

    def test_zoom_deferral_leaves_free_cells_for_following_photos(self):
        pages = core.poster_pages([False] * 4, 3, 1, zooms=[1, 3, 1, 1],
                                  aspect_ratios=[1, 3, 1, 1])
        self.assertEqual(pages, [[(0, 0, 0, 1, 1), (2, 0, 1, 1, 1), (3, 0, 2, 1, 1)],
                                 [(1, 0, 0, 1, 3)]])

    def test_zoom_exports_more_space_only_in_flexible_mode(self):
        for fixed in (False, True):
            records = [{"box": str(i), "anzahl": "Z=2" if i == 0 else "0", "comment": "Caption"}
                       for i in range(3)]
            with patch("core.read_metadata_batch", return_value=records), \
                 patch("core.create_poster", wraps=core.create_poster) as create:
                outputs = core.create_posters(self.paths, self.profile,
                                             core.PosterSettings(600, 100, 200, 2, 1, fixed))
            self.assertEqual(len(outputs), 2)
            placements = create.call_args_list[0].kwargs["placements"]
            if fixed:
                self.assertEqual(placements, [(0, 0, 1, 1), (0, 1, 1, 1)])
            else:
                self.assertEqual(placements, [(0, 0, 1, 2)])
            self.assertEqual(create.call_args_list[1].kwargs["title"], "2" if fixed else "1")
        with patch("core.read_metadata", return_value={"anzahl": "invalid"}):
            self.assertTrue(core.create_poster([self.paths[0]], self.profile,
                                              core.PosterSettings(400, 100, 100, 1, 1, True)).is_file())
        with patch("core.read_metadata", return_value={"anzahl": "2"}), \
             patch("core.poster_flexible_cells", wraps=core.poster_flexible_cells) as cells:
            core.create_poster([self.paths[0]], self.profile,
                               core.PosterSettings(400, 100, 100, 2, 1, False))
        self.assertEqual(cells.call_args.args[0], [(0, 0, 1, 2)])

    def test_per_image_poster_edits_do_not_change_other_images(self):
        records = [{"device": "A1", "comment": "First"},
                   {"device": "B1", "comment": "Second"}]
        with patch("core.read_metadata_batch", return_value=records), \
             patch("core.create_poster", return_value=self.root / "poster.jpg") as render:
            core.create_posters(self.paths[:2], self.profile,
                                core.PosterSettings(cols=2, rows=1, fixed=True),
                                path_overrides={self.paths[1]: {"device": "A1/90", "comment": "Edited"}})
        metadata = render.call_args.kwargs["metadata"]
        self.assertEqual(metadata, [records[0], {"device": "A1/90", "comment": "Edited"}])
        self.assertEqual(records[1], {"device": "B1", "comment": "Second"})

    def test_individual_refresh_preserves_other_panels_and_page_membership(self):
        for fixed in (False, True):
            with self.subTest(fixed=fixed):
                settings = core.PosterSettings(600, 100, 100, 2, 2, fixed)
                records = [{"device": "", "comment": "Original"} for _ in self.paths]
                initial = {}
                with patch("core.read_metadata_batch", return_value=records):
                    core.create_posters(self.paths, self.profile, settings, layout_state=initial)
                edited = [dict(record) for record in records]
                edited[0].update(device="B2/90/100", anzahl="Z=3", comment="Updated")
                # Even if unrelated metadata changes externally, an individual update
                # retains the other panels exactly as they appeared in the preview.
                edited[1]["comment"] = "Unrelated external change"
                updated = {}
                with patch("core.read_metadata_batch", return_value=edited), \
                     patch("core.Image.open", wraps=Image.open) as opened:
                    core.create_posters(self.paths, self.profile, settings, layout_state=updated,
                                        previous_layout=initial, changed_indices={0})
                self.assertTrue(all(call.args[0] == self.paths[0] for call in opened.call_args_list))
                self.assertEqual(len(updated["pages"]), len(initial["pages"]))
                for old_page, new_page in zip(initial["pages"], updated["pages"]):
                    self.assertEqual([item[0] for item in old_page["placements"]],
                                     [item[0] for item in new_page["placements"]])
                    for local, placement in enumerate(old_page["placements"]):
                        if placement[0] != 0:
                            self.assertEqual(new_page["cells"][local], old_page["cells"][local])
                            self.assertIs(new_page["panels"][local][0], old_page["panels"][local][0])
                self.assertEqual(updated["metadata"][1], records[1])
                self.assertEqual(updated["pages"][0]["placements"][0][1:3], (1, 1))

    def test_individual_transparency_change_keeps_flexible_cell_size(self):
        settings = core.PosterSettings(600, 100, 100, 3, 3, False)
        initial, updated = {}, {}
        with patch("core.read_metadata_batch", return_value=[{}]):
            core.create_posters(self.paths[:1], self.profile, settings, layout_state=initial)
        with patch("core.read_metadata_batch", return_value=[{"device": "////100"}]):
            core.create_posters(self.paths[:1], self.profile, settings, layout_state=updated,
                                previous_layout=initial, changed_indices={0})
        self.assertEqual(initial["pages"][0]["cells"], updated["pages"][0]["cells"])

    def test_ui_passes_title_and_only_new_additions(self):
        app = App.__new__(App)
        app.busy = app.search_mode = False
        app.media, app.profile = self.paths, self.profile
        app.settings = SimpleNamespace(poster=core.PosterSettings(), path=self.root / "setup.ini")
        app.media_baseline = {"box": "Existing", "comment": "Original"}
        app.values = Mock(return_value={"category": "Different category", "box": "Existing", "comment": "Added"})
        app.log = Mock()
        def run(work, done):
            outputs, temporary = work()
            self.assertNotEqual(create.call_args.args[1].images, self.profile.images)
            temporary.cleanup()
        app.task = run
        with patch("core.create_posters") as create:
            app.make_poster()
        self.assertEqual(create.call_args.kwargs,
                         {"field_overrides": {"category": "Different category", "comment": "Added"}, "setup_dir": self.root, "layout_state": {}})

    def test_poster_obeys_selected_metadata_mode(self):
        originals = [core.normalized({"box": "A", "comment": "first"}),
                     core.normalized({"box": "B", "comment": "second"})]
        baseline = core.common_metadata(originals)
        values = dict(baseline, comment="new")
        for mode in ("extend", "replace"):
            with self.subTest(mode=mode), patch("core.read_metadata_batch", return_value=originals), \
                 patch("core.poster_draw_text", wraps=core.poster_draw_text) as draw, \
                 patch("core.markdown_render.draw_poster", wraps=core.markdown_render.draw_poster) as captions:
                core.create_posters(self.paths[:2], self.profile, core.PosterSettings(600, 200, 200, 2, 1),
                                    field_overrides=values, field_baseline=baseline, edit_mode=mode,
                                    differing_fields=core.differing_metadata_fields(originals))
            self.assertEqual(draw.call_args_list[0].args[1], ["A"] if mode == "extend" else [])
            self.assertEqual(captions.call_args_list[0].args[1].plain_lines, ["first", "new"] if mode == "extend" else ["new"])
            self.assertEqual(captions.call_args_list[1].args[1].plain_lines, ["second", "new"] if mode == "extend" else ["new"])

    def test_multiple_posters_preserve_order_and_title(self):
        paths = self.paths * 3
        settings = core.PosterSettings(600, 200, 100, 2, 2)
        with patch("core.create_poster", wraps=core.create_poster) as create:
            with patch("core.read_metadata_batch", return_value=[{"box": str(i)} for i in range(9)]):
                outputs = core.create_posters(paths, self.profile, settings)
        self.assertEqual(len(outputs), 3)
        self.assertEqual(len(set(outputs)), 3)
        self.assertTrue(all(path.is_file() for path in outputs))
        self.assertEqual([call.args[0] for call in create.call_args_list],
                         [paths[:4], paths[4:8], paths[8:]])
        self.assertEqual([call.kwargs["title"] for call in create.call_args_list], ["0", "4", "8"])

    def test_batch_validation_before_export(self):
        for paths in ([], self.paths * 3 + [self.root / "movie.mp4"]):
            with self.assertRaises(ValueError):
                core.create_posters(paths, self.profile, core.PosterSettings())
        self.assertFalse((self.root / "poster").exists())

    def test_wrapping_preserves_long_words_and_paragraphs(self):
        font = core.poster_font(15)
        text = "LangesWortOhneLeerzeichen" * 3 + "\nGrüße aus Wien"
        lines = core.poster_wrap(text, font, 100)
        self.assertEqual("".join(lines).replace(" ", ""), text.replace("\n", "").replace(" ", ""))
        self.assertTrue(all(font.getlength(line) <= 100 for line in lines))

    def test_margin_and_color_configuration(self):
        settings = core.poster_settings("""[Poster]
POSTER-MARGIN-LEFT=2.5
POSTER-MARGIN-RIGHT=3
POSTER-MARGIN-TOP=0
POSTER-MARGIN-BOTTOM=7
POSTER-FRAMES=eeee
POSTER-BACKGROUND-COLOR=9bb195
POSTER-IMAGE-BACKGROUND-COLOR=FFFFFF
""")
        self.assertEqual((settings.margin_left, settings.margin_right, settings.margin_top, settings.margin_bottom),
                         (2.5, 3, 0, 7))
        self.assertEqual(settings.image_padding, 2)
        self.assertEqual(core.poster_color(settings.frames), (238, 238, 238, 238))
        for value in ("POSTER-MARGIN-TOP=-1", "POSTER-MARGIN-LEFT=nan", "POSTER-FRAMES=xyz", "POSTER-IMAGE-PADDING=-2", "POSTER-IMAGE-PADDING=inf"):
            with self.assertRaises(ValueError):
                core.poster_settings("[Poster]\n" + value)

    def test_background_search_fallback_and_cover(self):
        setup_dir = self.root / "setup"
        setup_dir.mkdir()
        self.assertEqual(core.poster_background((100, 200), "9bb195", setup_dir).getpixel((0, 0)),
                         (155, 177, 149))
        (setup_dir / "a.png").write_bytes(b"broken")
        Image.new("RGB", (20, 10), "blue").save(setup_dir / "b.png")
        Image.new("RGB", (10, 20), "red").save(setup_dir / "c.png")
        background = core.poster_background((100, 200), "9bb195", setup_dir)
        self.assertEqual(background.size, (100, 200))
        self.assertEqual(background.getextrema(), ((0, 0), (0, 0), (255, 255)))

    def test_configured_margin_frame_and_image_background_pixels(self):
        path = self.root / "transparent.png"
        Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(path)
        settings = core.PosterSettings(1000, 100, 100, 1, 1, margin_left=15, margin_right=10,
                                       margin_top=10, margin_bottom=15, frames="FF0000",
                                       background_color="0000FF", image_background_color="00FF00")
        with patch("core.read_metadata", return_value={}):
            output = core.create_poster([path], self.profile, settings)
        with Image.open(output) as image:
            for point, expected in (((50, 500), (0, 0, 255)), ((950, 500), (0, 0, 255)),
                                    ((500, 50), (0, 0, 255)), ((500, 950), (0, 0, 255)),
                                    ((500, 500), (0, 255, 0))):
                self.assertLess(max(abs(a-b) for a,b in zip(image.getpixel(point), expected)), 10)
            red, green, blue = image.getpixel((151, 500))
            self.assertGreater(red, green + 80)
            self.assertGreater(red, blue + 80)

    def test_padding_keeps_photo_inside_frame_in_millimeters(self):
        with patch("core.read_metadata", return_value={}):
            output = core.create_poster([self.paths[0]], self.profile,
                                       core.PosterSettings(1000, 100, 100, 1, 1, image_padding=2))
        with Image.open(output) as image:
            # 10 pixels/mm: left margin 150 + frame 3 + padding 20.
            self.assertTrue(all(value > 245 for value in image.getpixel((165, 450))))
            self.assertGreater(image.getpixel((180, 450))[0], 240)
            self.assertLess(image.getpixel((180, 450))[1], 20)

    def test_selection_message_shows_calculated_capacity(self):
        settings = core.PosterSettings(cols=4, rows=2)
        for paths in ([], self.paths * 3):
            with self.assertRaisesRegex(ValueError, r"^Select between 1 and 8 images\.$"):
                core.create_poster(paths, self.profile, settings)

    def test_validation_does_not_create_output(self):
        for paths in ([], self.paths + self.paths, [self.root / "movie.mp4"]):
            with self.assertRaises(ValueError):
                core.create_poster(paths, self.profile, core.PosterSettings(cols=2, rows=2))
        self.assertFalse((self.root / "poster").exists())
        for setting in ('POSTER-SIZE=0x10', 'POSTER-COLS=0', 'POSTER-ROWS=-1',
                        'POSTER-LIMIT=20001', 'POSTER-FIX=maybe', 'POSTER-SIZE=oops'):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                core.poster_settings('[Poster]\n' + setting)
        self.assertEqual(core.poster_settings('[ImageSize]\nPOSTER-LIMIT=400').limit, 400)


if __name__ == '__main__':
    unittest.main()
