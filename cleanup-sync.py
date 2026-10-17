#!/usr/bin/env python3
"""Clean recoverable sync debris without pruning history required by other clients."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import time


@dataclass
class Candidate:
    path: Path
    size: int


def safe_tree(path):
    """Refuse symlinks and special files rather than following them during cleanup."""
    entries = [path]
    if path.is_dir() and not path.is_symlink():
        entries.extend(path.rglob('*'))
    for entry in entries:
        if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
            raise ValueError('Unsafe cleanup entry: ' + str(entry))
    return entries


def journal_for(data):
    data = Path(data).expanduser().absolute()
    root = data / 'TXT' / '.chaosbox-sync'
    for path in [root, *root.parents]:
        if path.is_symlink():
            raise ValueError('Symlink refused: ' + str(path))
    if not root.is_dir() or not (root / 'device').is_file() and not (root / 'ops').is_dir():
        raise ValueError('No existing sync journal at ' + str(root))
    return root


@contextmanager
def locked(root):
    acquired = []
    try:
        # Desktop holds local.lock; all supported remote transports hold transfer.lock.
        for name in ('local.lock', 'transfer.lock'):
            path = root / name
            try:
                path.mkdir()
            except FileExistsError:
                raise ValueError('Synchronization or cleanup is busy; lock exists: ' + str(path))
            acquired.append(path)
        yield
    finally:
        for path in reversed(acquired):
            path.rmdir()


def plan(root, days, now=None):
    cutoff = (time.time() if now is None else now) - days * 86400
    referenced = set()
    # Keep blobs referenced anywhere in history, including conflicts and deleted objects.
    ops = root / 'ops'
    if ops.is_symlink():
        raise ValueError('Unsafe journal directory: ' + str(ops))
    if ops.exists():
        safe_tree(ops)
        for path in ops.glob('*.op'):
            op = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(op, dict) or op.get('schema') != 1
                    or op.get('revision') != path.stem
                    or not isinstance(op.get('parents'), list)
                    or op.get('kind') not in ('record', 'media', 'device')
                    or op.get('action') not in ('put', 'delete', 'ack', 'retire')):
                raise ValueError('Invalid journal operation: ' + str(path))
            blob = op.get('blob')
            if op['kind'] == 'media' and op['action'] == 'put' and blob is None:
                raise ValueError('Missing media reference: ' + str(path))
            if blob is not None:
                if not isinstance(blob, str) or not re.fullmatch('[a-f0-9]{64}', blob):
                    raise ValueError('Invalid media reference: ' + str(path))
                referenced.add(blob + '.blob')
    candidates = []
    for name in ('trash', 'reset-backups', 'blobs'):
        directory = root / name
        if directory.is_symlink():
            raise ValueError('Unsafe cleanup directory: ' + str(directory))
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise ValueError('Unsafe cleanup directory: ' + str(directory))
        for path in sorted(directory.iterdir()):
            entries = safe_tree(path)
            if name == 'blobs':
                if not path.is_file() or not re.fullmatch('[a-f0-9]{64}\\.blob', path.name):
                    continue
                if path.name in referenced:
                    continue
            if max(entry.stat().st_mtime for entry in entries) > cutoff:
                continue
            candidates.append(Candidate(path, sum(entry.stat().st_size for entry in entries if entry.is_file())))
    return candidates, len(referenced)


def cleanup(root, days, apply=False):
    candidates, protected = plan(root, days)
    for item in candidates:
        print(f'{"REMOVE" if apply else "WOULD REMOVE"} {item.size:>12} bytes  {item.path}')
    print(f'{len(candidates)} item(s), {sum(item.size for item in candidates):,} bytes; '
          f'{protected} media blob reference(s) protected by sync history.')
    if apply:
        for item in candidates:
            if item.path.is_dir():
                shutil.rmtree(item.path)
            else:
                item.path.unlink()
        print('Cleanup completed. Journal operations and active TXT/JPG files were retained.')
    else:
        print('Preview only. Repeat with --apply to permanently delete these items.')
    return candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True, type=Path,
                        help='One local or server collection directory containing TXT and JPG')
    parser.add_argument('--older-than-days', type=int, default=30,
                        help='Minimum age of every file/directory in a candidate (default: 30)')
    parser.add_argument('--apply', action='store_true', help='Permanently delete the listed cleanup candidates')
    args = parser.parse_args()
    if args.older_than_days < 0:
        parser.error('--older-than-days must be nonnegative')
    try:
        root = journal_for(args.data)
        # Hold both locks even during preview so the inventory is internally consistent.
        with locked(root):
            cleanup(root, args.older_than_days, args.apply)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Cleanup stopped: {error}\n')


if __name__ == '__main__':
    main()
