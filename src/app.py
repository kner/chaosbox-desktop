#!/usr/bin/env python3
"""ChaosBox desktop editor for Ubuntu. All Tk operations run on the UI thread."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from urllib.parse import urlsplit, unquote
from tkinter import ttk, messagebox, simpledialog

from PIL import Image, ImageTk

import core
from i18n import translate
import markdown_render


def media_drop_paths(interpreter, data):
    """Decode a Tcl file list without treating filenames as Tcl commands."""
    paths = []
    for name in interpreter.splitlist(data):
        if name.startswith("file:"):
            uri = urlsplit(name)
            if uri.netloc not in ("", "localhost") or uri.query or uri.fragment:
                raise ValueError("Only local media files can be dropped.")
            name = unquote(uri.path)
        path = Path(name)
        if path.is_absolute() and not path.exists():
            # Ubuntu tkdnd 2.6 decodes percent-escaped UTF-8 filenames as Latin-1.
            # Prefer the original spelling whenever it exists; repair only that
            # old transport encoding when it identifies a real local file.
            try:
                repaired = Path(name.encode("latin-1").decode("utf-8"))
                if repaired.is_file():
                    path = repaired
            except UnicodeError:
                pass
        if not path.is_absolute() or not path.is_file() or path.suffix.casefold() not in core.IMPORTS:
            raise ValueError("Drop existing JPG, PNG or MP4 files.")
        paths.append(path.resolve())
    return list(dict.fromkeys(paths))


class CommentEditor(ttk.Notebook):
    """Keep editable Markdown separate from its read-only native preview."""

    def __init__(self, parent, translator=translate):
        super().__init__(parent)
        self.t = translator
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
        self.add(edit, text=self.t("Markdown"))
        self.add(preview, text=self.t("Preview"))
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


def user_comment_preview(path):
    # Keep the original Markdown source, including its whitespace and line breaks.
    metadata = core.parse_metadata(core.read_user_comment(path))
    fields = [str(metadata.get(key) or "") for key in ("box", "category", "comment")]
    return " | ".join(fields) if any(fields) else ""


class ImageCanvas(tk.Canvas):
    def __init__(self, parent, translator=translate, **kwargs):
        super().__init__(parent, background="#101820", highlightthickness=0, **kwargs)
        self.t = translator
        self.original = None
        self.photo = None
        self.pending = None
        self.navigate_previous = None
        self.navigate_next = None
        self.select_media = None
        self.navigation_enabled = False
        self.navigation_bounds = None
        self.bind("<Configure>", self.resize)
        self.bind("<Button-1>", self.navigation_click)
        self.bind("<Motion>", self.navigation_motion)

    def set_navigation(self, previous, next_, select_media=None, enabled=False):
        self.navigate_previous, self.navigate_next = previous, next_
        self.select_media, self.navigation_enabled = select_media, enabled
        self.schedule()

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
        self.navigation_bounds = None
        vw, vh = max(1, self.winfo_width()), max(1, self.winfo_height())
        if self.original is None:
            self.create_text(vw / 2, vh / 2,
                             text=self.t("Drop JPG, PNG or MP4 here, or select files."), fill="#cbd5e1", font=("Sans", 15))
            if self.navigation_enabled:
                self.navigation_bounds = (0, 0, vw, vh)
                self.draw_navigation(0, 0, vw, vh)
            return
        scale = self.fit()
        if scale <= 0:
            return
        vw, vh = max(1, self.winfo_width()), max(1, self.winfo_height())
        width, height = max(1, round(self.original.width * scale)), max(1, round(self.original.height * scale))
        rendered = self.original.resize((width, height), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(rendered)
        self.create_image((vw - width) / 2, (vh - height) / 2, anchor="nw", image=self.photo)
        if self.navigation_enabled:
            self.navigation_bounds = (0, 0, vw, vh)
            self.draw_navigation(0, 0, vw, vh)

    def draw_navigation(self, left, top, right, bottom):
        center_y = (top + bottom) / 2
        width = right - left
        offset = min(42, width * .2)
        font = ("Sans", 30, "bold")
        for x, arrow in ((left + offset, "≪"), (right - offset, "≫")):
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, -1), (-1, 1), (1, 1)):
                self.create_text(x + dx, center_y + dy, text=arrow, fill="#111827", font=font)
            self.create_text(x, center_y, text=arrow, fill="#ffd43b", font=font)

    def is_navigation_click(self, event):
        if self.navigation_bounds is None:
            return 0
        left, top, right, bottom = self.navigation_bounds
        if not (left <= event.x <= right and top <= event.y <= bottom):
            return 0
        zone = min(76, (right - left) / 2)
        if event.x <= left + zone:
            return -1
        if event.x >= right - zone:
            return 1
        return 0

    def navigation_click(self, event):
        direction = self.is_navigation_click(event)
        if direction < 0 and self.navigate_previous is not None:
            self.navigate_previous()
            return "break"
        if direction > 0 and self.navigate_next is not None:
            self.navigate_next()
            return "break"
        if self.navigation_enabled and self.select_media is not None:
            self.select_media()
            return "break"

    def navigation_motion(self, event):
        self.configure(cursor="hand2" if self.navigation_enabled and self.is_navigation_click(event) else "")


class MetadataModeDialog(simpledialog.Dialog):
    def __init__(self, parent, labels, translator=None):
        self.labels = labels
        self.t = translator or getattr(parent, "_chaosbox_translator", translate)
        super().__init__(parent, title=self.t("Multiple selection"))

    def body(self, master):
        ttk.Label(master, text=self.t("Existing values differ in these fields:\n\n")
                  + ", ".join(self.labels) + "\n\n" + self.t("Replace the existing values or append the new values?"),
                  wraplength=520, justify="left").pack(padx=12, pady=12)

    def buttonbox(self):
        buttons = ttk.Frame(self)
        buttons.pack(padx=12, pady=12)
        def choose(mode):
            self.result = mode
            self.cancel()
        ttk.Button(buttons, text=self.t("Append – add new content"),
                   command=lambda: choose("extend")).pack(side="left", padx=4)
        ttk.Button(buttons, text=self.t("Replace – overwrite with new input"),
                   command=lambda: choose("replace")).pack(side="left", padx=4)
        ttk.Button(buttons, text=self.t("Cancel"), command=self.cancel).pack(side="left", padx=4)
        self.bind("<Return>", lambda event: choose("extend"))
        self.bind("<Escape>", self.cancel)


class FileOpenDialog(simpledialog.Dialog):
    """File chooser with an explicit order, independent of the OS."""

    def __init__(self, parent, folder, extensions, title, multiple=False, translator=translate,
                 alphabetical=False):
        self.folder = Path(folder)
        self.extensions = {suffix.casefold() for suffix in extensions}
        self.multiple, self.t = multiple, translator
        self.alphabetical = alphabetical
        self.paths = {}
        super().__init__(parent, title=title)

    def body(self, master):
        self.resizable(True, True)
        self.location = tk.StringVar(value=str(self.folder))
        navigation = ttk.Frame(master)
        navigation.pack(fill="x", pady=(0, 4))
        ttk.Button(navigation, text=self.t("Up"), command=lambda: self.browse(self.folder.parent)).pack(side="left")
        entry = ttk.Entry(navigation, textvariable=self.location, width=70)
        entry.pack(side="left", fill="x", expand=True, padx=(4, 0))
        entry.bind("<Return>", self.enter_path)
        frame = ttk.Frame(master)
        frame.pack(fill="both", expand=True)
        self.listing = ttk.Treeview(frame, columns=("modified",),
                                   selectmode="extended" if self.multiple else "browse", height=20)
        self.listing.heading("#0", text=self.t("File"))
        self.listing.heading("modified", text="" if self.alphabetical else self.t("Modified · newest first"))
        self.listing.column("#0", width=480, minwidth=120)
        self.listing.column("modified", width=180, minwidth=150, stretch=False)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.listing.yview)
        self.listing.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.listing.pack(side="left", fill="both", expand=True)
        self.listing.bind("<Double-Button-1>", self.activate)
        self.listing.bind("<Return>", lambda event: self.ok())
        self.browse(self.folder)
        return self.listing

    def buttonbox(self):
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=5, pady=4)
        ttk.Button(buttons, text=self.t("Cancel"), command=self.cancel).pack(side="right")
        ttk.Button(buttons, text=self.t("Open"), command=self.ok).pack(side="right", padx=4)
        self.bind("<Escape>", self.cancel)

    def browse(self, folder):
        try:
            folder = Path(folder).expanduser().resolve()
            directories, files = [], []
            for path in folder.iterdir():
                if path.name.startswith("."):
                    continue
                info = path.stat()
                if path.is_dir():
                    directories.append(path)
                elif path.is_file() and path.suffix.casefold() in self.extensions:
                    files.append((path, info.st_mtime_ns))
            directories.sort(key=lambda path: path.name.casefold())
            if self.alphabetical:
                files.sort(key=lambda item: item[0].name.casefold())
            else:
                files.sort(key=lambda item: (-item[1], item[0].name.casefold()))
        except OSError as error:
            messagebox.showerror(self.title(), str(error), parent=self)
            return
        self.folder = folder
        self.location.set(str(folder))
        self.listing.delete(*self.listing.get_children())
        self.paths = {}
        for path, modified in [(path, None) for path in directories] + files:
            row = self.listing.insert("", "end", text=path.name + ("/" if modified is None else ""),
                values=("" if modified is None or self.alphabetical else datetime.fromtimestamp(modified / 1e9).strftime("%Y-%m-%d %H:%M:%S"),))
            self.paths[row] = path

    def enter_path(self, event=None):
        path = Path(self.location.get()).expanduser()
        if not path.is_absolute():
            path = self.folder / path
        if path.is_dir():
            self.browse(path)
        else:
            self.browse(path.parent)
            row = next((row for row, file in self.paths.items() if file == path.resolve()), None)
            if row is not None:
                self.listing.selection_set(row)
                self.ok()
        return "break"

    def activate(self, event):
        row = self.listing.identify_row(event.y)
        if row:
            if row not in self.listing.selection():
                self.listing.selection_set(row)
            self.ok()
        return "break"

    def validate(self):
        selected = [self.paths[row] for row in self.listing.selection()]
        if len(selected) == 1 and selected[0].is_dir():
            self.browse(selected[0])
            return False
        if not selected or any(not path.is_file() or path.suffix.casefold() not in self.extensions for path in selected):
            return False
        self.selected_paths = selected
        return True

    def apply(self):
        self.result = self.selected_paths if self.multiple else self.selected_paths[0]


class MediaFolderDialog(simpledialog.Dialog):
    """Folder picker where a double-click confirms the folder immediately."""

    def __init__(self, parent, folder, translator=translate):
        self.folder = Path(folder)
        self.directories = []
        self.t = translator
        super().__init__(parent, title=self.t("Select media folder"))

    def body(self, master):
        self.location = tk.StringVar(value=str(self.folder))
        navigation = ttk.Frame(master)
        navigation.pack(fill="x")
        ttk.Button(navigation, text=self.t("Up"), command=lambda: self.browse(self.folder.parent)).pack(side="left")
        entry = ttk.Entry(navigation, textvariable=self.location, width=65)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        def enter_path(event):
            self.browse(Path(self.location.get()).expanduser())
            return "break"
        entry.bind("<Return>", enter_path)
        ttk.Label(master, text=self.t("Double-click a folder to select it and open its media.")).pack(anchor="w", pady=8)
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

    def buttonbox(self):
        buttons = ttk.Frame(self)
        buttons.pack(padx=12, pady=12)
        ttk.Button(buttons, text=self.t("Select"), command=self.ok).pack(side="left", padx=4)
        ttk.Button(buttons, text=self.t("Cancel"), command=self.cancel).pack(side="left", padx=4)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)

    def browse(self, folder):
        try:
            folder = folder.resolve()
            directories = sorted((path for path in folder.iterdir() if path.is_dir()),
                                 key=lambda path: path.name.casefold())
        except OSError as error:
            messagebox.showerror(self.t("Select media folder"), str(error), parent=self)
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
            messagebox.showerror(self.t("Select media folder"), self.t("This folder is no longer available."), parent=self)
            return False
        return True

    def apply(self):
        self.result = self.selected_folder()


class MediaGrid(ttk.Frame):
    """Scrollable thumbnail chooser; only visible tiles are rendered and loaded."""

    def __init__(self, parent, paths, base, changed, translator=translate):
        super().__init__(parent)
        self.t = translator
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
            comment_lines = 1
            for comment in self.comments.values():
                lines = sum(max(1, (len(line) + chars_per_line - 1) // chars_per_line)
                            for line in (comment or "").split("\n")) or 1
                comment_lines = max(comment_lines, lines)
            self.cell_height += comment_lines * 18 + 8
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
            canvas.create_text(width / 2, 60, text=self.t("Keine Medien gefunden. Verwende „Andere Dateien …“, um Bilder zu importieren."),
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
                                       text=self.t("Vorschau nicht verfügbar"), width=self.thumb_size, fill="#64748b")
            else:
                canvas.create_text(x + self.cell_width / 2, y + self.thumb_size / 2,
                                   text=self.t("Lädt …"), width=self.thumb_size, fill="#64748b")
            label = str(path.relative_to(self.base)) if path.is_relative_to(self.base) else path.name
            max_chars = max(6, int((self.cell_width - 14) / 8) * 2)
            if len(label) > max_chars:
                label = label[:max_chars - 1] + "…"
            canvas.create_text(x + self.cell_width / 2, y + self.thumb_size + 12,
                               anchor="n", text=label, width=max(12, self.cell_width - 14),
                               font=("Sans", 9), fill="#0f172a")
            if self.show_user_comment:
                canvas.create_text(x + self.cell_width / 2, y + self.thumb_size + 62,
                                   anchor="n", text=self.comments.get(index, self.t("Metadaten werden geladen …")),
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
        self.window_icon = ImageTk.PhotoImage(core.installation_icon(settings.icon_id))
        root.iconphoto(True, self.window_icon)
        root.title(settings.title)
        root.geometry("1240x850")
        root.minsize(850, 620)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chaosbox")
        self.events = queue.Queue()
        self.busy, self.closing = False, False
        self.media, self.box_path, self.records, self.record_index = [], None, [], None
        self.individual_paths, self.individual_index = None, 0
        self.created = ""
        self.sync_needs_reload = False
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
        style.configure("TButton", padding=(3, 1), width=0)
        style.configure("Title.TLabel", font=("Sans", 18, "bold"))
        style.configure("TLabel", font=("Sans", 11))
        for widget_class in ("TEntry", "TCombobox"):
            search_style = f"Search.{widget_class}"
            style.configure(search_style, fieldbackground="#fff9c4")
            style.map(search_style, fieldbackground=[("disabled", "#fff9c4"),
                                                     ("readonly", "#fff9c4")])
        self.build_ui()
        self.refresh_profile()
        self.clear()
        self.setup_media_drop()
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

    def setup_media_drop(self):
        self.dnd_available = False
        # Ubuntu's tkdnd package is normally found automatically. Also support
        # the same package extracted into the user's local library directory.
        local = Path.home() / ".local/lib/tkdnd2.6"
        if local.is_dir():
            self.root.tk.call("lappend", "auto_path", str(local))
        try:
            self.root.tk.call("package", "require", "tkdnd")
        except tk.TclError:
            self.log(self.t("Drag and drop requires tkdnd. File selection remains available."))
            return
        self.dnd_available = True
        if self.root.tk.call("tk", "windowingsystem") == "x11":
            # tkdnd 2.6's native reader can retain the preceding external
            # transfer. Tk's selection reader requests fresh XdndSelection
            # data for each drop, including drops from other applications.
            self.root.tk.eval('''
                proc ::tkdnd::xdnd::_GetDroppedData {time} {
                    variable _drop_target
                    variable _common_drag_source_types
                    # selection get services the X event loop. Nautilus can
                    # send XdndLeave while its data is being fetched; tkdnd 2.6
                    # clears the active drop context before its drop handler
                    # resumes. Preserve that context until data retrieval ends.
                    foreach name {_types _typelist _actionlist _pressedkeys
                                  _action _common_drag_source_types
                                  _common_drop_target_types _drag_source _drop_target} {
                        upvar #0 ::tkdnd::xdnd::$name state
                        if {[info exists state]} {set saved($name) $state}
                    }
                    try {
                        foreach type $_common_drag_source_types {
                            if {![catch {
                                selection get -displayof $_drop_target \\
                                    -selection XdndSelection -type $type
                            } result options]} {
                                # Tk can return unknown selection types as a list
                                # of byte values. tkdnd 2.6's URI decoder has a
                                # missing bracket and silently treats those bytes
                                # as individual filenames instead.
                                if {[string match text/uri-list* $type] &&
                                    [regexp {^\\s*(?:0[xX][0-9a-fA-F]+|-?[0-9]+)(?:\\s+(?:0[xX][0-9a-fA-F]+|-?[0-9]+))*\\s*$} $result]} {
                                    set result [encoding convertfrom utf-8 [binary format c* $result]]
                                }
                                return [_normalise_data $type $result]
                            }
                        }
                        return -code error "Could not read dropped media files."
                    } finally {
                        foreach name [array names saved] {
                            set ::tkdnd::xdnd::$name $saved($name)
                        }
                    }
                }
            ''')
        for widget in (self.root, self.preview, self.open_media_button):
            self.register_media_drop(widget)

    def register_media_drop(self, widget, dialog=None, on_select=None):
        if not getattr(self, "dnd_available", False):
            return
        context = (id(self), id(dialog), id(on_select))
        # tkdnd 2.6 can route XdndPosition to an enclosing Panedwindow/Frame
        # instead of its nested canvas. Register the whole ancestor chain so
        # window-addressed file-manager drops reach the same media handler.
        target = widget
        while target is not None:
            if getattr(target, "_chaosbox_drop_context", None) != context:
                drop = target.register(lambda data: self.drop_media(data, dialog, on_select))
                action = target.register(lambda target=target: self.media_drop_action(target))
                target.tk.call("tkdnd::drop_target", "register", target._w, "DND_Files")
                for event in ("<<DropEnter>>", "<<DropPosition>>"):
                    target.tk.call("bind", target._w, event, action)
                target.tk.call("bind", target._w, "<<Drop:DND_Files>>", drop + " %D")
                target._chaosbox_drop_context = context
            if isinstance(target, (tk.Tk, tk.Toplevel)):
                break
            target = target.master

    def media_drop_action(self, widget):
        if self.busy or self.search_mode:
            return "refuse_drop"
        grabbed = self.root.grab_current()
        if grabbed is not None and grabbed is not widget.winfo_toplevel():
            return "refuse_drop"
        return "copy"

    def drop_media(self, data, dialog=None, on_select=None):
        target = dialog if dialog is not None else self.root
        if self.media_drop_action(target) != "copy":
            return "refuse_drop"
        try:
            paths = media_drop_paths(self.root.tk, data)
            if not paths:
                return "refuse_drop"
            if on_select is not None and len(paths) != 1:
                raise ValueError(self.t("Drop one media file to attach to this JSON record."))
        except (ValueError, OSError, tk.TclError) as error:
            self.error(error)
            return "refuse_drop"
        self.remember_media_folder(paths[0].parent)
        if dialog is not None:
            dialog.destroy()
        if on_select is not None:
            on_select(paths[0])
        else:
            self.open_media(paths, preserve_fields=True)
        return "copy"

    def uppercase_category(self, *_):
        variable = self.variables["category"]
        value = variable.get()
        if value != value.upper():
            variable.set(value.upper())

    def t(self, text, **values):
        language = getattr(getattr(self, "settings", None), "language", "en")
        return translate(text, language, **values)

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
        header = ttk.Frame(self.root, padding=(10, 5))
        header.pack(fill="x")
        ttk.Label(header, text=self.settings.title, style="Title.TLabel").pack(side="left")
        self.profile_var = tk.StringVar(value=self.profile.id)
        self.profile_picker = ttk.Combobox(header, textvariable=self.profile_var, state="readonly", width=22)
        self.profile_picker.pack(side="left", padx=8)
        self.profile_picker.bind("<<ComboboxSelected>>", self.select_profile)
        self.controls.append(self.profile_picker)
        self.button(header, self.t("Poster"), self.make_poster, side="left", padx=2)
        self.button(header, "TXT", self.snippets_dialog, side="left", padx=2)
        menu_button = ttk.Button(header, text="☰", width=3, command=self.setup_dialog)
        ttk.Label(header, text=f"(c) kner.at · {core.VERSION}").pack(side="right", padx=2)
        menu_button.pack(side="right")
        self.controls.append(menu_button)
        self.button(header, self.t("Synchronize"), self.start_upload, side="right", padx=3)
        self.button(header, self.t("Conflicts"), self.show_sync_conflicts, side="right", padx=3)
        actions = ttk.Frame(self.root, padding=(10, 0, 10, 4))
        actions.pack(fill="x")
        self.save_button = self.button(actions, self.t("Save"), self.save, side="left", padx=2)
        self.open_media_button = self.button(actions, self.t("Open JPG / MP4"), self.choose_media, side="left")
        self.open_json_button = self.button(actions, self.t("Open JSON"), self.choose_json, side="left", padx=2)
        self.search_button = self.button(actions, self.t("Search"), self.search_clicked, side="left", padx=2)
        self.repeat_button = self.button(actions, self.t("Repeat search"), self.repeat_search, side="left", padx=2)
        self.button(actions, self.t("Clear all"), self.clear, side="left", padx=2)
        self.clear_fields_button = self.button(actions, self.t("Clear fields"), self.clear_fields, side="left", padx=2)
        self.delete_button = self.button(actions, self.t("Delete"), self.delete_current, side="left", padx=2)
        self.button(actions, self.t("Delete box"), self.delete_whole_box, side="left", padx=2)
        pane = ttk.Panedwindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        left = ttk.Frame(pane, width=380)
        pane.add(left, weight=0)
        form_canvas = tk.Canvas(left, highlightthickness=0, width=380)
        form_scroll = ttk.Scrollbar(left, orient="vertical", command=form_canvas.yview)
        form_scroll.pack(side="right", fill="y")
        form_canvas.pack(fill="both", expand=True)
        form_canvas.configure(yscrollcommand=form_scroll.set)
        form = ttk.Frame(form_canvas, padding=(0, 0, 8, 2))
        form_id = form_canvas.create_window(0, 0, window=form, anchor="nw")
        def resize_form(event=None):
            form_canvas.itemconfigure(form_id, width=form_canvas.winfo_width(),
                                      height=max(form.winfo_reqheight(), form_canvas.winfo_height()))
            form_canvas.configure(scrollregion=form_canvas.bbox("all"))
        form.bind("<Configure>", resize_form)
        form_canvas.bind("<Configure>", resize_form)
        for key in self.FORM_FIELDS:
            position = core.FIELDS.index(key)
            row = ttk.Frame(form)
            row.pack(fill="both" if key == "comment" else "x", expand=key == "comment", pady=(0, 3))
            label = ttk.Label(row, text=core.LABELS[position])
            label.pack(anchor="w", pady=(0, 1))
            if key == "comment":
                self.comment_editor = CommentEditor(row, self.t)
                self.comment_editor.source.configure(height=5, padx=4, pady=3)
                self.comment_editor.preview.configure(height=5, padx=4, pady=3)
                self.comment_editor.pack(fill="both", expand=True)
                widget = self.comment_editor.source
            elif key in ("category", "device"):
                widget = ttk.Combobox(row, textvariable=self.variables[key], font=("Sans", 11))
                widget.configure(postcommand=lambda widget=widget, key=key:
                                 self.find_combobox_entry(widget, prefix=key == "category"))
                widget.pack(fill="x", ipady=0)
                if key == "device":
                    widget.bind("<<ComboboxSelected>>", self.device_selected)
                else:
                    widget.bind("<<ComboboxSelected>>", lambda e: self.next_field("category"))
            elif key == "anzahl":
                quantity = ttk.Frame(row)
                quantity.pack(fill="x")
                ttk.Button(quantity, text="−", width=3, command=lambda: self.adjust(-1)).pack(side="left")
                widget = ttk.Entry(quantity, textvariable=self.variables[key], font=("Sans", 11))
                widget.pack(side="left", fill="x", expand=True, padx=5, ipady=0)
                ttk.Button(quantity, text="+", width=3, command=lambda: self.adjust(1)).pack(side="left")
            else:
                widget = ttk.Entry(row, textvariable=self.variables[key], font=("Sans", 11))
                widget.pack(fill="x", ipady=0)
            self.rows.append((row, label))
            self.field_widgets[key] = widget
            if key != "comment":
                widget.bind("<Return>", lambda e, key=key: self.field_enter(key))
                widget.bind("<KP_Enter>", lambda e, key=key: self.field_enter(key))
        right = ttk.Frame(pane)
        pane.add(right, weight=1)
        self.selected = tk.StringVar(value=self.t("No media or record selected"))
        selection = ttk.Frame(right)
        selection.pack(fill="x", pady=(3, 8))
        ttk.Label(selection, textvariable=self.selected, wraplength=540).pack(side="left", fill="x", expand=True)
        self.preview_link = ttk.Label(right, foreground="#2563eb", cursor="hand2",
                                      font=("Sans", 11, "underline"), wraplength=720, takefocus=True)
        self.preview_link.bind("<Button-1>", self.open_preview)
        self.preview_link.bind("<Return>", self.open_preview)
        self.preview_link.bind("<space>", self.open_preview)
        self.preview = ImageCanvas(right, translator=self.t)
        self.preview.pack(fill="both", expand=True)
        self.preview.set_navigation(lambda: self.navigate_preview(-1),
                                   lambda: self.navigate_preview(1),
                                   self.select_media_for_json)
        self.preview.bind("<Double-Button-1>", self.preview_double_click)
        self.preview.bind("<Button-3>", self.show_preview_link)
        ttk.Label(right, text=self.t("‹‹ / ›› browse · double-click to open · right-click for the file link"),
                  foreground="#475569").pack(pady=8)
        bottom = ttk.Frame(self.root, padding=(18, 0, 18, 12))
        bottom.pack(fill="x")
        self.status = tk.StringVar(value=self.t("Ready"))
        ttk.Label(bottom, textvariable=self.status).pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=170)
        self.progress.pack(side="right")

    def refresh_profile(self):
        self.profile_picker.configure(values=[p.id for p in self.settings.profiles])
        self.profile_var.set(self.profile.id)
        self.root.title(self.settings.title)
        for key, (row, label) in zip(self.FORM_FIELDS, self.rows):
            i = core.FIELDS.index(key)
            configured = self.profile.labels[i]
            label.configure(text=self.t(core.LABELS[i]) if configured == core.LABELS[i] else configured)
            row.pack_forget()
            if self.profile.labels[i]:
                row.pack(fill="both" if key == "comment" else "x", expand=key == "comment", pady=(0, 3))
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
                widget.configure(background="#fff9c4" if self.search_mode else "#ffffff")
            else:
                widget_class = "TCombobox" if key in ("category", "device") else "TEntry"
                widget.configure(style=f"Search.{widget_class}" if self.search_mode else widget_class)
        if self.search_mode:
            for button in (self.save_button, self.open_json_button, self.open_media_button):
                button.configure(state="disabled")
        self.delete_button.configure(state="normal" if not self.busy and not self.search_mode
                                     and (self.media or self.record_index is not None) else "disabled")
        self.clear_fields_button.configure(state="normal" if not self.busy and not self.search_mode
                                           and (self.media or self.record_index is not None) else "disabled")
        self.repeat_button.configure(state="normal" if self.last_search is not None and not self.busy else "disabled")
        self.search_button.configure(text=self.t("Run search") if self.search_mode else self.t("Search"))

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
                            self.status.set(self.t("Ready"))
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
        message = self.t(str(error))
        self.status.set(message.splitlines()[0] if message else type(error).__name__)
        messagebox.showerror(self.t("ChaosBox"), message, parent=self.root)

    def initialize(self):
        def work():
            return core.ensure_index(self.settings, self.log, self.newindex)
        def done(entries):
            self.newindex = False
            self.status.set(self.t("{count} records · {path}", count=len(entries), path=self.settings.index))
        self.task(work, done)

    def show_preview_link(self, event):
        if self.preview_path is None:
            return "break"
        link = self.preview_path.resolve().as_uri()
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label=link, state="disabled")
        menu.add_separator()
        menu.add_command(label=self.t("Open in editor"), command=self.open_preview_in_editor,
                         state="normal" if self.settings.editor else "disabled")
        menu.add_command(label=self.t("Copy link"), command=lambda: self.copy_preview_link(link))
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
        self.status.set(self.t("File link copied to clipboard."))

    def navigate_preview(self, direction):
        if self.busy:
            return
        if self.individual_paths:
            self.individual_index = (self.individual_index + direction) % len(self.individual_paths)
            self.open_media([self.individual_paths[self.individual_index]], individual=True)
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
        self.sync_needs_reload=False
        self.search_mode, self.before_search = False, None
        self.media, self.box_path, self.records, self.record_index = [], None, [], None
        self.individual_paths, self.individual_index = None, 0
        self.fill({"anzahl": 0})
        self.field_widgets["device"].configure(values=[])
        self.set_preview_path(None)
        self.preview.set_image(None)
        self.selected.set(self.t("No media or record selected"))
        self.refresh_profile()

    def clear_fields(self):
        if self.busy or self.search_mode:
            return
        self.fill({"anzahl": 0})
        if self.individual_paths:
            self.selected.set(self.t("Einzelbild {current}/{total} · Felder geleert",
                                     current=self.individual_index + 1, total=len(self.individual_paths)))

    def adjust(self, change):
        if self.busy or self.search_mode:
            return
        try:
            value = int(self.variables["anzahl"].get() or 0)
            self.variables["anzahl"].set(str(min(2147483647, max(0, value + change))))
        except ValueError:
            self.error(self.t("Quantity must be an integer."))

    def select_profile(self, event=None):
        if self.busy:
            return
        self.profile = self.settings.profile(self.profile_var.get())
        self.last_search = None
        self.clear()
        self.save_state()
        self.status.set(self.t("Search index: {path}", path=self.settings.index))

    def save_state(self):
        if not (self.settings.root / ".installation-id").is_file(): return
        try:
            core.atomic_write(self.state_file, json.dumps({
                "profile": self.profile.id, "media_folders": self.media_folders}))
        except OSError as error:
            self.error(error)

    def media_folder(self):
        folder = Path(self.media_folders.get(self.profile.id, str(self.profile.images)))
        return folder if folder.is_dir() and core.within(folder.resolve(),self.settings.root) else self.profile.images

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

    def media_dialog(self, paths, folder=None, on_select=None):
        folder = folder or self.media_folder()
        dialog = tk.Toplevel(self.root)
        dialog.title(self.t("Bild oder Video auswählen" if on_select else "JPG / MP4 auswählen"))
        dialog.geometry("1000x700")
        dialog.minsize(640, 400)
        # Keep normal window decorations: transient dialogs can lose their
        # maximize button under the Linux window manager.
        dialog.resizable(True, True)
        def maximize():
            if not dialog.winfo_exists():
                return
            try:
                dialog.state("zoomed")
            except tk.TclError:
                try:
                    dialog.attributes("-zoomed", True)
                except tk.TclError:
                    pass
        dialog.after_idle(maximize)
        style = ttk.Style(dialog)
        style.configure("MediaCompact.TButton", padding=(4, 2), width=0, font=("TkDefaultFont", 9))
        style.configure("MediaCompact.TCheckbutton", padding=0, font=("TkDefaultFont", 9))
        def compact_button(parent, **options):
            return ttk.Button(parent, style="MediaCompact.TButton", **options)
        # The window manager may not have mapped this Toplevel yet. Grabbing
        # immediately can abort construction with "window not viewable".
        def grab_when_mapped(event):
            if event.widget is dialog and dialog.winfo_viewable():
                dialog.grab_set()
        dialog.bind("<Map>", grab_when_mapped)
        navigation = ttk.Frame(dialog, padding=(4, 3))
        navigation.pack(fill="x")
        def open_folder(chosen):
            extensions = core.IMPORTS if on_select else core.MEDIA
            def loaded(paths):
                self.remember_media_folder(chosen)
                dialog.destroy()
                self.media_dialog(paths, chosen, on_select=on_select)
            self.task(lambda: sorted(core.files(chosen, extensions, recursive=False),
                                     key=lambda path: path.stat().st_mtime_ns, reverse=True), loaded)
        def change_folder():
            chosen = MediaFolderDialog(dialog, folder, self.t).result
            dialog.grab_set()
            if chosen:
                open_folder(chosen)
        compact_button(navigation, text=self.t("Up"), command=lambda: open_folder(folder.parent)).pack(side="left")
        compact_button(navigation, text=self.t("Folder …"), command=change_folder).pack(side="left")
        ttk.Label(navigation, text=str(folder), width=1, anchor="w").pack(side="left", fill="x", expand=True, padx=3)
        toolbar = navigation
        columns = tk.StringVar(value=str(getattr(self, "media_columns", 4)))
        picker = ttk.Combobox(toolbar, textvariable=columns, values=list(range(1, 11)),
                              state="readonly", width=3)
        picker.pack(side="right")
        ttk.Label(toolbar, text=self.t("Spalten")).pack(side="right", padx=3)
        count = tk.StringVar(value=f"0 / {len(paths)} {self.t('selected')}")
        def changed():
            if on_select and len(grid.selected) > 1:
                grid.selected = {grid.anchor: None}
                grid.redraw()
            count.set(f"{len(grid.selected)} / {len(paths)} {self.t('selected')}")
            open_button.configure(state="normal" if grid.selected else "disabled")
        try:
            subfolders = sorted((path for path in folder.iterdir()
                                 if path.is_dir() and not path.name.startswith('.')),
                                key=lambda path: path.name.casefold())
        except OSError as error:
            subfolders = []
            self.error(error)
        if subfolders:
            folders = ttk.Frame(dialog, padding=(4, 3))
            folders.pack(fill="x")
            listing = tk.Listbox(folders, height=min(4, len(subfolders)), exportselection=False)
            scroll = ttk.Scrollbar(folders, orient="vertical", command=listing.yview)
            listing.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            listing.pack(side="left", fill="x", expand=True)
            for path in subfolders:
                listing.insert("end", path.name + "/")
            def enter_subfolder(event=None):
                selected = listing.curselection()
                if selected:
                    open_folder(subfolders[selected[0]])
                return "break"
            listing.bind("<Double-Button-1>", enter_subfolder)
            listing.bind("<Return>", enter_subfolder)
        grid = MediaGrid(dialog, paths, folder, changed, translator=self.t)
        grid.pack(fill="both", expand=True, padx=4)
        show_comment = tk.BooleanVar(value=getattr(self, "media_show_user_comment", False))
        def change_comment():
            self.media_show_user_comment = show_comment.get()
            grid.set_show_user_comment(self.media_show_user_comment)
        ttk.Checkbutton(toolbar, text=self.t("Metadata"), variable=show_comment,
                        style="MediaCompact.TCheckbutton",
                        command=change_comment).pack(side="right", padx=3)
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
                if on_select:
                    on_select(selected[0])
                else:
                    self.open_media(selected)
        def edit_individual(paths):
            if not paths:
                return
            self.remember_media_folder(paths[0].parent)
            self.individual_paths, self.individual_index = list(paths), 0
            dialog.destroy()
            self.open_media([self.individual_paths[0]], individual=True)
        def external():
            title = self.t("Select an image or video" if on_select else "Select JPG, PNG or MP4 files")
            chosen = FileOpenDialog(dialog, folder, core.IMPORTS, title,
                                    multiple=not on_select, translator=self.t).result
            dialog.grab_set()
            if chosen:
                names = [chosen] if on_select else chosen
                self.remember_media_folder(Path(names[0]).parent)
                dialog.destroy()
                if on_select:
                    on_select(Path(names[0]))
                else:
                    self.open_media([Path(name) for name in names])
        buttons = ttk.Frame(dialog, padding=(4, 3))
        buttons.pack(fill="x")
        compact_button(buttons, text=self.t("Other …"), command=external).pack(side="left")
        if not on_select:
            compact_button(buttons, text=self.t("All"), command=grid.select_all).pack(side="left", padx=3)
        compact_button(buttons, text=self.t("None"), command=grid.clear_selection).pack(side="left")
        ttk.Label(buttons, textvariable=count).pack(side="left", padx=3)
        open_button = compact_button(buttons, text=self.t("Select") if on_select else self.t("Edit all"),
                                 command=open_selected, state="disabled")
        open_button.pack(side="right", padx=3)
        delete_button = None
        if not on_select:
            delete_button = compact_button(buttons, text=self.t("Del Selection"),
                command=lambda: self.delete_media_selection(grid.selection(), dialog), state="disabled")
            delete_button.pack(side="left", padx=3)
        individual_button = None
        if not on_select:
            individual_button = compact_button(buttons, text=self.t("Individual"),
                                           command=lambda: edit_individual(grid.selection()), state="disabled")
            individual_button.pack(side="right", padx=3)
        def selection_changed():
            changed()
            if delete_button is not None:
                delete_button.configure(state="normal" if grid.selected else "disabled")
            if individual_button is not None:
                individual_button.configure(state="normal" if grid.selected else "disabled")
        grid.changed = selection_changed
        dialog.bind("<Return>", open_selected)
        grid.canvas.bind("<Double-Button-1>", lambda event: grid.double_click(event, open_selected))
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        grid.canvas.focus_set()
        self.register_media_drop(dialog, dialog, on_select)
        self.register_media_drop(grid.canvas, dialog, on_select)

    def delete_media_selection(self, paths, dialog):
        if self.busy or not paths:
            return
        paths = list(paths)
        # Folder browsing may show files outside this dataset. Never delete them.
        if any(path.is_symlink() or not core.within(path.resolve(), self.profile.images.resolve())
               for path in paths):
            messagebox.showerror(self.t("Del Selection"),
                self.t("Only media inside the current dataset can be deleted."), parent=dialog)
            return
        if not messagebox.askyesno(self.t("Del Selection"), self.t(
                "Remove {count} selected media file(s) from the dataset and their original disk locations? "
                "Recovery copies remain in the local trash and sync history. "
                "Synchronize to propagate the deletion.", count=len(paths)), parent=dialog):
            return
        dialog.destroy()
        def work():
            try:
                for path in paths:
                    core.delete_entry(path)
                    if path.exists():
                        raise ValueError(f"Media could not be removed; resolve synchronization conflicts: {path}")
            finally:
                core.update_index(self.settings, paths, self.log)
        def refresh():
            self.clear()
            self.choose_media()
        def done(_):
            refresh()
            self.status.set(self.t("Media deleted locally; synchronize to propagate."))
        def failed(error):
            refresh()
            self.error(error)
        self.task(work, done, failed)

    def set_preview_path(self, path):
        self.preview_path = path
        self.preview.set_navigation(lambda: self.navigate_preview(-1),
                                    lambda: self.navigate_preview(1),
                                    self.select_media_for_json,
                                    enabled=bool(self.media or self.box_path))
        if path is None:
            self.preview_link.pack_forget()
        else:
            self.preview_link.configure(text=self.t("Preview: {path}", path=path.resolve()))
            self.preview_link.pack(before=self.preview, anchor="w", pady=(0, 8))

    def select_media_for_json(self):
        if self.busy or self.box_path is None or self.search_mode:
            return
        paths = sorted(core.files(self.profile.images, core.IMPORTS),
                       key=lambda path: path.stat().st_mtime_ns, reverse=True)
        self.media_dialog(paths, self.profile.images, on_select=self.use_media_for_json)

    def use_media_for_json(self, path):
        if self.busy or self.box_path is None or self.search_mode:
            return
        path = Path(path)
        if path.suffix.lower() not in core.IMPORTS:
            self.error(self.t("Select JPG, PNG or MP4 media."))
            return
        # Keep the currently displayed JSON fields as the metadata for this media.
        values = self.values()
        self.media, self.box_path, self.records, self.record_index = [path], None, [], None
        self.media_records = [dict(values)]
        self.media_baseline = dict(values)
        self.media_edit_mode, self.media_differing_fields = "replace", []
        self.selected.set(self.t("Selected media: {name} · JSON fields retained", name=path.name))
        self.set_preview_path(path)
        self.preview.set_image(None)
        def done(result):
            image, warning = result
            self.preview.set_image(image)
            if warning:
                self.status.set(warning)
        self.task(lambda: self.preview_result(path), done)

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
                self.error(self.t("Could not open: {path}", path=path))
        self.root.after(250, wait_for_open)

    def preview_result(self, path):
        if path is None:
            return None, ""
        try:
            return core.load_preview(path), ""
        except Exception as error:
            return None, self.t("Preview unavailable: {error}", error=error)

    def open_media(self, paths, individual=False, preserve_fields=False):
        if any(path.suffix.lower() not in core.IMPORTS for path in paths):
            self.error(self.t("Select JPG, PNG or MP4 files."))
            return
        if not individual:
            self.individual_paths, self.individual_index = None, 0
        def work():
            records = core.read_metadata_batch(list(dict.fromkeys(paths)), self.log)
            return records, self.preview_result(paths[0])
        def done(result):
            records, (image, warning) = result
            differing = core.differing_metadata_fields(records)
            mode = "replace"
            if differing and not preserve_fields:
                labels = [self.profile.labels[core.FIELDS.index(field)] or core.LABELS[core.FIELDS.index(field)]
                          for field in differing]
                mode = MetadataModeDialog(self.root, labels).result
                if mode is None:
                    return
            self.sync_needs_reload=False
            self.media_records = records
            self.media_edit_mode, self.media_differing_fields = mode, differing
            values = records[0] if len(records) == 1 else core.common_metadata(records)
            self.media_baseline = dict(values)
            self.media, self.box_path, self.records, self.record_index = list(dict.fromkeys(paths)), None, [], None
            self.field_widgets["device"].configure(values=[])
            if not preserve_fields:
                self.fill(values)
            self.set_preview_path(paths[0])
            self.preview.set_image(image)
            if self.individual_paths:
                self.selected.set(self.t("Einzelbild {current}/{total} · {name}", current=self.individual_index + 1,
                                         total=len(self.individual_paths), name=paths[0].name))
            else:
                self.selected.set(self.t("{count} file(s) selected", count=len(self.media)))
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
            self.status.set(self.t("{count} poster(s) saved: {path}", count=len(outputs), path=outputs[0].parent))
            messagebox.showinfo(self.t("Poster"), self.t("Posters saved:\n{paths}", paths="\n".join(map(str, outputs))), parent=self.root)
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
                name = FileOpenDialog(self.root, self.profile.data, {".json"},
                                      self.t("Open JSON"), translator=self.t, alphabetical=True).result
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
            self.sync_needs_reload=False
            self.individual_paths, self.individual_index = None, 0
            self.media, self.box_path, self.records = [], path, records
            self.record_index = min(selected or 0, len(records) - 1)
            if selected is None and len(records) > 1 and not self.profile.labels[2]:
                dialog = tk.Toplevel(self.root)
                dialog.title(self.t("Select JSON record"))
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
                ttk.Button(dialog, text=self.t("Open"), command=choose).pack(pady=12)
                listing.bind("<Double-Button-1>", choose)
                listing.bind("<Return>", choose)
                dialog.protocol("WM_DELETE_WINDOW", choose)
                return
            self.fill_record()
        self.task(work, done)

    def fill_record(self):
        if getattr(self,"sync_needs_reload",False):
            messagebox.showinfo("Reload entry", "Reopen the JSON file after synchronization.")
            return
        index = self.record_index
        values = core.normalized(self.records[index])
        values["box"] = values["box"] or self.box_path.stem
        self.fill(values)
        self.device_order = sorted(range(len(self.records)), key=lambda i: str(self.records[i].get("device", "")).casefold())
        self.field_widgets["device"].configure(values=[str(self.records[i].get("device", "")) or "(no device)" for i in self.device_order])
        self.selected.set(self.t("{path} — record {current}/{total}", path=self.box_path,
                                 current=index + 1, total=len(self.records)))
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
        if getattr(self,"sync_needs_reload",False):
            messagebox.showinfo("Reload entry", "Shared data may have changed. Reopen the entry before saving. Your form values remain here.")
            return
        if self.busy or self.search_mode:
            return
        try:
            values = self.values()
            if not self.media and not values["box"].strip() and not self.profile.labels[0]:
                name = simpledialog.askstring(self.t("Save JSON"), self.t("Box name:"), parent=self.root)
                if name is None:
                    return
                values["box"] = name
                self.variables["box"].set(name)
            batch_records = ([core.edited_metadata(original, values, self.media_baseline,
                              mode=getattr(self, "media_edit_mode", "auto"),
                              differing_fields=getattr(self, "media_differing_fields", ()))
                              for original in self.media_records] if len(self.media) > 1 else None)
            record = core.validate_record(values)
            expected_records = batch_records if batch_records is not None else ([record] if self.media else [])
            original_records = list(getattr(self, "media_records", ()))
            path = self.box_path if self.box_path else self.profile.data / core.box_filename(record["box"]) if not self.media else None
        except Exception as error:
            self.error(error)
            return
        if len(self.media) > 1 and not messagebox.askokcancel(
                self.t("Save"), self.t("Attention! All selected Images get this texts"),
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
            saved_result, warning = result
            self.profile = self.settings.profile(profile.id)
            if self.individual_paths and saved_result[0] == "media":
                _, saved_paths, (image, preview_warning), records = saved_result
                comment_changed = bool(original_records) and str(expected_records[0].get("comment", "")) != str(original_records[0].get("comment", ""))
                comment_verified = bool(records) and str(records[0].get("comment", "")) == str(expected_records[0].get("comment", ""))
                path = saved_paths[0]
                self.individual_paths[self.individual_index] = path
                self.media, self.box_path, self.records, self.record_index = saved_paths, None, [], None
                self.media_records = records
                self.media_baseline = dict(records[0])
                self.refresh_profile()
                self.fill(records[0])
                self.set_preview_path(path)
                self.preview.set_image(image)
                self.selected.set(self.t("Einzelbild {current}/{total} · {name}", current=self.individual_index + 1,
                                         total=len(self.individual_paths), name=path.name))
                diagnostic = self.t("Comment diagnostic: changed in form = {changed}; read-back matches = {verified}.",
                                     changed=self.t("yes") if comment_changed else self.t("no"),
                                     verified=self.t("yes") if comment_verified else self.t("no"))
                self.status.set(warning or preview_warning or (self.t("Einzelbild gespeichert. Sie können weiterblättern oder die Felder leeren.") + " · " + diagnostic))
                return
            diagnostic = ""
            if saved_result[0] == "media":
                _, _, _, saved_records = saved_result
                comment_changed = any(
                    index < len(original_records) and
                    str(expected.get("comment", "")) != str(original_records[index].get("comment", ""))
                    for index, expected in enumerate(expected_records))
                comment_verified = len(saved_records) == len(expected_records) and all(
                    str(actual.get("comment", "")) == str(expected.get("comment", ""))
                    for actual, expected in zip(saved_records, expected_records))
                diagnostic = " · " + self.t("Comment diagnostic: changed in form = {changed}; read-back matches = {verified}.",
                                             changed=self.t("yes") if comment_changed else self.t("no"),
                                             verified=self.t("yes") if comment_verified else self.t("no"))
            self.clear()
            self.status.set(warning or (self.t("Saved locally. Use Upload to synchronize manually.") + diagnostic))
        def failed(error):
            if isinstance(error, core.BatchError):
                self.media = error.selection
                self.set_preview_path(self.media[0])
            self.profile = self.settings.profile(profile.id)
            self.refresh_profile()
            self.error(error)
        self.task(work, done, failed)

    def delete_whole_box(self):
        if getattr(self,"sync_needs_reload",False):
            messagebox.showinfo("Reload entry", "Reopen the entry after synchronization before deleting.")
            return
        if self.busy or not self.box_path:return
        if not messagebox.askyesno("Delete box", "Delete all JSON records in this box? Media remain unchanged. The deletion propagates on synchronization."):return
        path=self.box_path
        def work():
            core.delete_box(path)
            core.update_index(self.settings,[path],self.log)
        self.task(work,lambda _:(self.clear(),self.status.set("Box deleted locally; synchronize to propagate.")))

    def show_sync_conflicts(self):
        if self.busy:return
        store=core.sync_store(self.profile)
        choices=[op for versions in store.heads().values() if len(versions)>1 for op in versions]
        if not choices:
            messagebox.showinfo("Synchronization", "No synchronization conflicts.");return
        dialog=tk.Toplevel(self.root);dialog.title("Choose the version to keep");dialog.geometry("820x520")
        listing=tk.Listbox(dialog,height=8);listing.pack(fill="x",padx=10,pady=10)
        preview=tk.Text(dialog,wrap="word");preview.pack(fill="both",expand=True,padx=10)
        for op in choices:listing.insert("end",op['action']+" · "+op['path']+" · "+op['revision'])
        def changed(_=None):
            indices=listing.curselection()
            if indices:
                preview.delete('1.0','end');preview.insert('1.0',json.dumps(choices[indices[0]],ensure_ascii=False,indent=2))
        listing.bind('<<ListboxSelect>>',changed)
        def resolve():
            indices=listing.curselection()
            if not indices:return
            op=choices[indices[0]]
            if not messagebox.askyesno("Resolve conflict", "Keep this version and supersede the other versions of this object? Journal history remains available.",parent=dialog):return
            def work():
                with store.locked():store.resolve(op['id'],op['revision'])
                core.build_index(self.settings,self.log)
            def done(_):
                self.sync_needs_reload=bool(self.media or self.box_path);dialog.destroy();self.status.set("Conflict resolved locally; synchronize to propagate.")
            self.task(work,done)
        ttk.Button(dialog,text="Keep selected version",command=resolve).pack(pady=10)

    def delete_current(self):
        if getattr(self,"sync_needs_reload",False):
            messagebox.showinfo("Reload entry", "Reopen the entry after synchronization before deleting.")
            return
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
            self.status.set(self.t("Deleted: {path}{warning}", path=path, warning=warning))
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
            self.status.set(self.t("Category: literal prefix; other fields: regular expressions. AND combined. Escape cancels."))
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
        self.status.set(self.t("Search cancelled"))

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
                self.status.set(self.t("No matches. Change the search fields and search again."))
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
            self.error(self.t("The source profile is no longer configured. Restart with --newindex."))
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
        dialog.title(self.t("{count} matches", count=len(hits)))
        dialog.geometry("1050x500")
        dialog.transient(self.root)
        dialog.grab_set()
        fields = [("anzahl", "Quantity"), ("device", "Device"), ("category", "Category"),
                  ("package", "Package")]
        columns = [key for key, _ in fields] + ["path"]
        table = ttk.Treeview(dialog, columns=columns, show="headings", selectmode="browse")
        for key, label in fields + [("path", "Path")]:
            table.heading(key, text=self.t(label))
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
        ttk.Button(dialog, text=self.t("Open"), command=select).pack(side="right", padx=12, pady=12)
        ttk.Button(dialog, text=self.t("Keep searching"), command=dialog.destroy).pack(side="right", pady=12)
        table.bind("<Double-Button-1>", select)
        table.bind("<Return>", select)

    def setup_dialog(self):
        if self.busy:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(self.t("Edit setup.ini"))
        dialog.geometry("900x680")
        dialog.transient(self.root)
        dialog.grab_set()
        setup_path = self.settings.active_path
        ttk.Label(dialog, text=str(setup_path), padding=10).pack(anchor="w")
        text = tk.Text(dialog, wrap="none", undo=True, font=("Monospace", 11))
        text.pack(fill="both", expand=True, padx=10)
        setup_text = core.english_setup(setup_path.read_text(encoding="utf-8"))
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
                messagebox.showerror(self.t("Setup"), str(error), parent=dialog)
        ttk.Button(dialog, text=self.t("Save"), command=save).pack(side="right", padx=10, pady=10)
        ttk.Button(dialog, text=self.t("Cancel"), command=dialog.destroy).pack(side="right", pady=10)
        def initialize():
            dialog.destroy()
            self.initialize_sync()
        ttk.Button(dialog, text=self.t("Initialize sync from local files"), command=initialize).pack(side="left", padx=10, pady=10)

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
        dialog.title(self.t("Select text snippet"))
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
                self.status.set(self.t("Text snippet copied. Paste with Ctrl+V."))
                dialog.destroy()
        names.bind("<<ListboxSelect>>", show)
        names.bind("<Double-Button-1>", copy)
        ttk.Button(right, text=self.t("Copy to clipboard"), command=copy).pack(side="right", pady=(10, 0))
        if snippets:
            names.selection_set(0)
            show()

    def initialize_sync(self):
        if self.busy:
            return
        if not messagebox.askyesno(self.t("Initialize synchronization"),
                self.t("Use only the saved local JSON and media files in the selected collection as the new sync baseline?\n\nServer records and media missing locally will be marked deleted. Unsaved form edits are excluded. A local recovery backup will be kept. Other clients must synchronize before editing.")):
            return
        self.start_upload(initialize=True)

    def start_upload(self, initialize=False):
        if self.busy:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(self.t("Manual upload"))
        dialog.geometry("900x530")
        dialog.transient(self.root)
        dialog.grab_set()
        text = tk.Text(dialog, wrap="word", font=("Monospace", 10))
        text.pack(fill="both", expand=True, padx=12, pady=12)
        self.upload_window, self.upload_text = dialog, text
        self.uploading = True
        self.cancel_upload.clear()
        cancel = ttk.Button(dialog, text=self.t("Cancel upload"), command=self.cancel_upload.set)
        cancel.pack(side="right", padx=12, pady=(0, 12))
        def close():
            if self.uploading:
                self.cancel_upload.set()
                text.insert("end", self.t("Cancellation requested …\n"))
            else:
                dialog.destroy()
                self.upload_window = self.upload_text = None
        dialog.protocol("WM_DELETE_WINDOW", close)
        def finish(error=None):
            self.sync_needs_reload=bool(self.media or self.box_path)
            self.uploading = False
            cancel.configure(text=self.t("Close"), command=close)
            if error:
                text.insert("end", f"\n{error}\n")
                dialog.title(self.t("Upload cancelled" if isinstance(error, core.Cancelled) else "Upload failed"))
            else:
                self.sync_needs_reload = bool(self.media or self.box_path)
                dialog.title(self.t("Upload completed"))
            text.see("end")
        def work():
            self.log("Loading synchronization settings …")
            self.settings.reload()
            core.upload(self.settings, self.profile, self.cancel_upload, self.log, initialize=initialize)
        self.task(work, lambda _: finish(), finish)

    def close(self):
        if self.busy:
            if self.uploading:
                self.cancel_upload.set()
                self.status.set(self.t("Cancelling upload. Close again after cancellation completes."))
            else:
                self.status.set(self.t("Please wait for the current file operation to finish before closing."))
            return
        self.closing = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="ChaosBox desktop editor")
    parser.add_argument("directory", nargs="?", type=Path, help="Installation directory (default: ~/ChaosBox)")
    parser.add_argument("--state-dir", type=Path, help="Override installation-local .state directory")
    parser.add_argument("--setup", type=Path)
    parser.add_argument("--newindex", action="store_true", help="Rebuild the shared search index for all profiles")
    parser.add_argument("--check", action="store_true", help="Check dependencies without opening a window or changing data")
    args = parser.parse_args()
    installdir = args.directory or Path.home() / "ChaosBox"
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
        messagebox.showerror(translate("ChaosBox startup", getattr(settings, "language", "en")), str(error), parent=root)
        root.destroy()
        raise SystemExit(1)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
