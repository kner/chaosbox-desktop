#!/usr/bin/env python3
"""Build an offline per-user installer in released/."""
import argparse
import hashlib
import json
import re
import io
from pathlib import Path
import tarfile

SOURCE = Path(__file__).resolve().parent.parent
FILES = ('VERSION', 'src/app.py', 'src/core.py', 'src/chaos_sync.py', 'src/sync_transport.py',
         'scripts/sync-data.py', 'scripts/cleanup-sync.py', 'src/i18n.py', 'src/markdown_render.py',
         'src/setup.ini', 'src/chaosbox.svg', 'src/chaosbox.png', 'scripts/install.py',
         'scripts/install-desktop.sh', 'scripts/run-desktop.sh', 'scripts/install-dependencies.sh', 'README.md')


def installation_setup(setup):
    """Export effective sync targets so recipients join the source dataset."""
    import core
    setup = Path(setup).resolve()
    settings = core.Settings(setup.parent, setup=setup)
    settings.reload()
    if any('/' in profile.id or profile.setup_path != setup for profile in settings.profiles):
        raise ValueError('Installer requires boxes from the central setup.ini')
    text = core.english_setup(setup.read_text(encoding='utf-8-sig'))
    lines = list(core.logical_lines(text))
    for section, _, begin, end in core.section_blocks(text):
        if section.casefold() == 'ssh' or section.casefold().startswith('ssh.'):
            for index in range(begin, end):
                line = lines[index]
                key = line.split('=', 1)[0].strip().casefold() if '=' in line else ''
                if key in ('keyfile', 'knownhosts'):
                    target = 'id_ed25519' if key == 'keyfile' else 'known_hosts'
                    lines[index] = line.split('=', 1)[0] + '=.state/credentials/' + target
    text = '\n'.join(lines).rstrip() + '\n'
    return bind_sync_targets(text, settings.profiles)


def bind_sync_targets(text, profiles):
    import core
    global_installation = next((values.get('installation', '').strip()
                                for section, values, _, _ in core.section_blocks(text)
                                if section.casefold() == 'ssh'), '')
    for profile in profiles:
        section = 'SSH.' + profile.id
        ssh = profile.options[2]
        # Host and login settings belong in [SSH]. Keep each profile section
        # limited to its data identity so both clients can use the same setup.
        values = {
            'installation': ('Installation', ssh['installation']),
            'remoteprofile': ('RemoteProfile', profile.id.casefold()),
        }
        if global_installation:
            values.pop('installation')
        lines = list(core.logical_lines(text))
        blocks = [block for block in core.section_blocks(text) if block[0].casefold() == section.casefold()]
        if blocks:
            _, _, begin, end = blocks[-1]
            seen = set()
            for index in range(begin + 1, end):
                key = lines[index].split('=', 1)[0].strip().casefold() if '=' in lines[index] else ''
                if key in ('installation', 'remoteprofile'):
                    label, value = ({
                        'installation': ('Installation', ssh['installation']),
                        'remoteprofile': ('RemoteProfile', profile.id.casefold()),
                    })[key]
                    lines[index] = f'{label}={value}'
                    seen.add(key)
            lines[end:end] = [f'{label}={value}' for key, (label, value) in values.items() if key not in seen]
        else:
            lines.extend(['', f'[{section}]', *(f'{label}={value}' for label, value in values.values())])
        text = '\n'.join(lines).rstrip() + '\n'
    return text


def build(output=None, name=None, setup=None):
    import sys
    sys.path.insert(0, str(SOURCE / 'src'))
    if name is not None and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', name):
        raise ValueError('Invalid installation name')
    if setup is not None and name is None:
        raise ValueError('A setup requires an installation name')
    output = Path(output) if output else SOURCE / 'released'
    output.mkdir(parents=True, exist_ok=True)
    version = (SOURCE / 'VERSION').read_text().strip()
    label = f'KBox-{name}' if name else 'chaosbox-desktop'
    package = output / f'{label}_{version}_user.run'
    header = '''#!/bin/sh
set -eu
if [ "$(id -u)" = 0 ]; then
    echo "Bitte als normaler Benutzer ohne sudo ausführen." >&2
    exit 1
fi
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT HUP INT TERM
tail -n +ARCHIVE_LINE "$0" | tar -xz -C "$stage"
"$stage/scripts/install-desktop.sh" "$@"
echo "Start über das eigene KBox-Symbol auf dem Desktop oder im Anwendungsmenü."
exit 0
'''
    header = header.replace('ARCHIVE_LINE', str(header.count('\n') + 1))
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        def add_bytes(filename, data):
            info = tarfile.TarInfo(filename)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))

        for filename in FILES:
            if filename == 'src/setup.ini' and setup is not None:
                add_bytes(filename, installation_setup(setup).encode())
            else:
                tar.add(SOURCE / filename, arcname=filename, recursive=False)
        if name:
            add_bytes('install-defaults.json', json.dumps({'directory': f'KBox-{name}'}).encode())
        if setup is not None:
            hosts = Path(setup).parent / '.state/credentials/known_hosts'
            if hosts.is_file():
                add_bytes('initial_known_hosts', hosts.read_bytes())
    package.write_bytes(header.encode() + archive.getvalue())
    package.chmod(0o755)
    package.with_suffix('.run.sha256').write_text(
        f'{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n')
    return package


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', help='Installation name, e.g. Bilder3')
    parser.add_argument('--setup', type=Path, help='Setup to embed (requires --name)')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    print(build(args.output, args.name, args.setup))
