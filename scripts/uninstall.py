#!/usr/bin/env python3
"""Remove a per-user KBox launcher, preserving all box data and settings."""
import argparse
import hashlib
import os
import shlex
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "src"))


from legacy_names import application_bases, installation_root, installation_title, LEGACY_SHARE, LEGACY_DEFAULT


def uninstall(home=None, installdir=None):
    home = Path.home() if home is None else Path(home)
    root = installation_root(installdir if installdir is not None else home / "KBox").resolve()
    removed = []
    for base in application_bases(home):
        _, files = _uninstall(home, root, base)
        removed.extend(files)
    # Old launchers may now use the new shared app. Keep it until all registries are empty.
    launchers = [launcher for base in application_bases(home)
                 for launcher in (base / "installations").glob("*/run-desktop.sh")]
    if not launchers:
        for base in application_bases(home):
            for name in ("VERSION", "app.py", "core.py", "poster_project.py", "KBox_sync.py", "chaos_sync.py",
                         "legacy_names.py", "sync_transport.py", "sync-data.py", "cleanup-sync.py", "i18n.py",
                         "markdown_render.py", "setup.ini", "KBox.svg", "KBox.png",
                         LEGACY_SHARE + ".svg", LEGACY_SHARE + ".png"):
                path = base / "desktop" / name
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    removed.append(path)
    return root, removed


def _uninstall(home, installdir, base):
    home = Path.home() if home is None else Path(home)
    root = Path(installdir if installdir is not None else home / "KBox").expanduser().resolve()
    prefix = "KBox" if base.name == "KBox" else LEGACY_SHARE
    app_id = prefix + "-" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    installations = base / "installations"
    target = installations / app_id
    # Never traverse a symlink to remove files outside the application directory.
    for directory in (home / ".local", home / ".local/share", base,
                      installations, target, base / "desktop",
                      base / "icons", home / ".local/bin",
                      home / ".local/share/applications"):
        if directory.is_symlink():
            raise ValueError(f"Refusing to uninstall through a symbolic link: {directory}")
    removed = []

    def remove(path):
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(path)

    def points_to_launcher(path):
        if path.is_symlink():
            return path.resolve() == (target / "run-desktop.sh").resolve()
        if not path.is_file():
            return False
        return str(target / "run-desktop.sh") in path.read_text(encoding="utf-8")

    title = installation_title(root) if base.name == "KBox" else ("KBox-" + root.name[5:] if root.name.casefold().startswith("KBox-".casefold()) else f"Chaosbox-{root.name}")
    for command in (home / ".local/bin" / prefix, home / ".local/bin" / app_id,
                    home / ".local/bin" / title):
        if points_to_launcher(command):
            remove(command)
    desktop_folder = home / "Desktop"
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))) / "user-dirs.dirs"
    if config.is_file():
        for line in config.read_text().splitlines():
            if line.startswith("XDG_DESKTOP_DIR="):
                values = shlex.split(line.split("=", 1)[1])
                if len(values) == 1:
                    candidate = Path(values[0].replace("${HOME}", str(home)).replace("$HOME", str(home)))
                    if candidate.is_absolute():
                        desktop_folder = candidate
    # Include the standard locations to clean up shortcuts after a locale change.
    for folder in {desktop_folder, home / "Desktop", home / "Schreibtisch"}:
        if folder == home:
            continue
        if any(parent.is_symlink() for parent in (folder, *folder.parents)):
            raise ValueError(f"Refusing to uninstall through a symbolic link: {folder}")
        shortcut = folder / f"{title}.desktop"
        if points_to_launcher(shortcut):
            remove(shortcut)

    remove(home / ".local/share/applications" / f"{app_id}.desktop")
    remove(target / "run-desktop.sh")
    remove(base / "icons" / f"{app_id}.png")
    # Leave directories, caches, credentials and any unrecognized files intact.
    return root, removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installdir", type=Path, default=Path.home() / "KBox")
    args = parser.parse_args()
    try:
        root, removed = uninstall(installdir=args.installdir)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Uninstall failed: {error}\n")
    print(f"Removed {len(removed)} application files.\nData and settings preserved: {root}")
