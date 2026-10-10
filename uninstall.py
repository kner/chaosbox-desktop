#!/usr/bin/env python3
"""Remove a per-user ChaosBox launcher, preserving all box data and settings."""
import argparse
import hashlib
from pathlib import Path


def uninstall(home=None, installdir=None):
    home = Path.home() if home is None else Path(home)
    root = Path(installdir if installdir is not None else home / "ChaosBox").expanduser().resolve()
    app_id = "chaosbox-" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    base = home / ".local/share/chaosbox"
    installations = base / "installations"
    target = installations / app_id
    # Never traverse a symlink to remove files outside the application directory.
    for directory in (home / ".local", home / ".local/share", base,
                      installations, target, base / "desktop",
                      home / ".local/share/applications"):
        if directory.is_symlink():
            raise ValueError(f"Refusing to uninstall through a symbolic link: {directory}")
    removed = []

    def remove(path):
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(path)

    remove(home / ".local/share/applications" / f"{app_id}.desktop")
    remove(target / "run-desktop.sh")
    # Shared code is still needed while another installation has a launcher.
    if not any(installations.glob("*/run-desktop.sh")):
        for name in ("app.py", "core.py", "markdown_render.py", "setup.ini", "chaosbox.svg", "chaosbox.png"):
            remove(base / "desktop" / name)
    # Leave directories, caches, credentials and any unrecognized files intact.
    return root, removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installdir", type=Path, default=Path.home() / "ChaosBox")
    args = parser.parse_args()
    try:
        root, removed = uninstall(installdir=args.installdir)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Uninstall failed: {error}\n")
    print(f"Removed {len(removed)} application files.\nData and settings preserved: {root}")
