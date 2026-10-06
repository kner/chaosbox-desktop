"""Queue recovery tests that run without a graphical display."""
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import queue
import io
import sys
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, MediaGrid, MediaFolderDialog, copy_image_clipboard, user_comment_preview
import core
import app as desktop_app


class InstallationCliTests(unittest.TestCase):
    def test_default_and_explicit_installations(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            for arguments, expected in (([], home / "ChaosBox"),
                                        (["--installdir", str(home / "cb1"), "--newindex"], home / "cb1"),
                                        ([str(home / "cb2")], home / "cb2"),
                                        ([str(home / "cb2"), "--newindex"], home / "cb2")):
                with self.subTest(arguments=arguments), patch("sys.argv", ["app.py", *arguments]), \
                     patch("app.Path.home", return_value=home), patch("app.tk.Tk"), patch("app.App") as application:
                    desktop_app.main()
                    settings = application.call_args.args[1]
                    self.assertEqual(settings.root, expected)
                    self.assertEqual(settings.path, expected / "setup.ini")
                    self.assertEqual(settings.index, expected / ".indices")
                    self.assertEqual(settings.state_dir, expected / ".state")
                    self.assertEqual(settings.default, settings.profiles[0].id)
                    self.assertTrue(all(profile.images.parent != expected for profile in settings.profiles))
                    self.assertEqual(settings.title, f"Chaosbox-{expected.name}")
                    self.assertEqual(application.call_args.kwargs["newindex"], "--newindex" in arguments)


class SharedIndexTests(unittest.TestCase):
    def test_hit_path_is_relative_to_owning_box(self):
        app = App.__new__(App)
        app.settings = SimpleNamespace(profiles=[
            SimpleNamespace(images=Path("/boxes/First/JPG"), data=Path("/boxes/First/TXT"),
                            legacy_data=Path("/boxes/First/TXT")),
            SimpleNamespace(images=Path("/boxes/Second/JPG"), data=Path("/boxes/Second/TXT"),
                            legacy_data=Path("/boxes/Second/TXT"))])
        for source, expected in (("/boxes/Second/JPG/nested/photo.jpg", "JPG/nested/photo.jpg"),
                                 ("/boxes/First/TXT/box.json", "TXT/box.json")):
            self.assertEqual(app.search_hit_path(core.Entry(Path(source), {}, True)), expected)

    def test_switch_does_not_initialize_or_rebuild(self):
        app = App.__new__(App)
        app.busy = False
        app.settings = Mock(index=Path("/tmp/shared-index"))
        app.profile_var = Mock()
        app.profile_var.get.return_value = "Bilderbox"
        app.clear = Mock()
        app.save_state = Mock()
        app.initialize = Mock()
        app.status = Mock()
        app.select_profile()
        app.settings.profile.assert_called_once_with("Bilderbox")
        app.initialize.assert_not_called()

    def test_hit_selects_owning_profile(self):
        app = App.__new__(App)
        other = SimpleNamespace(id="Other", images=Path("/other/images"),
                                data=Path("/other/data"), legacy_data=Path("/other/data"))
        app.settings = Mock(profiles=[other])
        app.settings.profile.return_value = other
        app.save_state = Mock()
        app.refresh_profile = Mock()
        app.open_media = Mock()
        hit = core.Entry(Path("/other/images/test.jpg"), {}, True)
        app.open_hit(hit)
        self.assertIs(app.profile, other)
        app.open_media.assert_called_once_with([hit.source])


class BatchSaveConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.app = App.__new__(App)
        app = self.app
        app.busy = app.search_mode = False
        app.root = Mock()
        app.media = [Path("first.jpg"), Path("second.jpg")]
        app.media_records = [core.normalized({"box": "A", "comment": "first"}),
                             core.normalized({"box": "B", "comment": "second"})]
        app.media_baseline = core.common_metadata(app.media_records)
        app.values = Mock(return_value=dict(app.media_baseline, comment="additional text"))
        app.box_path = app.record_index = None
        app.profile = SimpleNamespace(images=Path("/tmp/test-images"))
        app.settings = SimpleNamespace(image_width=3000, remember_category=Mock())
        app.task = Mock()
        app.error = Mock()
        app.log = Mock()
        app.preview_result = Mock(return_value=(None, ""))

    def test_cancel_leaves_files_and_inputs_untouched(self):
        with patch("app.messagebox.askokcancel", return_value=False) as confirm, \
             patch("core.save_batch") as save:
            self.app.save()
        confirm.assert_called_once_with("Save", "Attention! All selected Images get this texts",
                                        parent=self.app.root, icon="warning", default="cancel")
        self.app.task.assert_not_called()
        self.app.settings.remember_category.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.app.values()["comment"], "additional text")
        self.app.error.assert_not_called()

    def test_ok_saves_appended_text_for_each_selected_image(self):
        self.app.task.side_effect = lambda work, done, failed: work()
        with patch("app.messagebox.askokcancel", return_value=True), \
             patch("core.save_batch", return_value=self.app.media) as save, \
             patch("core.read_metadata", return_value=core.normalized({})), \
             patch("core.update_index"):
            self.app.save()
        records = save.call_args.kwargs["records"]
        self.assertEqual([record["comment"] for record in records],
                         ["first\nadditional text", "second\nadditional text"])
        self.assertEqual([record["box"] for record in records], ["A", "B"])
        self.app.task.assert_called_once()
        self.app.error.assert_not_called()

    def test_successful_save_clears_form_for_media_and_json(self):
        self.app.profile.id = "Test"
        self.app.settings.profile = Mock(return_value=self.app.profile)
        self.app.clear = Mock()
        self.app.status = Mock()
        for kind in ("media", "json"):
            with self.subTest(kind=kind), patch("app.messagebox.askokcancel", return_value=True):
                self.app.clear.reset_mock()
                self.app.save()
                done = self.app.task.call_args.args[1]
                done(((kind,), ""))
                self.app.clear.assert_called_once_with()
                self.app.status.set.assert_called_with(
                    "Saved locally. Use Upload to synchronize manually.")

    def test_single_image_does_not_ask_for_batch_confirmation(self):
        self.app.media = self.app.media[:1]
        with patch("app.messagebox.askokcancel") as confirm:
            self.app.save()
        confirm.assert_not_called()
        self.app.task.assert_called_once()


