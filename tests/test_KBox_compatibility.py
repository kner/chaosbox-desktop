"""Branding changes must preserve existing installations, metadata and history."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import core
import KBox_sync
import chaos_sync
import install
from uninstall import uninstall
from legacy_names import installation_root, repository


class KBoxCompatibilityTests(unittest.TestCase):
    def test_existing_installation_is_reused_without_new_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            old = home / 'Kbox-Bilder1'
            old.mkdir()
            (old / 'setup.ini').write_text('[Box]\nTitel=Photos\n')
            settings = core.Settings(old)
            settings.reload()
            identity = settings.installation_id
            canonical = core.Settings(home / 'KBox-Bilder1')
            canonical.reload()
            self.assertEqual(canonical.root, old)
            self.assertEqual(canonical.installation_id, identity)
            self.assertFalse((home / 'KBox-Bilder1').exists())

    def test_legacy_default_and_repository_paths_are_discovered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'ChaosBox').mkdir()
            (root / 'chaosbox-android').mkdir()
            self.assertEqual(installation_root(root / 'KBox'), root / 'ChaosBox')
            self.assertEqual(repository(root, 'KBox-android'), root / 'chaosbox-android')

    def test_old_launchers_use_new_shared_app_after_upgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            box = home / 'Kbox-Bilder1'
            box.mkdir()
            (box / 'setup.ini').write_text('[Box]\nTitel=Photos\n')
            launcher = home / '.local/share/chaosbox/installations/chaosbox-old/run-desktop.sh'
            launcher.parent.mkdir(parents=True)
            launcher.write_text(f'exec /usr/bin/python3 {home}/.local/share/chaosbox/desktop/app.py {box} "$@"\n')
            self.assertEqual(install.installed_roots(home), [box])
            with patch('install.shutil.which', return_value=None):
                install.install(home, credentials=False, installdir=home / 'KBox-Bilder1')
            self.assertIn(str(home / '.local/share/KBox/desktop/app.py'), launcher.read_text())
            self.assertTrue((home / '.local/share/KBox/desktop/KBox_sync.py').is_file())
            self.assertTrue((home / '.local/bin/KBox-Bilder1').is_file())
            self.assertEqual(install.installed_roots(home), [box])

    def test_old_xmp_prefix_and_existing_journal_remain_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / 'old.jpg'
            xml = b'''<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:chaosbox="urn:chaosbox:metadata:1.0/" chaosbox:version="1" chaosbox:device="legacy device" chaosbox:box="B1"/></rdf:RDF></x:xmpmeta>'''
            Image.new('RGB', (8, 8), 'red').save(image, xmp=xml)
            record = core.read_metadata(image)
            self.assertEqual(record['device'], 'legacy device')
            self.assertEqual(record['box'], 'B1')
            old = root / 'TXT/.chaosbox-sync'
            old.mkdir(parents=True)
            device = '00000000-0000-4000-8000-000000000001'
            (old / 'device').write_text(device)
            store = KBox_sync.Store(root / 'JPG', root / 'TXT')
            self.assertEqual(store.device, device)
            self.assertEqual(store.root, old)
            self.assertIs(chaos_sync.Store, KBox_sync.Store)

    def test_shared_app_survives_while_a_legacy_registry_still_uses_it(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            roots = [home / 'Kbox-First', home / 'Kbox-Second']
            targets = []
            with patch('install.shutil.which', return_value=None):
                for root in roots:
                    root.mkdir()
                    (root / 'setup.ini').write_text('[Box]\nTitel=Photos\n')
                    targets.append(install.install(home, credentials=False, installdir=root)[0])
            legacy = home / '.local/share/chaosbox/installations' / targets[1].name.replace('KBox-', 'chaosbox-', 1)
            legacy.parent.mkdir(parents=True)
            targets[1].rename(legacy)
            shared = home / '.local/share/KBox/desktop/app.py'
            uninstall(home, roots[0])
            self.assertTrue(shared.is_file())
            self.assertTrue((legacy / 'run-desktop.sh').is_file())
            uninstall(home, roots[1])
            self.assertFalse(shared.exists())
