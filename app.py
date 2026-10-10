#!/usr/bin/env python3
"""ChaosBox desktop editor for Ubuntu. All Tk operations run on the UI thread."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from PIL import Image, ImageTk

import core
import markdown_render


class CommentEditor(ttk.Notebook):
    """Keep editable Markdown separate from its read-only native preview."""

    def __init__(self, parent):
        super().__init__(parent)
        self.pending = None
        edit = ttk.Frame(self)
        preview = ttk.Frame(self)
        self.source = tk.Text(edit, height=13, wrap="word", font=("Sans", 11), undo=True, padx=8, pady=8)
        self.preview = tk.Text(preview, height=13, wrap="word", font=("DejaVu Sans", 11),
                               padx=8, pady=8, state="disabled", background="#ffffff")
        for frame, widget in ((edit, self.source), (preview, self.preview)):
            scroll = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
            scroll.pack(side="right", fill="y")
            widget.pack(side="left", fill="both", expand=True)
            widget.configure(yscrollcommand=scroll.set)
        self.add(edit, text="Markdown")
        self.add(preview, text="Preview")
        self.source.bind("<<Modified>>", self.changed)
        self.source.edit_modified(False)
        self.bind("<<NotebookTabChanged>>", self.refresh)
        self.bind("<Destroy>", self.cleanup)

    def changed(self, event=None):
        if not self.source.edit_modified():
            return
        self.source.edit_modified(False)
        if self.pending is not None:
            self.after_cancel(self.pending)
        self.pending = self.after(150, self.refresh)

    def refresh(self, event=None):
        if self.pending is not None:
            self.after_cancel(self.pending)
            self.pending = None
        if self.index(self.select()) == 1:
            markdown_render.render_preview(self.preview, self.source.get("1.0", "end-1c"))

    def cleanup(self, event):
        if event.widget is self and self.pending is not None:
            self.after_cancel(self.pending)
            self.pending = None


USER_COMMENT_PREVIEW_LIMIT = 60


def user_comment_preview(path):
    # Keep captions on a predictable number of lines without changing metadata.
    metadata = core.parse_metadata(core.read_user_comment(path))
    fields = [" ".join(str(metadata.get(key) or "").split())
              for key in ("box", "category", "comment")]
    return " | ".join(fields)[:USER_COMMENT_PREVIEW_LIMIT] if any(fields) else ""


class ImageCanvas(tk.Canvas):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, background="#101820", highlightthickness=0, **kwargs)
        self.original = None
        self.photo = None
        self.pending = None
        self.navigate_previous = None
        self.navigate_next = None
        self.image_bounds = None
        self.bind("<Configure>", self.resize)
        self.bind("<Button-1>", self.navigation_click)
        self.bind("<Motion>", self.navigation_motion)

    def set_navigation(self, previous, next_):
        self.navigate_previous, self.navigate_next = previous, next_

    def set_image(self, image):
        self.original = image
        self.resize()

    def fit(self):
        if self.original is None:
            return 1.
        return min(max(1, self.winfo_width()) / self.original.width,
                   max(1, self.winfo_height()) / self.original.height)

    def resize(self, event=None):
        self.schedule()

    def schedule(self):
        if self.pending:
            self.after_cancel(self.pending)
        self.pending = self.after(16, self.draw)

    def destroy(self):
        if self.pending is not None:
            self.after_cancel(self.pending)
            self.pending = None
        super().destroy()

    def draw(self):
        self.pending = None
        self.delete("all")
        self.image_bounds = None
        if self.original is None:
            self.create_text(max(1, self.winfo_width()) / 2, max(1, self.winfo_height()) / 2,
                             text="Select an image or video", fill="#cbd5e1", font=("Sans", 15))
            return
        scale = self.fit()
        if scale <= 0:
            return
        vw, vh = max(1, self.winfo_width()), max(1, self.winfo_height())
        width, height = max(1, round(self.original.width * scale)), max(1, round(self.original.height * scale))
        rendered = self.original.resize((width, height), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(rendered)
        self.create_image((vw - width) / 2, (vh - height) / 2, anchor="nw", image=self.photo)
        left, top = (vw - width) / 2, (vh - height) / 2
        right, bottom = left + width, top + height
        self.image_bounds = (left, top, right, bottom)
        center_y = (top + bottom) / 2
        offset = min(42, width * .2)
        self.create_text(left + offset + 2, center_y + 2, text="≪", fill="#101820", font=("Sans", 30, "bold"))
        self.create_text(left + offset, center_y, text="≪", fill="white", font=("Sans", 30, "bold"))
        self.create_text(right - offset + 2, center_y + 2, text="≫", fill="#101820", font=("Sans", 30, "bold"))
        self.create_text(right - offset, center_y, text="≫", fill="white", font=("Sans", 30, "bold"))

    def is_navigation_click(self, event):
        if self.image_bounds is None:
            return 0
        left, top, right, bottom = self.image_bounds
        if not top <= event.y <= bottom:
            return 0
        zone = min(76, (right - left) / 2)
        if event.x <= left + zone:
            return -1
        if event.x >= right - zone:
            return 1
        return 0

    def navigation_click(self, event):
        if self.original is None:
            return
        direction = self.is_navigation_click(event)
        if direction < 0 and self.navigate_previous is not None:
            self.navigate_previous()
            return "break"
        if direction > 0 and self.navigate_next is not None:
            self.navigate_next()
            return "break"

    def navigation_motion(self, event):
        self.configure(cursor="hand2" if self.is_navigation_click(event) else "")


class MetadataModeDialog(simpledialog.Dialog):
    def __init__(self, parent, labels):
        self.labels = labels
        super().__init__(parent, title="Mehrfachauswahl")

    def body(self, master):
        ttk.Label(master, text="Achtung, bestehende Inhalte in folgenden Feldern sind unterschiedlich:\n\n"
                  + ", ".join(self.labels) + "\n\nSollen bestehende Inhalte gelöscht oder erweitert werden?",
                  wraplength=520, justify="left").pack(padx=12, pady=12)

    def buttonbox(self):
        buttons = ttk.Frame(self)
        buttons.pack(padx=12, pady=12)
        def choose(mode):
            self.result = mode
            self.cancel()
        ttk.Button(buttons, text="Erweitert – neue Inhalte anhängen",
                   command=lambda: choose("extend")).pack(side="left", padx=4)
        ttk.Button(buttons, text="Gelöscht – durch neue Eingaben ersetzen",
                   command=lambda: choose("replace")).pack(side="left", padx=4)
        ttk.Button(buttons, text="Abbrechen", command=self.cancel).pack(side="left", padx=4)
        self.bind("<Return>", lambda event: choose("extend"))
        self.bind("<Escape>", self.cancel)


class MediaFolderDialog(simpledialog.Dialog):
    """Folder picker where a double-click confirms the folder immediately."""

    def __init__(self, parent, folder):
        self.folder = Path(folder)
        self.directories = []
        super().__init__(parent, title="Select media folder")

    def body(self, master):
        self.location = tk.StringVar(value=str(self.folder))
        navigation = ttk.Frame(master)
        navigation.pack(fill="x")
        ttk.Button(navigation, text="Up", command=lambda: self.browse(self.folder.parent)).pack(side="left")
        entry = ttk.Entry(navigation, textvariable=self.location, width=65)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        def enter_path(event):
            self.browse(Path(self.location.get()).expanduser())
            return "break"
        entry.bind("<Return>", enter_path)
        ttk.Label(master, text="Double-click a folder to select it and open its images.").pack(anchor="w", pady=8)
        frame = ttk.Frame(master)
        frame.pack(fill="both", expand=True)
        self.listing = tk.Listbox(frame, height=18, exportselection=False)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.listing.yview)
        self.listing.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.listing.pack(side="left", fill="both", expand=True)
        self.listing.bind("<Double-Button-1>", self.double_click)
        self.browse(self.folder)
        return self.listing

    def browse(self, folder):
        try:
            folder = folder.resolve()
            directories = sorted((path for path in folder.iterdir() if path.is_dir()),
                                 key=lambda path: path.name.casefold())
        except OSError as error:
            messagebox.showerror("Select media folder", str(error), parent=self)
            return
        self.folder, self.directories = folder, directories
        self.location.set(str(folder))
        self.listing.delete(0, "end")
        for path in directories:
            self.listing.insert("end", path.name)

    def selected_folder(self):
        selected = self.listing.curselection()
        return self.directories[selected[0]] if selected else self.folder

    def double_click(self, event):
        index = self.listing.nearest(event.y)
        bounds = self.listing.bbox(index)
        if bounds and bounds[1] <= event.y < bounds[1] + bounds[3]:
            self.listing.selection_clear(0, "end")
            self.listing.selection_set(index)
            self.ok()
        return "break"

    def validate(self):
        if not self.selected_folder().is_dir():
            messagebox.showerror("Select media folder", "This folder is no longer available.", parent=self)
            return False
        return True

    def apply(self):
        self.result = self.selected_folder()


class MediaGrid(ttk.Frame):
    """Scrollable thumbnail chooser; only visible tiles are rendered and loaded."""

    def __init__(self, parent, paths, base, changed):
        super().__init__(parent)
        self.paths, self.base, self.changed = list(paths), base, changed
        self.selected = {}
        self.anchor = None
        self.columns = 4
        self.cache = OrderedDict()
        self.cache_sizes = {}
        self.show_user_comment = False
        self.comments = {}
        self.comment_future = None
        self.photos = []
        self.photo_cache = {}
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="thumbnails")
        self.comment_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="thumbnail-comments")
        self.future = None
        self.pending = None
        self.canvas = tk.Canvas(self, background="#f1f5f9", highlightthickness=0,
                                takefocus=True)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.scroll)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<Button-1>", self.click)
        self.canvas.bind("<Button-4>", lambda e: self.scroll("scroll", -1, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.scroll("scroll", 1, "units"))
        self.canvas.bind("<MouseWheel>", lambda e: self.scroll("scroll", -1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Control-a>", lambda e: self.select_all())
        self.pending = self.after(60, self.poll)

    def set_columns(self, value):
        self.columns = max(1, min(10, int(value)))
        self.redraw()

    def set_show_user_comment(self, value):
        self.show_user_comment = bool(value)
        self.redraw()

    def scroll(self, *args):
        self.canvas.yview(*args)
        self.redraw()

    def selection(self):
        return [self.paths[index] for index in self.selected]

    def select_all(self):
        self.selected.update(dict.fromkeys(range(len(self.paths))))
        self.changed()
        self.redraw()
        return "break"

    def clear_selection(self):
        self.selected.clear()
        self.anchor = None
        self.changed()
        self.redraw()

    def click(self, event):
        self.canvas.focus_set()
        column = min(self.columns - 1, int(event.x // self.cell_width))
        index = int(self.canvas.canvasy(event.y) // self.cell_height) * self.columns + column
        if not 0 <= index < len(self.paths):
            return
        if event.state & 1 and self.anchor is not None:
            self.selected.update(dict.fromkeys(range(self.anchor, index + (1 if index >= self.anchor else -1),
                                                     1 if index >= self.anchor else -1)))
        else:
            if index in self.selected:
                self.selected.pop(index)
            else:
                self.selected[index] = None
            self.anchor = index
        self.changed()
        self.redraw()

    def double_click(self, event, confirm):
        column = int(event.x // self.cell_width)
        row = int(self.canvas.canvasy(event.y) // self.cell_height)
        index = row * self.columns + column
        if 0 <= column < self.columns and row >= 0 and 0 <= index < len(self.paths):
            self.selected = {index: None}
            self.anchor = index
            self.changed()
            confirm()
        return "break"

    def redraw(self):
        canvas = self.canvas
        width = max(1, canvas.winfo_width())
        self.cell_width = width / self.columns
        self.thumb_size = max(24, int(self.cell_width) - 16)
        self.cell_height = self.thumb_size + 66
        if self.show_user_comment:
            chars_per_line = max(1, int((self.cell_width - 14) / 10))
            self.cell_height += ((USER_COMMENT_PREVIEW_LIMIT + chars_per_line - 1) // chars_per_line) * 18 + 8
        rows = (len(self.paths) + self.columns - 1) // self.columns
        canvas.configure(scrollregion=(0, 0, width, rows * self.cell_height),
                         yscrollincrement=max(20, self.cell_height // 3))
        canvas.delete("all")
        self.photos.clear()
        top = max(0, int(canvas.canvasy(0) // self.cell_height))
        bottom = int((canvas.canvasy(0) + canvas.winfo_height()) // self.cell_height) + 1
        self.visible = list(range(top * self.columns, min(len(self.paths), bottom * self.columns)))
        self.photo_cache = {index: cached for index, cached in self.photo_cache.items() if index in self.visible}
        if not self.paths:
            canvas.create_text(width / 2, 60, text="No media found. Use Other files … to import images.",
                               width=max(100, width - 40), fill="#475569")
        for index in self.visible:
            path = self.paths[index]
            x = (index % self.columns) * self.cell_width
            y = (index // self.columns) * self.cell_height
            selected = index in self.selected
            canvas.create_rectangle(x + 3, y + 3, x + self.cell_width - 3, y + self.cell_height - 3,
                                    fill="#dbeafe" if selected else "white",
                                    outline="#2563eb" if selected else "#cbd5e1", width=2 if selected else 1)
            if index in self.cache:
                image = self.cache[index]
                self.cache.move_to_end(index)
                if image is not None:
                    cached = self.photo_cache.get(index)
                    if cached is not None and cached[0] is image and cached[1] == self.thumb_size:
                        photo = cached[2]
                    else:
                        scale = self.thumb_size / max(image.size)
                        thumbnail = image.resize((max(1, round(image.width * scale)),
                                                  max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
                        photo = ImageTk.PhotoImage(thumbnail, master=canvas)
                        self.photo_cache[index] = (image, self.thumb_size, photo)
                    self.photos.append(photo)
                    canvas.create_image(x + self.cell_width / 2, y + 8 + self.thumb_size / 2, image=photo)
                else:
                    canvas.create_text(x + self.cell_width / 2, y + self.thumb_size / 2,
                                       text="Preview unavailable", width=self.thumb_size, fill="#64748b")
            else:
                canvas.create_text(x + self.cell_width / 2, y + self.thumb_size / 2,
                                   text="Loading…", width=self.thumb_size, fill="#64748b")
            label = str(path.relative_to(self.base)) if path.is_relative_to(self.base) else path.name
            max_chars = max(6, int((self.cell_width - 14) / 8) * 2)
            if len(label) > max_chars:
                label = label[:max_chars - 1] + "…"
            canvas.create_text(x + self.cell_width / 2, y + self.thumb_size + 12,
                               anchor="n", text=label, width=max(12, self.cell_width - 14),
                               font=("Sans", 9), fill="#0f172a")
            if self.show_user_comment:
                canvas.create_text(x + self.cell_width / 2, y + self.thumb_size + 62,
                                   anchor="n", text=self.comments.get(index, "Loading…"),
                                   width=max(12, self.cell_width - 14),
                                   font=("Sans", 9), fill="#475569")
            canvas.create_text(x + 9, y + 9, anchor="nw", text="☑" if selected else "☐",
                               fill="#1d4ed8", font=("Sans", 14, "bold"))

    def poll(self):
        self.pending = None
        if self.comment_future is not None and self.comment_future.done():
            try:
                self.comments[self.comment_loading] = self.comment_future.result()
            except Exception:
                self.comments[self.comment_loading] = ""
            self.comment_future = None
            if self.show_user_comment:
                self.redraw()
        if self.future is not None and self.future.done():
            index, future = self.loading, self.future
            self.future = None
            try:
                self.cache[index] = future.result()
            except Exception:
                self.cache[index] = None
            self.cache_sizes[index] = self.loading_size
            # Bound decoded image memory as well as the number of small tiles.
            pixels = sum(image.width * image.height for image in self.cache.values() if image is not None)
            while len(self.cache) > 200 or (pixels > 16_000_000 and len(self.cache) > 1):
                removed, image = self.cache.popitem(last=False)
                self.cache_sizes.pop(removed, None)
                if image is not None:
                    pixels -= image.width * image.height
            self.redraw()
        if self.future is None:
            for index in getattr(self, "visible", []):
                if index not in self.cache or (self.cache[index] is not None
                                              and self.cache_sizes[index] < self.thumb_size):
                    self.loading = index
                    self.loading_size = self.thumb_size
                    self.future = self.worker.submit(core.load_preview, self.paths[index],
                                                     (self.thumb_size, self.thumb_size))
                    break
        if self.show_user_comment and self.comment_future is None:
            for index in getattr(self, "visible", []):
                if index not in self.comments:
                    self.comment_loading = index
                    self.comment_future = self.comment_worker.submit(user_comment_preview, self.paths[index])
                    break
        self.pending = self.after(60, self.poll)

    def destroy(self):
        if self.pending is not None:
            self.after_cancel(self.pending)
            self.pending = None
        # changed closes over this grid and Tk variables. Break that cycle on
        # the UI thread: otherwise a later worker allocation can collect it and
        # run Tk destructors there (potentially while holding executor locks).
        self.changed = None
        self.photos.clear()
        self.photo_cache.clear()
        self.cache.clear()
        self.cache_sizes.clear()
        self.comments.clear()
        self.worker.shutdown(wait=False, cancel_futures=True)
        self.comment_worker.shutdown(wait=False, cancel_futures=True)
        self.future = None
        self.comment_future = None
        super().destroy()
        # Canvas.master points back to this frame even after Tk destruction.
        self.canvas = None


class App:
    FORM_FIELDS = ("box", "anzahl", "device", "alias", "category", "package", "comment")

    def __init__(self, root, settings, newindex=False):
        self.root, self.settings = root, settings
        self.newindex = newindex
        self.window_icon = tk.PhotoImage(file=str(Path(__file__).with_name("chaosbox.png")))
        root.iconphoto(True, self.window_icon)
        root.title(settings.title)
        root.geometry("1240x850")
        root.minsize(850, 620)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chaosbox")
        self.events = queue.Queue()
        self.busy, self.closing = False, False
        self.media, self.box_path, self.records, self.record_index = [], None, [], None
        self.created = ""
        self.preview_path = None
        self.search_mode, self.before_search, self.last_search = False, None, None
        self.cancel_upload = threading.Event()
        self.uploading = False
        self.upload_window, self.upload_text = None, None
        self.controls, self.field_widgets, self.rows = [], {}, []
        self.variables = {key: tk.StringVar() for key in core.FIELDS if key != "comment"}
        self.variables["category"].trace_add("write", self.uppercase_category)
        self.state_file = settings.state_dir / "desktop-state.json"
        try:
            state = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            state = {}
        self.media_folders = state.get("media_folders", {})
        self.profile = settings.profile(settings.default)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TButton", padding=(10, 7))
        style.configure("Title.TLabel", font=("Sans", 18, "bold"))
        style.configure("TLabel", font=("Sans", 11))
        for widget_class in ("TEntry", "TCombobox"):
            search_style = f"Search.{widget_class}"
            style.configure(search_style, fieldbackground="#eeedda")
            style.map(search_style, fieldbackground=[("disabled", "#eeedda"),
                                                     ("readonly", "#eeedda")])
        self.build_ui()
        self.refresh_profile()
        self.clear()
        self.root.bind("<Control-s>", lambda e: self.save())
        self.root.bind("<Control-o>", lambda e: self.choose_media())
        self.root.bind("<Control-f>", lambda e: self.search_clicked())
        for widget_class in ("Text", "Entry", "TEntry", "TCombobox", "Spinbox", "TSpinbox"):
            for shortcut in ("<Control-a>", "<Control-A>"):
                self.root.bind_class(widget_class, shortcut, self.select_all_text)
        self.root.bind("<Escape>", lambda e: self.cancel_search())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self.drain)
        self.root.after(150, self.initialize)

    def uppercase_category(self, *_):
        variable = self.variables["category"]
        value = variable.get()
        if value != value.upper():
            variable.set(value.upper())

    @staticmethod
    def select_all_text(event):
        widget = event.widget
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "end-1c")
        else:
            widget.selection_range(0, "end")
            widget.icursor("end")
        return "break"

    def button(self, parent, text, command, **pack):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(**pack)
        self.controls.append(button)
        return button

    @staticmethod
    def combobox_match(widget, prefix=False):
        query = widget.get().casefold()
        values = widget.cget("values")
        if not query:
            return
        index = next((i for i, value in enumerate(values) if str(value).casefold() == query), None)
        if index is None:
            index = next((i for i, value in enumerate(values)
                          if (str(value).casefold().startswith(query) if prefix else
                              query in str(value).casefold())), None)
        if index is None:
            return
        return index

    @staticmethod
    def find_combobox_entry(widget, prefix=False):
        index = App.combobox_match(widget, prefix)
        if index is None:
            return

        def highlight():
            if not widget.winfo_exists():
                return
            popdown = widget.tk.call("ttk::combobox::PopdownWindow", str(widget))
            listing = f"{popdown}.f.l"
            widget.tk.call(listing, "selection", "clear", 0, "end")
            widget.tk.call(listing, "selection", "set", index)
            widget.tk.call(listing, "activate", index)
            widget.tk.call(listing, "see", index)

        # Tk fills the dropdown after postcommand; select after that step.
        widget.after_idle(highlight)

    def next_field(self, key):
        position = self.FORM_FIELDS.index(key)
        for next_key in self.FORM_FIELDS[position + 1:]:
            widget = self.field_widgets[next_key]
            if next_key == "comment" and getattr(self, "comment_editor", None) is not None:
                if self.comment_editor.winfo_viewable() and str(widget.cget("state")) != "disabled":
                    self.comment_editor.select(0)
                    widget.focus_set()
                    return
            if widget.winfo_viewable() and str(widget.cget("state")) != "disabled":
                widget.focus_set()
                return
        self.field_widgets[key].tk_focusNext().focus_set()

    def field_enter(self, key):
        if key == "category":
            widget = self.field_widgets[key]
            index = self.combobox_match(widget, prefix=True)
            if index is not None:
                widget.current(index)
        self.next_field(key)
        return "break"

    def build_ui(self):
        header = ttk.Frame(self.root, padding=(18, 14))
        header.pack(fill="x")
        ttk.Label(header, text=self.settings.title, style="Title.TLabel").pack(side="left")
        self.profile_var = tk.StringVar(value=self.profile.id)
        self.profile_picker = ttk.Combobox(header, textvariable=self.profile_var, state="readonly", width=22)
        self.profile_picker.pack(side="left", padx=20)
        self.profile_picker.bind("<<ComboboxSelected>>", self.select_profile)
        self.controls.append(self.profile_picker)
        self.button(header, "Setup", self.setup_dialog, side="right")
        self.button(header, "Upload saved files", self.start_upload, side="right", padx=8)
        actions = ttk.Frame(self.root, padding=(18, 0, 18, 10))
        actions.pack(fill="x")
        self.open_media_button = self.button(actions, "Open JPG / MP4", self.choose_media, side="left")
        self.open_json_button = self.button(actions, "Open JSON", self.choose_json, side="left", padx=6)
        self.save_button = self.button(actions, "Save", self.save, side="left", padx=6)
        self.button(actions, "Poster", self.make_poster, side="left", padx=6)
        self.search_button = self.button(actions, "Search", self.search_clicked, side="left", padx=6)
        self.repeat_button = self.button(actions, "Repeat search", self.repeat_search, side="left", padx=6)
        self.button(actions, "Clear all", self.clear, side="left", padx=6)
        self.button(actions, "TXT", self.snippets_dialog, side="left", padx=6)
        self.delete_button = self.button(actions, "Del", self.delete_current, side="left", padx=6)
        pane = ttk.Panedwindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        left = ttk.Frame(pane, width=380)
        pane.add(left, weight=0)
        form_canvas = tk.Canvas(left, highlightthickness=0, width=380)
        form_scroll = ttk.Scrollbar(left, orient="vertical", command=form_canvas.yview)
        form_scroll.pack(side="right", fill="y")
        form_canvas.pack(fill="both", expand=True)
        form_canvas.configure(yscrollcommand=form_scroll.set)
        form = ttk.Frame(form_canvas, padding=(0, 4, 14, 10))
        form_id = form_canvas.create_window(0, 0, window=form, anchor="nw")
        form.bind("<Configure>", lambda e: form_canvas.configure(scrollregion=form_canvas.bbox("all")))
        form_canvas.bind("<Configure>", lambda e: form_canvas.itemconfigure(form_id, width=e.width))
        for key in self.FORM_FIELDS:
            position = core.FIELDS.index(key)
            row = ttk.Frame(form)
            row.pack(fill="x", pady=(0, 13))
            label = ttk.Label(row, text=core.LABELS[position])
            label.pack(anchor="w", pady=(0, 4))
            if key == "comment":
                self.comment_editor = CommentEditor(row)
                self.comment_editor.pack(fill="both", expand=True)
                widget = self.comment_editor.source
            elif key in ("category", "device"):
                widget = ttk.Combobox(row, textvariable=self.variables[key], font=("Sans", 11))
                widget.configure(postcommand=lambda widget=widget, key=key:
                                 self.find_combobox_entry(widget, prefix=key == "category"))
                widget.pack(fill="x", ipady=4)
                if key == "device":
                    widget.bind("<<ComboboxSelected>>", self.device_selected)
                else:
                    widget.bind("<<ComboboxSelected>>", lambda e: self.next_field("category"))
            elif key == "anzahl":
                quantity = ttk.Frame(row)
                quantity.pack(fill="x")
                ttk.Button(quantity, text="−", width=3, command=lambda: self.adjust(-1)).pack(side="left")
                widget = ttk.Entry(quantity, textvariable=self.variables[key], font=("Sans", 11))
                widget.pack(side="left", fill="x", expand=True, padx=5, ipady=4)
                ttk.Button(quantity, text="+", width=3, command=lambda: self.adjust(1)).pack(side="left")
            else:
                widget = ttk.Entry(row, textvariable=self.variables[key], font=("Sans", 11))
                widget.pack(fill="x", ipady=4)
            self.rows.append((row, label))
            self.field_widgets[key] = widget
            widget.bind("<Return>", lambda e, key=key: self.field_enter(key))
            widget.bind("<KP_Enter>", lambda e, key=key: self.field_enter(key))
            if key == "comment":
                widget.bind("<Shift-Return>", lambda e: None)
        right = ttk.Frame(pane)
        pane.add(right, weight=1)
        self.selected = tk.StringVar(value="No media or record selected")
        selection = ttk.Frame(right)
        selection.pack(fill="x", pady=(3, 8))
        ttk.Label(selection, textvariable=self.selected, wraplength=540).pack(side="left", fill="x", expand=True)
        self.preview_link = ttk.Label(right, foreground="#2563eb", cursor="hand2",
                                      font=("Sans", 11, "underline"), wraplength=720, takefocus=True)
        self.preview_link.bind("<Button-1>", self.open_preview)
        self.preview_link.bind("<Return>", self.open_preview)
        self.preview_link.bind("<space>", self.open_preview)
        self.preview = ImageCanvas(right)
        self.preview.pack(fill="both", expand=True)
        self.preview.set_navigation(lambda: self.navigate_preview(-1),
                                   lambda: self.navigate_preview(1))
        self.preview.bind("<Double-Button-1>", self.preview_double_click)
        self.preview.bind("<Button-3>", self.show_preview_link)
        ttk.Label(right, text="‹‹ / ›› browse · double-click to open · right-click for the file link",
                  foreground="#475569").pack(pady=8)
        bottom = ttk.Frame(self.root, padding=(18, 0, 18, 12))
        bottom.pack(fill="x")
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bottom, textvariable=self.status).pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=170)
        self.progress.pack(side="right")

    def refresh_profile(self):
        self.profile_picker.configure(values=[p.id for p in self.settings.profiles])
        self.profile_var.set(self.profile.id)
        self.root.title(self.settings.title)
        for key, (row, label) in zip(self.FORM_FIELDS, self.rows):
            i = core.FIELDS.index(key)
            label.configure(text=self.profile.labels[i])
            row.pack_forget()
            if self.profile.labels[i]:
                row.pack(fill="x", pady=(0, 13))
        self.field_widgets["category"].configure(values=sorted(self.profile.categories, key=str.casefold))
        self.update_controls()

    def update_controls(self):
        for control in self.controls:
            control.configure(state="disabled" if self.busy else "normal")
        if not self.busy:
            self.profile_picker.configure(state="readonly")
        for key, widget in self.field_widgets.items():
            widget.configure(state="disabled" if self.busy else "normal")
            if key == "comment":
                widget.configure(background="#eeedda" if self.search_mode else "#ffffff")
            else:
                widget_class = "TCombobox" if key in ("category", "device") else "TEntry"
                widget.configure(style=f"Search.{widget_class}" if self.search_mode else widget_class)
        if self.search_mode:
            for button in (self.save_button, self.open_json_button, self.open_media_button):
                button.configure(state="disabled")
        self.delete_button.configure(state="normal" if not self.busy and not self.search_mode
                                     and (self.media or self.record_index is not None) else "disabled")
        self.repeat_button.configure(state="normal" if self.last_search is not None and not self.busy else "disabled")
        self.search_button.configure(text="Run search" if self.search_mode else "Search")

    def log(self, message):
        self.events.put(("progress", message))

    def task(self, work, done=None, failed=None):
        if self.busy:
            return
        self.busy = True
        self.update_controls()
        self.progress.start(12)
        def execute():
            try:
                self.events.put(("done", work(), done))
            except Exception as error:
                self.events.put(("error", error, failed))
        self.executor.submit(execute)

    def drain(self):
        if self.closing:
            return
        try:
            for _ in range(100):
                try:
                    item = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    if item[0] == "progress":
                        self.status.set(item[1])
                        if self.upload_text is not None and self.upload_text.winfo_exists():
                            self.upload_text.insert("end", item[1] + "\n")
                            self.upload_text.see("end")
                    else:
                        self.busy = False
                        self.progress.stop()
                        self.update_controls()
                        if item[0] == "done":
                            self.status.set("Ready")
                            if item[2]:
                                item[2](item[1])
                        elif item[2]:
                            item[2](item[1])
                        else:
                            self.error(item[1])
                except Exception as error:
                    self.root.report_callback_exception(type(error), error, error.__traceback__)
        finally:
            if not self.closing:
                self.root.after(80, self.drain)

    def error(self, error):
        self.status.set(str(error).splitlines()[0] if str(error) else type(error).__name__)
        messagebox.showerror("ChaosBox", str(error), parent=self.root)

    def initialize(self):
        def work():
            return core.ensure_index(self.settings, self.log, self.newindex)
        def done(entries):
            self.newindex = False
            self.status.set(f"{len(entries)} records · {self.settings.index}")
        self.task(work, done)

    def show_preview_link(self, event):
        if self.preview_path is None:
            return "break"
        link = self.preview_path.resolve().as_uri()
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label=link, state="disabled")
        menu.add_separator()
        menu.add_command(label="Open in editor", command=self.open_preview_in_editor,
                         state="normal" if self.settings.editor else "disabled")
        menu.add_command(label="Copy link", command=lambda: self.copy_preview_link(link))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def preview_double_click(self, event):
        if self.preview.is_navigation_click(event):
            return "break"
        return self.open_preview(event)

    def copy_preview_link(self, link):
        self.root.clipboard_clear()
        self.root.clipboard_append(link)
        self.status.set("File link copied to clipboard.")

    def navigate_preview(self, direction):
        if self.busy:
            return
        if self.media:
            paths = self.media
            try:
                index = paths.index(self.preview_path)
            except ValueError:
                index = 0
            path = paths[(index + direction) % len(paths)]
            self.set_preview_path(path)
            def done(result):
                image, warning = result
                self.preview.set_image(image)
                if warning:
                    self.status.set(warning)
            self.task(lambda: self.preview_result(path), done)
            return
        if self.box_path is not None:
            paths = core.files(self.box_path.parent, {".json"}, recursive=False)
            if not paths:
                return
            try:
                index = paths.index(self.box_path)
            except ValueError:
                index = 0
            self.load_json(paths[(index + direction) % len(paths)], selected=0)

    def open_preview_in_editor(self):
        if self.preview_path is None or not self.settings.editor:
            return
        path = self.preview_path.resolve()
        try:
            if not path.is_file():
                raise FileNotFoundError(f"File not found: {path}")
            subprocess.Popen([self.settings.editor, str(path)])
        except OSError as error:
            self.error(error)

    def values(self):
        data = {key: variable.get() for key, variable in self.variables.items()}
        data["comment"] = self.field_widgets["comment"].get("1.0", "end-1c")
        data["created"] = self.created
        return data

    def fill(self, values):
        for key, variable in self.variables.items():
            value = str(values.get(key, ""))
            variable.set(value.upper() if key == "category" else value)
        self.field_widgets["comment"].delete("1.0", "end")
        self.field_widgets["comment"].insert("1.0", str(values.get("comment", "")))
        if getattr(self, "comment_editor", None) is not None:
            self.comment_editor.refresh()
        self.created = values.get("created", "")

    def clear(self):
        if self.busy:
            return
        self.search_mode, self.before_search = False, None
        self.media, self.box_path, self.records, self.record_index = [], None, [], None
        self.fill({"anzahl": 0})
        self.field_widgets["device"].configure(values=[])
        self.set_preview_path(None)
        self.preview.set_image(None)
        self.selected.set("No media or record selected")
        self.refresh_profile()

    def adjust(self, change):
        if self.busy or self.search_mode:
            return
        try:
            value = int(self.variables["anzahl"].get() or 0)
            self.variables["anzahl"].set(str(min(2147483647, max(0, value + change))))
        except ValueError:
            self.error("Quantity must be an integer.")

    def select_profile(self, event=None):
        if self.busy:
            return
        self.profile = self.settings.profile(self.profile_var.get())
        self.last_search = None
        self.clear()
        self.save_state()
        self.status.set(f"Search index: {self.settings.index}")

    def save_state(self):
        try:
            core.atomic_write(self.state_file, json.dumps({
                "profile": self.profile.id, "media_folders": self.media_folders}))
        except OSError as error:
            self.error(error)

    def media_folder(self):
        folder = Path(self.media_folders.get(self.profile.id, str(self.profile.images)))
        return folder if folder.is_dir() else self.profile.images

    def remember_media_folder(self, folder):
        self.media_folders[self.profile.id] = str(Path(folder).resolve())
        self.save_state()

    def choose_media(self):
        if self.busy or self.search_mode:
            return
        folder = self.media_folder()
        self.task(lambda: sorted(core.files(folder, core.MEDIA, recursive=False),
                                 key=lambda path: path.stat().st_mtime_ns, reverse=True),
                  lambda paths: self.media_dialog(paths, folder))

    def media_dialog(self, paths, folder=None):
        folder = folder or self.media_folder()
        dialog = tk.Toplevel(self.root)
        dialog.title("Open JPG / MP4")
        dialog.geometry("1000x700")
        dialog.minsize(640, 400)
        # Keep normal window decorations: transient dialogs can lose their
        # maximize button under the Linux window manager.
        dialog.resizable(True, True)
        # The window manager may not have mapped this Toplevel yet. Grabbing
        # immediately can abort construction with "window not viewable".
        def grab_when_mapped(event):
            if event.widget is dialog and dialog.winfo_viewable():
                dialog.grab_set()
        dialog.bind("<Map>", grab_when_mapped)
        navigation = ttk.Frame(dialog, padding=12)
        navigation.pack(fill="x")
        def change_folder():
            chosen = MediaFolderDialog(dialog, folder).result
            dialog.grab_set()
            if chosen:
                self.remember_media_folder(chosen)
                dialog.destroy()
                self.choose_media()
        ttk.Button(navigation, text="Choose folder …", command=change_folder).pack(side="left")
        ttk.Label(navigation, text=str(folder), wraplength=750).pack(side="left", padx=12)
        toolbar = ttk.Frame(dialog, padding=12)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="Click to select · Shift-click for a range · Double-click to open one file").pack(side="left")
        columns = tk.StringVar(value=str(getattr(self, "media_columns", 4)))
        picker = ttk.Combobox(toolbar, textvariable=columns, values=list(range(1, 11)),
                              state="readonly", width=3)
        picker.pack(side="right")
        ttk.Label(toolbar, text="Columns").pack(side="right", padx=8)
        count = tk.StringVar(value=f"0 / {len(paths)} selected")
        def changed():
            count.set(f"{len(grid.selected)} / {len(paths)} selected")
            open_button.configure(state="normal" if grid.selected else "disabled")
        grid = MediaGrid(dialog, paths, folder, changed)
        grid.pack(fill="both", expand=True, padx=12)
        show_comment = tk.BooleanVar(value=getattr(self, "media_show_user_comment", False))
        def change_comment():
            self.media_show_user_comment = show_comment.get()
            grid.set_show_user_comment(self.media_show_user_comment)
        ttk.Checkbutton(toolbar, text="Show metadata", variable=show_comment,
                        command=change_comment).pack(side="right", padx=8)
        change_comment()
        def change_columns(event=None):
            self.media_columns = int(columns.get())
            grid.set_columns(self.media_columns)
        picker.bind("<<ComboboxSelected>>", change_columns)
        change_columns()
        def open_selected(event=None):
            selected = grid.selection()
            if selected:
                self.remember_media_folder(selected[0].parent)
                dialog.destroy()
                self.open_media(selected)
        def external():
            dialog.destroy()
            chosen = filedialog.askopenfilenames(parent=self.root, initialdir=folder,
                title="Select JPG, PNG or MP4 files", filetypes=[("Images and videos", "*.jpg *.jpeg *.JPG *.JPEG *.png *.PNG *.mp4 *.MP4"), ("All files", "*")])
            if chosen:
                self.remember_media_folder(Path(chosen[0]).parent)
                self.open_media([Path(name) for name in chosen])
        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Other files …", command=external).pack(side="left")
        ttk.Button(buttons, text="Select all", command=grid.select_all).pack(side="left", padx=6)
        ttk.Button(buttons, text="Clear", command=grid.clear_selection).pack(side="left")
        ttk.Label(buttons, textvariable=count).pack(side="left", padx=8)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        open_button = ttk.Button(buttons, text="Open", command=open_selected, state="disabled")
        open_button.pack(side="right", padx=8)
        dialog.bind("<Return>", open_selected)
        grid.canvas.bind("<Double-Button-1>", lambda event: grid.double_click(event, open_selected))
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        grid.canvas.focus_set()

    def set_preview_path(self, path):
        self.preview_path = path
        if path is None:
            self.preview_link.pack_forget()
        else:
            self.preview_link.configure(text=f"preview: {path.resolve()}")
            self.preview_link.pack(before=self.preview, anchor="w", pady=(0, 8))

    def open_preview(self, event=None):
        if self.preview_path is None or self.busy:
            return
        path = self.preview_path.resolve()
        try:
            if not path.is_file():
                raise FileNotFoundError(f"File not found: {path}")
            process = subprocess.Popen(["xdg-open", str(path)])
        except OSError as error:
            self.error(error)
            return
        def wait_for_open():
            result = process.poll()
            if result is None:
                self.root.after(250, wait_for_open)
            elif result != 0:
                self.error(f"Could not open: {path}")
        self.root.after(250, wait_for_open)

    def preview_result(self, path):
        if path is None:
            return None, ""
        try:
            return core.load_preview(path), ""
        except Exception as error:
            return None, f"Preview unavailable: {error}"

    def open_media(self, paths):
        if any(path.suffix.lower() not in core.IMPORTS for path in paths):
            self.error("Select JPG, PNG or MP4 files.")
            return
        def work():
            records = core.read_metadata_batch(list(dict.fromkeys(paths)), self.log)
            return records, self.preview_result(paths[0])
        def done(result):
            records, (image, warning) = result
            differing = core.differing_metadata_fields(records)
            mode = "replace"
            if differing:
                labels = [self.profile.labels[core.FIELDS.index(field)] or core.LABELS[core.FIELDS.index(field)]
                          for field in differing]
                mode = MetadataModeDialog(self.root, labels).result
                if mode is None:
                    return
            self.media_records = records
            self.media_edit_mode, self.media_differing_fields = mode, differing
            values = records[0] if len(records) == 1 else core.common_metadata(records)
            self.media_baseline = dict(values)
            self.media, self.box_path, self.records, self.record_index = list(dict.fromkeys(paths)), None, [], None
            self.field_widgets["device"].configure(values=[])
            self.fill(values)
            self.set_preview_path(paths[0])
            self.preview.set_image(image)
            self.selected.set(f"{len(self.media)} file(s) selected")
            if warning:
                self.status.set(warning)
        self.task(work, done)

    def make_poster(self):
        if self.busy or self.search_mode:
            return
        paths, profile, settings = list(self.media), self.profile, self.settings.poster
        values = self.values()
        baseline = getattr(self, "media_baseline", {})
        overrides = {field: value for field, value in values.items()
                     if value != str(baseline.get(field, ""))}
        edit_options = {}
        if len(paths) > 1 and hasattr(self, "media_edit_mode"):
            overrides = values
            edit_options = dict(field_baseline=baseline, edit_mode=self.media_edit_mode,
                                differing_fields=self.media_differing_fields)
        def done(outputs):
            self.status.set(f"{len(outputs)} poster(s) saved: {outputs[0].parent}")
            messagebox.showinfo("Poster", "Posters saved:\n" + "\n".join(map(str, outputs)), parent=self.root)
        self.task(lambda: core.create_posters(paths, profile, settings, self.log,
                                            field_overrides=overrides,
                                            **edit_options,
                                            setup_dir=getattr(self.settings, "active_path", self.settings.path).parent), done)

    def choose_json(self):
        if self.busy or self.search_mode:
            return
        try:
            box = self.variables["box"].get().strip()
            if box:
                filename = core.box_filename(box)
                matches = [file for file in core.json_files(self.profile) if file.name == filename]
                preferred = self.profile.data / filename
                if preferred in matches:
                    chosen = preferred
                elif len(matches) == 1:
                    chosen = matches[0]
                else:
                    raise ValueError("Box not found or ambiguous. Clear Box to choose its JSON file.")
            else:
                name = filedialog.askopenfilename(parent=self.root, initialdir=self.profile.data,
                                                  title="Open JSON", filetypes=[("JSON files", "*.json")])
                if not name:
                    return
                chosen = Path(name)
                if not any(core.within(chosen, root) for root in (self.profile.data, self.profile.legacy_data)):
                    raise ValueError("Choose a JSON file from this profile's data folders.")
            self.load_json(chosen)
        except Exception as error:
            self.error(error)

    def load_json(self, path, selected=None):
        def work():
            records = core.load_box(path)
            if not records:
                raise ValueError("No records available in this JSON file.")
            return records
        def done(records):
            self.media, self.box_path, self.records = [], path, records
            self.record_index = min(selected or 0, len(records) - 1)
            if selected is None and len(records) > 1 and not self.profile.labels[2]:
                dialog = tk.Toplevel(self.root)
                dialog.title("Select JSON record")
                dialog.geometry("650x400")
                dialog.transient(self.root)
                dialog.grab_set()
                listing = tk.Listbox(dialog, exportselection=False)
                listing.pack(fill="both", expand=True, padx=12, pady=12)
                for index, record in enumerate(records):
                    values = core.normalized(record)
                    listing.insert("end", f"{index + 1}: {values['device']} | {values['alias']} | {values['comment'][:100]}")
                listing.selection_set(0)
                def choose(event=None):
                    if listing.curselection():
                        self.record_index = listing.curselection()[0]
                        dialog.destroy()
                        self.fill_record()
                ttk.Button(dialog, text="Open", command=choose).pack(pady=12)
                listing.bind("<Double-Button-1>", choose)
                listing.bind("<Return>", choose)
                dialog.protocol("WM_DELETE_WINDOW", choose)
                return
            self.fill_record()
        self.task(work, done)

    def fill_record(self):
        index = self.record_index
        values = core.normalized(self.records[index])
        values["box"] = values["box"] or self.box_path.stem
        self.fill(values)
        self.device_order = sorted(range(len(self.records)), key=lambda i: str(self.records[i].get("device", "")).casefold())
        self.field_widgets["device"].configure(values=[str(self.records[i].get("device", "")) or "(no device)" for i in self.device_order])
        self.selected.set(f"{self.box_path} — record {index + 1}/{len(self.records)}")
        path, record = self.box_path, values
        def work():
            entries = core.load_index(self.settings)
            entries = [entry for entry in entries if core.within(entry.source, self.profile.images)]
            preview = core.related_media(core.Entry(path, record, False, index), entries)
            return preview, self.preview_result(preview)
        def done(result):
            preview, (image, warning) = result
            self.set_preview_path(preview)
            self.preview.set_image(image)
            if warning:
                self.status.set(warning)
        self.task(work, done)

    def device_selected(self, event=None):
        if not self.busy and not self.search_mode and self.records:
            index = self.field_widgets["device"].current()
            if index >= 0:
                self.record_index = self.device_order[index]
                self.fill_record()

    def save(self):
        if self.busy or self.search_mode:
            return
        try:
            values = self.values()
            if not self.media and not values["box"].strip() and not self.profile.labels[0]:
                name = simpledialog.askstring("Save JSON", "Box name:", parent=self.root)
                if name is None:
                    return
                values["box"] = name
                self.variables["box"].set(name)
            batch_records = ([core.edited_metadata(original, values, self.media_baseline,
                              mode=getattr(self, "media_edit_mode", "auto"),
                              differing_fields=getattr(self, "media_differing_fields", ()))
                              for original in self.media_records] if len(self.media) > 1 else None)
            record = core.validate_record(values)
            path = self.box_path if self.box_path else self.profile.data / core.box_filename(record["box"]) if not self.media else None
        except Exception as error:
            self.error(error)
            return
        if len(self.media) > 1 and not messagebox.askokcancel(
                "Save", "Attention! All selected Images get this texts",
                parent=self.root, icon="warning", default="cancel"):
            return
        selected, profile, media = self.record_index, self.profile, list(self.media)
        def work():
            self.settings.remember_category(profile, record["category"])
            if media:
                try:
                    saved = core.save_batch(media, profile, record, self.settings.image_width, self.log, records=batch_records)
                except core.BatchError as error:
                    if error.completed:
                        try:
                            previous = [p for p in media[:error.completed]
                                        if core.within(p, profile.images) and p.suffix.lower() in core.MEDIA]
                            core.update_index(self.settings, previous + error.selection[:error.completed], self.log)
                        except Exception as index_error:
                            self.log(f"Search index update failed: {index_error}")
                    raise
                result = ("media", saved, self.preview_result(saved[0]),
                          core.read_metadata_batch(saved, self.log))
            else:
                records, index = core.save_box(path, record, selected)
                result = ("json", records, index)
            warning = ""
            try:
                previous = [p for p in media if core.within(p, profile.images) and p.suffix.lower() in core.MEDIA]
                core.update_index(self.settings, previous + saved if media else [path], self.log)
            except Exception as error:
                warning = f"Saved locally, but the search index could not be updated: {error}"
            return result, warning
        def done(result):
            _, warning = result
            self.profile = self.settings.profile(profile.id)
            self.clear()
            self.status.set(warning or "Saved locally. Use Upload to synchronize manually.")
        def failed(error):
            if isinstance(error, core.BatchError):
                self.media = error.selection
                self.set_preview_path(self.media[0])
            self.profile = self.settings.profile(profile.id)
            self.refresh_profile()
            self.error(error)
        self.task(work, done, failed)

    def delete_current(self):
        if self.busy or self.search_mode:
            return
        path = self.box_path if self.box_path else self.preview_path if self.media else None
        if path is None:
            return
        selected = self.record_index if self.box_path else None
        if self.box_path and selected is None:
            return
        def work():
            core.delete_entry(path, selected)
            warning = ""
            try:
                core.update_index(self.settings, [path], self.log)
            except Exception as error:
                warning = f" Search index update failed: {error}"
            return warning
        def done(warning):
            self.clear()
            self.status.set(f"Deleted: {path}" + warning)
        self.task(work, done)

    def search_clicked(self):
        if self.busy:
            return
        if not self.search_mode:
            self.before_search = self.values()
            self.search_mode = True
            self.fill({})
            self.field_widgets["device"].configure(values=[])
            self.update_controls()
            self.status.set("Category: literal prefix; other fields: regular expressions. AND combined. Escape cancels.")
            return
        self.run_search()

    def cancel_search(self):
        if self.busy or not self.search_mode:
            return
        self.search_mode = False
        if self.before_search is not None:
            self.fill(self.before_search)
        self.before_search = None
        self.refresh_profile()
        if self.records:
            self.field_widgets["device"].configure(values=[str(self.records[i].get("device", "")) for i in self.device_order])
        self.status.set("Search cancelled")

    def repeat_search(self):
        if self.busy or self.last_search is None:
            return
        if not self.search_mode:
            self.search_clicked()
        self.fill(self.last_search)
        self.run_search()

    def run_search(self):
        values = self.values()
        query = {field: values[field] for i, field in enumerate(core.FIELDS) if self.profile.labels[i]}
        try:
            patterns = core.compile_query(query)
        except Exception as error:
            self.error(error)
            return
        self.last_search = dict(values)
        def work():
            entries = core.load_index(self.settings)
            return core.search(entries, patterns)
        def done(hits):
            if not hits:
                self.status.set("No matches. Change the search fields and search again.")
            elif len(hits) == 1:
                self.open_hit(hits[0])
            else:
                self.results_dialog(hits)
        self.task(work, done)

    def open_hit(self, hit):
        self.search_mode, self.before_search = False, None
        owner = next((profile for profile in self.settings.profiles
                      if any(core.within(hit.source, folder) for folder in
                             (profile.images, profile.data, profile.legacy_data))), None)
        if owner is None:
            self.error(ValueError("The source profile is no longer configured. Restart with --newindex."))
            return
        self.profile = self.settings.profile(owner.id)
        self.save_state()
        self.refresh_profile()
        if hit.media:
            self.open_media([hit.source])
        else:
            self.load_json(hit.source, hit.index)

    def search_hit_path(self, hit):
        for profile in self.settings.profiles:
            if any(core.within(hit.source, folder) for folder in
                   (profile.images, profile.data, profile.legacy_data)):
                return str(hit.source.resolve().relative_to(profile.images.parent.resolve()))
        return str(hit.source)

    def results_dialog(self, hits):
        dialog = tk.Toplevel(self.root)
        dialog.title(f"{len(hits)} matches")
        dialog.geometry("1050x500")
        dialog.transient(self.root)
        dialog.grab_set()
        fields = [("anzahl", "Quantity"), ("device", "Device"), ("category", "Category"),
                  ("package", "Package")]
        columns = [key for key, _ in fields] + ["path"]
        table = ttk.Treeview(dialog, columns=columns, show="headings", selectmode="browse")
        for key, label in fields + [("path", "Path")]:
            table.heading(key, text=label)
            table.column(key, width=440 if key == "path" else 150)
        table.pack(fill="both", expand=True, padx=12, pady=12)
        for i, hit in enumerate(hits):
            table.insert("", "end", iid=str(i),
                         values=tuple(hit.data.get(key, "") for key, _ in fields) + (self.search_hit_path(hit),))
        def select(event=None):
            if table.selection():
                hit = hits[int(table.selection()[0])]
                dialog.destroy()
                self.open_hit(hit)
        ttk.Button(dialog, text="Open", command=select).pack(side="right", padx=12, pady=12)
        ttk.Button(dialog, text="Keep searching", command=dialog.destroy).pack(side="right", pady=12)
        table.bind("<Double-Button-1>", select)
        table.bind("<Return>", select)

    def setup_dialog(self):
        if self.busy:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Edit setup.ini")
        dialog.geometry("900x680")
        dialog.transient(self.root)
        dialog.grab_set()
        setup_path = self.settings.active_path
        ttk.Label(dialog, text=str(setup_path), padding=10).pack(anchor="w")
        text = tk.Text(dialog, wrap="none", undo=True, font=("Monospace", 11))
        text.pack(fill="both", expand=True, padx=10)
        setup_text = setup_path.read_text(encoding="utf-8")
        if setup_path == self.settings.path:
            if not any(name.casefold() == "poster" for name in core.sections(setup_text)):
                poster = self.settings.poster
                setup_text += (f"\n[Poster]\nPOSTER-LIMIT={poster.limit}\n"
                               f"POSTER-SIZE={poster.height:g}x{poster.width:g}\n"
                               f"POSTER-COLS={poster.cols}\nPOSTER-ROWS={poster.rows}\n"
                               f"POSTER-FIX={str(poster.fixed).lower()}\n")
            groups = core.sections(setup_text)
            poster_section = next(name for name in groups if name.casefold() == "poster")
            additions = []
            for key in ("margin_left", "margin_right", "margin_top", "margin_bottom",
                        "frames", "background_color", "image_background_color", "image_padding"):
                option = "POSTER-" + key.upper().replace("_", "-")
                if option.lower() not in groups[poster_section]:
                    additions.append(f"{option}={getattr(self.settings.poster, key)}")
            if additions:
                heading = f"[{poster_section}]"
                setup_text = setup_text.replace(heading, heading + "\n" + "\n".join(additions), 1)
        text.insert("1.0", setup_text)
        def save():
            try:
                self.settings.save_text(text.get("1.0", "end-1c") + "\n", setup_path)
                self.profile = self.settings.profile(self.profile.id)
                self.last_search = None
                self.clear()
                dialog.destroy()
                self.initialize()
            except Exception as error:
                messagebox.showerror("Setup", str(error), parent=dialog)
        ttk.Button(dialog, text="Save", command=save).pack(side="right", padx=10, pady=10)
        ttk.Button(dialog, text="Cancel", command=dialog.destroy).pack(side="right", pady=10)

    def snippets_dialog(self):
        if self.busy:
            return
        try:
            self.settings.reload()
        except Exception as error:
            self.error(error)
            return
        snippets = list(self.settings.snippets.items())
        dialog = tk.Toplevel(self.root)
        dialog.title("Select text snippet")
        dialog.geometry("850x500")
        dialog.transient(self.root)
        dialog.grab_set()
        names = tk.Listbox(dialog, exportselection=False, width=24, font=("Sans", 11))
        names.pack(side="left", fill="y", padx=10, pady=10)
        right = ttk.Frame(dialog, padding=10)
        right.pack(fill="both", expand=True)
        contents = tk.Text(right, wrap="word", font=("Sans", 11))
        contents.pack(fill="both", expand=True)
        for name, _ in snippets:
            names.insert("end", name)
        def show(event=None):
            if names.curselection():
                contents.delete("1.0", "end")
                contents.insert("1.0", snippets[names.curselection()[0]][1])
        def copy(event=None):
            if names.curselection():
                self.root.clipboard_clear()
                self.root.clipboard_append(snippets[names.curselection()[0]][1])
                self.status.set("Text snippet copied. Paste with Ctrl+V.")
                dialog.destroy()
        names.bind("<<ListboxSelect>>", show)
        names.bind("<Double-Button-1>", copy)
        ttk.Button(right, text="Copy to clipboard", command=copy).pack(side="right", pady=(10, 0))
        if snippets:
            names.selection_set(0)
            show()

    def start_upload(self):
        if self.busy:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Manual upload")
        dialog.geometry("900x530")
        dialog.transient(self.root)
        dialog.grab_set()
        text = tk.Text(dialog, wrap="word", font=("Monospace", 10))
        text.pack(fill="both", expand=True, padx=12, pady=12)
        self.upload_window, self.upload_text = dialog, text
        self.uploading = True
        self.cancel_upload.clear()
        cancel = ttk.Button(dialog, text="Cancel upload", command=self.cancel_upload.set)
        cancel.pack(side="right", padx=12, pady=(0, 12))
        def close():
            if self.uploading:
                self.cancel_upload.set()
                text.insert("end", "Cancellation requested …\n")
            else:
                dialog.destroy()
                self.upload_window = self.upload_text = None
        dialog.protocol("WM_DELETE_WINDOW", close)
        def finish(error=None):
            self.uploading = False
            cancel.configure(text="Close", command=close)
            if error:
                text.insert("end", f"\n{error}\n")
                dialog.title("Upload cancelled" if isinstance(error, core.Cancelled) else "Upload failed")
            else:
                dialog.title("Upload completed")
            text.see("end")
        def work():
            self.settings.reload()
            core.upload(self.settings, self.profile, self.cancel_upload, self.log)
        self.task(work, lambda _: finish(), finish)

    def close(self):
        if self.busy:
            if self.uploading:
                self.cancel_upload.set()
                self.status.set("Cancelling upload. Close again after cancellation completes.")
            else:
                self.status.set("Please wait for the current file operation to finish before closing.")
            return
        self.closing = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="ChaosBox desktop editor")
    parser.add_argument("directory", nargs="?", type=Path, help="Installation directory (default: ~/ChaosBox)")
    parser.add_argument("--installdir", type=Path,
                        help="Installation directory containing setup.ini, projects and index (default: ~/ChaosBox)")
    parser.add_argument("--state-dir", type=Path, help="Override installation-local .state directory")
    parser.add_argument("--setup", type=Path)
    parser.add_argument("--newindex", action="store_true", help="Rebuild the shared search index for all profiles")
    parser.add_argument("--check", action="store_true", help="Check dependencies without opening a window or changing data")
    args = parser.parse_args()
    if args.directory is not None and args.installdir is not None:
        parser.error("Use either a directory argument or --installdir, not both.")
    installdir = args.directory or args.installdir or Path.home() / "ChaosBox"
    if args.check:
        import shutil
        import paramiko
        import mutagen
        core.image_metadata_library()
        for tool in ("ffmpeg", "ffprobe"):
            if shutil.which(tool) is None:
                raise SystemExit(f"Missing program: {tool}")
        print(f"Dependencies ready: Tk {tk.TkVersion}, Pillow {Image.__version__}, "
              f"GExiv2, Mutagen {mutagen.version_string}, markdown-it-py, Paramiko {paramiko.__version__}")
        return
    settings = core.Settings(installdir, args.state_dir, args.setup)
    root = tk.Tk(className=settings.window_class)
    root.withdraw()
    try:
        settings.ensure()
        app = App(root, settings, newindex=args.newindex)
    except Exception as error:
        messagebox.showerror("ChaosBox startup", str(error), parent=root)
        root.destroy()
        raise SystemExit(1)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
