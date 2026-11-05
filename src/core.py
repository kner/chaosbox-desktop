"""Desktop storage and metadata operations, compatible with KBox Android."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import errno
import hashlib
import io
import math
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageColor

import markdown_render
import KBox_sync
from legacy_names import installation_root, installation_title
import sync_transport

_version_file = Path(__file__).resolve().parent / "VERSION"
if not _version_file.is_file():
    _version_file = Path(__file__).resolve().parent.parent / "VERSION"
VERSION = _version_file.read_text(encoding="utf-8").strip()

def installation_icon(app_id):
    """Tint the desktop and window icon consistently for one installation."""
    with Image.open(Path(__file__).with_name("KBox.png")) as original:
        icon = original.convert("RGBA")
    # Hash the full ID so similar installation names get unrelated colors.
    shift = int.from_bytes(hashlib.sha256(app_id.encode("utf-8")).digest()[:2], "big") % 256
    numbered = re.fullmatch(r"KBox-.*?([0-9]+)", app_id)
    if numbered:
        shift = (int(numbered.group(1)) * 97) % 256
    hue, saturation, value = icon.convert("RGB").convert("HSV").split()
    hue = hue.point([(level + shift) % 256 for level in range(256)])
    tinted = Image.merge("HSV", (hue, saturation, value)).convert("RGBA")
    tinted.putalpha(icon.getchannel("A"))
    return tinted


FIELDS = ("box", "anzahl", "device", "alias", "category", "comment", "package")
LABELS = ("Box", "Quantity", "Device", "Alias", "Category", "Comment", "Package")
MEDIA = {".jpg", ".jpeg", ".mp4"}
IMPORTS = MEDIA | {".png"}
RECORD_KEYS = set(FIELDS) | {"count", "pack", "created", "modified", "id", "revision"}
REPO = Path(__file__).resolve().parent.parent
DEFAULT_SETUP = Path(__file__).with_name("setup.ini")
XMP_NAMESPACE = "urn:chaosbox:metadata:1.0/"
XMP_FIELDS = {field: f"Xmp.KBox.{field}" for field in FIELDS if field != "comment"}
XMP_FIELDS.update({key: "Xmp.KBox." + key for key in ("id", "revision", "base_revision")})
XMP_FIELDS.update(comment="Xmp.dc.description", created="Xmp.xmp.CreateDate")


@lru_cache(maxsize=1)
def image_metadata_library():
    """Load the Ubuntu-packaged Python binding, registering our inventory schema."""
    import gi
    gi.require_version("GExiv2", "0.10")
    from gi.repository import GExiv2
    GExiv2.Metadata.try_register_xmp_namespace(XMP_NAMESPACE, "KBox")
    return GExiv2


class Cancelled(Exception):
    pass


class BatchError(Exception):
    def __init__(self, message, selection, completed):
        super().__init__(message)
        self.selection, self.completed = selection, completed


def logical_lines(text):
    """Keep Android's quoted multiline TextSnippets intact (including section-like text)."""
    active, pending = False, None
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if pending is not None:
            pending += "\n" + raw
            if raw.count('"') % 2:
                if not line.endswith('"'):
                    raise ValueError("A text snippet's closing quote must end its line.")
                yield pending
                pending = None
            continue
        if line.startswith("[") and line.endswith("]"):
            active = line[1:-1].strip().casefold() == "textsnippets"
        elif active and line and not line.startswith(("#", ";")) and "=" in line:
            value = line.split("=", 1)[1].strip()
            if value.startswith('"') and value.count('"') % 2:
                pending = raw
                continue
        yield raw
    if pending is not None:
        raise ValueError("A text snippet is missing its closing quote.")


def english_setup(text):
    """Canonical English setup keys; never translate user values or snippet bodies."""
    lines = list(logical_lines(text))
    section = ''
    changed = False
    names = {'titel': 'Title', 'felder': 'Fields', 'kategorie': 'Categories'}
    for index, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip().casefold()
        elif section in ('app', 'box'):
            match = re.match(r'^(\s*(?:[;#]\s*)?)(Titel|Felder|Kategorie)(\s*=)', raw, re.I)
            if match:
                lines[index] = match[1] + names[match[2].casefold()] + match[3] + raw[match.end():]
                changed = True
    if not changed:
        return text
    result = '\n'.join(lines) + ('\n' if text.endswith('\n') else '')
    return result.replace('\n', '\r\n') if '\r\n' in text else result


def section_blocks(text):
    """Preserve repeated sections and their line positions for targeted edits."""
    blocks = []
    lines = list(logical_lines(text))
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            if blocks:
                blocks[-1][3] = i
            blocks.append([line[1:-1].strip(), {}, i, len(lines)])
        elif blocks and line and not line.startswith(("#", ";")) and "=" in line:
            key, value = line.split("=", 1)
            key = key.strip() if blocks[-1][0].casefold() == "textsnippets" else key.strip().casefold()
            if blocks[-1][0].casefold() in ("app", "box"):
                key = {"title": "titel", "fields": "felder", "categories": "kategorie"}.get(key, key)
            blocks[-1][1][key] = value.strip()
    return blocks


def sections(text):
    """Return singleton settings; repeated Box sections use section_blocks."""
    result = {}
    for name, values, _, _ in section_blocks(text):
        if name.casefold() != "box":
            result.setdefault(name, {}).update(values)
    return result


def unique_categories(text):
    seen, result = set(), []
    for item in text.split(","):
        item = item.strip().upper()
        if item and item.casefold() not in seen:
            seen.add(item.casefold())
            result.append(item)
    return result


def within(path, parent):
    return path.resolve().is_relative_to(parent.resolve())


def atomic_write(path, content, newer=False, mode=None):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symbolic link: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".KBox-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content if isinstance(content, bytes) else content.encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, mode if mode is not None else stat.S_IMODE(previous.st_mode) if previous else 0o600)
        if newer and previous:
            modified = max(time.time(), int(previous.st_mtime) + 1)
            os.utime(temporary, (modified, modified))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass
class Profile:
    id: str
    title: str
    images: Path
    data: Path
    legacy_data: Path
    index: Path
    labels: list[str]
    categories: list[str]
    setup_path: Path | None = None
    section_name: str = ""
    options: tuple | None = None


class Settings:
    def __init__(self, root, state_dir=None, setup=None):
        root = installation_root(root)
        if Path(root).expanduser().is_symlink():
            raise ValueError("Installation directory must not be a symbolic link")
        self.root = Path(root).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve() if state_dir else self.root / ".state"
        self.path = Path(setup).expanduser().resolve() if setup else self.root / "setup.ini"
        for owned in (self.state_dir, self.path, self.root/".indices", self.state_dir/"credentials"):
            if not within(owned.resolve(), self.root):
                raise ValueError("Setup and state must stay inside the installation directory")
        self.index = self.root / ".indices"
        self.title = installation_title(self.root)
        self.app_id = "KBox-" + hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        self.icon_id = self.title if self.root.name.casefold().startswith("KBox-".casefold()) else self.app_id
        self.window_class = "KBox-" + self.app_id.removeprefix("KBox-")
        self.profiles = []
        self.image_width = 3000
        self.editor = None
        self.language = "en"
        self.snippets = {}
        self.ssh = {}

    @property
    def installation_id(self):
        # The generation lives ONLY in this directory: recreating the same path is a new installation.
        identity = self.root / ".installation-id"
        if identity.is_symlink():
            raise ValueError("Installation identity must not be a symbolic link")
        if not identity.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".identity-", dir=self.root)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(str(uuid.uuid4()) + "\n")
                    stream.flush(); os.fsync(stream.fileno())
                try: os.link(temporary, identity)
                except FileExistsError: pass
            finally: os.unlink(temporary)
        generation = str(uuid.UUID(identity.read_text().strip()))
        try: machine = Path('/etc/machine-id').read_text().strip()
        except OSError: machine = __import__('socket').gethostname()
        slug = re.sub(r'[^a-z0-9_-]+', '-', self.root.name.casefold()).strip('-_') or 'installation'
        return slug[:40] + '-' + hashlib.sha256((machine+'\0'+str(self.root)+'\0'+generation).encode()).hexdigest()[:32]

    def assert_current(self):
        identity = self.root / ".installation-id"
        if not identity.is_file() or self.installation_id != getattr(self, "_loaded_installation_id", None):
            raise ValueError("Installation directory was removed or reset. Close this window and reopen the installation.")

    def ensure(self, default_box=None):
        if not self.path.exists():
            text = DEFAULT_SETUP.read_text(encoding="utf-8")
            if default_box and not any(name.casefold() == "box" for name, *_ in section_blocks(text)):
                text += f"\n[Box]\nTitle={default_box}\n"
            text += ("\n[SSH]\nHost=access983197478.webspace-data.io\nPort=22\nUser=u114229695\n"
                     f"KeyFile={self.state_dir / 'credentials/id_ed25519'}\n"
                     f"KnownHosts={self.state_dir / 'credentials/known_hosts'}\n")
            atomic_write(self.path, text)
        self.reload()

    @staticmethod
    def _text(groups):
        return "\n\n".join("[" + name + "]\n" + "\n".join(
            f"{key}={value}" for key, value in values.items()) for name, values in groups.items())

    def _load(self, text, overrides=None):
        overrides = overrides or {}
        profiles, ids, directories = [], set(), set()

        def read_config(content):
            blocks = section_blocks(content)
            globals_, boxes, app = {}, [], {}
            for name, values, _, _ in blocks:
                section = name.casefold()
                if section.startswith("app.") or section == "pfade":
                    raise ValueError("Old setup format: replace [App.NAME] with [Box] and Titel=NAME; use JPG and TXT folders.")
                if section in ("app", "box"):
                    if any(key in values for key in ("standard", "jpg", "bilder", "daten")):
                        raise ValueError("Standard, JPG and Daten are no longer supported; folders are fixed to JPG and TXT.")
                    if section == "box":
                        title = values.get("titel", "").strip()
                        if (not title or title in (".", "..") or "/" in title or "\\" in title
                                or title.casefold() in ("jpg", "txt", ".indices", ".state", "setup.ini")):
                            raise ValueError("Each Box needs a Title that is a single non-reserved folder name.")
                        boxes.append((title, values))
                    else:
                        app.update(values)
                else:
                    globals_.setdefault(section, {}).update(values)
            if len({title.casefold() for title, _ in boxes}) != len(boxes):
                raise ValueError("Box titles must be unique within their parent folder.")
            return app, boxes, globals_

        def visit(directory, identifier, values, inherited, owner, selector, config=None):
            if directory.is_symlink():
                raise ValueError(f"Cyclic or symbolic project folder: {directory}")
            resolved = directory.resolve()
            if not within(resolved, self.root) or resolved in directories:
                raise ValueError(f"Cyclic or escaping project folder: {directory}")
            directories.add(resolved)
            if identifier.casefold() in ids:
                raise ValueError("Profile names must be unique.")
            ids.add(identifier.casefold())
            effective = {key: dict(value) for key, value in inherited.items()}
            values = dict(values)
            children = []
            local = directory / "setup.ini"
            if local.is_symlink() or not within(local.resolve(),self.root):
                raise ValueError("Profile setup must remain inside the installation")
            if config is None and (local.is_file() or local in overrides):
                config = overrides[local] if local in overrides else local.read_text(encoding="utf-8-sig")
                owner, selector = local, ""
            if config is not None:
                app, children, globals_ = read_config(config)
                values.update(app)
                for key, entries in globals_.items():
                    effective.setdefault(key, {}).update(entries)
            try:
                image_width = int(values.get("imagewidth", "3000"))
            except ValueError as error:
                raise ValueError("ImageWidth must be an integer between 1 and 20000.") from error
            if not 1 <= image_width <= 20000:
                raise ValueError("ImageWidth must be between 1 and 20000.")
            poster = poster_settings(self._text(effective))
            labels = values.get("felder", ",".join(LABELS)).split(",")
            if len(labels) > 7:
                raise ValueError(f"{identifier}: Fields has more than seven positions.")
            translated = {"anzahl": "Quantity", "kategorie": "Category", "kommentar": "Comment"}
            labels = [translated.get(label.strip().casefold(), label.strip()) for label in labels]
            labels += [""] * (7 - len(labels))
            snippets = {name: value[1:-1] if value.startswith('"') and value.endswith('"') else value
                        for name, value in effective.get("textsnippets", {}).items()}
            editor = values.get("editor", "").strip() or None
            ssh = dict(effective.get("ssh", {}))
            ssh.update(effective.get("ssh."+identifier.casefold(),{}))
            profile_path=identifier.casefold()
            web_root=ssh.get('webinstallation','l1').strip()
            if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',part) for part in web_root.split('/')):
                raise ValueError('Invalid web installation directory')
            installation = ssh.get('installation', '').strip() or self.installation_id
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', installation):
                raise ValueError('Invalid SSH Installation: use letters, digits, underscores or hyphens.')
            sync_root=web_root+"/storage/installations/"+installation+"/profiles/"+profile_path
            # Destinations are derived from the selected installation and profile.
            for key in ('datadestination','sftpdatadestination','shelldatadestination','imagedestination'):
                ssh.pop(key,None)
            ssh['installation']=installation
            for key, name in (('keyfile','id_ed25519'),('knownhosts','known_hosts')):
                default = self.state_dir/'credentials'/name
                configured = Path(ssh.get(key, '').strip() or str(default)).expanduser()
                if not configured.is_absolute(): configured = self.root/configured
                ssh[key] = str(configured.resolve())
            for key, value in dict(host="access983197478.webspace-data.io", port="22", user="u114229695",
                                   imagedestination=sync_root+"/JPG", datadestination=sync_root+"/TXT",
                                   keyfile=str(self.state_dir / "credentials/id_ed25519"),
                                   knownhosts=str(self.state_dir / "credentials/known_hosts")).items():
                ssh.setdefault(key, value)
            ssh.setdefault("transport", "sftp")
            ssh.setdefault("shelldatadestination", "/kunden/homepages/13/d983197478/htdocs/"+sync_root+"/TXT"
                if ssh["host"]=="access983197478.webspace-data.io" and ssh["datadestination"]==sync_root+"/TXT" else ssh["datadestination"])
            if ssh["transport"].lower() not in ("sftp", "rsync", "ssh"):
                raise ValueError("SSH Transport must be sftp, rsync or ssh.")
            if not 1 <= int(ssh["port"]) <= 65535:
                raise ValueError("SSH port must be between 1 and 65535.")
            images, data = directory / "JPG", directory / "TXT"
            for folder in (images, data):
                if folder.is_symlink() or not within(folder.resolve(), self.root):
                    raise ValueError(f"Project folder must remain inside the installation: {folder}")
            profile = Profile(identifier, directory.name, images, data, data, self.index,
                              labels, unique_categories(values.get("kategorie", "")), owner, selector,
                              (image_width, snippets, ssh, poster, editor))
            profiles.append(profile)
            for title, entries in children:
                child_values = {key: value for key, value in values.items() if key != "titel"}
                child_values.update(entries)
                child_id = f"{identifier}/{title}"
                visit(directory / title, child_id, child_values, effective, owner, title)

        defaults, boxes, globals_ = read_config(text)
        if not boxes:
            raise ValueError("Setup must contain at least one [Box] section with Title=.")
        for title, entries in boxes:
            visit(self.root / title, title, dict(defaults, **entries), globals_, self.path, title)
        first = profiles[0]
        language = defaults.get("language", "en").strip().lower().replace("_", "-").split("-", 1)[0]
        if language not in {"en", "de", "fr", "it"}:
            raise ValueError("Language must be one of: en, de, fr, it.")
        return profiles, first.id, *first.options[:3], language

    def parse(self, text):
        return self._load(text)

    def reload(self):
        text = self.path.read_text(encoding="utf-8-sig")
        loaded = self._load(text)
        self.profiles, self.default, self.image_width, self.snippets, self.ssh, self.language = loaded
        self.poster = poster_settings(text)
        self._loaded_installation_id = self.installation_id
        self.profile(getattr(self, "active", self.default))

    def save_text(self, text, path=None):
        text = english_setup(text)
        path = Path(path) if path else self.path
        root_text = text if path == self.path else self.path.read_text(encoding="utf-8-sig")
        self._load(root_text, {path: text})
        atomic_write(path, text)
        self.reload()

    def profile(self, name):
        profile = next((p for p in self.profiles if p.id.casefold() == name.casefold()),
                       next(p for p in self.profiles if p.id.casefold() == self.default.casefold()))
        self.active = profile.id
        self.image_width, self.snippets, self.ssh, self.poster, self.editor = profile.options
        return profile

    @property
    def active_path(self):
        return self.profile(self.active).setup_path

    def remember_category(self, profile, entered):
        if "\n" in entered or "\r" in entered:
            raise ValueError("Category must be a single line.")
        self.reload()
        current = self.profile(profile.id)
        known = {item.casefold() for item in current.categories}
        additions = [item for item in unique_categories(entered) if item.casefold() not in known]
        if not additions:
            return
        text = current.setup_path.read_text(encoding="utf-8")
        lines = list(logical_lines(text))
        selected = next((block for block in section_blocks(text)
                         if (not current.section_name and block[0].casefold() == "app")
                         or (current.section_name and block[0].casefold() == "box"
                             and block[1].get("titel", "").casefold() == current.section_name.casefold())), None)
        value = "Categories=" + ", ".join(current.categories + additions)
        if selected:
            _, _, start, end = selected
            key = next((i for i in range(start + 1, end)
                        if lines[i].split("=", 1)[0].strip().casefold() in ("kategorie", "categories")), None)
            if key is None:
                lines.insert(end, value)
            else:
                lines[key] = value
        else:
            lines.extend(["", "[App]", value])
        self.save_text("\n".join(lines).rstrip() + "\n", current.setup_path)


