#!/usr/bin/env python3
"""Install this desktop build for the current user without root or network access."""
import argparse
import io
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "src"))
import core


def desktop_directory(home):
    """Resolve the user's desktop without executing user-dirs shell content."""
    home = Path(home)
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    path = config / "user-dirs.dirs"
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.startswith("XDG_DESKTOP_DIR="):
                values = shlex.split(line.split("=", 1)[1])
                if len(values) == 1:
                    directory = Path(values[0].replace("${HOME}", str(home)).replace("$HOME", str(home)))
                    if directory.is_absolute():
                        return None if directory == home else directory
    return home / "Desktop"


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
            if root is None and len(command) > 3 and command[2].endswith("/app.py"):
                root = Path(command[3]).expanduser()
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
    source = PROJECT
    payload = source / "src"
    settings = core.Settings(installdir if installdir is not None else home / "ChaosBox")
    seed_assets = not settings.path.exists()
    settings.root.mkdir(parents=True, exist_ok=True)
    if seed_assets and (source / "install-defaults.json").is_file():
        core.atomic_write(settings.path, (payload / "setup.ini").read_bytes())
        settings.reload()
    elif seed_assets:
        settings.ensure(default_box="ChaosBox")
    else:
        settings.reload()
    shared = home / ".local/share/chaosbox/desktop"
    shared.mkdir(parents=True, exist_ok=True)
    for name in ("VERSION", "app.py", "core.py", "chaos_sync.py", "sync_transport.py", "sync-data.py", "cleanup-sync.py", "i18n.py", "markdown_render.py", "setup.ini", "chaosbox.svg", "chaosbox.png"):
        origin = source / "VERSION" if name == "VERSION" else payload / name
        if name in ("sync-data.py", "cleanup-sync.py"):
            origin = source / "scripts" / name
        core.atomic_write(shared / name, origin.read_bytes(), mode=0o644)
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
        migrated = migrated.replace(" --installdir=", " ").replace(" --installdir ", " ")
        if migrated != script:
            core.atomic_write(existing, migrated, mode=0o755)
    icon = shared.parent / "icons" / f"{settings.app_id}.png"
    icon_data = io.BytesIO()
    core.installation_icon(settings.icon_id).save(icon_data, format="PNG")
    core.atomic_write(icon, icon_data.getvalue(), mode=0o644)
    launcher = target / "run-desktop.sh"
    script = ('#!/usr/bin/env bash\nset -euo pipefail\n'
              f'exec /usr/bin/python3 {shlex.quote(str(shared / "app.py"))} '
              f'{shlex.quote(str(settings.root))} "$@"\n')
    core.atomic_write(launcher, script, mode=0o755)
    command = home / ".local/bin/chaosbox"
    core.atomic_write(command, '#!/bin/sh\nexec ' + shlex.quote(str(launcher)) + ' "$@"\n', mode=0o755)
    named_command = home / ".local/bin" / settings.app_id
    if settings.root.name.casefold().startswith("kbox-"):
        named_command = home / ".local/bin" / settings.title
    core.atomic_write(named_command, '#!/bin/sh\nexec ' + shlex.quote(str(launcher)) + ' "$@"\n', mode=0o755)
    state = settings.state_dir
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    for profile in settings.profiles:
        for directory in (profile.images, profile.data, profile.index):
            directory.mkdir(parents=True, exist_ok=True)
    first = settings.profiles[0]
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
        assets = PROJECT.parent / "chaosbox-android/app/src/main/assets"
        for name in ("known_hosts",):
            destination = credential_dir / name
            seed = source / "initial_known_hosts"
            if not seed.is_file():
                seed = assets / name
            if not destination.exists() and seed.is_file():
                core.atomic_write(destination, seed.read_bytes(), mode=0o600)
    def quoted(path):
        return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$') + '"'
    desktop = home / ".local/share/applications" / f"{settings.app_id}.desktop"
    core.atomic_write(desktop, "[Desktop Entry]\nType=Application\nVersion=1.0\n"
                      f"Name={settings.title}\n"
                      "Comment=Edit image and video metadata, JSON records and synchronize with SSH\n"
                      f"Exec={quoted(launcher)}\nIcon={icon}\n"
                      "Terminal=false\nCategories=Graphics;Utility;\nStartupNotify=true\n"
                      f"StartupWMClass={settings.window_class}\n", mode=0o644)
    desktop_folder = desktop_directory(home)
    if desktop_folder is not None:
        shortcut = desktop_folder / f"{settings.title}.desktop"
        core.atomic_write(shortcut, desktop.read_bytes(), mode=0o755)
        # File managers that support this attribute can launch it directly.
        gio = shutil.which("gio")
        if gio:
            subprocess.run([gio, "set", str(shortcut), "metadata::trusted", "true"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return target, settings.path, desktop


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update all existing installations, or create ~/ChaosBox on first install")
    parser.add_argument("--installdir", type=Path,
                        help="Install or update a specific box (shared app updates apply to all boxes)")
    args = parser.parse_args()
    defaults = Path(__file__).with_name("install-defaults.json")
    if args.installdir is None and defaults.is_file():
        directory = json.loads(defaults.read_text())["directory"]
        if not directory.startswith("KBox-") or Path(directory).name != directory or "\\" in directory:
            parser.error("Invalid bundled installation directory")
        args.installdir = Path.home() / directory
    try:
        results = install_all(installdir=args.installdir)
    except FileExistsError as error:
        parser.exit(1, f"Installation aborted: a required directory is occupied by a file: {error.filename}\n")
    print(f"Updated shared app for all boxes: {results[0][0].parent.parent / 'desktop'}")
    for target, setup, desktop in results:
        print(f"Setup: {setup}\nApplication menu: {desktop}")
    print("Reopen running boxes to use the new version.")
