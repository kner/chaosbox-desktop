"""Exercise the real Tk UI against temporary data; no SSH connections or user data."""
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
import weakref
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from app import App, MediaGrid


def main():
    with tempfile.TemporaryDirectory(prefix="chaosbox-ui-test-") as temporary:
        home = Path(temporary)
        settings = core.Settings(home, home / ".config")
        settings.ensure()
        source = home / "source.jpg"
        Image.new("RGB", (320, 180), "teal").save(source)
        errors = []
        root = tk.Tk(className="Chaosbox")
        root.withdraw()
        root.report_callback_exception = lambda kind, error, traceback: errors.append(error)
        with patch("tkinter.messagebox.showerror", lambda *a, **k: errors.append(a)), \
             patch("tkinter.messagebox.showwarning", lambda *a, **k: errors.append(a)):
            app = App(root, settings)
            assert root.winfo_class() == "Chaosbox"
            assert app.window_icon.width() == 128
            def settle():
                deadline = time.monotonic() + 30
                quiet = None
                while time.monotonic() < deadline:
                    root.update()
                    if not app.busy and app.events.empty():
                        quiet = quiet or time.monotonic()
                        if time.monotonic() - quiet > .35:
                            assert not errors, errors
                            return
                    else:
                        quiet = None
                    time.sleep(.01)
                raise AssertionError("UI operation timed out")
            settle()
            # Thumbnail selection, responsive column changes, and damaged media.
            root.deiconify()
            root.update()
            gallery_paths = []
            for index in range(12):
                path = home / f"gallery-{index}.jpg"
                Image.new("RGB", (80, 60), "teal").save(path)
                gallery_paths.append(path)
            gallery_paths[-1].write_bytes(b"invalid image")
            app.media_dialog(gallery_paths)
            chooser = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
            grid = next(child for child in chooser.winfo_children() if isinstance(child, MediaGrid))
            root.update()
            grid.click(SimpleNamespace(x=20, y=20, state=0))
            grid.click(SimpleNamespace(x=grid.cell_width + 20, y=20, state=0))
            assert grid.selection() == gallery_paths[:2]
            grid.click(SimpleNamespace(x=3 * grid.cell_width + 20, y=20, state=1))
            assert grid.selection() == gallery_paths[:4]
            for columns in (1, 10, 4):
                grid.set_columns(columns)
                root.update()
                assert grid.columns == columns
                assert grid.thumb_size == int(grid.cell_width) - 16
                assert grid.selection() == gallery_paths[:4]
            grid.cache[0] = Image.new("RGB", (80, 60), "teal")
            grid.cache_sizes[0] = 80
            grid.set_columns(1)
            assert grid.photos[0].width() == grid.thumb_size > 320
            assert abs(grid.photos[0].height() / grid.photos[0].width() - .75) < .01
            grid.set_columns(4)
            grid.select_all()
            assert grid.selection() == gallery_paths
            grid.clear_selection()
            assert not grid.selection()
            grid.scroll("moveto", 1)
            deadline = time.monotonic() + 10
            while 11 not in grid.cache and time.monotonic() < deadline:
                root.update()
                time.sleep(.02)
            assert 11 in grid.cache and grid.cache[11] is None
            assert any(image is not None for image in grid.cache.values())
            grid.selected = {0, 1}
            grid.changed()
            with patch.object(app, "open_media") as opened:
                buttons = [widget for frame in chooser.winfo_children()
                           for widget in frame.winfo_children()
                           if widget.winfo_class() == "TButton"]
                next(button for button in buttons if button.cget("text") == "Open").invoke()
                root.update()
                opened.assert_called_once_with(gallery_paths[:2])
            settle()
            app.media_dialog([])
            root.update()
            chooser = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
            chooser.destroy()
            settle()
            # Closed dialogs must release Tk objects immediately on the UI
            # thread, before a worker's cyclic GC can finalize them instead.
            def close_gallery():
                app.media_dialog(gallery_paths)
                window = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
                gallery = next(child for child in window.winfo_children() if isinstance(child, MediaGrid))
                reference = weakref.ref(gallery)
                root.update()
                time.sleep(.08)
                root.update()
                window.destroy()
                return reference
            for _ in range(10):
                reference = close_gallery()
                assert reference() is None, "Closed gallery retains a Tk reference cycle"
                settle()
            app.open_media([source])
            settle()
            app.fill(dict(zip(core.FIELDS, ["A11", "2", "Camera", "Test alias", "Desktop test", "Grüße", "P1"])))
            app.save()
            settle()
            saved = app.media[0]
            assert saved != source and saved.is_file()
            assert "Desktop test" in app.profile.categories
            assert core.read_metadata(saved)["comment"] == "Grüße"
            app.search_clicked()
            app.fill({"comment": "Camera"})
            app.run_search()
            settle()
            assert not app.search_mode and app.media == [saved]
            app.clear()
            app.repeat_search()
            settle()
            assert app.media == [saved]
            app.clear()
            app.fill(dict(zip(core.FIELDS, ["NewBox", "4", "New device", "", "Desktop test", "JSON", ""])))
            app.save()
            settle()
            assert app.box_path.name == "newbox.json"
            assert core.load_box(app.box_path)[0]["anzahl"] == 4
            app.variables["anzahl"].set("7")
            app.save()
            settle()
            assert len(core.load_box(app.box_path)) == 1
            assert core.load_box(app.box_path)[0]["anzahl"] == 7
            app.search_clicked()
            app.fill({"comment": "temporary search"})
            app.cancel_search()
            assert app.variables["anzahl"].get() == "7"
            # Full-screen window and canvas zoom work without altering the source.
            app.open_media([saved])
            settle()
            app.fullscreen()
            settle()
            dialogs = [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
            assert len(dialogs) == 1
            viewer = next(child for child in dialogs[0].winfo_children() if isinstance(child, tk.Canvas))
            copy_button = next(widget for frame in dialogs[0].winfo_children()
                               for widget in frame.winfo_children()
                               if widget.winfo_class() == "TButton" and widget.cget("text") == "COPY")
            with patch("app.copy_image_clipboard") as copy_image:
                copy_button.invoke()
                settle()
                copy_image.assert_called_once_with(viewer.original)
                assert copy_button.cget("text") == "COPIED"
            viewer.scale(2)
            root.update()
            assert viewer.zoom == 2
            viewer.reset()
            assert viewer.zoom == 1
            app.fullscreen()
            assert len([child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]) == 1
            for _ in range(20):
                viewer.scale(1.2)
            # Closing while a redraw is pending must cancel that callback.
            dialogs[0].destroy()
            settle()
            app.fullscreen()
            settle()
            app.fullscreen_window.destroy()
            settle()
            app.profile_var.set("Bilderbox")
            app.select_profile()
            settle()
            assert app.profile.id == "Bilderbox" and not app.profile.labels[1]
            hidden_records = app.profile.data / "hidden.json"
            core.atomic_write(hidden_records, '[{"device":"first"},{"device":"second"}]')
            app.load_json(hidden_records)
            settle()
            chooser = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
            listing = next(child for child in chooser.winfo_children() if isinstance(child, tk.Listbox))
            listing.selection_clear(0, "end")
            listing.selection_set(1)
            next(child for child in chooser.winfo_children() if child.winfo_class() == "TButton").invoke()
            settle()
            assert app.record_index == 1 and app.variables["device"].get() == "second"
            app.close()
        print("PASS: Tk open/save, category addition, search/repeat, JSON editing, full-screen zoom, profiles")


if __name__ == "__main__":
    main()
