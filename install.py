#!/usr/bin/env python3
"""Install this desktop build for the current user without root or network access."""
import argparse
import os
import shlex
import shutil
from pathlib import Path

import core


def install(home=None, credentials=True, installdir=None):
    home = Path.home() if home is None else Path(home)
    source = Path(__file__).resolve().parent
    settings = core.Settings(installdir if installdir is not None else home / "ChaosBox")
    seed_assets = not settings.path.exists()
    settings.root.mkdir(parents=True, exist_ok=True)
    settings.ensure(default_box="ChaosBox")
    shared = home / ".local/share/chaosbox/desktop"
    shared.mkdir(parents=True, exist_ok=True)
    for name in ("app.py", "core.py", "setup.ini", "chaosbox.svg", "chaosbox.png"):
        core.atomic_write(shared / name, (source / name).read_bytes(), mode=0o644)
    installations = shared.parent / "installations"
    target = installations / settings.app_id
    target.mkdir(parents=True, exist_ok=True)
    # Upgrade launchers produced by the per-box installer, retaining their
    # installation arguments verbatim (including shell quoting and "$@").
    for existing in installations.glob("*/run-desktop.sh"):
        old_command = f'exec /usr/bin/python3 {shlex.quote(str(existing.parent / "desktop/app.py"))} '
        new_command = f'exec /usr/bin/python3 {shlex.quote(str(shared / "app.py"))} '
        script = existing.read_text(encoding="utf-8")
        migrated = "".join(new_command + line[len(old_command):] if line.startswith(old_command) else line
                           for line in script.splitlines(keepends=True))
        if migrated != script:
            core.atomic_write(existing, migrated, mode=0o755)
    launcher = target / "run-desktop.sh"
    script = ('#!/usr/bin/env bash\nset -euo pipefail\n'
              f'exec /usr/bin/python3 {shlex.quote(str(shared / "app.py"))} '
              f'--installdir {shlex.quote(str(settings.root))} "$@"\n')
    core.atomic_write(launcher, script, mode=0o755)
    state = settings.state_dir
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    for profile in settings.profiles:
        for directory in (profile.images, profile.data, profile.index):
            directory.mkdir(parents=True, exist_ok=True)
    first = settings.profiles[0]
    if seed_assets:
        def copy_missing(source_file, destination_file):
            if not Path(destination_file).exists():
                shutil.copy2(source_file, destination_file)
            return destination_file
        for name, destination in (("JPG", first.images), ("TXT", first.data)):
            shutil.copytree(source / "assets" / name, destination, dirs_exist_ok=True,
                            copy_function=copy_missing)
    if credentials:
        credential_dir = state / "credentials"
        credential_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(credential_dir, 0o700)
        # Reuse the same local SSH credentials as Android; never include them in desktop packages.
        assets = source.parent / "app/src/main/assets"
        for name in ("android_copy", "known_hosts"):
            destination = credential_dir / name
            if not destination.exists() and (assets / name).is_file():
                core.atomic_write(destination, (assets / name).read_bytes(), mode=0o600)
    def quoted(path):
        return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$') + '"'
    desktop = home / ".local/share/applications" / f"{settings.app_id}.desktop"
    core.atomic_write(desktop, "[Desktop Entry]\nType=Application\nVersion=1.0\n"
                      f"Name={settings.title}\n"
                      "Comment=Edit image and video metadata, JSON records and synchronize with SSH\n"
                      f"Exec={quoted(launcher)}\nIcon={shared / 'chaosbox.svg'}\n"
                      "Terminal=false\nCategories=Graphics;Utility;\nStartupNotify=true\n"
                      f"StartupWMClass={settings.window_class}\n", mode=0o644)
    return target, settings.path, desktop


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update the shared app for all boxes and install a box launcher")
    parser.add_argument("--installdir", type=Path, default=Path.home() / "ChaosBox")
    args = parser.parse_args()
    try:
        target, setup, desktop = install(installdir=args.installdir)
    except FileExistsError as error:
        parser.exit(1, f"Installation aborted: a required directory is occupied by a file: {error.filename}\n")
    print(f"Updated shared app for all boxes: {target.parent.parent / 'desktop'}\n"
          f"Setup: {setup}\nApplication menu: {desktop}\n"
          "Reopen running boxes to use the new version.")