@dataclass(frozen=True)
class PosterSettings:
    limit: int = 6000
    height: float = 200
    width: float = 100
    cols: int = 3
    rows: int = 3
    fixed: bool = True
    margin_left: float = 15
    margin_right: float = 10
    margin_top: float = 10
    margin_bottom: float = 15
    frames: str = "eeee"
    background_color: str = "9bb195"
    image_background_color: str = "FFFFFF"
    image_padding: float = 2
    transparency: float | None = None
    date_rotation: int | None = None
    font_size: float | None = None


def poster_settings(text):
    groups = {key.casefold(): value for key, value in sections(text).items()}
    values = dict(groups.get("imagesize", {}))
    values.update(groups.get("poster", {}))
    size = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*[x×]\s*([0-9]+(?:\.[0-9]+)?)\s*",
                        values.get("poster-size", "200x100"))
    if not size:
        raise ValueError("POSTER-SIZE must be height x width in millimeters, e.g. 200x100.")
    height, width = map(float, size.groups())
    limit = int(values.get("poster-limit", "6000"))
    cols, rows = int(values.get("poster-cols", "3")), int(values.get("poster-rows", "3"))
    fixed = values.get("poster-fix", "true").casefold()
    if not (math.isfinite(height) and math.isfinite(width) and height > 0 and width > 0
            and 1 <= limit <= 20000 and 1 <= cols <= 100 and 1 <= rows <= 100
            and fixed in ("true", "false")):
        raise ValueError("Invalid poster settings: positive size, LIMIT 1–20000, COLS/ROWS 1–100, FIX true/false required.")
    margins = {side: float(values.get("poster-margin-" + side, default))
               for side, default in (("left", "15"), ("right", "10"), ("top", "10"), ("bottom", "15"))}
    if any(not math.isfinite(value) or value < 0 for value in margins.values()):
        raise ValueError("POSTER-MARGIN values must be finite, nonnegative millimeters.")
    padding = float(values.get("poster-image-padding", "2"))
    if not math.isfinite(padding) or padding < 0:
        raise ValueError("POSTER-IMAGE-PADDING must be finite, nonnegative millimeters.")
    def optional_number(key, minimum, maximum):
        raw = values.get(key, "").strip()
        if raw.casefold() in ("", "none"):
            return None
        value = float(raw)
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{key.upper()} must be between {minimum:g} and {maximum:g}.")
        return value
    transparency = optional_number("poster-transparency", 0, 100)
    font_size = optional_number("poster-font-size", .1, 1000)
    rotation = values.get("poster-date-rotation", "").strip()
    date_rotation = int(rotation) if rotation in ("0", "90") else None
    colors = {key: values.get("poster-" + key.replace("_", "-"), default)
              for key, default in (("frames", "eeee"), ("background_color", "9bb195"),
                                   ("image_background_color", "FFFFFF"))}
    for key, value in colors.items():
        poster_color(value)
    return PosterSettings(limit, height, width, cols, rows, fixed == "true",
                          **{"margin_" + side: value for side, value in margins.items()}, image_padding=padding, transparency=transparency,
                          date_rotation=date_rotation, font_size=font_size, **colors)


def poster_color(value):
    color = str(value).strip().lstrip("#")
    if len(color) not in (3, 4, 6, 8) or not re.fullmatch(r"[0-9a-fA-F]+", color):
        raise ValueError(f"Invalid poster color {value!r}: use RGB, RGBA, RRGGBB or RRGGBBAA hex.")
    return ImageColor.getcolor("#" + color, "RGBA")


def poster_background(size, color, setup_dir=None, progress=lambda _: None):
    background = Image.new("RGBA", size, "white")
    background.alpha_composite(Image.new("RGBA", size, poster_color(color)))
    if setup_dir is not None:
        candidates = files(setup_dir, {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}, recursive=False)
        for path in candidates:
            try:
                with Image.open(path) as source:
                    picture = ImageOps.exif_transpose(source).convert("RGBA")
                    picture = ImageOps.fit(picture, size, Image.Resampling.LANCZOS)
                    background.alpha_composite(picture)
                progress(f"Poster background: {path.name}")
                break
            except (OSError, ValueError) as error:
                progress(f"Skipping unreadable poster background {path.name}: {error}")
    return background.convert("RGB")


def common_metadata(records):
    return {field: records[0].get(field, "") if all(str(record.get(field, "")) ==
            str(records[0].get(field, "")) for record in records) else "" for field in FIELDS}


def differing_metadata_fields(records):
    return [field for field in FIELDS if records and any(
        str(record.get(field, "")) != str(records[0].get(field, "")) for record in records[1:])]


def edited_metadata(original, values, baseline, *, mode="auto", differing_fields=(), validate=True):
    result = dict(original)
    for field in FIELDS:
        value = values.get(field, "")
        if str(value) == str(baseline.get(field, "")):
            continue
        old = original.get(field, "")
        append = mode == "extend" or (mode == "auto" and baseline.get(field, "") == "")
        if mode == "extend" and not str(value).strip():
            continue
        if append and str(value).strip():
            if field == "anzahl":
                result[field] = int(old or 0) + int(value)
            else:
                separator = "\n" if field == "comment" else ", "
                result[field] = separator.join(str(part) for part in (old, value) if str(part))
        else:
            result[field] = value
    # Parsed legacy metadata still contains aliases. They must not override
    # the edited canonical fields when an MP4 comment is read back.
    for alias, field in (("count", "anzahl"), ("Anzahl", "anzahl"),
                         ("pack", "package"), ("kommentar", "comment"),
                         ("Kommentar", "comment")):
        if field in result:
            result.pop(alias, None)
    if "category" in result:
        result = {key: value for key, value in result.items()
                  if key == "category" or key.casefold() not in ("category", "kategorie")}
    return validate_record(result) if validate else result


def poster_font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError as error:
        raise ValueError("Poster fonts unavailable. Install fonts-dejavu-core.") from error


def poster_wrap(text, font, width):
    """Wrap paragraphs and long words without dropping any comment text."""
    lines = []
    for paragraph in str(text).split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = (line + " " + word).lstrip()
            if font.getlength(candidate) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            for character in word:
                if font.getlength(line + character) > width:
                    if not line:
                        raise ValueError("Poster text area is too narrow.")
                    lines.append(line)
                    line = ""
                line += character
        lines.append(line)
    return lines


def poster_text(texts, width, max_height, preferred, minimum, bold=False):
    for size in range(max(1, preferred), max(1, minimum) - 1, -1):
        font = poster_font(size, bold)
        try:
            lines = [poster_wrap(text, font, width) if text else [] for text in texts]
        except ValueError:
            continue
        line_height = sum(font.getmetrics())
        height = max((len(block) * line_height for block in lines), default=0)
        if height <= max_height:
            return font, lines, line_height, height
    raise ValueError("Poster text does not fit. Increase POSTER-SIZE/LIMIT, reduce the grid or shorten comments.")


def poster_draw_text(draw, lines, font, line_height, left, top, width):
    for line in lines:
        draw.text((left + width / 2, top), line, font=font, fill="#202020", anchor="mt")
        top += line_height


def poster_caption(record):
    return str(record.get("comment", ""))


def poster_parts(record):
    """Page/x/y/rotation/transparency/z-order, with legacy grid addresses readable."""
    value = str(record.get("device", "") or "").strip()
    parts = [part.strip() for part in value.split("/")]
    if re.fullmatch(r"[A-Za-z]+[1-9][0-9]*", parts[0]):
        parts.insert(0, "")
    if len(parts) > 1:
        address = re.fullmatch(r"([A-Za-z]+)([1-9][0-9]*)", parts[1])
        if address:
            col = 0
            for letter in address[1].upper():
                col = col * 26 + ord(letter) - ord("A") + 1
            parts[1:2] = [str(col - 1), str(int(address[2]) - 1)]
    if len(parts) > 6:
        raise ValueError(f"Invalid poster page/x/y/rotation/transparency/z-order in field 3: {value!r}.")
    return parts + [""] * (6 - len(parts))


def poster_coordinate_text(value):
    text = str(value or "").strip()
    if re.search(r"[A-Za-z]+[1-9][0-9]*", text):
        try:
            return "/".join(poster_parts({"device": text})).rstrip("/")
        except ValueError:
            # Invalid stored values must remain editable; poster creation validates them.
            return text
    return text


