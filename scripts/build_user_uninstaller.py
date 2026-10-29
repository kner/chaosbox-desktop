#!/usr/bin/env python3
"""Build a standalone uninstaller that needs only the system Python."""
import argparse
import hashlib
from pathlib import Path
import re

SOURCE = Path(__file__).resolve().parent.parent


def build(name, output=None):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', name):
        raise ValueError('Invalid installation name')
    output = Path(output) if output else SOURCE / 'released'
    output.mkdir(parents=True, exist_ok=True)
    package = output / f'KBox-{name}_uninstall.run'
    source = (SOURCE / 'scripts/uninstall.py').read_text().split('if __name__ == "__main__":')[0]
    version = (SOURCE / 'VERSION').read_text().strip()
    script = '#!/bin/sh\nset -eu\nexec /usr/bin/python3 - "$@" <<\'CHAOSBOX_UNINSTALL_PY\'\n'
    script += source
    script += f'''
import sys
if os.getuid() == 0:
    sys.exit("Please run without sudo as the installation's user.")
if len(sys.argv) > 1:
    sys.exit("This uninstaller targets KBox-{name}; no arguments are needed.")
print("KBox-{name} uninstaller · (c) kner.at · {version}")
removed = []
try:
    for directory in ("KBox-{name}", "Kbox-{name}"):
        _, files = uninstall(installdir=Path.home() / directory)
        removed.extend(files)
except (OSError, ValueError) as error:
    sys.exit(f"Uninstall failed: {{error}}")
print(f"Removed {{len(removed)}} application files. Data, settings and SSH credentials preserved.")
CHAOSBOX_UNINSTALL_PY
'''
    package.write_text(script)
    package.chmod(0o755)
    package.with_suffix('.run.sha256').write_text(
        f'{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n')
    return package


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    print(build(args.name, args.output))
