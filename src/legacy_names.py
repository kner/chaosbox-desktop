"""Compatibility for installations and repositories created before the KBox rename.

Historical wire identifiers and existing paths remain readable. New code, files,
launchers and installations use KBox; existing dataset identities never change.
"""
from pathlib import Path

LEGACY_SHARE = 'chaosbox'
LEGACY_DEFAULT = 'ChaosBox'
LEGACY_PREFIX = 'Kbox-'


def installation_root(path):
    path = Path(path).expanduser()
    if path.exists() or path.is_symlink():
        return path
    candidates = []
    if path.name == 'KBox':
        candidates = [path.with_name(LEGACY_DEFAULT)]
    elif path.name.startswith('KBox-'):
        candidates = [path.with_name(LEGACY_PREFIX + path.name[5:])]
    return next((old for old in candidates if old.exists() or old.is_symlink()), path)


def repository(parent, name):
    parent = Path(parent)
    current = parent / name
    if current.exists():
        return current
    legacy = parent / name.replace('KBox', LEGACY_SHARE, 1)
    return legacy if legacy.exists() else current


def application_bases(home):
    return [Path(home) / '.local/share/KBox', Path(home) / '.local/share' / LEGACY_SHARE]


def installation_title(root):
    name = Path(root).name
    if name.casefold() in ('KBox'.casefold(), LEGACY_DEFAULT.casefold()):
        return 'KBox'
    for prefix in ('KBox-', LEGACY_DEFAULT + '-'):
        if name.casefold().startswith(prefix.casefold()):
            return 'KBox-' + name[len(prefix):]
    return 'KBox-' + name
