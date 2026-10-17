import importlib.util
import io
import json
from pathlib import Path
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

spec = importlib.util.spec_from_file_location('cleanup_sync', Path(__file__).resolve().parents[1] / 'cleanup-sync.py')
cleanup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cleanup
spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / 'collection'
        self.root = self.data / 'TXT/.chaosbox-sync'
        (self.root / 'ops').mkdir(parents=True)
        (self.root / 'device').write_text('device')

    def tearDown(self):
        self.temp.cleanup()

    def old(self, relative, content='backup'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        os.utime(path, (1, 1))
        return path

    def test_preview_and_apply_keep_history_and_active_files(self):
        blob = 'a' * 64
        referenced = self.old('blobs/' + blob + '.blob')
        orphan = self.old('blobs/' + 'b' * 64 + '.blob')
        op = dict(schema=1, revision='a', parents=[], kind='media', action='put', blob=blob)
        historical = self.old('ops/a.op', json.dumps(op))
        self.old('ops/b.op', json.dumps(dict(schema=1, revision='b', parents=['a'], kind='media', action='delete')))
        trash = self.old('trash/old/photo.jpg')
        os.utime(trash.parent, (1, 1))
        backup = self.old('reset-backups/old/box.json')
        os.utime(backup.parent, (1, 1))
        active = self.data / 'TXT/box.json'
        active.write_text('[]')
        with redirect_stdout(io.StringIO()):
            candidates = cleanup.cleanup(self.root, 30)
            self.assertEqual(len(candidates), 3)
            self.assertTrue(orphan.exists())
            cleanup.cleanup(self.root, 30, True)
        self.assertTrue(referenced.exists())
        self.assertTrue(historical.exists())
        self.assertTrue(active.exists())
        self.assertFalse(orphan.exists())
        self.assertFalse(trash.exists())
        self.assertFalse(backup.exists())

    def test_new_child_protects_old_backup(self):
        old = self.old('reset-backups/old/first.json')
        (old.parent / 'recent.json').write_text('recent')
        os.utime(old.parent, (1, 1))
        self.assertEqual(cleanup.plan(self.root, 30)[0], [])

    def test_invalid_journal_stops_before_deletion(self):
        orphan = self.old('blobs/' + 'b' * 64 + '.blob')
        self.old('ops/a.op', '{}')
        with self.assertRaises(ValueError), redirect_stdout(io.StringIO()):
            cleanup.cleanup(self.root, 0, True)
        self.assertTrue(orphan.exists())

    def test_busy_lock_is_preserved_and_own_lock_is_released(self):
        (self.root / 'transfer.lock').mkdir()
        with self.assertRaises(ValueError):
            with cleanup.locked(self.root):
                self.fail('Busy journal accepted')
        self.assertTrue((self.root / 'transfer.lock').exists())
        self.assertFalse((self.root / 'local.lock').exists())

    def test_symlink_is_refused_without_touching_target(self):
        outside = Path(self.temp.name) / 'outside'
        outside.write_text('keep')
        (self.root / 'trash').mkdir()
        (self.root / 'trash/link').symlink_to(outside)
        with self.assertRaises(ValueError):
            cleanup.plan(self.root, 0)
        self.assertEqual(outside.read_text(), 'keep')

    def test_missing_collection_is_not_created(self):
        missing = Path(self.temp.name) / 'missing'
        with self.assertRaises(ValueError):
            cleanup.journal_for(missing)
        self.assertFalse(missing.exists())
