#!/usr/bin/env python3
"""ChaosBox desktop editor for Ubuntu. All Tk operations run on the UI thread."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from PIL import Image, ImageTk

import core


def copy_image_clipboard(image):
    """Publish actual PNG pixels, with xclip retaining clipboard ownership."""
    executable = shutil.which("xclip")
    if executable is None:
        raise RuntimeError("Image copy requires xclip. Install it with: sudo apt install xclip")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    subprocess.run([executable, "-selection", "clipboard", "-target", "image/png", "-in"],
                   input=buffer.getvalue(), stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True, timeout=15)


USER_COMMENT_PREVIEW_LIMIT = 60


def user_comment_preview(path):
    # Keep captions on a predictable number of lines without changing metadata.
    metadata = core.read_metadata(path)
    fields = [" ".join(str(metadata.get(key) or "").split())
              for key in ("box", "category", "comment")]
    return " | ".join(fields)[:USER_COMMENT_PREVIEW_LIMIT] if any(fields) else ""


class ImageCanvas(tk.Canvas):
    def __init__(self, parent, zoomable=False, **kwargs):
        super().__init__(parent, background="#101820", highlightthickness=0, **kwargs)
        self.original = None
        self.photo = None
        self.zoom, self.x, self.y = 1., 0., 0.
        self.drag = None
        self.pending = None
        self.zoomable = zoomable
        self.bind("<Configure>", self.resize)
        if zoomable:
            self.bind("<Button-4>", lambda e: self.scale(1.2, e.x, e.y))
            self.bind("<Button-5>", lambda e: self.scale(1 / 1.2, e.x, e.y))
            self.bind("<MouseWheel>", lambda e: self.scale(1.2 if e.delta > 0 else 1 / 1.2, e.x, e.y))
            self.bind("<ButtonPress-1>", self.start_drag)
            self.bind("<B1-Motion>", self.move_drag)
            self.bind("<Double-Button-1>", lambda e: self.reset() if self.zoom > 1.01 else self.scale(2.5, e.x, e.y))

    def set_image(self, image):
        self.original = image
        self.zoom = 1.
        self.resize()

    def fit(self):
        if self.original is None:
            return 1.
        return min(max(1, self.winfo_width()) / self.original.width,
                   max(1, self.winfo_height()) / self.original.height)

    def resize(self, event=None):
        self.zoom = 1.
        if self.original is not None:
            scale = self.fit()
            self.x = (self.winfo_width() - self.original.width * scale) / 2
            self.y = (self.winfo_height() - self.original.height * scale) / 2
        self.schedule()

    def reset(self):
        self.resize()

    def scale(self, factor, x=None, y=None):
        if self.original is None:
            return
        x = self.winfo_width() / 2 if x is None else x
        y = self.winfo_height() / 2 if y is None else y
        new = max(1., min(8., self.zoom * factor))
        factor = new / self.zoom
        self.x = x - (x - self.x) * factor
        self.y = y - (y - self.y) * factor
        self.zoom = new
        self.constrain()
        self.schedule()

    def constrain(self):
        if self.original is None:
            return
        scale = self.fit() * self.zoom
        w, h = self.original.width * scale, self.original.height * scale
        vw, vh = self.winfo_width(), self.winfo_height()
        self.x = (vw - w) / 2 if w <= vw else max(vw - w, min(0., self.x))
        self.y = (vh - h) / 2 if h <= vh else max(vh - h, min(0., self.y))

    def start_drag(self, event):
        self.drag = (event.x, event.y)

    def move_drag(self, event):
        if self.drag:
            self.x += event.x - self.drag[0]
            self.y += event.y - self.drag[1]
            self.drag = (event.x, event.y)
            self.constrain()
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
        if self.original is None:
            self.create_text(max(1, self.winfo_width()) / 2, max(1, self.winfo_height()) / 2,
                             text="Select an image or video", fill="#cbd5e1", font=("Sans", 15))
            return
        scale = self.fit() * self.zoom
        if scale <= 0:
            return
        vw, vh = max(1, self.winfo_width()), max(1, self.winfo_height())
        # Render only the visible region, keeping memory bounded even at 8× zoom.
        left, top = max(0., -self.x / scale), max(0., -self.y / scale)
        right = min(self.original.width, (vw - self.x) / scale)
        bottom = min(self.original.height, (vh - self.y) / scale)
        if right <= left or bottom <= top:
            return
        width, height = max(1, round((right - left) * scale)), max(1, round((bottom - top) * scale))
        rendered = self.original.resize((width, height), Image.Resampling.LANCZOS, box=(left, top, right, bottom))
        self.photo = ImageTk.PhotoImage(rendered)
        self.create_image(max(0, self.x), max(0, self.y), anchor="nw", image=self.photo)
        if self.zoomable:
            self.create_text(18, 18, anchor="nw", text=f"{self.zoom:.1f}×", fill="white", font=("Sans", 12, "bold"))


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
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="thumbnails")
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
                    scale = self.thumb_size / max(image.size)
                    thumbnail = image.resize((max(1, round(image.width * scale)),
                                              max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
                    photo = ImageTk.PhotoImage(thumbnail, master=canvas)
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
                    self.comment_future = self.worker.submit(user_comment_preview, self.paths[index])
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
        self.cache.clear()
        self.cache_sizes.clear()
        self.comments.clear()
        self.worker.shutdown(wait=False, cancel_futures=True)
        self.future = None
        self.comment_future = None
        super().destroy()
        # Canvas.master points back to this frame even after Tk destruction.
        self.canvas = None


class App:
    def __init__(self, root, settings):
        self.root, self.settings = root, settings
        self.window_icon = tk.PhotoImage(file=str(Path(__file__).with_name("chaosbox.png")))
        root.iconphoto(True, self.window_icon)
        root.title("ChaosBox — Desktop")
        root.geometry("1240x850")
        root.minsize(850, 620)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chaosbox")
        self.events = queue.Queue()
        self.busy, self.closing = False, False
        self.media, self.box_path, self.records, self.record_index = [], None, [], None
        self.created = ""
        self.preview_path = None
        self.fullscreen_window = None
        self.search_mode, self.before_search, self.last_search = False, None, None
        self.cancel_upload = threading.Event()
        self.uploading = False
        self.upload_window, self.upload_text = None, None
        self.controls, self.field_widgets, self.rows = [], {}, []
        self.variables = {key: tk.StringVar() for key in core.FIELDS if key != "comment"}
        self.state_file = settings.state_dir / "desktop-state.json"
        try:
            state = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            state = {}
        self.media_folders = state.get("media_folders", {})
        self.profile = settings.profile(state.get("profile", settings.default))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TButton", padding=(10, 7))
        style.configure("Title.TLabel", font=("Sans", 18, "bold"))
        style.configure("TLabel", font=("Sans", 11))
        self.build_ui()
        self.refresh_profile()
        self.clear()
        self.root.bind("<Control-s>", lambda e: self.save())
        self.root.bind("<Control-o>", lambda e: self.choose_media())
        self.root.bind("<Control-f>", lambda e: self.search_clicked())
        self.root.bind("<Escape>", lambda e: self.cancel_search())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self.drain)
        self.root.after(150, self.initialize)

    def button(self, parent, text, command, **pack):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(**pack)
        self.controls.append(button)
        return button

    def build_ui(self):
        header = ttk.Frame(self.root, padding=(18, 14))
        header.pack(fill="x")
        ttk.Label(header, text="ChaosBox", style="Title.TLabel").pack(side="left")
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
        self.cancel_search_button = self.button(actions, "Cancel search", self.cancel_search, side="left", padx=6)
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
        for position, key in enumerate(core.FIELDS):
            row = ttk.Frame(form)
            row.pack(fill="x", pady=(0, 13))
            label = ttk.Label(row, text=core.LABELS[position])
            label.pack(anchor="w", pady=(0, 4))
            if key == "comment":
                widget = tk.Text(row, height=13, wrap="word", font=("Sans", 11), undo=True, padx=8, pady=8)
                widget.pack(fill="both", expand=True)
            elif key in ("category", "device"):
                widget = ttk.Combobox(row, textvariable=self.variables[key], font=("Sans", 11))
                widget.pack(fill="x", ipady=4)
                if key == "device":
                    widget.bind("<<ComboboxSelected>>", self.device_selected)
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
        right = ttk.Frame(pane)
        pane.add(right, weight=1)
        self.selected = tk.StringVar(value="No media or record selected")
        ttk.Label(right, textvariable=self.selected, wraplength=720).pack(anchor="w", pady=(3, 8))
        self.preview = ImageCanvas(right)
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Double-Button-1>", self.fullscreen)
        ttk.Label(right, text="Double-click an image for full-screen view · mouse wheel to zoom", foreground="#475569").pack(pady=8)
        bottom = ttk.Frame(self.root, padding=(18, 0, 18, 12))
        bottom.pack(fill="x")
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bottom, textvariable=self.status).pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=170)
        self.progress.pack(side="right")

    def refresh_profile(self):
        self.profile_picker.configure(values=[p.id for p in self.settings.profiles])
        self.profile_var.set(self.profile.id)
        self.root.title(f"{self.profile.title} — ChaosBox Desktop")
        for i, (row, label) in enumerate(self.rows):
            label.configure(text=self.profile.labels[i])
            row.pack_forget()
            if self.profile.labels[i]:
                row.pack(fill="x", pady=(0, 13))
        self.field_widgets["category"].configure(values=[] if self.search_mode else self.profile.categories)
        self.update_controls()

    def update_controls(self):
        for control in self.controls:
            control.configure(state="disabled" if self.busy else "normal")
        if not self.busy:
            self.profile_picker.configure(state="readonly")
        for widget in self.field_widgets.values():
            widget.configure(state="disabled" if self.busy else "normal")
        if self.search_mode:
            for button in (self.save_button, self.open_json_button, self.open_media_button):
                button.configure(state="disabled")
        self.cancel_search_button.configure(state="normal" if self.search_mode and not self.busy else "disabled")
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
        profile = self.profile
        def work():
            for directory in (profile.images, profile.data, profile.index):
                directory.mkdir(parents=True, exist_ok=True)
            warnings = core.migrate_media(profile, self.log)
            entries = core.build_index(profile, self.log)
            return warnings, len(entries)
        def done(result):
            warnings, count = result
            self.status.set(f"{count} records · {self.settings.path}")
            if warnings:
                messagebox.showwarning("Some files were not organized", "\n".join(warnings), parent=self.root)
        self.task(work, done)

    def values(self):
        data = {key: variable.get() for key, variable in self.variables.items()}
        data["comment"] = self.field_widgets["comment"].get("1.0", "end-1c")
        data["created"] = self.created
        return data

    def fill(self, values):
        for key, variable in self.variables.items():
            variable.set(str(values.get(key, "")))
        self.field_widgets["comment"].delete("1.0", "end")
        self.field_widgets["comment"].insert("1.0", str(values.get("comment", "")))
        self.created = values.get("created", "")

    def clear(self):
        if self.busy:
            return
        self.search_mode, self.before_search = False, None
        self.media, self.box_path, self.records, self.record_index = [], None, [], None
        self.fill({"anzahl": 0})
        self.field_widgets["device"].configure(values=[])
        self.preview_path = None
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
        self.initialize()

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
        self.task(lambda: core.files(folder, core.MEDIA, recursive=False),
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
        dialog.grab_set()
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
        ttk.Label(toolbar, text="Click to select multiple files · Shift-click selects a range").pack(side="left")
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
        ttk.Checkbutton(toolbar, text="Show UserComment", variable=show_comment,
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
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        grid.canvas.focus_set()

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
            records = [core.read_metadata(path) for path in dict.fromkeys(paths)]
            return records, self.preview_result(paths[0])
        def done(result):
            records, (image, warning) = result
            self.media_records = records
            values = records[0] if len(records) == 1 else core.common_metadata(records)
            self.media_baseline = dict(values)
            self.media, self.box_path, self.records, self.record_index = list(dict.fromkeys(paths)), None, [], None
            self.field_widgets["device"].configure(values=[])
            self.fill(values)
            self.preview_path = paths[0]
            self.preview.set_image(image)
            self.selected.set(f"{len(self.media)} file(s) selected — preview: {paths[0].name}")
            if warning:
                self.status.set(warning)
        self.task(work, done)

    def make_poster(self):
        if self.busy or self.search_mode:
            return
        paths, profile, settings = list(self.media), self.profile, self.settings.poster
        values = self.values()
        baseline = getattr(self, "media_baseline", {})
        additions = {field: values.get(field, "") if values.get(field, "") != str(baseline.get(field, ""))
                     else "" for field in ("box", "comment")}
        def done(path):
            self.status.set(f"Poster saved: {path}")
            messagebox.showinfo("Poster", f"Poster saved:\n{path}", parent=self.root)
        self.task(lambda: core.create_poster(paths, profile, settings, self.log,
                                            title=values.get("category", ""),
                                            box_addition=additions["box"],
                                            comment_addition=additions["comment"],
                                            setup_dir=self.settings.path.parent), done)

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
            entries = core.build_index(self.profile, self.log)
            preview = core.related_media(core.Entry(path, record, False, index), entries)
            return preview, self.preview_result(preview)
        def done(result):
            preview, (image, warning) = result
            self.preview_path = preview
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
            batch_records = ([core.edited_metadata(original, values, self.media_baseline)
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
                    saved = core.save_batch(media, profile, record, self.settings.limit, self.log, records=batch_records)
                except core.BatchError as error:
                    if error.completed:
                        try:
                            previous = [p for p in media[:error.completed]
                                        if core.within(p, profile.images) and p.suffix.lower() in core.MEDIA]
                            core.update_index(profile, previous + error.selection[:error.completed], self.log)
                        except Exception as index_error:
                            self.log(f"Search index update failed: {index_error}")
                    raise
                result = ("media", saved, self.preview_result(saved[0]))
            else:
                records, index = core.save_box(path, record, selected)
                result = ("json", records, index)
            warning = ""
            try:
                previous = [p for p in media if core.within(p, profile.images) and p.suffix.lower() in core.MEDIA]
                core.update_index(profile, previous + saved if media else [path], self.log)
            except Exception as error:
                warning = f"Saved locally, but the search index could not be updated: {error}"
            return result, warning
        def done(result):
            data, warning = result
            self.created = record["created"]
            self.profile = self.settings.profile(profile.id)
            self.refresh_profile()
            if data[0] == "media":
                self.media = data[1]
                self.media_records = batch_records or [record]
                self.media_baseline = (core.common_metadata(self.media_records) if len(self.media_records) > 1
                                       else dict(self.media_records[0]))
                self.fill(self.media_baseline)
                self.preview_path = self.media[0]
                self.preview.set_image(data[2][0])
                self.selected.set(f"Saved {len(self.media)} file(s) locally — {self.media[0]}")
            else:
                self.box_path, self.records, self.record_index = path, data[1], data[2]
                self.fill(core.normalized(self.records[self.record_index]))
                self.device_order = sorted(range(len(self.records)), key=lambda i: str(self.records[i].get("device", "")).casefold())
                self.field_widgets["device"].configure(values=[str(self.records[i].get("device", "")) or "(no device)" for i in self.device_order])
                self.selected.set(f"Saved locally: {path}")
            self.status.set(warning or "Saved locally. Use Upload to synchronize manually.")
        def failed(error):
            if isinstance(error, core.BatchError):
                self.media = error.selection
                self.preview_path = self.media[0]
            self.profile = self.settings.profile(profile.id)
            self.refresh_profile()
            self.error(error)
        self.task(work, done, failed)

    def search_clicked(self):
        if self.busy:
            return
        if not self.search_mode:
            self.before_search = self.values()
            self.search_mode = True
            self.fill({})
            self.field_widgets["category"].configure(values=[])
            self.field_widgets["device"].configure(values=[])
            self.update_controls()
            self.status.set("Enter regular expressions; fields are combined with AND. Escape cancels.")
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
            entries = core.build_index(self.profile, self.log)
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
        self.refresh_profile()
        if hit.media:
            self.open_media([hit.source])
        else:
            self.load_json(hit.source, hit.index)

    def results_dialog(self, hits):
        dialog = tk.Toplevel(self.root)
        dialog.title(f"{len(hits)} matches")
        dialog.geometry("1050x500")
        dialog.transient(self.root)
        dialog.grab_set()
        table = ttk.Treeview(dialog, columns=("box", "device", "alias", "path"), show="headings", selectmode="browse")
        for key in ("box", "device", "alias", "path"):
            table.heading(key, text=key.title())
            table.column(key, width=440 if key == "path" else 150)
        table.pack(fill="both", expand=True, padx=12, pady=12)
        for i, hit in enumerate(hits):
            table.insert("", "end", iid=str(i), values=(hit.data.get("box", ""), hit.data.get("device", ""),
                                                        hit.data.get("alias", ""), str(hit.source)))
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
        ttk.Label(dialog, text=str(self.settings.path), padding=10).pack(anchor="w")
        text = tk.Text(dialog, wrap="none", undo=True, font=("Monospace", 11))
        text.pack(fill="both", expand=True, padx=10)
        setup_text = self.settings.path.read_text(encoding="utf-8")
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
                self.settings.save_text(text.get("1.0", "end-1c") + "\n")
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

    def fullscreen(self, event=None):
        if self.fullscreen_window is not None and self.fullscreen_window.winfo_exists():
            self.fullscreen_window.lift()
            self.fullscreen_window.focus_set()
            return
        if self.busy or self.preview.original is None or self.preview_path is None or self.preview_path.suffix.lower() == ".mp4":
            return
        path = self.preview_path
        self.task(lambda: core.load_preview(path, maximum=(12000, 12000)), self.show_fullscreen)

    def show_fullscreen(self, image):
        dialog = tk.Toplevel(self.root)
        self.fullscreen_window = dialog
        dialog.title("Image — ChaosBox")
        dialog.attributes("-fullscreen", True)
        dialog.configure(background="#101820")
        view = ImageCanvas(dialog, zoomable=True)
        view.pack(fill="both", expand=True)
        view.set_image(image)
        bar = ttk.Frame(dialog, padding=6)
        bar.place(relx=1., x=-14, y=12, anchor="ne")
        ttk.Button(bar, text="−", command=lambda: view.scale(1 / 1.25)).pack(side="left")
        ttk.Button(bar, text="+", command=lambda: view.scale(1.25)).pack(side="left")
        ttk.Button(bar, text="Fit", command=view.reset).pack(side="left", padx=5)
        def copied(result):
            self.status.set("Image copied. Paste with Ctrl+V.")
            if copy_button.winfo_exists():
                copy_button.configure(text="COPIED")
        copy_button = ttk.Button(bar, text="COPY",
                                 command=lambda: self.task(lambda: copy_image_clipboard(image), copied))
        copy_button.pack(side="left", padx=5)
        ttk.Button(bar, text="Close", command=dialog.destroy).pack(side="left")
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.focus_set()

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
            def indexing(message):
                if self.cancel_upload.is_set():
                    raise core.Cancelled("Upload cancelled. Local files are retained.")
                self.log(message)
            core.build_index(self.profile, indexing)
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
    parser.add_argument("--data-root", type=Path, default=Path.home(), help="Base folder for relative setup paths (default: home)")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".config/chaosbox")
    parser.add_argument("--setup", type=Path)
    parser.add_argument("--check", action="store_true", help="Check dependencies without opening a window or changing data")
    args = parser.parse_args()
    if args.check:
        import shutil
        import paramiko
        for tool in ("exiftool", "ffmpeg"):
            if shutil.which(tool) is None:
                raise SystemExit(f"Missing program: {tool}")
        print(f"Dependencies ready: Tk {tk.TkVersion}, Pillow {Image.__version__}, Paramiko {paramiko.__version__}")
        return
    root = tk.Tk(className="Chaosbox")
    root.withdraw()
    try:
        settings = core.Settings(args.data_root, args.state_dir, args.setup)
        settings.ensure()
        app = App(root, settings)
    except Exception as error:
        messagebox.showerror("ChaosBox startup", str(error), parent=root)
        root.destroy()
        raise SystemExit(1)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
