"""Small poster configurations containing source references and layout data only."""
from dataclasses import asdict
import json
import math
import os
import re
from pathlib import Path
import zipfile
from types import SimpleNamespace

import core


FORMAT = "chaosbox-poster"
SCHEMA = 3


def poster_filename(original, paths):
    """Use real source folder names, retaining their selection order."""
    folders = []
    for path in paths:
        folder = re.sub(r"[/\\\x00-\x1f\x7f]", "_", Path(path).parent.name).strip(". ")
        if folder and folder not in folders:
            folders.append(folder)
    category = "_".join(folders) or "unassigned"
    # Leave room for the timestamp, page/collision suffix and extension.
    category = category.encode("utf-8")[:120].decode("utf-8", errors="ignore")
    stem = Path(original).stem.removeprefix("poster_")
    return f"poster_{category}_{stem}.jpg"


def reserve_poster_pair(directory, name, *, with_project=True):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("Poster directory must not be a symbolic link.")
    directory.mkdir(parents=True, exist_ok=True)
    stem, extension = Path(name).stem, Path(name).suffix
    number = 0
    while True:
        target = directory / (f"{stem}_{number}{extension}" if number else name)
        project = target.with_suffix(".cbposter")
        number += 1
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        if not with_project:
            if project.exists():
                target.unlink()
                continue
            return target, None
        try:
            fd = os.open(project, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            target.unlink()
            continue
        except Exception:
            target.unlink(missing_ok=True)
            raise
        os.close(fd)
        return target, project


def project_files(directory):
    """List configurations in the box's fixed poster folder, newest first."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted((path for path in directory.iterdir() if path.is_file() and
                   path.suffix.lower() == ".cbposter"),
                  key=lambda path: (-path.stat().st_mtime_ns, path.name.casefold()))


def _relative(path, project):
    return os.path.relpath(Path(path).resolve(), project.parent)


def _write_manifest(path, manifest):
    manifest = dict(manifest)
    manifest["schema"] = SCHEMA
    manifest["pages"] = [{key: page[key] for key in ("title", "placements", "cells")}
                         for page in manifest["pages"]]
    core.atomic_write(path, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n")


def save_project(path, paths, settings, layout, outputs, setup_dir, profile_id):
    path = Path(path).absolute()
    if path.resolve() in {Path(source).resolve() for source in paths}:
        raise ValueError("A poster project cannot replace one of its source images.")
    if len(layout["pages"]) != len(outputs) or len(paths) != len(layout["metadata"]):
        raise ValueError("Poster project preview does not match its image layout.")
    manifest = dict(format=FORMAT, schema=SCHEMA, settings=asdict(settings),
                    profile_id=profile_id, setup_dir=_relative(setup_dir, path),
                    sources=[_relative(source, path) for source in paths],
                    metadata=layout["metadata"], fixed_modes=layout["fixed_modes"],
                    poster_names=[Path(output).name for output in outputs], pages=layout["pages"])
    _write_manifest(path, manifest)


def _settings(values):
    settings = core.PosterSettings(**values)
    names = {"limit": "LIMIT", "cols": "COLS", "rows": "ROWS", "fixed": "FIX"}
    lines = [f"POSTER-SIZE={settings.height}x{settings.width}"]
    for key, value in asdict(settings).items():
        if key in {"height", "width"}:
            continue
        name = names.get(key, key.replace("_", "-").upper())
        lines.append(f"POSTER-{name}={str(value).lower() if isinstance(value, bool) else value}")
    return core.poster_settings("[Poster]\n" + "\n".join(lines))


def load_project(path, output_dir):
    """Rebuild the preview from referenced originals, preserving saved geometry."""
    path, output_dir = Path(path).absolute(), Path(output_dir)
    try:
        legacy = zipfile.is_zipfile(path)
        if legacy:
            # Old archives remain readable, but their embedded images are ignored.
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("project.json"))
        else:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["format"] != FORMAT or manifest["schema"] not in (1, 2, SCHEMA):
            raise ValueError("Unsupported poster project format or version.")
        settings = _settings(manifest["settings"])
        sources = [(path.parent / source).resolve() for source in manifest["sources"]]
        records, modes = manifest["metadata"], manifest["fixed_modes"]
        if manifest["schema"] < 3:
            records = [dict(record) for record in records]
            for record in records:
                parts = str(record.get("device", "") or "").split("/")
                if not any(re.fullmatch(r"[A-Za-z]+[1-9][0-9]*", part.strip()) for part in parts):
                    parts += [""] * (5 - len(parts))
                    parts[1:2] = ["", ""]
                    record["device"] = "/".join(parts).rstrip("/")
            manifest["metadata"] = records
        if not sources or len(set(sources)) != len(sources) or len(sources) != len(records) or len(modes) != len(sources):
            raise ValueError("Invalid poster project image list.")
        if not all(isinstance(record, dict) for record in records) or not all(type(mode) is bool for mode in modes):
            raise ValueError("Invalid poster project image metadata.")
        for record in records:
            core.poster_transform(record)
        missing = [source for source in sources if not source.is_file()]
        if missing:
            raise FileNotFoundError("Poster project images are missing:\n" + "\n".join(map(str, missing)))
        if any(source.suffix.lower() not in {".jpg", ".jpeg", ".png"} for source in sources):
            raise ValueError("Poster projects require JPG or PNG images.")
        scale = settings.limit / max(settings.height, settings.width)
        size = max(1, round(settings.width * scale)), max(1, round(settings.height * scale))
        layout = dict(metadata=records, fixed_modes=modes, pages=[])
        seen = set()
        if not manifest["pages"]:
            raise ValueError("Poster project has no pages.")
        for saved in manifest["pages"]:
            placements, cells = saved["placements"], saved["cells"]
            if len(placements) != len(cells):
                raise ValueError("Invalid poster project page layout.")
            page = dict(title=str(saved["title"]), placements=[], cells=[])
            previous_index = -1
            for placement, cell in zip(placements, cells):
                if len(placement) != 5 or not all(type(placement[index]) is int for index in (0, 3, 4)) or not all(
                        type(value) in (int, float) and math.isfinite(value) for value in placement[1:3]):
                    raise ValueError("Invalid poster project placement.")
                index, row, col, rows, cols = placement
                if not 0 <= index < len(sources) or index <= previous_index or index in seen or not (
                        0 < rows <= settings.rows and cols > 0):
                    raise ValueError("Invalid poster project image order or placement.")
                previous_index = index
                seen.add(index)
                if len(cell) != 4 or not all(type(value) is int for value in cell) or not (
                        cell[0] < cell[2] and cell[1] < cell[3]):
                    raise ValueError("Invalid poster project image area.")
                page["placements"].append(tuple(placement))
                page["cells"].append(tuple(cell))
            layout["pages"].append(page)
        if seen != set(range(len(sources))):
            raise ValueError("Poster project layout is missing images.")
        modes = core.poster_layout_modes(records, settings.fixed, layout["pages"])
        layout["fixed_modes"] = modes
        profile_id = manifest["profile_id"]
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError("Invalid poster project collection.")
        setup_dir = (path.parent / manifest["setup_dir"]).resolve()
        files = []
        profile = SimpleNamespace(images=output_dir / "JPG")
        for page in layout["pages"]:
            indices = [item[0] for item in page["placements"]]
            rendered = {}
            files.append(core.create_poster([sources[index] for index in indices], profile, settings,
                         title=page["title"], metadata=[records[index] for index in indices],
                         placements=[item[1:] for item in page["placements"]],
                         fixed_modes=[modes[index] for index in indices], frozen_cells=page["cells"],
                         selection_numbers=[index + 1 for index in indices],
                         layout_state=rendered, setup_dir=setup_dir))
            page.update(rendered)
        if legacy:
            # Once successfully rebuilt, replace the bulky archive with references.
            _write_manifest(path, manifest)
        return dict(paths=sources, settings=settings, layout=layout, outputs=files,
                    setup_dir=setup_dir, profile_id=profile_id, project_path=path,
                    poster_names=manifest.get("poster_names", []))
    except (KeyError, TypeError, IndexError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid poster project: {error}") from error