class UserCommentTests(unittest.TestCase):
    def test_caption_shows_fields_in_order_and_limits_unicode_to_60_characters(self):
        raw = json.dumps({"box": "A11", "category": "Elektronik", "comment": "Grüße 🎬 " + "ä" * 70,
                          "device": "Not displayed"}, ensure_ascii=False)
        with patch("core.run_tool", return_value=raw.encode("utf-8")):
            caption = user_comment_preview(Path("photo.jpg"))
            self.assertEqual(caption, ("A11 | Elektronik | Grüße 🎬 " + "ä" * 70)[:60])
            self.assertEqual(len(caption), 60)

    def test_empty_and_multiline_comments(self):
        for raw, expected in (("", ""), ("First\nsecond\tthird", " |  | First second third"),
                              ('{"box":"A11","category":"Tools"}', "A11 | Tools | "),
                              ('{"box":"A11","comment":"Note"}', "A11 |  | Note")):
            with self.subTest(raw=raw), patch("core.read_user_comment", return_value=raw):
                self.assertEqual(user_comment_preview(Path("photo.jpg")), expected)

    def test_failed_comment_load_keeps_selection_and_polling(self):
        grid = MediaGrid.__new__(MediaGrid)
        grid.comment_future = Future()
        grid.comment_future.set_exception(ValueError("Unreadable metadata"))
        grid.comment_loading = 0
        grid.comments = {}
        grid.show_user_comment = True
        grid.future = None
        grid.visible = []
        grid.selected = {0}
        grid.redraw = Mock()
        grid.after = Mock()
        grid.poll()
        self.assertEqual(grid.comments, {0: ""})
        self.assertEqual(grid.selected, {0})
        grid.after.assert_called_once_with(60, grid.poll)


