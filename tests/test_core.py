import errno
import json
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


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chaosbox-desktop-test-")
        self.root = Path(self.temp.name)
        self.settings = core.Settings(self.root, self.root / ".config/chaosbox")
        self.settings.ensure()
        self.profile = self.settings.profile("Chaosbox")
        self.profile.images.mkdir(parents=True)
        self.profile.data.mkdir(parents=True)
        self.record = core.validate_record(dict(zip(core.FIELDS, ["A11", "3", "Sensor", "Alias", "Elektronik", "Grüße 🎬\n東京", "Pack"])))

    def tearDown(self):
        self.temp.cleanup()

    def picture(self, name="source.jpg", size=(120, 80)):
        path = self.root / name
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
            self.settings.save_text(before.replace("Bilderbox/JPG", "ChaosBox/JPG"))
        self.assertEqual(self.settings.path.read_text(), unchanged)

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
        source = self.picture("ChaosBox/JPG/old/photo.jpg")
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
        source = self.picture("ChaosBox/JPG/old/photo.jpg")
        existing = self.picture("ChaosBox/JPG/elektronik/photo.jpg")
        before = existing.read_bytes()
        moved = core.save_media(source, self.profile, self.record, 3000)
        self.assertEqual(moved.name, "photo_1.jpg")
        self.assertFalse(source.exists())
        self.assertEqual(existing.read_bytes(), before)

    def test_category_move_rolls_back_if_source_cannot_be_removed(self):
        source = self.picture("ChaosBox/JPG/old/photo.jpg")
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
        first = self.picture("ChaosBox/JPG/one/same.jpg")
        second = self.picture("ChaosBox/JPG/two/same.jpg")
        box = self.profile.data / "a11.json"
        box.write_text('[{"device":"first"},{"device":"second"}]')
        with patch("core.read_metadata", return_value=core.normalized(self.record)):
            core.build_index(self.profile)
        imported = self.picture("ChaosBox/JPG/new/import_cb.jpg")
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

    def test_incremental_index_recovers_missing_or_broken_cache(self):
        path = self.picture("ChaosBox/JPG/sample.jpg")
        for content in (None, "invalid", '{"version":99,"entries":[]}', '{"version":1,"entries":[{}]}'):
            cache = self.profile.index / "desktop-index.json"
            if content is not None:
                core.atomic_write(cache, content)
            with patch("core.read_metadata", return_value=core.normalized(self.record)) as read:
                entries = core.update_index(self.profile, [path])
                read.assert_called_once_with(path)
                self.assertEqual(len(entries), 1)

    def test_failed_incremental_read_preserves_index_and_invalidates_cache(self):
        path = self.picture("ChaosBox/JPG/sample.jpg")
        with patch("core.read_metadata", return_value=core.normalized(self.record)):
            core.build_index(self.profile)
        index = self.profile.index / "records.json"
        before = index.read_bytes()
        with patch("core.read_metadata", side_effect=ValueError("unreadable")):
            with self.assertRaises(ValueError):
                core.update_index(self.profile, [path])
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse((self.profile.index / "desktop-index.json").exists())

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
        self.assertTrue((target / "desktop/app.py").is_file())
        self.assertTrue(launcher.is_file())
        settings = core.Settings(home, home / ".config/chaosbox")
        settings.reload()
        settings.remember_category(settings.profile("Chaosbox"), "Keep this")
        install.install(home, credentials=False)
        settings.reload()
        self.assertIn("Keep this", settings.profile("Chaosbox").categories)
        self.assertFalse((home / ".config/chaosbox/credentials/android_copy").exists())
        self.assertIn("Exec=\"", launcher.read_text())

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
