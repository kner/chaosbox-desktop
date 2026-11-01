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


class UploadDialogErrorTest(unittest.TestCase):
    def test_upload_failure_includes_guidance_in_the_upload_window(self):
        from unittest.mock import Mock, patch
        from app import App
        import core
        for error, title in (
            (ValueError('Untracked file collision: /JPG/photo.jpg'), 'Upload failed'),
            (core.Cancelled('Synchronization cancelled'), 'Upload cancelled'),
            (RuntimeError('Unexpected failure'), 'Upload failed'),
        ):
            with self.subTest(error=error):
                app = App.__new__(App)
                app.busy = False
                app.root = Mock()
                app.t = lambda text: text
                app.cancel_upload = Mock()
                app.media = []
                app.box_path = None
                app.task = Mock()
                with patch('app.tk.Toplevel') as window, patch('app.tk.Text') as text, patch('app.ttk.Button') as button:
                    app.start_upload()
                    failed = app.task.call_args.args[2]
                    failed(error)
                    message = text.return_value.insert.call_args.args[1]
                    self.assertIn(str(error), message)
                    self.assertIn('What happened:', message)
                    self.assertIn('Your options:', message)
                    window.return_value.title.assert_called_with(title)
                    button.return_value.configure.assert_called_once()
                    self.assertFalse(app.uploading)
