#!/usr/bin/env python3
"""Build a system-wide Debian package without root or external Python libraries."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parent
PACKAGE = "chaosbox-desktop"


def build(version, output):
    subprocess.run(["dpkg", "--validate-version", version], check=True)
    if "/" in version or "\\" in version:
        raise ValueError("Version must not contain path separators")
    output.mkdir(parents=True, exist_ok=True)
    package = output / f"{PACKAGE}_{version}_all.deb"
    with tempfile.TemporaryDirectory(prefix="chaosbox-deb-") as temporary:
        root = Path(temporary)

        def write(name, text, mode=0o644):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            path.chmod(mode)

        def copy(name, source):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read_bytes())
            path.chmod(0o644)

        # Explicit allowlist: never package user data, SSH keys or local settings.
        for name in ("app.py", "core.py", "setup.ini"):
            write(f"usr/share/{PACKAGE}/{name}", (SOURCE / name).read_text())
        copy(f"usr/share/{PACKAGE}/chaosbox.png", SOURCE / "chaosbox.png")
        copy("usr/share/icons/hicolor/128x128/apps/chaosbox.png", SOURCE / "chaosbox.png")
        write("usr/bin/chaosbox", '#!/bin/sh\nexec /usr/bin/python3 /usr/share/chaosbox-desktop/app.py "$@"\n', 0o755)
        write("usr/share/icons/hicolor/scalable/apps/chaosbox.svg", (SOURCE / "chaosbox.svg").read_text())
        write(f"usr/share/applications/{PACKAGE}.desktop", """[Desktop Entry]
Type=Application
Version=1.0
Name=ChaosBox Desktop
Comment=Edit image and video metadata and JSON records
Exec=chaosbox
Icon=chaosbox
Terminal=false
Categories=Graphics;
StartupNotify=true
StartupWMClass=Chaosbox
""")
        write(f"usr/share/doc/{PACKAGE}/README.md", (SOURCE / "README.md").read_text())
        files = sorted(path for path in root.rglob("*") if path.is_file())
        size = sum((path.stat().st_size + 1023) // 1024 for path in files)
        write("DEBIAN/control", f"""Package: {PACKAGE}
Version: {version}
Section: graphics
Priority: optional
Architecture: all
Maintainer: ChaosBox maintainers <chaosbox@localhost>
Installed-Size: {size}
Depends: python3 (>= 3.10), python3-tk, python3-pil (>= 9.1), python3-pil.imagetk, python3-paramiko, libimage-exiftool-perl, ffmpeg, xclip, fonts-dejavu-core
Description: Native ChaosBox desktop editor
 Edit image and video metadata and JSON records with a Python/Tk interface.
 Includes image viewing, search and optional manual SSH synchronization.
 User settings and media are stored in each user's home directory.
""")
        write("DEBIAN/md5sums", "".join(
            f"{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(root)}\n" for path in files))
        # TemporaryDirectory is private; package directories must be traversable.
        root.chmod(0o755)
        for path in root.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        validator = shutil.which("desktop-file-validate")
        if validator:
            subprocess.run([validator, str(root / f"usr/share/applications/{PACKAGE}.desktop")], check=True)
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(root), str(package)], check=True)
    checksum = hashlib.sha256(package.read_bytes()).hexdigest()
    package.with_suffix(".deb.sha256").write_text(f"{checksum}  {package.name}\n")
    return package


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="1.0.2")
    parser.add_argument("--output", type=Path, default=SOURCE / "dist")
    args = parser.parse_args()
    print(build(args.version, args.output.resolve()))