class MediaFolderTests(unittest.TestCase):
    def test_thumbnail_double_click_opens_only_clicked_file(self):
        grid = MediaGrid.__new__(MediaGrid)
        grid.paths = [Path("first.jpg"), Path("second.jpg"), Path("third.jpg")]
        grid.selected = {0: None, 2: None}
        grid.columns = 2
        grid.cell_width = grid.cell_height = 100
        grid.canvas = Mock()
        grid.canvas.canvasy.side_effect = lambda y: y
        grid.changed = Mock()
        confirm = Mock()
        self.assertEqual(grid.double_click(SimpleNamespace(x=150, y=50), confirm), "break")
        self.assertEqual(grid.selection(), [Path("second.jpg")])
        confirm.assert_called_once_with()
        confirm.reset_mock()
        grid.double_click(SimpleNamespace(x=150, y=150), confirm)
        confirm.assert_not_called()

    def test_double_click_confirms_clicked_folder(self):
        dialog = MediaFolderDialog.__new__(MediaFolderDialog)
        dialog.listing = Mock()
        dialog.listing.nearest.return_value = 1
        dialog.listing.bbox.return_value = (0, 20, 100, 20)
        dialog.directories = [Path("first"), Path("second")]
        dialog.listing.curselection.return_value = (1,)
        dialog.ok = Mock(side_effect=dialog.apply)
        self.assertEqual(dialog.double_click(SimpleNamespace(y=25)), "break")
        dialog.listing.selection_clear.assert_called_once_with(0, "end")
        dialog.listing.selection_set.assert_called_once_with(1)
        dialog.ok.assert_called_once()
        self.assertEqual(dialog.result, Path("second"))

    def test_double_click_on_empty_space_does_not_confirm(self):
        dialog = MediaFolderDialog.__new__(MediaFolderDialog)
        dialog.listing = Mock()
        dialog.listing.nearest.return_value = 0
        dialog.ok = Mock()
        for bounds in (None, (0, 0, 100, 20)):
            dialog.listing.bbox.return_value = bounds
            self.assertEqual(dialog.double_click(SimpleNamespace(y=80)), "break")
        dialog.ok.assert_not_called()

    def test_folder_selection_is_shallow_and_persisted_per_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "chosen"
            nested = folder / "nested"
            nested.mkdir(parents=True)
            direct = folder / "photo.JPG"
            direct.touch()
            hidden = nested / "hidden.jpg"
            hidden.touch()
            app = App.__new__(App)
            app.profile = SimpleNamespace(id="first", images=root)
            app.state_file = root / "state.json"
            app.media_folders = {}
            app.error = Mock()
            app.remember_media_folder(folder)
            app.media_folders = json.loads(app.state_file.read_text())["media_folders"]
            app.busy = app.search_mode = False
            app.media_dialog = Mock()
            app.task = lambda work, done: done(work())
            app.choose_media()
            app.media_dialog.assert_called_once_with([direct], folder)
            self.assertEqual(core.files(folder, core.MEDIA), [hidden, direct])
            app.profile = SimpleNamespace(id="second", images=root)
            self.assertEqual(app.media_folder(), root)
            app.save_state()
            self.assertEqual(json.loads(app.state_file.read_text())["media_folders"]["first"], str(folder))
            app.profile = SimpleNamespace(id="first", images=root)
            hidden.unlink()
            nested.rmdir()
            direct.unlink()
            folder.rmdir()
            self.assertEqual(app.media_folder(), root)
            app.error.assert_not_called()


class ClipboardTests(unittest.TestCase):
    def test_copy_publishes_png_pixels(self):
        image = Image.new("RGB", (17, 11), "teal")
        with patch("app.shutil.which", return_value="/usr/bin/xclip"), patch("app.subprocess.run") as run:
            copy_image_clipboard(image)
        self.assertIn("image/png", run.call_args.args[0])
        pasted = Image.open(io.BytesIO(run.call_args.kwargs["input"]))
        self.assertEqual(pasted.size, image.size)
        self.assertEqual(pasted.tobytes(), image.tobytes())

    def test_missing_clipboard_helper_is_reported(self):
        with patch("app.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "sudo apt install xclip"):
                copy_image_clipboard(Image.new("RGB", (1, 1)))


class QueueRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.app = App.__new__(App)
        self.app.root = Mock()
        self.app.status = Mock()
        self.app.progress = Mock()
        self.app.update_controls = Mock()
        self.app.events = queue.Queue()
        self.app.busy = True
        self.app.closing = False
        self.app.uploading = False
        self.app.upload_text = None
        self.app.executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.app.executor.shutdown)

    def test_callback_failure_does_not_strand_next_file_operation(self):
        app = self.app
        finished = Mock()

        def broken_callback(result):
            app.task(lambda: "second file", finished)
            raise RuntimeError("viewer could not be opened")

        app.events.put(("done", None, broken_callback))
        app.drain()
        # Wait for the worker, then simulate the next scheduled Tk poll.
        app.executor.submit(lambda: None).result(timeout=5)
        app.drain()
        finished.assert_called_once_with("second file")
        self.assertFalse(app.busy)
        app.root.report_callback_exception.assert_called_once()
        self.assertEqual(app.root.after.call_count, 2)
        app.close()
        app.root.destroy.assert_called_once()

    def test_failed_error_handler_keeps_polling(self):
        handler = Mock(side_effect=RuntimeError("dialog closed"))
        self.app.events.put(("error", ValueError("bad image"), handler))
        self.app.drain()
        self.assertFalse(self.app.busy)
        self.app.root.after.assert_called_once_with(80, self.app.drain)

    def test_progress_error_does_not_hide_completion(self):
        self.app.status.set.side_effect = [RuntimeError("status error"), None]
        done = Mock()
        self.app.events.put(("progress", "loading"))
        self.app.events.put(("done", None, done))
        self.app.drain()
        done.assert_called_once_with(None)
        self.assertFalse(self.app.busy)

    def test_existing_viewer_is_reused(self):
        self.app.fullscreen_window = Mock()
        self.app.fullscreen_window.winfo_exists.return_value = True
        self.app.fullscreen()
        self.app.fullscreen_window.lift.assert_called_once()
        self.assertTrue(self.app.events.empty())

    def test_closing_during_write_is_still_blocked(self):
        self.app.close()
        self.app.root.destroy.assert_not_called()
        self.assertFalse(self.app.closing)


if __name__ == "__main__":
    unittest.main()
