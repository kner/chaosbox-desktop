#!/usr/bin/env python3
"""Install this desktop build for the current user without root or network access."""
import argparse
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import core


def installed_roots(home):
    """Read registered installation paths without executing their launchers."""
    roots = []
    installations = Path(home) / ".local/share/chaosbox/installations"
    for launcher in sorted(installations.glob("*/run-desktop.sh")):
        for line in launcher.read_text(encoding="utf-8").splitlines():
            if not line.startswith("exec "):
                continue
            command = shlex.split(line)
            root = None
            for index, argument in enumerate(command):
                if argument == "--installdir" and index + 1 < len(command):
                    root = Path(command[index + 1]).expanduser()
                    break
                if argument.startswith("--installdir="):
                    root = Path(argument.split("=", 1)[1]).expanduser()
                    break
            # Stale launchers must not recreate removed installations.
            if root is not None and root.is_absolute() and (root / "setup.ini").is_file():
                root = root.resolve()
                if root not in roots:
                    roots.append(root)
    return roots


def install_all(home=None, credentials=True, installdir=None):
    """Update registered boxes, or create the default box on first install."""
    home = Path.home() if home is None else Path(home)
    roots = [installdir] if installdir is not None else installed_roots(home)
    if not roots:
        roots = [home / "ChaosBox"]
    return [install(home, credentials=credentials, installdir=root) for root in roots]


def install(home=None, credentials=True, installdir=None):
    home = Path.home() if home is None else Path(home)
    source = Path(__file__).resolve().parent
    settings = core.Settings(installdir if installdir is not None else home / "ChaosBox")
    seed_assets = not settings.path.exists()
    settings.root.mkdir(parents=True, exist_ok=True)
    if seed_assets:
        settings.ensure(default_box="ChaosBox")
    else:
        settings.reload()
    shared = home / ".local/share/chaosbox/desktop"
    shared.mkdir(parents=True, exist_ok=True)
    for name in ("app.py", "core.py", "i18n.py", "markdown_render.py", "setup.ini", "chaosbox.svg", "chaosbox.png"):
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
        # Each installation owns its key; upgrades keep the existing key pair.
        key = credential_dir / "id_ed25519"
        if not key.exists():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                            "-C", settings.app_id, "-f", str(key)], check=True)
        os.chmod(key, 0o600)
        # Host identities may be shared; private authentication keys may not.
        assets = source.parent / "app/src/main/assets"
        for name in ("known_hosts",):
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
    parser = argparse.ArgumentParser(description="Update all existing installations, or create ~/ChaosBox on first install")
    parser.add_argument("--installdir", type=Path,
                        help="Install or update a specific box (shared app updates apply to all boxes)")
    args = parser.parse_args()
    try:
        results = install_all(installdir=args.installdir)
    except FileExistsError as error:
        parser.exit(1, f"Installation aborted: a required directory is occupied by a file: {error.filename}\n")
    print(f"Updated shared app for all boxes: {results[0][0].parent.parent / 'desktop'}")
    for target, setup, desktop in results:
        print(f"Setup: {setup}\nApplication menu: {desktop}")
    print("Reopen running boxes to use the new version.")