def poster_zorder(record, selection_number=1):
    value = poster_parts(record)[5]
    if not value:
        return selection_number
    try:
        z = float(value.replace(",", "."))
    except ValueError as error:
        raise ValueError(f"Invalid poster z-order in field 3: {value!r}.") from error
    if not math.isfinite(z):
        raise ValueError("Poster z-order in field 3 must be a finite number.")
    return z


def poster_page(record):
    candidate = poster_parts(record)[0]
    if not candidate:
        return None
    if not re.fullmatch(r"[1-9][0-9]*", candidate):
        raise ValueError("Poster page in field 3 must be a positive integer.")
    return int(candidate)


def poster_page_mode(metadata, indices, default):
    mode, owner = default, None
    for index in indices:
        value = str(metadata[index].get("alias", "") or "").strip().casefold()
        if value:
            mode, owner = value in {"1", "true", "t"}, index
    return mode, owner


def poster_layout_modes(metadata, default, pages=None):
    """Resolve one mode per page; an empty page starts from the setup default."""
    if pages is None:
        groups, number = {}, 1
        for index, record in enumerate(metadata):
            number = poster_page(record) or number
            groups.setdefault(number, []).append(index)
        indices_by_page = groups.values()
    else:
        indices_by_page = [[item[0] for item in (page["placements"] if isinstance(page, dict) else page)] for page in pages]
    modes = [default] * len(metadata)
    for indices in indices_by_page:
        mode, _ = poster_page_mode(metadata, indices, default)
        for index in indices:
            modes[index] = mode
    return modes


def poster_transform(record):
    """Parse the position, clockwise rotation and panel transparency."""
    poster_page(record)
    poster_zorder(record)
    _, x_value, y_value, angle_value, transparency_value, _ = poster_parts(record)
    position = None
    if x_value or y_value:
        try:
            x = float(x_value.replace(",", ".")) if x_value else 0
            y = float(y_value.replace(",", ".")) if y_value else 0
        except ValueError as error:
            raise ValueError("Poster x/y coordinates in field 3 must be finite numbers.") from error
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("Poster x/y coordinates in field 3 must be finite numbers.")
        position = y, x
    try:
        angle = float(angle_value.replace(",", ".")) if angle_value else 0
    except ValueError as error:
        raise ValueError(f"Invalid poster angle in field 3: {angle_value!r}.") from error
    if not math.isfinite(angle):
        raise ValueError("Poster angle in field 3 must be a finite number.")
    try:
        transparency = float(transparency_value.replace(",", ".")) if transparency_value else 0
    except ValueError as error:
        raise ValueError(f"Invalid poster transparency in field 3: {transparency_value!r}.") from error
    if not math.isfinite(transparency) or not 0 <= transparency <= 100:
        raise ValueError("Poster transparency in field 3 must be between 0 and 100.")
    return position, angle % 360, transparency


def poster_position(record):
    return poster_transform(record)[0]


def poster_angle(record):
    return poster_transform(record)[1]


def poster_transparency(record, default=0):
    return poster_transform(record)[2] if poster_parts(record)[4] else default


def initial_poster_record(record, settings):
    record = dict(record)
    if settings.transparency is not None:
        parts = poster_parts(record)
        parts[4] = f"{settings.transparency:g}"
        record["device"] = "/".join(parts).rstrip("/")
    return record


def poster_date(path, record):
    # Use the same inferred creation date as the image chooser.
    try:
        date = datetime.fromtimestamp(selection_creation_time(path))
    except (OSError, ValueError):
        try:
            date = datetime.fromisoformat(str(record.get("created", "")))
        except ValueError:
            return ""
    return f"{date.day}.{date.month}.{date.year}"



def poster_rotated_size(width, height, angle):
    radians = math.radians(angle)
    cosine, sine = abs(math.cos(radians)), abs(math.sin(radians))
    return width * cosine + height * sine, width * sine + height * cosine


def poster_zoom(record):
    value = str(record.get("anzahl", "")).strip()
    if not value:
        return 1
    value = re.sub(r"^Z\s*=\s*", "", value, flags=re.IGNORECASE)
    try:
        zoom = float(value.replace(",", "."))
    except ValueError as error:
        raise ValueError(f"Invalid poster zoom in field 2: {value!r}.") from error
    if not math.isfinite(zoom) or zoom < 0:
        raise ValueError("Poster zoom in field 2 must be a finite, non-negative number.")
    return max(1, zoom)


def poster_occupied(row, col, height, width, rows, cols):
    return {(r, c) for r in range(max(0, math.floor(row)), min(rows, math.ceil(row + height)))
            for c in range(max(0, math.floor(col)), min(cols, math.ceil(col + width)))}


def poster_repack_page(indices, shapes, positions, cols, rows, modes, portraits):
    """Honor a forced page, reducing flexible spans before allowing overlap."""
    def pack(candidate_shapes, overlap=False):
        occupied, result = set(), []
        for index in indices:
            height, width = candidate_shapes[index]
            position = positions[index]
            if position is None:
                position = next(((r, c) for r in range(rows) for c in range(cols)
                                 if r + height <= rows and c + width <= cols and not any(
                                     (y, x) in occupied for y in range(r, r + height)
                                     for x in range(c, c + width))), None)
                if position is None:
                    if not overlap:
                        return None
                    position = divmod(len(result) % (cols * rows), cols)
            row, col = position
            occupied.update(poster_occupied(row, col, height, width, rows, cols))
            result.append((index, row, col, height, width))
        return result

    candidates = dict(enumerate(shapes))
    result = pack(candidates)
    if result is not None:
        return result
    # Requested page numbers take priority over flexible zoom and portrait spans.
    for index in indices:
        if not modes[index] and positions[index] is None:
            candidates[index] = (min(rows, 2) if portraits[index] else 1, 1)
    result = pack(candidates)
    if result is not None:
        return result
    for index in indices:
        if not modes[index] and positions[index] is None:
            candidates[index] = (1, 1)
    return pack(candidates, overlap=True)


