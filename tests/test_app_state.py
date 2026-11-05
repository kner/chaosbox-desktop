"""Installation-local box selection across application restarts."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from app import App
import core


class AppStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'setup.ini').write_text('[Box]\nTitel=First\n[Box]\nTitel=Rea\n')
        (self.root / '.installation-id').write_text('00000000-0000-4000-8000-000000000001')
        self.settings = core.Settings(self.root)
        self.settings.reload()

    def app(self):
        app = App.__new__(App)
        app.settings = self.settings
        app.state_file = self.settings.state_dir / 'desktop-state.json'
        app.error = Mock()
        return app

    def test_reopen_restores_last_selected_box_and_media_folder(self):
        app = self.app()
        app.restore_state()
        self.assertEqual(app.profile.id, 'First')
        app.profile = self.settings.profile('Rea')
        app.media_folders = {'Rea': str(self.root / 'Rea/JPG')}
        app.save_state()
        reopened = self.app()
        reopened.restore_state()
        self.assertEqual(reopened.profile.id, 'Rea')
        self.assertEqual(self.settings.active, 'Rea')
        self.assertEqual(reopened.media_folders, app.media_folders)
        app.error.assert_not_called()

    def test_missing_removed_or_invalid_selection_uses_default(self):
        app = self.app()
        app.state_file.parent.mkdir(parents=True, exist_ok=True)
        for state in ('{}', '{"profile":"Removed"}', '{"profile":null}',
                      '{"profile":42}', '[]', 'broken JSON'):
            with self.subTest(state=state):
                app.state_file.write_text(state)
                app.restore_state()
                self.assertEqual(app.profile.id, 'First')

    def test_close_persists_current_box(self):
        app = self.app()
        app.restore_state()
        app.profile = self.settings.profile('Rea')
        app.busy = False
        app.executor = Mock()
        app.root = Mock()
        app.close()
        self.assertEqual(json.loads(app.state_file.read_text())['profile'], 'Rea')
        app.root.destroy.assert_called_once()
