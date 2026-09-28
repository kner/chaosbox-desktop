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
            self.assertGreater(image.getpixel((175, 450))[0], 240)
        second = core.create_poster(self.paths, self.profile, settings)
        self.assertNotEqual(path, second)

    def test_flexible_last_image_uses_remaining_row(self):
        path = core.create_poster(self.paths, self.profile, core.PosterSettings(400, 100, 100, 2, 2, False))
        with Image.open(path) as image:
            self.assertGreater(image.getpixel((200, 300))[1], 100)
            self.assertLess(image.getpixel((200, 300))[0], 10)
            self.assertLess(max(abs(a-b) for a,b in zip(image.getpixel((25, 300)), (155, 177, 149))), 4)

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
             patch("core.poster_draw_text", wraps=core.poster_draw_text) as draw_text:
            output = core.create_poster(self.paths, self.profile,
                                        core.PosterSettings(1000, 100, 200, 3, 1, True), title="Werkzeug")
        self.assertEqual([call.args[0] for call in read.call_args_list], self.paths)
        self.assertEqual(draw_text.call_args_list[0].args[1], ["Werkzeug"])
        for index, call in enumerate(draw_text.call_args_list[1:]):
            self.assertEqual(" ".join(call.args[1]), "Beschreibung " + str(index))
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

    def test_poster_caption_appends_form_values_without_changing_metadata(self):
        record = {"box": "Box1", "comment": "Original", "category": "Saved category"}
        self.assertEqual(core.poster_caption(record, "Extra box", "Extra comment"),
                         "Box1: Original\nExtra box\nExtra comment")
        self.assertEqual(record["comment"], "Original")
        self.assertEqual(core.poster_caption({"comment": "Only comment"}), "Only comment")
        self.assertEqual(core.poster_caption({"box": "Box1"}), "Box1")
        before = [path.read_bytes() for path in self.paths]
        with patch("core.read_metadata", return_value=record), \
             patch("core.poster_draw_text", wraps=core.poster_draw_text) as draw:
            core.create_poster(self.paths, self.profile, core.PosterSettings(1000, 200, 200, 3, 1),
                               title="Form title", box_addition="Extra box", comment_addition="Extra comment")
        self.assertEqual(draw.call_args_list[0].args[1], ["Form title"])
        self.assertEqual(draw.call_args_list[1].args[1], ["Box1: Original", "Extra box", "Extra comment"])
        self.assertEqual([path.read_bytes() for path in self.paths], before)

    def test_last_photo_uses_remaining_rows_and_preceding_row_spreads_evenly(self):
        bounds = (15, 5, 315, 305)
        self.assertEqual(core.poster_cells(3, 3, 3, bounds, 0),
                         [(15, 5, 165, 105), (165, 5, 315, 105), (15, 105, 315, 305)])
        self.assertEqual(core.poster_cells(1, 3, 3, bounds, 0), [bounds])
        cells = core.poster_cells(5, 3, 3, bounds, 0)
        self.assertEqual(cells[-2:], [(15, 105, 315, 205), (15, 205, 315, 305)])
        full = core.poster_cells(9, 3, 3, bounds, 0)
        self.assertEqual(len(full), 9)
        self.assertTrue(all(right - left == 100 and bottom - top == 100
                            for left, top, right, bottom in full))
        self.assertEqual(core.poster_cells(8, 3, 3, bounds, 0)[-2:],
                         [(15, 205, 165, 305), (165, 205, 315, 305)])
        with patch("core.read_metadata", return_value={}), \
             patch("core.poster_draw_text", wraps=core.poster_draw_text) as draw:
            core.create_poster(self.paths, self.profile, core.PosterSettings(1000, 100, 200, 3, 3))
        cells = [call.args for call in draw.call_args_list[1:]]
        self.assertEqual(cells[0][4], 87)
        self.assertAlmostEqual(cells[0][6], 406, delta=1)
        self.assertAlmostEqual(cells[1][6], 406, delta=1)
        self.assertEqual(cells[2][4], 87)
        self.assertEqual(cells[2][6], 851)

    def test_ui_passes_title_and_only_new_additions(self):
        app = App.__new__(App)
        app.busy = app.search_mode = False
        app.media, app.profile = self.paths, self.profile
        app.settings = SimpleNamespace(poster=core.PosterSettings(), path=self.root / "setup.ini")
        app.media_baseline = {"box": "Existing", "comment": "Original"}
        app.values = Mock(return_value={"category": "Unsaved title", "box": "Existing", "comment": "Added"})
        app.log = Mock()
        app.task = lambda work, done: work()
        with patch("core.create_poster") as create:
            app.make_poster()
        self.assertEqual(create.call_args.kwargs,
                         {"title": "Unsaved title", "box_addition": "", "comment_addition": "Added", "setup_dir": self.root})

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
