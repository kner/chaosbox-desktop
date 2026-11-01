import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from error_messages import describe
from chaos_sync import Store

class ErrorMessagesTest(unittest.TestCase):
    def test_collision_retains_local_file_and_explains_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'JPG', root / 'TXT')
            payload = root / 'remote.jpg'
            payload.write_bytes(b'remote bytes')
            store.issue('media', 'photo.jpg', {'id': '00000000-0000-4000-8000-000000000001'}, payload)
            target = store.images / 'photo.jpg'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'local bytes')
            with self.assertRaises(ValueError) as raised:
                store.apply()
            message = describe(raised.exception)
            self.assertIn(str(target), message)
            self.assertIn('copy the existing file to a backup', message)
            self.assertEqual(target.read_bytes(), b'local bytes')

    def test_unknown_and_empty_errors_have_guidance(self):
        for error in (RuntimeError('unexpected failure'), RuntimeError()):
            self.assertIn('Your options:', describe(error))
            self.assertIn('earlier steps may have completed', describe(error))

    def test_guidance_is_not_duplicated(self):
        message = describe(ValueError('Untracked file collision: photo.jpg'))
        self.assertEqual(describe(ValueError(message)), message)

    def test_cli_reports_actionable_error_and_failure_status(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/sync-data.py'), '--data', folder], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Your options:', result.stderr)
            self.assertNotIn('Traceback (most recent call last)', result.stderr)
