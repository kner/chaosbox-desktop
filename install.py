#!/usr/bin/env python3
"""Install this desktop build for the current user without root or network access."""
import os
from pathlib import Path
import shutil
import sys

import core


def install(home=None, credentials=True):
    home = Path.home() if home is None else Path(home)
    source = Path(__file__).resolve().parent
    target = home / ".local/share/chaosbox"
    (target / "desktop").mkdir(parents=True, exist_ok=True)
    for name in ("app.py", "core.py", "setup.ini", "chaosbox.svg", "chaosbox.png"):
        core.atomic_write(target / "desktop" / name, (source / name).read_bytes(), mode=0o644)
    launcher = target / "run-desktop.sh"
    core.atomic_write(launcher, '#!/usr/bin/env bash\nset -euo pipefail\ncd -- "$(dirname -- "${BASH_SOURCE[0]}")"\nexec /usr/bin/python3 desktop/app.py "$@"\n', mode=0o755)
    state = home / ".config/chaosbox"
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    settings = core.Settings(home, state)
    settings.ensure()
    for profile in settings.profiles:
        for directory in (profile.images, profile.data, profile.index):
            directory.mkdir(parents=True, exist_ok=True)
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
    desktop = home / ".local/share/applications/chaosbox.desktop"
    core.atomic_write(desktop, "[Desktop Entry]\nType=Application\nVersion=1.0\nName=ChaosBox Desktop\n"
                      "Comment=Edit image and video metadata, JSON records and synchronize with SSH\n"
                      f"Exec={quoted(launcher)}\nIcon={target / 'desktop/chaosbox.svg'}\n"
                      "Terminal=false\nCategories=Graphics;Utility;\nStartupNotify=true\nStartupWMClass=Chaosbox\n", mode=0o644)
    return target, settings.path, desktop


if __name__ == "__main__":
    target, setup, desktop = install()
    print(f"Installed: {target}\nSetup: {setup}\nApplication menu: {desktop}")
