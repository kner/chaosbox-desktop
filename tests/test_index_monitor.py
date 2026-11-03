import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import core


class IndexMonitorTests(unittest.TestCase):
    def test_new_changed_deleted_files_and_empty_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = SimpleNamespace(id='test', images=root/'JPG', data=root/'TXT',
                                      legacy_data=root/'TXT', categories=['Existing'])
            profile.images.mkdir()
            profile.data.mkdir()
            settings = SimpleNamespace(profiles=[profile], index=root/'.indices')
            core.write_index(settings, [])
            monitor = core.IndexMonitor()
            monitor.poll(settings)
            path = profile.data/'new.json'
            path.write_text(json.dumps([{'comment': 'new'}]))
            (profile.images/'Tools').mkdir()
            (profile.images/'.hidden').mkdir()
            categories = monitor.poll(settings)
            self.assertEqual(core.load_index(settings), [])
            self.assertEqual(categories['test'], ['Existing', 'TOOLS'])
            monitor.poll(settings)
            self.assertEqual(core.load_index(settings)[0].data['comment'], 'new')
            path.write_text(json.dumps([{'comment': 'changed'}]))
            monitor.poll(settings)
            monitor.poll(settings)
            self.assertEqual(core.load_index(settings)[0].data['comment'], 'changed')
            path.unlink()
            monitor.poll(settings)
            monitor.poll(settings)
            self.assertEqual(core.load_index(settings), [])

    def test_existing_cache_is_reconciled_after_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = SimpleNamespace(id='test', images=root/'JPG', data=root/'TXT',
                                      legacy_data=root/'TXT', categories=[])
            profile.data.mkdir()
            path = profile.data/'old.json'
            path.write_text('[{"comment": "current"}]')
            settings = SimpleNamespace(profiles=[profile], index=root/'.indices')
            core.write_index(settings, [core.Entry(path, {'comment': 'stale'}, False, 0)])
            monitor = core.IndexMonitor()
            monitor.poll(settings)
            monitor.poll(settings)
            self.assertEqual(core.load_index(settings)[0].data['comment'], 'current')


class MonitorSchedulingTests(unittest.TestCase):
    def test_background_result_updates_choices_without_changing_form(self):
        from concurrent.futures import Future
        from unittest.mock import Mock
        from app import App
        app = App.__new__(App)
        app.closing, app.busy = False, True
        app.profile = SimpleNamespace(id='test')
        app.root = Mock()
        app.field_widgets = {'category': Mock()}
        app.index_monitor_future = Future()
        app.index_monitor_future.set_result({'test': ['TOOLS']})
        app.check_installation_files()
        app.field_widgets['category'].configure.assert_called_once_with(values=['TOOLS'])
        app.root.after.assert_called_once_with(2000, app.check_installation_files)
        self.assertIsNone(app.index_monitor_future)
