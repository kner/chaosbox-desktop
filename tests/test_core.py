import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
import install


class LegacyMetadataTests(unittest.TestCase):
    def test_category_from_label_or_second_pipe_field(self):
        for raw, expected in (("3 | Elektronik | Sensor", "Elektronik"),
                              ("Anzahl: 3\nKategorie: Mechanik\nKommentar: Test", "Mechanik"),
                              ("3 | Kategorie: Dichtung | Test", "Dichtung"),
                              ("3 | Ersatz | Kategorie: Pneumatik\nTest", "Pneumatik"),
                              ("Kategorie:\nKommentar: Test", ""),
                              ("Freier\nKommentar", "")):
            with self.subTest(raw=raw):
                data = core.parse_metadata(raw)
                self.assertEqual(data["category"], expected)
                self.assertEqual(data["comment"], raw)

    def test_binary_comment_preserves_line_breaks(self):
        path = Path("photo.jpg").resolve()
        with patch("core.run_tool", return_value=b"3 | Tools | First\nsecond\n") as run:
            self.assertEqual(core.read_user_comment(path), "3 | Tools | First\nsecond\n")
        run.assert_called_once_with("exiftool", "-b", "-EXIF:UserComment", str(path))


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chaosbox-desktop-test-")
        self.root = Path(self.temp.name)
        self.settings = core.Settings(self.root, self.root / ".config/chaosbox")
        template = core.DEFAULT_SETUP.read_text()
        template = ('[App]\nImageWidth=3000\nFelder=Box,Quantity,Device,Alias,Category,Comment,Package\n'
                    '[Box]\nTitel=Chaosbox\nKategorie=Heizung\n'
                    '[Box]\nTitel=Bilderbox\nFelder=Box,,,Tags,Category,Comment\nKategorie=Familie\n\n'
                    + template[template.index('[Poster]'):])
        core.atomic_write(self.settings.path, template)
        self.settings.ensure()
        self.profile = self.settings.profile("Chaosbox")
        self.profile.images.mkdir(parents=True)
        self.profile.data.mkdir(parents=True)
        self.record = core.validate_record(dict(zip(core.FIELDS, ["A11", "3", "Sensor", "Alias", "Elektronik", "Grüße 🎬\n東京", "Pack"])))

    def tearDown(self):
        self.temp.cleanup()

    def picture(self, name="source.jpg", size=(120, 80)):
        path = self.profile.images / name[4:] if name.startswith("JPG/") else self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (32, 140, 180)).save(path)
        return path

    def test_setup_categories_and_snippets(self):
        self.assertIn("Analysiere", self.settings.snippets["chatgpt"])
        before = self.settings.path.read_text()
        self.settings.remember_category(self.profile, "New category, HEIZUNG, new CATEGORY")
        self.assertEqual(self.settings.profile("Chaosbox").categories.count("New category"), 1)
        self.assertNotIn("New category", self.settings.profile("Bilderbox").categories)
        self.assertEqual(self.settings.snippets["chatgpt"], core.Settings(self.root, self.root / "unused").parse(before)[3]["chatgpt"])
        unchanged = self.settings.path.read_text()
        self.settings.remember_category(self.profile, "NEW CATEGORY")
        self.assertEqual(self.settings.path.read_text(), unchanged)
        with self.assertRaises(ValueError):
            self.settings.remember_category(self.profile, "evil\n[App.Bad]")
        self.assertIn("[TextSnippets]", unchanged)
        with self.assertRaises(ValueError):
            self.settings.save_text(before + "\n[Box]\nTitel=Bilderbox\n")
        self.assertEqual(self.settings.path.read_text(), unchanged)

    def test_recursive_project_setup_inheritance_and_sibling_isolation(self):
        inherited_limit = self.settings.image_width
        local = self.root / "Bilderbox/setup.ini"
        local.parent.mkdir()
        local.write_text('[Poster]\nPOSTER-COLS=2\n'
                         '[TextSnippets]\nLocal="parent"\n'
                         '[App]\nImageWidth=1200\nKategorie=Local\n'
                         '[Box]\nTitel=Urlaub\n'
                         '[Box]\nTitel=Familie\n')
        nested = local.parent / "Urlaub/setup.ini"
        nested.parent.mkdir()
        nested.write_text('[TextSnippets]\nLocal="child"\n'
                          '[App]\nimagewidth=800\nKategorie=\n'
                          '[Box]\nTitel=Berge\n')
        self.settings.reload()
        child = self.settings.profile("Bilderbox/Urlaub")
        self.assertEqual(child.images, nested.parent / "JPG")
        self.assertEqual(child.categories, [])
        self.assertEqual(self.settings.image_width, 800)
        self.assertEqual(self.settings.poster.cols, 2)
        self.assertEqual(self.settings.poster.rows, 3)
        self.assertEqual(self.settings.snippets["Local"], "child")
        self.assertIn("chatgpt", self.settings.snippets)
        grandchild = self.settings.profile("Bilderbox/Urlaub/Berge")
        self.assertEqual(grandchild.images, nested.parent / "Berge/JPG")
        self.assertEqual(grandchild.labels, child.labels)
        self.assertEqual(self.settings.image_width, 800)
        sibling = self.settings.profile("Bilderbox/Familie")
        self.assertEqual(sibling.categories, ["Local"])
        self.assertEqual(self.settings.image_width, 1200)
        self.assertEqual(self.settings.snippets["Local"], "parent")
        self.settings.profile("Chaosbox")
        self.assertEqual(self.settings.image_width, inherited_limit)
        self.assertNotIn("Local", self.settings.snippets)
        self.assertEqual(self.settings.active_path, self.settings.path)

    def test_local_setup_save_validation_and_category_destination(self):
        local = self.root / "Bilderbox/setup.ini"
        local.parent.mkdir()
        local.write_text('[App]\nImageWidth=1000\n'
                         '[Box]\nTitel=Child\n')
        original = self.settings.path.read_bytes()
        self.settings.reload()
        profile = self.settings.profile("Bilderbox")
        self.settings.remember_category(profile, "New local")
        self.assertEqual(self.settings.path.read_bytes(), original)
        self.assertEqual(set(core.sections(local.read_text())["App"]), {"kategorie", "imagewidth"})
        self.assertIn("New local", self.settings.profile("Bilderbox/Child").categories)
        self.settings.remember_category(self.settings.profile("Bilderbox/Child"), "Child only")
        self.assertNotIn("Child only", self.settings.profile("Bilderbox").categories)
        self.assertIn("Child only", self.settings.profile("Bilderbox/Child").categories)
        before = local.read_bytes()
        with self.assertRaises(ValueError):
            self.settings.save_text('[Poster]\nPOSTER-COLS=0\n', local)
        self.assertEqual(local.read_bytes(), before)
        self.settings.save_text(local.read_text().replace('ImageWidth=1000', 'ImageWidth=900'), local)
        self.assertEqual(self.settings.active, "Bilderbox/Child")
        self.assertEqual(self.settings.image_width, 900)

    def test_image_width_box_overrides_and_validation(self):
        text = ("[App]\nImageWidth=2048\n"
                "[Box]\nTitel=First\nImageWidth=1200\n"
                "[Box]\nTitel=Second\n")
        self.settings.save_text(text)
        self.settings.profile("First")
        self.assertEqual(self.settings.image_width, 1200)
        self.settings.profile("Second")
        self.assertEqual(self.settings.image_width, 2048)
        for value in ("0", "20001", "invalid", "1.5"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "ImageWidth"):
                self.settings.parse(text.replace("ImageWidth=1200", f"ImageWidth={value}"))
        profiles, *_ = self.settings.parse("[App]\n[Box]\nTitel=Default\n")
        self.assertEqual(profiles[0].options[0], 3000)

    def test_image_width_resizes_portrait_proportionally_without_height_limit(self):
        source = self.picture("portrait.jpg", (120, 240))
        saved = core.save_media(source, self.profile, self.record, 60)
        with Image.open(saved) as image:
            self.assertEqual(image.size, (60, 120))
        narrow = self.picture("narrow.jpg", (40, 240))
        saved = core.save_media(narrow, self.profile, self.record, 60)
        with Image.open(saved) as image:
            self.assertEqual(image.size, (40, 240))

    def test_recursive_setup_rejects_escaping_and_duplicate_folders(self):
        local = self.root / "Bilderbox/setup.ini"
        local.parent.mkdir()
        for title in ("..", "../outside", "/tmp/outside", "JPG", "TXT", ".indices", ""):
            local.write_text(f"[Box]\nTitel={title}\n")
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.settings.reload()
        local.write_text('[Box]\nTitel=One\n[Box]\nTitel=one\n')
        with self.assertRaisesRegex(ValueError, "unique"):
            self.settings.reload()
        local.write_text('[Box]\nTitel=Loop\n')
        (local.parent / "Loop").symlink_to(local.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            self.settings.reload()

    def test_repeated_box_categories_are_edited_independently(self):
        self.settings.save_text('[App]\nKategorie=Root\n[Box]\nTitel=One\nKategorie=A\n'
                                '[Box]\nTitel=Two\nKategorie=B\n')
        self.settings.remember_category(self.settings.profile("Two"), "C")
        self.assertEqual(self.settings.profile("One").categories, ["A"])
        self.assertEqual(self.settings.profile("Two").categories, ["B", "C"])
        self.assertEqual([profile.id for profile in self.settings.profiles], ["One", "Two"])
        self.assertEqual(self.settings.profile("Two").data, self.root / "Two/TXT")
        self.assertEqual(self.settings.path.read_text().count('[Box]'), 2)

    def test_jpeg_and_png_metadata(self):
        source = self.picture()
        original = source.read_bytes()
        target = core.save_media(source, self.profile, self.record, 3000)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(core.read_metadata(target)["comment"], self.record["comment"])
        def scan(data):
            position = 2
            while data[position:position + 2] != b"\xff\xda":
                size = int.from_bytes(data[position + 2:position + 4], "big")
                position += size + 2
            return data[position:]
        self.assertEqual(scan(original), scan(target.read_bytes()))
        previous = target.stat().st_mtime
        saved = core.save_media(target, self.profile, dict(self.record, comment="Changed"), 3000)
        self.assertEqual(saved, target)
        self.assertGreater(int(saved.stat().st_mtime), int(previous))
        self.assertEqual(core.read_metadata(target)["comment"], "Changed")
        # Collision-safe imports and white PNG transparency, without changing the PNG.
        self.assertNotEqual(core.save_media(source, self.profile, self.record, 3000), target)
        png = self.root / "alpha.png"
        Image.new("RGBA", (200, 100), (0, 0, 0, 0)).save(png)
        png_before = png.read_bytes()
        converted = core.save_media(png, self.profile, self.record, 60)
        with Image.open(converted) as image:
            self.assertEqual(image.size, (60, 30))
            self.assertEqual(image.getpixel((10, 10)), (255, 255, 255))
        self.assertEqual(png.read_bytes(), png_before)

    def test_capture_date_overrides_filename_and_legacy_created(self):
        source = self.picture("IMG_20240102_030405.jpg")
        core.run_tool("exiftool", "-overwrite_original", "-DateTimeOriginal=2020:05:06 07:08:09",
                      "-OffsetTimeOriginal=+02:00", str(source))
        saved = core.save_media(source, self.profile, dict(self.record, created="wrong", modified="old"), 60)
        raw = json.loads(core.read_user_comment(saved))
        self.assertEqual(raw["created"], "2020-05-06T07:08:09+02:00")
        self.assertNotIn("modified", raw)
        self.assertEqual(core.read_metadata(saved)["created"], raw["created"])

    def test_filename_dates_and_invalid_exif(self):
        for name, expected in (("IMG_20240102_030405_cb_2.jpg", "2024-01-02T03:04:05"),
                               ("2024-01-02_03-04-05.png", "2024-01-02T03:04:05"),
                               ("IMG-20240102-WA0001.jpg", "2024-01-02"),
                               ("IMG_20240230_030405.jpg", "existing"),
                               ("photo.jpg", "existing")):
            with self.subTest(name=name), patch("core.run_tool", return_value=b'[{"DateTimeOriginal":"0000:00:00 00:00:00"}]'):
                self.assertEqual(core.media_created(Path(name), "existing"), expected)

    def test_source_mtime_fallback_survives_repeated_saves(self):
        source = self.picture()
        os.utime(source, (946684800, 946684800))
        expected = core.datetime.fromtimestamp(946684800).astimezone().isoformat(timespec="seconds")
        saved = core.save_media(source, self.profile, self.record, 3000)
        self.assertEqual(json.loads(core.read_user_comment(saved))["created"], expected)
        saved = core.save_media(saved, self.profile, self.record, 3000)
        self.assertEqual(json.loads(core.read_user_comment(saved))["created"], expected)

    def test_batch_dates_and_index_remove_modified(self):
        sources = [self.picture("IMG_20240102_030405.jpg"), self.picture("IMG_20240203_040506.png")]
        saved = core.save_batch(sources, self.profile, dict(self.record, modified="old"), 60)
        self.assertEqual([json.loads(core.read_user_comment(path))["created"] for path in saved],
                         ["2024-01-02T03:04:05", "2024-02-03T04:05:06"])
        core.write_index(self.profile, [core.Entry(saved[0], {"modified": "old", "created": "kept"}, True)])
        self.assertNotIn("modified", (self.profile.index / "records.json").read_text())
        self.assertNotIn("modified", (self.profile.index / "desktop-index.json").read_text())

    def test_box_save_removes_legacy_modified_from_all_records(self):
        path = self.profile.data / "old.json"
        path.write_text('[{"device":"A","modified":"old"},{"device":"B","modified":"old","extra":42}]')
        core.save_box(path, dict(self.record, modified="new"), 0)
        records = json.loads(path.read_text())
        self.assertTrue(all("modified" not in record for record in records))
        self.assertEqual(records[1]["extra"], 42)
        self.assertNotIn("modified", core.validate_record(dict(self.record, modified="old")))

    def test_boxes_preserve_selected_record_and_unknown_fields(self):
        path = self.profile.data / "nested/a11.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"first": {"device": "Same", "count": 2},
                                   "second": {"device": "Same", "count": 3, "pack": "Old", "created": "old", "extra": 42}}))
        records, index = core.save_box(path, dict(self.record, device="Renamed"), 1)
        self.assertEqual(index, 1)
        self.assertEqual(records[0]["count"], 2)
        self.assertEqual(records[1]["count"], 3)
        self.assertEqual(records[1]["pack"], "Pack")
        self.assertEqual(records[1]["created"], "old")
        self.assertEqual(records[1]["extra"], 42)
        self.assertEqual(core.box_filename(" A11.JSON "), "a11.json")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            core.save_box(path, self.record, 5)
        self.assertEqual(path.read_bytes(), before)

    def test_saved_media_moves_with_category_and_cleared_category(self):
        source = self.picture("JPG/old/photo.jpg")
        core.build_index(self.profile)
        moved = core.save_media(source, self.profile, self.record, 3000)
        self.assertEqual(moved, self.profile.images / "elektronik" / source.name)
        self.assertFalse(source.exists())
        self.assertEqual(core.read_metadata(moved)["category"], "Elektronik")
        entries = core.update_index(self.profile, [source, moved])
        self.assertEqual([entry.source for entry in entries], [moved])
        cleared = core.save_media(moved, self.profile, dict(self.record, category=""), 3000)
        self.assertEqual(cleared, self.profile.images / "unassigned" / source.name)
        self.assertFalse(moved.exists())
        self.assertEqual(core.read_metadata(cleared)["category"], "")
        self.assertEqual(core.save_media(cleared, self.profile, dict(self.record, category=""), 3000), cleared)

    def test_category_move_preserves_colliding_file(self):
        source = self.picture("JPG/old/photo.jpg")
        existing = self.picture("JPG/elektronik/photo.jpg")
        before = existing.read_bytes()
        moved = core.save_media(source, self.profile, self.record, 3000)
        self.assertEqual(moved.name, "photo_1.jpg")
        self.assertFalse(source.exists())
        self.assertEqual(existing.read_bytes(), before)

    def test_category_move_rolls_back_if_source_cannot_be_removed(self):
        source = self.picture("JPG/old/photo.jpg")
        before = source.read_bytes()
        unlink = Path.unlink
        def fail_source(path, *args, **kwargs):
            if path == source:
                raise PermissionError("Source cannot be removed")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_source):
            with self.assertRaises(PermissionError):
                core.save_media(source, self.profile, self.record, 3000)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(core.files(self.profile.images, core.MEDIA), [source])

    def test_partial_batch_retry_and_search(self):
        source = self.picture()
        bad = self.root / "broken.jpg"
        bad.write_text("broken")
        with self.assertRaises(core.BatchError) as context:
            core.save_batch([source, bad], self.profile, self.record, 3000)
        failure = context.exception
        self.assertEqual(failure.completed, 1)
        self.assertNotEqual(failure.selection[0], source)
        self.assertEqual(failure.selection[1], bad)
        Image.new("RGB", (20, 20), "red").save(bad)
        result = core.save_batch(failure.selection, self.profile, self.record, 3000)
        self.assertEqual(result[0], failure.selection[0])
        self.assertEqual(len(core.files(self.profile.images, core.MEDIA)), 2)
        entries = core.build_index(self.profile)
        self.assertEqual(len(core.search(entries, core.compile_query({"category": "grüße", "device": "sensor"}))), 2)
        self.assertFalse(core.search(entries, core.compile_query({"comment": "absent"})))
        self.assertEqual(len(core.search(entries, core.compile_query({"comment": "ALIAS"}))), 2)
        index = self.profile.index / "records.json"
        before = index.read_bytes()
        (self.profile.data / "bad.json").write_text("invalid")
        with self.assertRaises(ValueError):
            core.build_index(self.profile)
        self.assertEqual(index.read_bytes(), before)

    def test_incremental_index_only_reads_changed_sources(self):
        first = self.picture("JPG/one/same.jpg")
        second = self.picture("JPG/two/same.jpg")
        box = self.profile.data / "a11.json"
        box.write_text('[{"device":"first"},{"device":"second"}]')
        with patch("core.read_metadata", return_value=core.normalized(self.record)):
            core.build_index(self.profile)
        imported = self.picture("JPG/new/import_cb.jpg")
        with patch("core.files", side_effect=AssertionError("Unexpected directory scan")), \
             patch("core.load_box", side_effect=AssertionError("Unchanged JSON read")), \
             patch("core.read_metadata", return_value=core.normalized(dict(self.record, comment="changed"))) as read:
            entries = core.update_index(self.profile, [first, imported])
            self.assertEqual({call.args[0] for call in read.call_args_list}, {first, imported})
        by_path = {entry.source: entry for entry in entries if entry.media}
        self.assertEqual(by_path[first].data["comment"], "changed")
        self.assertEqual(by_path[second].data["comment"], self.record["comment"])
        self.assertIn(imported, by_path)
        box.write_text('[{"device":"replacement"}]')
        with patch("core.read_metadata", side_effect=AssertionError("Unchanged image read")), \
             patch("core.load_box", wraps=core.load_box) as read_box:
            entries = core.update_index(self.profile, [box])
            read_box.assert_called_once_with(box)
        records = [entry for entry in entries if entry.source == box]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].data["device"], "replacement")
        self.assertEqual(records[0].index, 0)
        first.unlink()
        entries = core.update_index(self.profile, [first])
        self.assertNotIn(first, [entry.source for entry in entries])
        self.assertEqual(len(json.loads((self.profile.index / "records.json").read_text())), len(entries))

    def test_incremental_index_recovers_missing_cache(self):
        path = self.picture("JPG/sample.jpg")
        with patch("core.read_metadata", return_value=core.normalized(self.record)) as read:
            entries = core.update_index(self.settings, [path])
        read.assert_called_once_with(path)
        self.assertEqual(len(entries), 1)

    def test_broken_cache_requires_explicit_rebuild(self):
        core.atomic_write(self.settings.index / "desktop-index.json", "invalid")
        with patch("core.build_index") as build:
            with self.assertRaises(ValueError):
                core.ensure_index(self.settings)
            build.assert_not_called()

    def test_failed_incremental_read_preserves_cache(self):
        path = self.picture("JPG/sample.jpg")
        with patch("core.read_metadata", return_value=core.normalized(self.record)):
            core.build_index(self.settings)
        cache = self.settings.index / "desktop-index.json"
        before = cache.read_bytes()
        with patch("core.read_metadata", side_effect=ValueError("unreadable")):
            with self.assertRaises(ValueError):
                core.update_index(self.settings, [path])
        self.assertEqual(cache.read_bytes(), before)

    def test_shared_index_reuses_cache_and_explicitly_rebuilds(self):
        for profile in self.settings.profiles:
            core.atomic_write(profile.data / "box.json", '[{"box":"' + profile.id + '"}]')
            self.assertEqual(profile.index, self.settings.index)
        entries = core.ensure_index(self.settings)
        self.assertEqual({entry.data["box"] for entry in entries}, {p.id for p in self.settings.profiles})
        added = self.profile.data / "new.json"
        core.atomic_write(added, '[{"box":"new"}]')
        with patch("core.build_index", side_effect=AssertionError("Unexpected rebuild")), \
             patch("core.migrate_media", side_effect=AssertionError("Unexpected migration")):
            self.assertEqual(len(core.ensure_index(self.settings)), len(entries))
        self.assertEqual(len(core.ensure_index(self.settings, newindex=True)), len(entries) + 1)
        core.atomic_write(added, '[{"box":"edited"}]')
        updated = core.update_index(self.settings, [added])
        self.assertEqual({entry.data["box"] for entry in updated},
                         {p.id for p in self.settings.profiles} | {"edited"})

    def test_installations_have_separate_setup_state_and_index(self):
        settings = core.Settings(self.root / "cb2")
        settings.ensure()
        self.assertEqual(settings.index, self.root / "cb2/.indices")
        self.assertEqual(settings.path, self.root / "cb2/setup.ini")
        self.assertEqual(settings.state_dir, self.root / "cb2/.state")
        self.assertEqual(settings.default, settings.profiles[0].id)
        self.assertTrue(all(profile.images.parent != settings.root for profile in settings.profiles))
        self.assertEqual(settings.title, "Chaosbox-cb2")
        self.assertTrue(all(profile.index == settings.index for profile in settings.profiles))
        self.assertNotEqual(settings.index, self.settings.index)

    def test_mp4_metadata_preserves_streams(self):
        video = self.root / "source.mp4"
        core.run_tool("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=80x60:rate=5", "-t", "0.4",
                      "-c:v", "mpeg4", "-metadata", "title=Keep title", str(video))
        def hashes(path):
            return core.run_tool("ffmpeg", "-v", "error", "-i", str(path), "-map", "0", "-c", "copy", "-f", "streamhash", "-")
        before = hashes(video)
        saved = core.save_media(video, self.profile, self.record, 60)
        self.assertEqual(hashes(saved), before)
        self.assertEqual(core.read_metadata(saved)["comment"], self.record["comment"])
        self.assertEqual(core.run_tool("exiftool", "-s3", "-Title", str(saved)).decode().strip(), "Keep title")
        entries = core.build_index(self.profile)
        self.assertEqual(len(core.search(entries, core.compile_query({"comment": "東京"}))), 1)
        self.assertEqual(core.related_media(entries[0], entries), saved)
        self.assertIsNotNone(core.load_preview(saved))

    def test_install_preserves_data_and_excludes_credentials(self):
        home = self.root / "home"
        target, setup, launcher = install.install(home, credentials=False)
        self.assertTrue((home / ".local/share/chaosbox/desktop/app.py").is_file())
        self.assertTrue(launcher.is_file())
        settings = core.Settings(home / "ChaosBox")
        settings.reload()
        settings.remember_category(settings.profile("Chaosbox"), "Keep this")
        asset = next(path for path in settings.profiles[0].data.rglob("*") if path.is_file())
        asset.write_text("User content")
        setup_before = setup.read_bytes()
        shared_app = home / ".local/share/chaosbox/desktop/app.py"
        shared_app.write_text("old version")
        self.assertEqual(install.install(home, credentials=False), (target, setup, launcher))
        self.assertEqual(asset.read_text(), "User content")
        self.assertEqual(setup.read_bytes(), setup_before)
        self.assertEqual(shared_app.read_bytes(), Path(install.__file__).with_name("app.py").read_bytes())
        settings.reload()
        self.assertIn("Keep this", settings.profile(settings.default).categories)
        self.assertFalse((home / ".config/chaosbox/credentials/android_copy").exists())
        self.assertIn("Exec=\"", launcher.read_text())

    def test_installer_populates_only_first_box_from_assets(self):
        home = self.root / "seeded-home"
        _, setup, _ = install.install(home, credentials=False)
        settings = core.Settings(setup.parent)
        settings.reload()
        assets = Path(install.__file__).resolve().parent / "assets"
        self.assertGreater(len(settings.profiles), 1)
        for profile in settings.profiles:
            for name, destination in (("JPG", profile.images), ("TXT", profile.data)):
                if profile is not settings.profiles[0]:
                    self.assertEqual(list(destination.iterdir()), [])
                    continue
                source = assets / name
                files = [path for path in source.rglob("*") if path.is_file()]
                self.assertTrue(files)
                for path in files:
                    with self.subTest(profile=profile.id, asset=path.relative_to(assets)):
                        self.assertEqual((destination / path.relative_to(source)).read_bytes(),
                                         path.read_bytes())
        self.assertFalse((settings.root / "JPG").exists())
        self.assertFalse((settings.root / "TXT").exists())

    def test_installer_creates_default_box_when_setup_has_no_boxes(self):
        template = self.root / "no-boxes.ini"
        template.write_text("[App]\nKategorie=Example\nImageWidth=2048\n")
        home = self.root / "default-box-home"
        with patch.object(core, "DEFAULT_SETUP", template):
            _, setup, _ = install.install(home, credentials=False)
        settings = core.Settings(setup.parent)
        settings.reload()
        self.assertEqual([profile.id for profile in settings.profiles], ["ChaosBox"])
        self.assertEqual(settings.image_width, 2048)
        self.assertEqual(settings.profiles[0].categories, ["Example"])
        self.assertTrue(list(settings.profiles[0].images.iterdir()))
        self.assertTrue(list(settings.profiles[0].data.iterdir()))
        self.assertIn("[SSH]", setup.read_text())
        self.assertFalse((settings.root / "JPG").exists())

    def test_installer_accepts_existing_empty_directory(self):
        home = self.root / "existing-home"
        destination = home / "existing-box"
        destination.mkdir(parents=True)
        _, setup, desktop = install.install(home, credentials=False, installdir=destination)
        self.assertTrue(setup.is_file())
        self.assertTrue(desktop.is_file())

    def test_installer_rejects_file_as_installation_directory(self):
        home = self.root / "file-home"
        home.mkdir()
        destination = home / "box"
        destination.write_text("Keep")
        with self.assertRaises(FileExistsError):
            install.install(home, credentials=False, installdir=destination)
        self.assertEqual(destination.read_text(), "Keep")
        self.assertFalse((home / ".local").exists())

    def test_installer_creates_independent_apps_and_no_root_media_folders(self):
        home = self.root / "desktop-home"
        first_dir, second_dir = home / "one/cb2", home / "two space/cb2"
        first = install.install(home, credentials=False, installdir=first_dir)
        first_setup = first[1].read_bytes()
        second = install.install(home, credentials=False, installdir=second_dir)
        self.assertNotEqual(first[0], second[0])
        self.assertNotEqual(first[2], second[2])
        for target, setup, desktop in (first, second):
            settings = core.Settings(setup.parent)
            settings.reload()
            self.assertIn("Name=Chaosbox-cb2\n", desktop.read_text())
            self.assertIn(f"StartupWMClass={settings.window_class}", desktop.read_text())
            import shlex
            command = shlex.split((target / "run-desktop.sh").read_text().splitlines()[-1])
            self.assertEqual(command[2], str(home / ".local/share/chaosbox/desktop/app.py"))
            self.assertEqual(command[command.index("--installdir") + 1], str(setup.parent))
            self.assertFalse((setup.parent / "JPG").exists())
            self.assertFalse((setup.parent / "TXT").exists())
            self.assertTrue(all(profile.images.is_dir() and profile.data.is_dir() for profile in settings.profiles))
        self.assertEqual(install.install(home, credentials=False, installdir=first_dir), first)
        self.assertEqual(first[1].read_bytes(), first_setup)
        self.assertTrue(second[2].is_file())

    def test_shared_update_migrates_old_launchers_and_preserves_box_arguments(self):
        import shlex
        home = self.root / "home with spaces"
        first = install.install(home, credentials=False, installdir=home / "first ' box")
        shared = home / ".local/share/chaosbox/desktop/app.py"
        launcher = first[0] / "run-desktop.sh"
        old_app = first[0] / "desktop/app.py"
        core.atomic_write(old_app, 'raise RuntimeError("Obsolete runtime")\n')
        launcher.write_text(launcher.read_text().replace(shlex.quote(str(shared)), shlex.quote(str(old_app))))
        original_setup = first[1].read_bytes()
        original_menu = first[2].read_bytes()
        source_app = Path(install.__file__).resolve().with_name("app.py")
        read_bytes = Path.read_bytes
        for version in ("new version", "next version"):
            runtime = f'import json, sys\nprint(json.dumps([{version!r}, sys.argv[1:]]))\n'.encode()
            with patch.object(Path, "read_bytes", lambda path: runtime if path == source_app else read_bytes(path)):
                second = install.install(home, credentials=False, installdir=home / f"second box {version}")
            for target, setup, _ in (first, second):
                output = subprocess.check_output([str(target / "run-desktop.sh"), "--extra", "argument with spaces"], text=True)
                self.assertEqual(json.loads(output), [version, ["--installdir", str(setup.parent),
                                                               "--extra", "argument with spaces"]])
            self.assertEqual(first[1].read_bytes(), original_setup)
            self.assertEqual(first[2].read_bytes(), original_menu)

    def test_installation_is_not_a_box_or_indexed(self):
        core.atomic_write(self.root / "TXT/ignored.json", '[{"box":"ignored"}]')
        entries = core.ensure_index(self.settings)
        self.assertEqual(entries, [])
        self.assertEqual([p.id for p in self.settings.profiles], ["Chaosbox", "Bilderbox"])
        self.assertFalse((self.root / "JPG").exists())
        core.write_index(self.settings, [core.Entry(self.root / "TXT/ignored.json", {"box": "old"}, False, 0)])
        self.assertEqual(core.ensure_index(self.settings), [])
        with self.assertRaisesRegex(ValueError, "at least one"):
            self.settings.parse('[App]\nKategorie=Empty\n')

    def test_sftp_upload_is_manual_one_way_and_atomic(self):
        import paramiko
        local = self.profile.images / "nested/a.jpg"
        local.parent.mkdir()
        local.write_bytes(b"test upload")
        key, hosts = self.root / "key", self.root / "hosts"
        key.write_text("not a real key")
        hosts.write_text("not real hosts")
        self.settings.ssh.update(keyfile=str(key), knownhosts=str(hosts))
        remote, directory = {}, set()
        renamed = []
        class Sftp:
            def get_channel(self): return types.SimpleNamespace(settimeout=lambda value: None)
            def stat(self, path):
                if path in directory: return types.SimpleNamespace(st_mode=0o40755, st_mtime=0)
                if path in remote: return types.SimpleNamespace(st_mode=0o100644, st_mtime=remote[path][1])
                raise FileNotFoundError(errno.ENOENT, "missing")
            def mkdir(self, path): directory.add(path)
            def put(self, source, destination, callback):
                callback(4, 4)
                remote[destination] = (Path(source).read_bytes(), 0)
            def utime(self, path, times): remote[path] = (remote[path][0], times[1])
            def posix_rename(self, source, destination):
                renamed.append((source, destination))
                remote[destination] = remote.pop(source)
        class Client:
            def load_host_keys(self, path): pass
            def set_missing_host_key_policy(self, policy):
                assert isinstance(policy, paramiko.RejectPolicy)
            def connect(self, host, **kwargs):
                assert kwargs["allow_agent"] is False and kwargs["look_for_keys"] is False
            def open_sftp(self): return Sftp()
            def close(self): pass
        with patch.object(paramiko, "SSHClient", Client):
            core.upload(self.settings, self.profile, threading.Event())
            self.assertEqual(len(renamed), 1)
            self.assertTrue(renamed[0][0].endswith(".upload"))
            self.assertTrue(renamed[0][1].endswith("/nested/a.jpg"))
            core.upload(self.settings, self.profile, threading.Event())
            self.assertEqual(len(renamed), 1, "Unchanged remote must be skipped")
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(core.Cancelled):
                core.upload(self.settings, self.profile, cancelled)


if __name__ == "__main__":
    unittest.main()