def poster_pages(portraits, cols, rows, *, fixed=False, zooms=None,
                 aspect_ratios=None, cell_aspect=1, positions=None,
                 page_numbers=None, fixed_modes=None):
    """Reserve rectangular photo areas; defer photos that cannot fit this page."""
    modes = fixed_modes if fixed_modes is not None else [fixed] * len(portraits)
    explicit_pages = page_numbers or [None] * len(portraits)
    if rows < 2 and any(portrait and not modes[index] and explicit_pages[index] is None
                        for index, portrait in enumerate(portraits)):
        raise ValueError("Portrait photos require POSTER-ROWS of at least 2.")
    shapes = []
    for index, portrait in enumerate(portraits):
        base_rows = 2 if portrait and not modes[index] else 1
        if explicit_pages[index] is not None:
            base_rows = min(rows, base_rows)
        zoom = zooms[index] if zooms is not None else 1
        if modes[index]:
            width = max(1, math.ceil(zoom))
            if width > cols and (positions is None or positions[index] is None):
                raise ValueError(f"Fixed poster zoom in field 2 requires {width} columns, but POSTER-COLS is {cols}.")
            if width > rows and (positions is None or positions[index] is None):
                raise ValueError(f"Fixed poster zoom in field 2 requires {width} rows, but POSTER-ROWS is {rows}.")
            shapes.append((width, width))
            continue
        if zoom <= 1:
            shapes.append((base_rows, 1))
            continue
        # A requested zoom can exceed the available page, especially for
        # portraits that already reserve two rows. Use the largest area the
        # configured grid can provide so one oversized photo cannot prevent
        # the poster from being created.
        required = min(math.ceil(base_rows * zoom), cols * rows)
        aspect = aspect_ratios[index] if aspect_ratios is not None else (.5 if portrait else 1)
        candidates = [(height, width) for height in range(base_rows, rows + 1)
                      for width in range(1, cols + 1) if height * width >= required]
        shapes.append(min(candidates, key=lambda shape:
                          (shape[0] * shape[1] - required,
                           abs(math.log(shape[1] * cell_aspect / shape[0] / aspect)),
                           -shape[1])))
    positions = positions or [None] * len(portraits)
    for position in positions:
        if position is not None and not all(math.isfinite(value) for value in position):
            raise ValueError("Poster x/y coordinates in field 3 must be finite numbers.")
    start_pages, start_page = [], 1
    for page in page_numbers or [None] * len(portraits):
        if page is not None:
            start_page = page
        start_pages.append(start_page)
    pending = list(range(len(portraits)))
    pages = []
    while pending:
        occupied = set()
        page, deferred = [], []
        repacked_page = False
        for index in pending:
            if start_pages[index] > len(pages) + 1:
                deferred.append(index)
                continue
            if positions[index] is not None:
                row, col = positions[index]
                row_span, col_span = shapes[index]
                page.append((index, row, col, row_span, col_span))
                occupied.update(poster_occupied(row, col, row_span, col_span, rows, cols))
                continue
            if modes[index]:
                height, width = shapes[index]
                slot = next((slot for slot in range(cols * rows)
                             if slot % cols + width <= cols and not any(
                                 (slot // cols, c) in occupied for c in range(slot % cols, slot % cols + width))), None)
            else:
                slot = next((slot for slot in range(cols * rows) if divmod(slot, cols) not in occupied), None)
            if slot is None and explicit_pages[index] is None:
                deferred.append(index)
                continue
            row, col = divmod(slot, cols) if slot is not None else (0, 0)
            row_span, col_span = shapes[index]
            area = {(r, c) for r in range(row, row + row_span)
                    for c in range(col, col + col_span)}
            if slot is None or row + row_span > rows or col + col_span > cols or area & occupied:
                if explicit_pages[index] is not None:
                    repacked_page = True
                    page = poster_repack_page([item[0] for item in page] + [index], shapes, positions,
                                              cols, rows, modes, portraits)
                    occupied = set().union(*(poster_occupied(r0, c0, height, width, rows, cols)
                                             for _, r0, c0, height, width in page))
                else:
                    deferred.append(index)
                continue
            occupied.update(area)
            page.append((index, row, col, row_span, col_span))
        if repacked_page:
            remaining = []
            for index in deferred:
                height, width = shapes[index]
                position = next(((r, c) for r in range(rows) for c in range(cols)
                                 if start_pages[index] <= len(pages) + 1 and r + height <= rows and
                                 c + width <= cols and not any((y, x) in occupied
                                 for y in range(r, r + height) for x in range(c, c + width))), None)
                if position is None:
                    remaining.append(index)
                    continue
                row, col = position
                page.append((index, row, col, height, width))
                occupied.update((r, c) for r in range(row, row + height) for c in range(col, col + width))
            deferred = remaining
            page.sort()
        pages.append(page)
        pending = deferred
    return pages


def poster_photo_pages(paths, metadata, settings, *, ignore_pages=False, fixed_modes=None, force_page=False):
    portraits, aspects = [], []
    for path, record in zip(paths, metadata):
        with Image.open(path) as source:
            width, height = source.size
            if source.getexif().get(274) in (5, 6, 7, 8):
                width, height = height, width
            width, height = poster_rotated_size(width, height, poster_angle(record))
            portraits.append(width + 1e-8 < height)
            aspects.append(width / height)
    zooms = [poster_zoom(record) for record in metadata]
    positions = [poster_position(record) for record in metadata]
    numbers = [1] * len(paths) if force_page else [None] * len(paths) if ignore_pages else [poster_page(record) for record in metadata]
    def pack(indices, mode):
        return poster_pages([portraits[index] for index in indices], settings.cols, settings.rows,
                            fixed=mode, zooms=[zooms[index] for index in indices],
                            aspect_ratios=[aspects[index] for index in indices],
                            positions=[positions[index] for index in indices],
                            page_numbers=[1 if numbers[index] == number else None for index in indices],
                            cell_aspect=settings.width * settings.rows / (settings.height * settings.cols))
    if fixed_modes is not None or not any(str(record.get("alias", "") or "").strip() for record in metadata):
        return poster_pages(portraits, settings.cols, settings.rows, fixed=settings.fixed,
                            zooms=zooms, aspect_ratios=aspects, positions=positions, page_numbers=numbers,
                            fixed_modes=fixed_modes,
                            cell_aspect=settings.width * settings.rows / (settings.height * settings.cols))
    floors, floor = [], 1
    for requested in numbers:
        floor = requested or floor
        floors.append(floor)
    pending, pages = list(range(len(paths))), []
    while pending:
        number = len(pages) + 1
        eligible = [index for index in pending if floors[index] <= number]
        if not eligible:
            pages.append([])
            continue
        blocked, committed_owner = set(), None
        mode, _ = poster_page_mode(metadata, eligible, settings.fixed)
        while True:
            candidates = [index for index in eligible if index not in blocked]
            if not candidates:
                page = []
                break
            planned = pack(candidates, mode)
            page = [(candidates[local], *placement) for local, *placement in planned[0]]
            accepted = [item[0] for item in page]
            # If an override cannot fit after applying its mode, it belongs to
            # a later page and must not control this page (or cause oscillation).
            if committed_owner is not None and committed_owner not in accepted:
                blocked.add(committed_owner)
                committed_owner = None
                mode, _ = poster_page_mode(metadata, accepted, settings.fixed)
                continue
            desired, owner = poster_page_mode(metadata, accepted, settings.fixed)
            if desired == mode:
                break
            mode, committed_owner = desired, owner
        pages.append(page)
        accepted = {item[0] for item in page}
        pending = [index for index in pending if index not in accepted]
    return pages


def poster_grid_cells(placements, cols, rows, bounds, gap):
    left, top, right, bottom = bounds
    width = (right - left - gap * (cols - 1)) / cols
    height = (bottom - top - gap * (rows - 1)) / rows
    return [tuple(round(value) for value in
                  (left + col * (width + gap), top + row * (height + gap),
                   left + col * (width + gap) + width * col_span + gap * (col_span - 1),
                   top + row * (height + gap) + height * span + gap * (span - 1)))
            for row, col, span, col_span in placements]


def poster_flexible_cells(placements, bounds, gap, *, cols=None, rows=None, fixed_modes=None):
    """Trim empty outer rows/columns and grow panels into adjacent empty cells."""
    cols = cols or max(col + col_span for row, col, span, col_span in placements)
    rows = rows or max(row + span for row, col, span, col_span in placements)
    occupied = {(r, c) for row, col, span, col_span in placements
                for r in range(row, row + span) for c in range(col, col + col_span)}
    panels = [list(placement) for placement in placements]
    # Fill partial rows first, then extend panels into unused space below.
    for index, panel in enumerate(panels):
        if fixed_modes is not None and fixed_modes[index]:
            continue
        row, col, panel_rows, panel_cols = panel
        while col + panel_cols < cols and all(
                (r, col + panel_cols) not in occupied for r in range(row, row + panel_rows)):
            occupied.update((r, col + panel_cols) for r in range(row, row + panel_rows))
            panel_cols += 1
        panel[3] = panel_cols
    for index, panel in enumerate(panels):
        if fixed_modes is not None and fixed_modes[index]:
            continue
        row, col, panel_rows, panel_cols = panel
        while row + panel_rows < rows and all(
                (row + panel_rows, c) not in occupied for c in range(col, col + panel_cols)):
            occupied.update((row + panel_rows, c) for c in range(col, col + panel_cols))
            panel_rows += 1
        panel[2] = panel_rows
    left, top, right, bottom = bounds
    width = (right - left - gap * (cols - 1)) / cols
    height = (bottom - top - gap * (rows - 1)) / rows
    return [tuple(round(value) for value in
                  (left + col * (width + gap), top + row * (height + gap),
                   left + col * (width + gap) + width * col_span + gap * (col_span - 1),
                   top + row * (height + gap) + height * row_span + gap * (row_span - 1)))
            for row, col, row_span, col_span in panels]


def create_posters(paths, profile, settings, progress=lambda _: None, *,
                   field_overrides=None, field_baseline=None, edit_mode="replace",
                   differing_fields=(), setup_dir=None, path_overrides=None,
                   layout_state=None, previous_layout=None, changed_indices=None):
    paths = list(paths)
    if not paths:
        raise ValueError("Select at least 1 image.")
    if any(path.suffix.lower() not in {".jpg", ".jpeg", ".png"} for path in paths):
        raise ValueError("Posters require JPG or PNG images.")
    metadata = []
    metadata_cache = dict(previous_layout.get("source_cache", {})) if previous_layout else {}
    for path, record in zip(paths, read_metadata_batch(paths, progress, cache=metadata_cache)):
        metadata.append({**record, **(field_overrides or {})} if field_baseline is None else
                        edited_metadata(record, field_overrides or {}, field_baseline,
                                        mode=edit_mode, differing_fields=differing_fields, validate=False))
        metadata[-1].update((path_overrides or {}).get(path, {}))
    source_paths = {Path(path).resolve() for path in paths}
    metadata_cache = {path: value for path, value in metadata_cache.items() if path in source_paths}
    changed = set(changed_indices or ())
    if previous_layout is not None:
        metadata = [record if index in changed else dict(previous_layout["metadata"][index])
                    for index, record in enumerate(metadata)]
    # Validate every source before rendering or reserving any poster output.
    for path, record in zip(paths, metadata):
        try:
            poster_transform(record)
            poster_zoom(record)
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from error
    modes = poster_layout_modes(metadata, settings.fixed, previous_layout["pages"] if previous_layout is not None else None)
    recalculate_pages = set()
    if previous_layout is not None:
        # Keep every untouched panel on its page, even when an edited image moves.
        pages = [[item for item in old_page["placements"] if item[0] not in changed]
                 for old_page in previous_layout["pages"]]
        for old_number, old_page in enumerate(previous_layout["pages"], 1):
            for index, row, col, row_span, col_span in old_page["placements"]:
                if index not in changed:
                    continue
                old_record, record = previous_layout["metadata"][index], metadata[index]
                old_mode = previous_layout.get("fixed_modes", modes)[index]
                if ((poster_transform(record)[:2], poster_zoom(record), modes[index]) !=
                        (poster_transform(old_record)[:2], poster_zoom(old_record), old_mode)):
                    _, _, _, row_span, col_span = poster_photo_pages(
                        [paths[index]], [record], settings, ignore_pages=True, fixed_modes=[modes[index]])[0][0]
                    position = poster_position(record)
                    if position is not None:
                        row, col = position
                number = poster_page(record) or old_number
                if modes[index] and poster_position(record) is None and poster_zoom(record) != poster_zoom(old_record):
                    recalculate_pages.add(old_number)
                if poster_page(record) is not None and poster_position(record) is None and (
                        number != old_number or poster_page(record) != poster_page(old_record)):
                    recalculate_pages.update((old_number, number))
                while len(pages) < number:
                    pages.append([])
                pages[number - 1].append((index, row, col, row_span, col_span))
        pages = [sorted(page) for page in pages]
        resolved_modes = poster_layout_modes(metadata, settings.fixed, pages)
        mode_pages = {number for number, page in enumerate(pages, 1)
                      if any(resolved_modes[item[0]] != previous_layout.get("fixed_modes", modes)[item[0]]
                             for item in page)}
        if recalculate_pages:
            # A page override also reconsiders images previously deferred by flexible sizing.
            pages = poster_photo_pages(paths, metadata, settings)
            replanned_modes = poster_layout_modes(metadata, settings.fixed, pages)
            for number, page in enumerate(pages, 1):
                old_pages = previous_layout["pages"]
                if (number > len(old_pages) or page != old_pages[number - 1]["placements"] or
                        any(replanned_modes[item[0]] !=
                            previous_layout.get("fixed_modes", modes)[item[0]] for item in page)):
                    recalculate_pages.add(number)
        else:
            # A page mode edit repacks that page without moving untouched images
            # to other pages through the running explicit-page starting point.
            for number in mode_pages:
                indices = [item[0] for item in pages[number - 1]]
                replanned = poster_photo_pages([paths[index] for index in indices],
                    [metadata[index] for index in indices], settings, force_page=True,
                    fixed_modes=[resolved_modes[index] for index in indices])[0]
                pages[number - 1] = [(indices[local], *placement) for local, *placement in replanned]
            recalculate_pages.update(mode_pages)
    else:
        pages = poster_photo_pages(paths, metadata, settings)
    modes = poster_layout_modes(metadata, settings.fixed, pages)
    if layout_state is not None:
        layout_state.update(metadata=[dict(record) for record in metadata], fixed_modes=list(modes), pages=[],
                            source_cache=metadata_cache)
    outputs = []
    try:
        for number, page in enumerate(pages, 1):
            progress(f"Poster page: {number}/{len(pages)}")
            indices = [item[0] for item in page]
            page_title = next((str(metadata[index].get("box", "")).strip()
                               for index in indices if str(metadata[index].get("box", "")).strip()), "")
            page_state = {}
            render_options = {}
            if layout_state is not None:
                render_options["layout_state"] = page_state
            if previous_layout is not None:
                old_pages = previous_layout["pages"]
                if number <= len(old_pages):
                    old_title = old_pages[number - 1]["title"]
                    title_changed = any(index in changed and metadata[index].get("box", "") !=
                                        previous_layout["metadata"][index].get("box", "") for index in indices)
                    if not title_changed or len(changed) != len(paths):
                        page_title = old_title
                cached = {item[0]: (old_page, local) for old_page in old_pages
                          for local, item in enumerate(old_page["placements"])}
                old_locations = {item[0]: old_number for old_number, old_page in enumerate(old_pages, 1)
                                 for item in old_page["placements"]}
                previous_page = {key: [cached[index][0][key][cached[index][1]] for index in indices]
                                 for key in ("cells", "placements", "metadata", "panels")}
                if number <= len(old_pages):
                    previous_page.update(title=old_pages[number - 1]["title"], base=old_pages[number - 1].get("base"))
                    previous_page.update({key: old_pages[number - 1].get(key, {})
                                          for key in ("date_cache", "caption_cache")})
                previous_page["reflow"] = {
                    local for local, index in enumerate(indices)
                    if (number in recalculate_pages or index in changed and (old_locations[index] != number or
                        previous_layout.get("fixed_modes", modes)[index] != modes[index]))}
                render_options.update(previous_page=previous_page,
                                      changed_indices={local for local, index in enumerate(indices) if index in changed or number in recalculate_pages})
            def page_progress(message):
                progress(f"Page {number}/{len(pages)} · {message}")
            outputs.append(create_poster([paths[index] for index in indices], profile, settings, page_progress,
                                         title=page_title,
                                         metadata=[metadata[index] for index in indices],
                                         placements=[item[1:] for item in page], setup_dir=setup_dir,
                                         fixed_modes=[modes[index] for index in indices],
                                         selection_numbers=[index + 1 for index in indices], **render_options))
            if layout_state is not None:
                page_state["placements"] = list(page)
                page_state["title"] = page_title
                layout_state["pages"].append(page_state)
    except Cancelled as error:
        error.outputs = outputs
        raise
    return outputs


def poster_date_overlay(photo, text, font, rotation):
    """Put a readable date badge inside the photo's bottom-right corner."""
    if not text or rotation not in (0, 90):
        return
    left, top, right, bottom = font.getbbox(text)
    padding = max(1, round(font.size * .15))
    badge = Image.new("RGBA", (right - left + 2 * padding, bottom - top + 2 * padding),
                      (0, 0, 0, 64))  # 75% transparent = 25% opacity.
    ImageDraw.Draw(badge).text((padding - left, padding - top), text,
                               font=font, fill=(255, 255, 255, 255))
    if rotation == 90:
        badge = badge.rotate(90, expand=True)
    if badge.width > photo.width or badge.height > photo.height:
        raise ValueError("Poster date does not fit inside the image at POSTER-FONT-SIZE. "
                         "Reduce the font size or grid.")
    photo.alpha_composite(badge, (photo.width - badge.width, photo.height - badge.height))


def poster_load_photo(path, size):
    """Decode JPEGs near panel size, keeping full-resolution layout geometry.

    Leave at least twice the final resolution for the Lanczos filter. PNGs
    retain their full decode, including alpha; EXIF orientation is applied to
    both formats before resizing.
    """
    with Image.open(path) as source:
        swapped = source.getexif().get(274) in (5, 6, 7, 8)
        width, height = source.size[::-1] if swapped else source.size
        target_width, target_height = size
        if width / height > target_width / target_height:
            target_height = round(height / width * target_width)
        elif width / height < target_width / target_height:
            target_width = round(width / height * target_height)
        target = (max(1, target_width), max(1, target_height))
        if source.format == "JPEG":
            decode_size = (target[0] * 2, target[1] * 2)
            source.draft("RGB", decode_size[::-1] if swapped else decode_size)
        photo = ImageOps.exif_transpose(source).convert("RGBA")
        return photo.resize(target, Image.Resampling.LANCZOS)


def create_poster(paths, profile, settings, progress=lambda _: None, *,
                  title=None, metadata=None, placements=None, setup_dir=None,
                  layout_state=None, previous_page=None, changed_indices=None, fixed_modes=None, frozen_cells=None, selection_numbers=None):
    paths = list(paths)
    if not paths and placements != []:
        raise ValueError(f"Select between 1 and {settings.cols * settings.rows} images.")
    if any(path.suffix.lower() not in {".jpg", ".jpeg", ".png"} for path in paths):
        raise ValueError("Posters require JPG or PNG images.")
    scale = settings.limit / max(settings.height, settings.width)
    width, height = max(1, round(settings.width * scale)), max(1, round(settings.height * scale))
    left_edge, top_edge = round(settings.margin_left * scale), round(settings.margin_top * scale)
    right_edge = width - round(settings.margin_right * scale)
    bottom_edge = height - round(settings.margin_bottom * scale)
    content_width, content_height = right_edge - left_edge, bottom_edge - top_edge
    if content_width <= 0 or content_height <= 0:
        raise ValueError(f"POSTER-SIZE={settings.height:g}x{settings.width:g} mm leaves no image area: "
                         f"left+right margins={settings.margin_left + settings.margin_right:g} mm, "
                         f"top+bottom margins={settings.margin_top + settings.margin_bottom:g} mm.")
    if metadata is None:
        metadata = [read_metadata(path) for path in paths]
    if len(paths) > settings.cols * settings.rows and not any(poster_position(record) is not None or poster_page(record) is not None for record in metadata):
        raise ValueError(f"Select between 1 and {settings.cols * settings.rows} images.")
    if title is None:
        title = str(metadata[0].get("box", "")) if metadata else ""
    gap = max(1, round(3 * scale))
    title_font, title_lines, title_line_height, title_height = poster_text(
        [title], content_width, content_height * .15, round(4 * scale), round(1.2 * scale), True)
    grid_top = top_edge + title_height + (gap if title_height else 0)
    bounds = (left_edge, grid_top, right_edge, bottom_edge)
    if placements is None:
        pages = poster_photo_pages(paths, metadata, settings)
        if len(pages) != 1:
            raise ValueError("Selection needs multiple posters; use create_posters.")
        placements = [item[1:] for item in pages[0]]
    frozen_base = previous_page.get("base") if previous_page is not None and title == previous_page.get("title") else None
    if not paths:
        if layout_state is not None:
            layout_state.update(cells=[], panels=[], metadata=[])
        canvas = frozen_base.copy() if frozen_base is not None else poster_background((width, height), settings.background_color, setup_dir, progress)
        if frozen_base is None:
            poster_draw_text(ImageDraw.Draw(canvas), title_lines[0], title_font, title_line_height,
                             left_edge, top_edge, content_width)
        if layout_state is not None:
            layout_state["base"] = canvas.convert("RGBA")
        return save_poster_canvas(canvas, profile, settings, width, height)
    modes = fixed_modes if fixed_modes is not None else [poster_page_mode(metadata, range(len(metadata)), settings.fixed)[0]] * len(metadata)
    if all(modes) or any(poster_position(record) is not None for record in metadata):
        cells = poster_grid_cells(placements, settings.cols, settings.rows, bounds, gap)
    elif any(modes):
        cells = poster_flexible_cells(placements, bounds, gap, cols=settings.cols, rows=settings.rows,
                                      fixed_modes=modes)
    else:
        cells = poster_flexible_cells(placements, bounds, gap)
    if frozen_cells is not None:
        cells = [tuple(cell) for cell in frozen_cells]
    if previous_page is not None:
        cells = [previous_page["cells"][index] if index not in changed_indices or
                 (index not in previous_page.get("reflow", ()) and
                  tuple(placement) == tuple(previous_page["placements"][index][1:])) else cells[index]
                 for index, placement in enumerate(placements)]
    if layout_state is not None:
        layout_state.update(cells=list(cells), panels=[None] * len(paths), metadata=[dict(record) for record in metadata])
    cell_width = min(right - left for left, top, right, bottom in cells)
    cell_height = min(bottom - top for left, top, right, bottom in cells)
    if min(cell_width, cell_height) < 2:
        raise ValueError("Poster grid does not fit inside the margins. Increase POSTER-SIZE/LIMIT or reduce COLS/ROWS.")
    inset = max(1, round(.3 * scale)) + max(0, round(settings.image_padding * scale))
    cell_width -= 2 * inset
    cell_height -= 2 * inset
    if min(cell_width, cell_height) < 2:
        raise ValueError("Poster cells are too small for frames. Increase SIZE/LIMIT or reduce COLS/ROWS.")
    text_size = (max(1, round(settings.font_size * 25.4 / 72 * scale))
                 if settings.font_size is not None else round(3 * scale))
    text_minimum = text_size if settings.font_size is not None else round(1 * scale)
    date_font = poster_font(text_size)
    old_dates = previous_page.get("date_cache", {}) if previous_page is not None else {}
    date_cache, dates = {}, []
    for path, record in zip(paths, metadata):
        if settings.date_rotation in (0, 90):
            key = (Path(path).resolve(), file_stamp(path), str(record.get("created", "")))
            dates.append(old_dates[key] if key in old_dates else poster_date(path, record))
            date_cache[key] = dates[-1]
        else:
            dates.append("")
    caption_cache = dict(previous_page.get("caption_cache", {})) if previous_page is not None else {}
    comments = [poster_caption(record) for record in metadata]
    captions, caption_height = markdown_render.fit(
        comments, int(cell_width), cell_height * .4, text_size, text_minimum,
        preserve_whitespace=True, cache=caption_cache)
    caption_space = caption_height + (max(1, round(scale)) if caption_height else 0)
    canvas = frozen_base.copy() if frozen_base is not None else poster_background((width, height), settings.background_color, setup_dir, progress)
    if frozen_base is None:
        poster_draw_text(ImageDraw.Draw(canvas), title_lines[0], title_font, title_line_height,
                         left_edge, top_edge, content_width)
    output_canvas = canvas.convert("RGBA")
    if layout_state is not None:
        layout_state["base"] = output_canvas.copy()
        layout_state.update(date_cache=date_cache, caption_cache=caption_cache)

    def composite_cell(layer, cell, origin):
        left, top, right, bottom = cell
        clipped = (max(left, bounds[0]), max(top, bounds[1]),
                   min(right, bounds[2]), min(bottom, bounds[3]))
        x, y, r, b = clipped
        if r <= x or b <= y:
            if layout_state is not None:
                layout_state["panels"][index] = (Image.new("RGBA", (1, 1)), (0, 0))
            return
        opacity = 1 - poster_transparency(metadata[index], settings.transparency or 0) / 100
        def panel_color(value):
            red, green, blue, alpha = poster_color(value)
            return red, green, blue, round(alpha * opacity)
        panel = Image.new("RGBA", (r - x, b - y), panel_color(settings.image_background_color))
        border = Image.new("RGBA", panel.size)
        ImageDraw.Draw(border).rectangle((0, 0, panel.width - 1, panel.height - 1),
                                         outline=panel_color(settings.frames), width=frame_width)
        panel.alpha_composite(border)
        if r - x > 2 * inset and b - y > 2 * inset:
            ox, oy = origin
            interior = layer.crop((x + inset - ox, y + inset - oy, r - inset - ox, b - inset - oy))
            panel.alpha_composite(interior, (inset, inset))
        output_canvas.alpha_composite(panel, (x, y))
        if layout_state is not None:
            layout_state["panels"][index] = (panel, (x, y))

    selection_numbers = selection_numbers if selection_numbers is not None else list(range(1, len(paths) + 1))
    draw_order = sorted(range(len(paths)), key=lambda index: (poster_zorder(metadata[index], selection_numbers[index]), index))
    for index in draw_order:
        path = paths[index]
        if previous_page is not None and index not in changed_indices:
            panel, position = previous_page["panels"][index]
            output_canvas.alpha_composite(panel, position)
            if layout_state is not None:
                layout_state["panels"][index] = (panel, position)
            continue
        progress(f"Poster: {index + 1}/{len(paths)} — {path.name}")
        left, top, right, bottom = cells[index]
        frame_width = max(1, round(.3 * scale))
        origin = (max(left, bounds[0]), max(top, bounds[1]))
        panel_size = (max(1, min(right, bounds[2]) - origin[0]),
                      max(1, min(bottom, bounds[3]) - origin[1]))
        canvas = Image.new("RGBA", panel_size)
        draw = ImageDraw.Draw(canvas)
        if right <= bounds[0] or bottom <= bounds[1] or left >= bounds[2] or top >= bounds[3]:
            composite_cell(canvas, cells[index], origin)
            continue
        inset = frame_width + max(0, round(settings.image_padding * scale))
        left, top, right, bottom = left + inset, top + inset, right - inset, bottom - inset
        left, right = left - origin[0], right - origin[0]
        top, bottom = top - origin[1], bottom - origin[1]
        angle = poster_angle(metadata[index])
        if angle:
            available_width, available_height = right - left, bottom - top
            # Compose in the photo's original orientation, then rotate the
            # entire photo/caption group clockwise into the panel.
            local_width, local_height = available_width, available_height
            if 45 <= angle % 180 <= 135:
                local_width, local_height = local_height, local_width
            documents, text_height = markdown_render.fit(
                [comments[index]], local_width, local_height * .4,
                text_size, text_minimum, preserve_whitespace=True, cache=caption_cache)
            text_gap = max(1, round(scale)) if text_height else 0
            image_height = local_height - text_height - text_gap
            if image_height < 1:
                raise ValueError("No room for images below poster captions.")
            photo = poster_load_photo(path, (local_width, image_height))
            poster_date_overlay(photo, dates[index], date_font, settings.date_rotation)
            group = Image.new("RGBA", (local_width, photo.height + text_gap + text_height))
            group.alpha_composite(photo, ((local_width - photo.width) // 2, 0))
            markdown_render.draw_poster(ImageDraw.Draw(group), documents[0],
                                        0, photo.height + text_gap, local_width)
            group = group.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=True)
            group = ImageOps.contain(group, (available_width, available_height), Image.Resampling.LANCZOS)
            canvas.alpha_composite(group, (left + (available_width - group.width) // 2,
                                           top + (available_height - group.height) // 2))
            composite_cell(canvas, cells[index], origin)
            continue
        image_height = bottom - top - caption_space
        if image_height < 1:
            raise ValueError("No room for images below poster captions.")
        photo = poster_load_photo(path, (right - left, image_height))
        poster_date_overlay(photo, dates[index], date_font, settings.date_rotation)
        own_caption_height = captions[index].height
        text_gap = max(1, round(scale)) if own_caption_height else 0
        group_height = photo.height + text_gap + own_caption_height
        photo_top = top + (bottom - top - group_height) // 2
        canvas.alpha_composite(photo, (left + (right - left - photo.width) // 2, photo_top))
        markdown_render.draw_poster(draw, captions[index],
                                    left, photo_top + photo.height + text_gap, right - left)
        composite_cell(canvas, cells[index], origin)
    return save_poster_canvas(output_canvas, profile, settings, width, height)


def save_poster_canvas(canvas, profile, settings, width, height):
    output = io.BytesIO()
    canvas.convert("RGB").save(output, "JPEG", quality=95,
                dpi=(width * 25.4 / settings.width, height * 25.4 / settings.height))
    directory = profile.images.parent / "poster"
    if directory.is_symlink():
        raise ValueError("Poster directory must not be a symbolic link.")
    target = reserve_output(directory, datetime.now().strftime("poster_%Y%m%d_%H%M%S.jpg"))
    try:
        atomic_write(target, output.getvalue())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def files(folder, extensions, recursive=True):
    folder = Path(folder)
    if not folder.exists():
        return []
    result = []
    for base, directories, names in os.walk(folder, followlinks=False):
        directories[:] = (sorted(d for d in directories if not d.startswith(".") and not (Path(base) / d).is_symlink())
                          if recursive else [])
        for name in sorted(names):
            path = Path(base) / name
            if path.suffix.lower() in extensions and not path.is_symlink() and path.is_file():
                result.append(path)
    return sorted(result)


def category_folder(value):
    first = re.split(r"[,;|\r\n]", value or "")[0].split(">", 1)[0].strip().lower()
    if not first or first in ("ohne kategorie", ".", ".."):
        return "unassigned"
    return re.sub(r"[/\\\x00-\x1f\x7f]", "_", first)


def normalized(record):
    data = dict(record)
    data.pop("modified", None)
    data["anzahl"] = max(0, int(data.get("count", data.get("anzahl", data.get("Anzahl", 0))) or 0))
    data["package"] = data.get("pack", data.get("package", ""))
    data["comment"] = data.get("comment", data.get("kommentar", data.get("Kommentar", "")))
    category = next((v for k, v in data.items() if k.casefold() in ("category", "kategorie")), "")
    if isinstance(category, list):
        category = ", ".join(str(v) for v in category)
    data["category"] = str(category or "").upper()
    for field in FIELDS:
        data.setdefault(field, "")
    return data


def parse_metadata(raw):
    if not raw.strip():
        return normalized({})
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            records = [normalized(item) for item in data if isinstance(item, dict)]
            if not records:
                return normalized({})
            first = records[0]
            first["comment"] = "\n\n".join(str(item["comment"]) for item in records if item["comment"])
            first["category"] = ", ".join(dict.fromkeys(str(item["category"]) for item in records if item["category"]))
            return first
        if not isinstance(data, dict):
            raise ValueError("Not an object")
        return normalized(data)
    except (ValueError, TypeError):
        # Legacy comments use Anzahl | Kategorie | ... or a labelled category.
        labelled = re.search(r"(?:^|[|\r\n])[^\S\r\n]*Kategorie:[^\S\r\n]*([^|\r\n]*)", raw, re.IGNORECASE)
        fields = raw.split("|")
        category = labelled.group(1).strip() if labelled else fields[1].strip() if len(fields) > 1 else ""
        return normalized({"comment": raw, "category": category})


def run_tool(*arguments, timeout=300):
    result = subprocess.run(arguments, capture_output=True, timeout=timeout)
    if result.returncode:
        error = result.stderr.decode("utf-8", "replace").strip()
        raise ValueError(error or f"{arguments[0]} failed ({result.returncode}).")
    return result.stdout


def open_image_metadata(path):
    metadata = image_metadata_library().Metadata()
    metadata.open_path(str(path))
    return metadata


def legacy_image_comment(path, metadata):
    """Decode EXIF's character-code prefix, including Android's UTF-8 comments."""
    if not metadata.try_has_tag("Exif.Photo.UserComment"):
        return ""
    raw = bytes(metadata.try_get_tag_raw("Exif.Photo.UserComment").get_data())
    prefix, content = raw[:8], raw[8:]
    if prefix == b"UNICODE\0":
        if content.startswith((b"\xff\xfe", b"\xfe\xff")):
            return content.decode("utf-16").rstrip("\0")
        # Some writers label UTF-8 as UNICODE. Real UTF-16 Latin text contains NULs.
        if b"\0" not in content:
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                pass
        with Image.open(path) as image:
            little_endian = image.info.get("exif", b"")[6:8] == b"II"
        return content.decode("utf-16-le" if little_endian else "utf-16-be").rstrip("\0")
    if prefix == b"JIS\0\0\0\0\0":
        return content.decode("shift_jis").rstrip("\0")
    if prefix in (b"ASCII\0\0\0", b"\0" * 8):
        raw = content
    return raw.decode("utf-8").rstrip("\0")


def image_comment(metadata, complete=False):
    # GExiv2's generic multiple-value accessor splits lang-alt text at commas.
    # Read the actual RDF value so punctuation and line breaks remain intact.
    packet = metadata.try_get_xmp_packet()
    if not packet:
        return ""
    root = ET.fromstring(packet)
    dc = "{http://purl.org/dc/elements/1.1/}description"
    rdf = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
    lang = "{http://www.w3.org/XML/1998/namespace}lang"
    description = root.find(".//" + dc)
    if description is None:
        return next((node.attrib[dc] for node in root.iter() if dc in node.attrib), "")
    alternatives = description.findall(rdf + "Alt/" + rdf + "li")
    for item in alternatives:
        if item.get(lang) == "x-default":
            return "".join(item.itertext())
    if alternatives:
        return "" if complete else "".join(alternatives[0].itertext())
    return "".join(description.itertext())


def image_record(path, metadata):
    # A complete XMP record takes precedence, even when fields have been cleared.
    complete = metadata.try_has_tag("Xmp.KBox.version")
    record = {} if complete else parse_metadata(legacy_image_comment(path, metadata))
    for field, tag in XMP_FIELDS.items():
        if not metadata.try_has_tag(tag):
            continue
        if field == "comment":
            record[field] = image_comment(metadata, complete)
        else:
            record[field] = metadata.try_get_tag_string(tag) or ""
        # Legacy aliases must not override a more recent XMP property.
        if field == "anzahl":
            record.pop("count", None)
            record.pop("Anzahl", None)
        elif field == "package":
            record.pop("pack", None)
        elif field == "category":
            record = {key: value for key, value in record.items()
                      if key == "category" or key.casefold() not in ("category", "kategorie")}
    return normalized(record)


def image_capture_tags(metadata):
    names = {"DateTimeOriginal": "Exif.Photo.DateTimeOriginal", "CreateDate": "Exif.Photo.DateTimeDigitized",
             "OffsetTimeOriginal": "Exif.Photo.OffsetTimeOriginal", "OffsetTimeDigitized": "Exif.Photo.OffsetTimeDigitized"}
    return {name: metadata.try_get_tag_string(tag) for name, tag in names.items()
            if metadata.try_has_tag(tag)}


def video_key_comments(path, payload=None):
    """Read/update existing QuickTime Keys comments through Mutagen's atom engine.

    Mutagen's public writer handles moov.udta.meta only. Its atom writer also
    handles the moov.meta variant used by cameras, updating chunk offsets when
    a longer comment grows the file. Keep that compatibility code in one place.
    """
    from mutagen.mp4 import MP4Tags
    from mutagen.mp4._atom import Atom, Atoms

    comments = []
    for parent in ((b"moov", b"udta", b"meta"), (b"moov", b"meta")):
        with Path(path).open("r+b" if payload is not None else "rb") as stream:
            atoms = Atoms(stream)
            try:
                keys_atom = atoms[parent + (b"keys",)]
                ilst_path = atoms.path(*parent, b"ilst")
            except KeyError:
                continue
            ok, keys = keys_atom.read(stream)
            if not ok or len(keys) < 8:
                raise ValueError("Truncated MP4 Keys metadata.")
            position, indices = 8, []
            for index in range(1, int.from_bytes(keys[4:8], "big") + 1):
                size = int.from_bytes(keys[position:position + 4], "big")
                if size < 8 or position + size > len(keys):
                    raise ValueError("Invalid MP4 Keys metadata.")
                name = keys[position + 8:position + size]
                if name in (b"comment", b"com.apple.quicktime.comment"):
                    indices.append(index.to_bytes(4, "big").decode("latin-1"))
                position += size
            if not indices:
                continue

            class MetadataPath:
                def path(self, *names):
                    return ilst_path

            tags = MP4Tags(MetadataPath(), stream)
            for key in indices:
                if key in tags:
                    comments.extend(tags[key])
                if payload is not None:
                    tags[key] = [payload]
            if payload is not None:
                rendered = [tags._render(key, value) for key, value in tags.items()]
                # Preserve unknown binary metadata exactly as Mutagen's public save does.
                for key, values in tags._failed_atoms.items():
                    if key not in tags:
                        rendered.extend(Atom.render(key.encode("latin-1"), value) for value in values)
                tags._MP4Tags__save_existing(stream, atoms, ilst_path,
                                            Atom.render(b"ilst", b"".join(rendered)), None)
    return comments


def video_metadata(path):
    from mutagen.mp4 import MP4
    media = MP4(path)
    probe = json.loads(run_tool("ffprobe", "-v", "error", "-show_entries", "format_tags:stream_tags",
                                "-of", "json", str(path)))
    tags = probe.get("format", {}).get("tags", {})
    comments = (media.tags or {}).get("\xa9cmt")
    if comments is None:
        comments = video_key_comments(path)
    raw = comments[0] if comments else tags.get("comment", "")
    created = tags.get("creation_time", "")
    if not created:
        created = next((item["tags"]["creation_time"] for item in probe.get("streams", [])
                        if item.get("tags", {}).get("creation_time")), "")
    return raw, {"DateTimeOriginal": tags.get("com.apple.quicktime.creationdate", ""), "CreateDate": created}


def read_user_comment(path):
    """Compatibility accessor: return XMP fields as JSON, or the original legacy comment."""
    path = Path(path).resolve()
    if path.suffix.lower() == ".mp4":
        return video_metadata(path)[0]
    metadata = open_image_metadata(path)
    if any(metadata.try_has_tag(tag) for tag in (*XMP_FIELDS.values(), "Xmp.KBox.version")):
        return json.dumps(image_record(path, metadata), ensure_ascii=False)
    return legacy_image_comment(path, metadata)


def read_metadata(path):
    return read_metadata_batch([path])[0]


def file_stamp(path):
    info = Path(path).stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_metadata_batch(paths, progress=lambda _: None, *, cache=None):
    """Read each file once, retaining input order and independent duplicate records."""
    paths = [Path(path).resolve() for path in paths]
    unique = list(dict.fromkeys(paths))
    records = {}
    for index, path in enumerate(unique, 1):
        try:
            stamp = file_stamp(path) if cache is not None else None
            cached = cache.get(path) if cache is not None else None
            if cached is not None and cached[0] == stamp:
                record = dict(cached[1])
            else:
                if path.suffix.lower() == ".mp4":
                    raw, tags = video_metadata(path)
                    record = parse_metadata(raw)
                else:
                    metadata = open_image_metadata(path)
                    record = image_record(path, metadata)
                    tags = image_capture_tags(metadata)
                record["created"] = media_created(path, record.get("created"), tags=tags)
                if cache is not None and file_stamp(path) == stamp:
                    cache[path] = (stamp, dict(record))
            records[path] = record
        except Exception as error:
            raise ValueError(f"{path}: {error}") from error
        if index % 128 == 0 or index == len(unique):
            progress(f"Reading metadata: {index}/{len(unique)}")
    return [dict(records[path]) for path in paths]


def write_image_metadata(original, target, record, dimensions=None):
    metadata = open_image_metadata(original)
    metadata.try_clear_tag("Exif.Photo.UserComment")
    # Remove obsolete mirrored UserComment metadata, retaining unrelated XMP.
    metadata.try_clear_tag("Xmp.exif.UserComment")
    metadata.try_set_tag_string("Xmp.KBox.version", "1")
    for field, tag in XMP_FIELDS.items():
        if field in KBox_sync.SYNC_KEYS and field not in record: continue
        value = str(record.get(field, ""))
        if field == "comment":
            value = 'lang="x-default" ' + value
        if not metadata.try_set_tag_string(tag, value):
            raise ValueError(f"Cannot write {tag}.")
    if dimensions:
        metadata.try_set_orientation(image_metadata_library().Orientation.NORMAL)
        metadata.try_set_metadata_pixel_width(dimensions[0])
        metadata.try_set_metadata_pixel_height(dimensions[1])
        metadata.try_erase_exif_thumbnail()
    metadata.save_file(str(target))


def write_video_metadata(path, record):
    from mutagen.mp4 import MP4
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    media = MP4(path)
    if media.tags is None:
        media.add_tags()
    media.tags["\xa9cmt"] = [payload]
    media.save()
    video_key_comments(path, payload)


def filename_creation_date(path):
    """Recognize full dates, without treating counters or partial years as dates."""
    stem = Path(path).stem
    months = {name: number for number, names in enumerate((
        ('jan', 'january'), ('feb', 'february'), ('mar', 'march'),
        ('apr', 'april'), ('may',), ('jun', 'june'), ('jul', 'july'),
        ('aug', 'august'), ('sep', 'sept', 'september'), ('oct', 'october'),
        ('nov', 'november'), ('dec', 'december')), 1) for name in names}
    year = r'(?:19|20)\d{2}'
    patterns = [
        (rf'(?P<y>{year})(?P<m>\d{{2}})(?P<d>\d{{2}})', False),
        (rf'(?P<y>{year})[-_. ](?P<m>\d{{1,2}})[-_. ](?P<d>\d{{1,2}})', False),
        (rf'(?P<d>\d{{1,2}})[-_. ](?P<m>\d{{1,2}})[-_. ](?P<y>{year})', True),
        (rf'(?P<d>\d{{1,2}})[-_. ](?P<m>[A-Za-z]+)[-_. ](?P<y>{year})', False),
        (rf'(?P<m>[A-Za-z]+)[-_. ](?P<d>\d{{1,2}})[-_. ,]+(?P<y>{year})', False),
        (rf'(?P<y>{year})[-_. ](?P<m>[A-Za-z]+)[-_. ](?P<d>\d{{1,2}})', False),
    ]
    candidates = []
    for pattern, day_first in patterns:
        for match in re.finditer(r'(?<!\d)' + pattern + r'(?!\d)', stem):
            values = match.groupdict()
            month = months.get(values['m'].lower(), 0) if values['m'].isalpha() else int(values['m'])
            day = int(values['d'])
            if day_first and month > 12 and day <= 12:
                month, day = day, month  # Unambiguous US month/day/year.
            try:
                date = datetime(int(values['y']), month, day)
            except ValueError:
                continue
            tail = stem[match.end():]
            clock = re.match(r'^[Tt _.-]?(\d{2})[_.:-]?(\d{2})[_.:-]?(\d{2})(?!\d)', tail)
            if clock:
                try:
                    date = date.replace(hour=int(clock[1]), minute=int(clock[2]), second=int(clock[3]))
                except ValueError:
                    pass
            candidates.append((match.start(), date))
    # Compact date+time has no digit boundary after the date.
    for match in re.finditer(r'(?<!\d)((?:19|20)\d{12})(?!\d)', stem):
        try:
            candidates.append((match.start(), datetime.strptime(match[1], '%Y%m%d%H%M%S')))
        except ValueError:
            pass
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def selection_creation_time(path, discrepancy_seconds=48 * 3600, *, creation_dates=None):
    """Infer capture time for browsing only; never rewrite stored metadata."""
    path = Path(path)
    stat = path.stat()
    filesystem = [stat.st_mtime]
    birth = getattr(stat, 'st_birthtime', None)
    if birth is not None:
        filesystem.append(birth)
    # Unix ctime is an inode-change time, not a file-creation time.
    metadata_dates = []
    try:
        tags = (video_metadata(path)[1] if path.suffix.lower() == '.mp4'
                else image_capture_tags(open_image_metadata(path)))
    except Exception:
        tags = {}  # Unreadable metadata must not prevent selecting other files.
    for key, offset in (('DateTimeOriginal', 'OffsetTimeOriginal'), ('CreateDate', 'OffsetTimeDigitized')):
        value = re.sub(r'^(\d{4}):(\d{2}):(\d{2})', r'\1-\2-\3', str(tags.get(key, '')))
        try:
            date = datetime.fromisoformat(value)
            if date.tzinfo is None and tags.get(offset):
                date = datetime.fromisoformat(value + str(tags[offset]))
            metadata_dates.append(date.timestamp())
        except (ValueError, OverflowError, OSError):
            continue
    named = filename_creation_date(path)
    alternatives = metadata_dates + filesystem
    preferred = named.timestamp() if named else metadata_dates[0] if metadata_dates else min(filesystem)
    chosen = (min([preferred] + alternatives)
              if any(abs(preferred - date) > discrepancy_seconds for date in alternatives)
              else preferred)
    if creation_dates is not None:
        date = datetime.fromtimestamp(chosen)
        creation_dates[path] = f"{date.day}.{date.month}.{date.year}"
    return chosen


class SelectionMedia(list):
    """Ordered chooser paths and date labels computed in the same worker pass."""

    def __init__(self, paths, creation_dates):
        super().__init__(paths)
        self.creation_dates = creation_dates


def sort_media_for_selection(paths):
    ranked = []
    creation_dates = {}
    for path in paths:
        try:
            ranked.append((selection_creation_time(path, creation_dates=creation_dates), path))
        except FileNotFoundError:
            continue  # A file removed while the chooser loads is no longer selectable.
    return SelectionMedia(
        [path for _, path in sorted(ranked, key=lambda item: (-item[0], str(item[1]).casefold(), str(item[1])))],
        creation_dates)


def media_created(path, existing=None, *, tags=None):
    """Prefer capture metadata, then filename, saved creation date, and source mtime."""
    path = Path(path)
    if tags is None:
        tags = video_metadata(path)[1] if path.suffix.lower() == ".mp4" else image_capture_tags(open_image_metadata(path))
    for key, offset in (("DateTimeOriginal", "OffsetTimeOriginal"),
                        ("CreateDate", "OffsetTimeDigitized")):
        value = str(tags.get(key, ""))
        value = re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", value)
        try:
            date = datetime.fromisoformat(value)
            if date.tzinfo is None and tags.get(offset):
                date = datetime.fromisoformat(value + str(tags[offset]))
            return date.isoformat(timespec="seconds")
        except ValueError:
            continue
    # Camera/phone names, including IMG_20240102_030405 and 2024-01-02_03-04-05.
    pattern = (r"(?<!\d)(\d{4})[-_]?(\d{2})[-_]?(\d{2})"
               r"(?:[T _-]?(\d{2})[.:-]?(\d{2})[.:-]?(\d{2}))?(?!\d)")
    for match in re.finditer(pattern, path.stem):
        try:
            parts = match.groups()
            date = datetime(*(int(part) if part else 0 for part in parts))
            # Keep date-only names date-only instead of claiming a known capture time.
            return date.isoformat(timespec="seconds") if parts[3] else date.date().isoformat()
        except ValueError:
            continue
    return existing or datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def validate_record(values):
    data = dict(values)
    data["category"] = str(data.get("category", "") or "").upper()
    try:
        quantity = str(data.get("anzahl", "")).strip() or "0"
        if not re.fullmatch(r"[0-9]+", quantity) or int(quantity) > 2147483647:
            raise ValueError()
        data["anzahl"] = int(quantity)
    except (ValueError, TypeError):
        raise ValueError("Quantity must be an integer between 0 and 2147483647.")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    data["created"] = data.get("created") or now
    data.pop("modified", None)
    return data


def reserve_output(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    stem, extension = Path(name).stem, Path(name).suffix
    suffix = 0
    while True:
        path = directory / (f"{stem}_{suffix}{extension}" if suffix else name)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            return path
        except FileExistsError:
            suffix += 1


def _save_media(source, profile, metadata, image_width):
    source = Path(source)
    if source.is_symlink() or not source.is_file() or source.suffix.lower() not in IMPORTS:
        raise ValueError(f"Unsupported or unavailable media file: {source}")
    video = source.suffix.lower() == ".mp4"
    original = source.resolve()
    metadata = dict(metadata)
    metadata["category"] = str(metadata.get("category", "") or "").upper()
    metadata.pop("modified", None)
    metadata["created"] = read_metadata(original)["created"]
    editing = within(original, profile.images) and source.suffix.lower() in MEDIA
    destination = profile.images / category_folder(metadata.get("category", ""))
    if destination.is_symlink() or destination.resolve().parent != profile.images.resolve():
        raise ValueError("Invalid category folder.")
    destination.mkdir(parents=True, exist_ok=True)
    extension = ".mp4" if video else ".jpg"
    target = original if editing and original.parent == destination.resolve() else None
    reserved = False
    fd, temp_name = tempfile.mkstemp(prefix=".save-", suffix=extension, dir=destination)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        resized = False
        if video:
            shutil.copyfile(original, temporary)
        else:
            with Image.open(original) as image:
                if image.format not in ("JPEG", "MPO", "PNG"):
                    raise ValueError("Select a JPG, PNG or MP4 file.")
                display_width = image.height if image.getexif().get(274) in (5, 6, 7, 8) else image.width
                # Camera JPGs can contain MPF auxiliary images and open as MPO.
                # Export the primary frame as JPEG: copying MPF offsets while
                # rewriting metadata could leave the auxiliary images invalid.
                resized = image.format in ("PNG", "MPO") or display_width > image_width
                if resized:
                    converted = ImageOps.exif_transpose(image).convert("RGBA")
                    if converted.width > image_width:
                        height = max(1, round(converted.height * image_width / converted.width))
                        converted = converted.resize((image_width, height), Image.Resampling.LANCZOS)
                    rgb = Image.new("RGB", converted.size, "white")
                    rgb.paste(converted, mask=converted.getchannel("A"))
                    rgb.save(temporary, "JPEG", quality=92, icc_profile=image.info.get("icc_profile"))
                    width, height = rgb.size
                else:
                    shutil.copyfile(original, temporary)
        if video:
            write_video_metadata(temporary, metadata)
        else:
            write_image_metadata(original, temporary, metadata, (width, height) if resized else None)
        # Validate the newly written metadata before replacing any user file.
        result = read_metadata(temporary)
        mismatches = [field for field in (*FIELDS, "created")
                      if str(result.get(field, "")) != str(metadata.get(field, ""))]
        if mismatches:
            raise ValueError("Metadata verification failed; the source file was not changed. "
                             "Fields: " + ", ".join(mismatches))
        if target is None:
            target = reserve_output(destination, original.name if editing else original.stem + "_cb" + extension)
            reserved = True
        old_time = max(target.stat().st_mtime, original.stat().st_mtime) if editing else target.stat().st_mtime
        modified = max(time.time(), int(old_time) + 1)
        os.utime(temporary, (modified, modified))
        if editing:
            os.chmod(temporary, stat.S_IMODE(original.stat().st_mode))
        with temporary.open("rb") as saved:
            os.fsync(saved.fileno())
        os.replace(temporary, target)
        if editing and target != original:
            # Remove the source only after the new file has been verified and
            # installed. If removal fails, the reserved target is rolled back.
            original.unlink()
        reserved = False
        return target
    finally:
        temporary.unlink(missing_ok=True)
        if reserved:
            target.unlink(missing_ok=True)


def save_batch(paths, profile, record, image_width, progress=lambda _: None, records=None):
    selection = list(paths)
    if not selection:
        return selection
    store = sync_store(profile)
    with store.locked():
        try:
            store.capture()
        except Exception as error:
            raise BatchError(f"Saved 0 of {len(selection)} files. {selection[0].name}: {error}", selection, 0) from error
        for index, source in enumerate(selection):
            try:
                progress(f"Saving {index + 1}/{len(selection)}: {source.name}")
                selection[index] = _save_registered_media(
                    store, source, profile, records[index] if records is not None else record, image_width)
            except Exception as error:
                raise BatchError(f"Saved {index} of {len(selection)} files. {source.name}: {error}", selection, index) from error
    return selection


def box_filename(name):
    name = name.strip().lower()
    if not name or re.search(r"[/\\\x00-\x1f\x7f]", name):
        raise ValueError("Box must be a valid filename without slashes or control characters.")
    return name if name.endswith(".json") else name + ".json"


def records_from(value):
    if isinstance(value, list):
        result = value
    elif isinstance(value, dict):
        result = [value] if RECORD_KEYS.intersection(value) else [value[key] for key in sorted(value)]
    else:
        raise ValueError("Expected a JSON object or array.")
    if any(not isinstance(item, dict) for item in result):
        raise ValueError("JSON records must be objects.")
    return result


def load_box(path):
    return records_from(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def _save_box(path, record, selected=None):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Box file must not be a symbolic link.")
    records = load_box(path) if path.exists() else []
    if selected is not None and not 0 <= selected < len(records):
        raise ValueError("The selected record no longer exists. Reopen the box.")
    if selected is None:
        selected = next((i for i, item in enumerate(records) if item.get("device", "") == record.get("device", "")), None)
    if selected is None:
        record = dict(record)
        record.setdefault("id", str(uuid.uuid4()))
        records.append(record)
        selected = len(records) - 1
    else:
        item = records[selected]
        created = item.get("created")
        item.update(record)
        if created:
            item["created"] = created
        if "count" in item:
            item["count"] = record["anzahl"]
        if "pack" in item:
            item["pack"] = record.get("package", "")
    for item in records:
        item.pop("modified", None)
    atomic_write(path, json.dumps(records, ensure_ascii=False, indent=2) + "\n", newer=True)
    return records, selected


def sync_store(profile):
    def read(path):
        raw = read_user_comment(path)
        try:
            record = json.loads(raw)
            return record if isinstance(record, dict) else {"comment": raw}
        except ValueError:
            return {"comment": raw}
    def write(path, record):
        previous=path.stat()
        temporary=path.with_name('.sync-'+uuid.uuid4().hex+path.suffix)
        try:
            shutil.copyfile(path,temporary)
            if path.suffix.lower()=='.mp4': write_video_metadata(temporary,record)
            else: write_image_metadata(path,temporary,record)
            os.chmod(temporary,stat.S_IMODE(previous.st_mode))
            os.utime(temporary,ns=(previous.st_atime_ns,previous.st_mtime_ns))
            os.replace(temporary,path)
        finally: temporary.unlink(missing_ok=True)
    return KBox_sync.Store(profile.images, profile.data, read, write)


def profile_for_path(path):
    path = Path(path)
    root = next((p for p in path.parents if p.name in ('TXT', 'boxes', 'JPG')), path.parent)
    if root.name not in ('TXT','boxes','JPG') and path.suffix.lower() in MEDIA:
        return Profile('', '', path.parent, path.parent/'TXT', path.parent/'TXT', path.parent/'.indices', [], [])
    data = root.parent / 'TXT' if root.name == 'JPG' else root
    return Profile('', '', data.parent / 'JPG', data, data, data.parent / '.indices', [], [])


def save_box(path, record, selected=None):
    store = sync_store(profile_for_path(path))
    with store.locked():
        store.capture()
        result = _save_box(path, record, selected)
        store.capture(paths=[Path(path)])
        return load_box(path), result[1]


def save_media(source, profile, metadata, image_width):
    store = sync_store(profile)
    with store.locked():
        store.capture()
        return _save_registered_media(store, source, profile, metadata, image_width)


def _save_registered_media(store, source, profile, metadata, image_width):
    """Save one file after capture, while the caller holds the collection lock."""
    metadata = dict(metadata)
    if within(Path(source), profile.images):
        relative = Path(source).resolve().relative_to(profile.images.resolve()).as_posix()
        candidates = [op for versions in store.heads().values() for op in versions
                      if op['kind'] == 'media' and op['path'] == relative and op['action'] == 'put']
        matching_id = [op for op in candidates if op['id'] == metadata.get('id')]
        candidate = matching_id[0] if len(matching_id) == 1 else candidates[0] if len(candidates) == 1 else None
        if candidate is not None:
            metadata.update({key: candidate['record'][key] for key in ('id', 'revision')})
    metadata.setdefault("id", str(uuid.uuid4()))
    result = _save_media(source, profile, metadata, image_width)
    store.capture(paths=[result])
    store.embed_media(result)
    return result


def delete_entry(path, selected=None):
    path = Path(path)
    if path.is_symlink(): raise ValueError('Entry must not be a symbolic link.')
    store = sync_store(profile_for_path(path))
    with store.locked():
        store.delete(path, selected)


def delete_box(path):
    store=sync_store(profile_for_path(path))
    with store.locked():store.delete_box(path)


@dataclass
class Entry:
    source: Path
    data: dict
    media: bool
    index: int | None = None


def json_files(profile):
    return sorted(set(files(profile.data, {".json"}) + files(profile.legacy_data, {".json"})))


def index_entries(path, media):
    if media:
        return [Entry(path, read_metadata(path), True)]
    entries = []
    for index, raw in enumerate(load_box(path)):
        data = normalized(raw)
        data["box"] = data["box"] or path.stem
        entries.append(Entry(path, data, False, index))
    return entries


def write_index(profile, entries):
    for entry in entries:
        entry.data.pop("modified", None)
    (profile.index / "desktop-index.json").unlink(missing_ok=True)
    serialized = [dict(entry.data, path=entry.source.name) for entry in entries]
    atomic_write(profile.index / "records.json", json.dumps(serialized, ensure_ascii=False, indent=2) + "\n")
    # Keep the shared records.json format unchanged. The desktop cache needs
    # full paths to distinguish equal filenames in different category folders.
    cache = {"version": 1, "entries": [dict(source=str(entry.source), data=entry.data,
             media=entry.media, index=entry.index) for entry in entries]}
    atomic_write(profile.index / "desktop-index.json", json.dumps(cache, ensure_ascii=False) + "\n")


def build_index(target, progress=lambda _: None, *, metadata_cache=None):
    """Build one index over all configured profiles (or a standalone profile)."""
    entries = []
    profiles = target.profiles if isinstance(target, Settings) else [target]
    seen = set()
    sources = []
    for profile in profiles:
        for media, paths in ((True, files(profile.images, MEDIA)), (False, json_files(profile))):
            for path in paths:
                if path in seen:
                    continue
                seen.add(path)
                sources.append((path, media))
    metadata = dict(metadata_cache or {})
    unread = [path for path, media in sources if media and path not in metadata]
    if unread:
        metadata.update(zip(unread, read_metadata_batch(unread, progress)))
    for path, media in sources:
        progress(f"Reading {path.name}")
        try:
            entries.extend([Entry(path, metadata[path], True)] if media else index_entries(path, False))
        except Exception as error:
            raise ValueError(f"{path}: {error}") from error
    write_index(target, entries)
    return entries


def load_index(target):
    cache = json.loads((target.index / "desktop-index.json").read_text(encoding="utf-8"))
    if cache["version"] != 1 or not isinstance(cache["entries"], list):
        raise ValueError("Unsupported index cache; restart with --newindex")
    entries = []
    for item in cache["entries"]:
        if (not isinstance(item["source"], str) or not Path(item["source"]).is_absolute()
                or not isinstance(item["data"], dict) or not isinstance(item["media"], bool)
                or (item["index"] is not None and type(item["index"]) is not int)):
            raise ValueError("Invalid index entry; restart with --newindex")
        entries.append(Entry(Path(item["source"]), item["data"], item["media"], item["index"]))
    if isinstance(target, Settings):
        folders = [folder for profile in target.profiles for folder in (profile.images, profile.data)]
        entries = [entry for entry in entries if any(within(entry.source, folder) for folder in folders)]
    return entries


def ensure_index(settings, progress=lambda _: None, newindex=False):
    for profile in settings.profiles:
        for directory in (profile.images, profile.data):
            directory.mkdir(parents=True, exist_ok=True)
    if not newindex and (settings.index / "desktop-index.json").exists():
        return load_index(settings)
    metadata = {}
    for profile in settings.profiles:
        for warning in migrate_media(profile, progress, metadata_cache=metadata):
            progress(warning)
    return build_index(settings, progress, metadata_cache=metadata)


def available_categories(profile):
    categories = {value.casefold(): value for value in profile.categories}
    for folder in (profile.images, profile.data):
        for base, directories, _ in os.walk(folder, followlinks=False):
            directories[:] = [name for name in directories
                              if not name.startswith('.') and not (Path(base) / name).is_symlink()]
            for name in directories:
                if name.casefold() != 'unassigned':
                    categories.setdefault(name.casefold(), name.upper())
    return sorted(categories.values(), key=str.casefold)


class IndexMonitor:
    """Reconcile disk sources, waiting for two stable observations before reading."""

    def __init__(self):
        self.previous = {}
        self.indexed = {}
        self.initialized = False

    def poll(self, settings):
        current = {}
        for profile in settings.profiles:
            for path in files(profile.images, MEDIA) + json_files(profile):
                try:
                    stat = path.stat()
                    current[path] = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                except FileNotFoundError:
                    continue
        if not self.initialized:
            # Re-read existing sources as well: cached metadata may predate startup.
            self.indexed = {entry.source: None for entry in load_index(settings)}
            self.initialized = True
        changed = [path for path in current
                   if current[path] == self.previous.get(path)
                   and current[path] != self.indexed.get(path)]
        changed.extend(path for path in self.indexed if path not in current and path not in self.previous)
        self.previous = current
        if changed:
            update_index(settings, changed)
            for path in changed:
                if path in current:
                    self.indexed[path] = current[path]
                else:
                    self.indexed.pop(path, None)
        return {profile.id: available_categories(profile) for profile in settings.profiles}


def update_index(target, changed, progress=lambda _: None):
    """Replace changed sources while retaining entries from every other profile."""
    try:
        entries = load_index(target)
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        # A missing or stale cache must not block saving an otherwise valid
        # image. Rebuild from the configured profiles and current file data.
        return build_index(target, progress)
    paths = set(map(Path, changed))
    entries = [entry for entry in entries if entry.source not in paths]
    media_paths = sorted(path for path in paths if path.exists() and path.suffix.lower() in IMPORTS)
    metadata = dict(zip(media_paths, read_metadata_batch(media_paths, progress))) if media_paths else {}
    for path in sorted(paths):
        progress(f"Updating index: {path.name}")
        if path.exists():
            entries.extend([Entry(path, metadata[path], True)] if path in metadata else index_entries(path, False))
    entries.sort(key=lambda entry: (not entry.media, entry.source, entry.index or 0))
    write_index(target, entries)
    return entries


def compile_query(values):
    result = {}
    for key, value in values.items():
        if value:
            if key == "category":
                result[key] = str(value).casefold()
                continue
            try:
                result[key] = re.compile(value, re.IGNORECASE)
            except re.error as error:
                raise ValueError(f"{LABELS[FIELDS.index(key)]}: invalid regular expression: {error}") from error
    return result


def search(entries, patterns):
    def match(entry):
        for field, pattern in patterns.items():
            if field == "category":
                if not str(entry.data.get(field, "")).casefold().startswith(pattern):
                    return False
                continue
            targets = ("device", "alias", "comment") if field in ("alias", "comment") else (field, "comment") if field == "device" else (field,)
            if not any(pattern.search(str(entry.data.get(key, ""))) for key in targets):
                return False
        return True
    return [entry for entry in entries if match(entry)]


def related_media(hit, entries):
    if hit.media:
        return hit.source
    return next((entry.source for entry in entries if entry.media and hit.data.get("box")
                 and str(entry.data.get("box", "")).casefold() == str(hit.data["box"]).casefold()
                 and entry.data.get("device", "") == hit.data.get("device", "")), None)


def migrate_media(profile, progress=lambda _: None, *, metadata_cache=None):
    warnings = []
    sources = [source for source in files(profile.images, MEDIA) if source.parent == profile.images]
    try:
        metadata = dict(zip(sources, read_metadata_batch(sources, progress))) if sources else {}
    except Exception:
        # Keep migration's per-file warnings and continue with readable files.
        metadata = {}
    for source in sources:
        try:
            progress(f"Organizing {source.name}")
            record = metadata[source] if source in metadata else read_metadata(source)
            directory = profile.images / category_folder(record.get("category", ""))
            if directory.is_symlink():
                raise ValueError("Category directory is a symbolic link.")
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / source.name
            # An exclusive hard link makes collisions safe; no existing file is overwritten.
            os.link(source, target)
            source.unlink()
            if metadata_cache is not None:
                metadata_cache[target] = record
        except Exception as error:
            warnings.append(f"{source.name}: {error}")
    return warnings


def load_preview(path, maximum=(1600, 1200)):
    path = Path(path)
    if path.suffix.lower() == ".mp4":
        import io
        image = Image.open(io.BytesIO(run_tool("ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
                                               "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease",
                                               "-f", "image2pipe", "-vcodec", "png", "-", timeout=60)))
    else:
        image = Image.open(path)
    with image:
        # Resize before transposing/converting: JPEG thumbnail() can use decoder
        # downsampling, avoiding multiple full-resolution image buffers.
        target = maximum[::-1] if image.getexif().get(274) in (5, 6, 7, 8) else maximum
        image.thumbnail(target, Image.Resampling.LANCZOS)
        result = ImageOps.exif_transpose(image).convert("RGB")
        return result.copy()


def _legacy_upload(settings, profile, cancel: threading.Event, progress=lambda _: None):
    import paramiko
    def check():
        if cancel.is_set():
            raise Cancelled("Upload cancelled. Local files are retained.")
    ssh = settings.ssh
    key, hosts = Path(ssh["keyfile"]).expanduser(), Path(ssh["knownhosts"]).expanduser()
    if not key.is_file() or not hosts.is_file():
        raise ValueError("SSH key or known_hosts is missing. Configure KeyFile and KnownHosts in Setup → [SSH].")
    upload_root = settings.root
    pending, destinations = [], set()
    for root, candidates, remote_root in (
            (profile.images, files(profile.images, MEDIA), ssh["imagedestination"]),
            (profile.data, json_files(profile), ssh["datadestination"])):
        for file in candidates:
            if not within(file, upload_root):
                continue
            relative_root = root if within(file, root) else profile.legacy_data
            remote = posixpath.join(remote_root, file.relative_to(relative_root).as_posix())
            if remote in destinations:
                raise ValueError(f"Duplicate upload destination: {remote}")
            destinations.add(remote)
            pending.append((file, remote))
    if not pending:
        progress("No eligible files to upload. Only files below the installation directory are uploaded.")
        return
    client = paramiko.SSHClient()
    client.load_host_keys(str(hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    stop_watcher = threading.Event()
    def watch_cancel():
        while not stop_watcher.wait(.2):
            if cancel.is_set():
                client.close()
                return
    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()
    stage, copied, skipped = "Connect", 0, 0
    try:
        check()
        progress(f"Connecting to {ssh['host']} …")
        client.connect(ssh["host"], port=int(ssh["port"]), username=ssh["user"], key_filename=str(key),
                       allow_agent=False, look_for_keys=False, timeout=15, auth_timeout=15, banner_timeout=15)
        check()
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(20)
        directories = set()
        def mkdirs(directory):
            if directory in ("", ".", "/") or directory in directories:
                return
            check()
            try:
                item = sftp.stat(directory)
                if not stat.S_ISDIR(item.st_mode):
                    raise ValueError(f"Not a server directory: {directory}")
            except OSError as error:
                if error.errno != errno.ENOENT:
                    raise
                mkdirs(posixpath.dirname(directory))
                sftp.mkdir(directory)
                progress(f"Created {directory}")
            directories.add(directory)
        for local, remote in pending:
            check()
            stage = f"Upload {local.name} → {remote}"
            local_stat = local.stat()
            try:
                if sftp.stat(remote).st_mtime >= int(local_stat.st_mtime):
                    skipped += 1
                    progress(f"Skipped unchanged/newer: {remote}")
                    continue
            except OSError as error:
                if error.errno != errno.ENOENT:
                    raise
            mkdirs(posixpath.dirname(remote))
            temporary = remote + "." + uuid.uuid4().hex[:12] + ".upload"
            progress(stage)
            sftp.put(str(local), temporary, callback=lambda sent, total: check())
            check()
            if local.stat().st_mtime_ns != local_stat.st_mtime_ns or local.stat().st_size != local_stat.st_size:
                raise ValueError(f"Local file changed during upload: {local}")
            sftp.utime(temporary, (int(local_stat.st_atime), int(local_stat.st_mtime)))
            check()
            sftp.posix_rename(temporary, remote)
            copied += 1
        progress(f"Completed: {copied} uploaded, {skipped} unchanged/newer.")
    except Exception as error:
        if cancel.is_set():
            raise Cancelled("Upload cancelled. Local files are retained.") from error
        raise ValueError(f"{stage}: {error}\nLocal files are retained; retry using Upload.") from error
    finally:
        stop_watcher.set()
        client.close()


def upload(settings, profile, cancel: threading.Event, progress=lambda _: None, initialize=False):
    settings.assert_current()
    progress('Preparing synchronization …')
    progress('Installation: '+settings.ssh['installation'])
    progress('Box: '+profile.title)
    store = sync_store(profile)
    with store.locked():
        ssh = settings.ssh
        def check():
            settings.assert_current()
            if cancel.is_set(): raise Cancelled('Synchronization cancelled; retry manually.')
        check()
        transport = ssh.get('transport', 'sftp').lower()
        transfer_started = False
        try:
            if transport in ('ssh','rsync'):
                transfer_started = True
                sync_transport.shell_exchange(store, ssh, transport, cancel, progress, initialize=initialize)
            else:
                with sync_transport.sftp_connection(ssh,cancel,progress) as sftp:
                    transfer_started = True
                    sync_transport.sftp_exchange(store,sftp,posixpath.join(ssh.get('sftpdatadestination',ssh['datadestination']),'.chaosbox-sync'),check,progress,initialize=initialize)
        except Exception as error:
            if cancel.is_set(): raise Cancelled('Synchronization cancelled; retry manually.') from error
            if not transfer_started or isinstance(error,sync_transport.RemoteLockError): raise
            progress('Refreshing local index after synchronization error …')
            try: build_index(settings,progress)
            except Exception as error: progress(f'Local index update failed: {error}')
            raise
        check()
        conflicts={ident:versions for ident,versions in store.heads().items()
                   if len(versions)>1 and versions[0]['kind']!='device'}
        progress('Updating local search index …')
        build_index(settings, progress)
        progress(f'Synchronization completed. {len(conflicts)} conflict(s); all versions retained.')
        if conflicts: progress('Resolve with: python3 sync-data.py --data PROFILE_DIRECTORY --conflicts')
