import hashlib
from pathlib import Path
import tempfile
import unittest

import uninstall


class UninstallTests(unittest.TestCase):
    def test_preserves_data_and_keeps_shared_app_until_last_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            base = home / ".local/share/chaosbox"
            shared = base / "desktop"
            shared.mkdir(parents=True)
            for name in ("app.py", "core.py", "markdown_render.py", "setup.ini", "chaosbox.svg", "chaosbox.png"):
                (shared / name).write_text("application")
            (shared / "unknown.txt").write_text("keep")
            roots = [home / "one box", home / "two box"]
            preserved = {}
            launchers = []
            for root in roots:
                for name in ("setup.ini", "Box/JPG/photo.jpg", "Box/TXT/record.json",
                             ".indices/records.json", ".state/credentials/key"):
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"preserved")
                    preserved[path] = path.read_bytes()
                app_id = "chaosbox-" + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
                launcher = base / "installations" / app_id / "run-desktop.sh"
                launcher.parent.mkdir(parents=True)
                launcher.write_text("launcher")
                desktop = home / ".local/share/applications" / f"{app_id}.desktop"
                desktop.parent.mkdir(parents=True, exist_ok=True)
                desktop.write_text("menu")
                launchers.append((launcher, desktop))
            directories = {path for path in home.rglob("*") if path.is_dir()}
            uninstall.uninstall(home, roots[0])
            self.assertTrue((shared / "app.py").exists())
            self.assertTrue(all(path.exists() for path in launchers[1]))
            self.assertTrue(all(not path.exists() for path in launchers[0]))
            uninstall.uninstall(home, roots[1])
            self.assertFalse((shared / "app.py").exists())
            self.assertFalse((shared / "markdown_render.py").exists())
            self.assertTrue((shared / "unknown.txt").exists())
            self.assertEqual(uninstall.uninstall(home, roots[1])[1], [])
            self.assertEqual({path: path.read_bytes() for path in preserved}, preserved)
            self.assertTrue(all(path.is_dir() for path in directories))

    def test_refuses_symlinked_application_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            outside = home / "outside"
            outside.mkdir()
            sentinel = outside / "app.py"
            sentinel.write_text("keep")
            base = home / ".local/share/chaosbox"
            base.mkdir(parents=True)
            (base / "desktop").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                uninstall.uninstall(home)
            self.assertEqual(sentinel.read_text(), "keep")

    def test_missing_installation_is_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root, removed = uninstall.uninstall(home)
            self.assertEqual(root, home / "ChaosBox")
            self.assertEqual(removed, [])
            self.assertEqual(list(home.iterdir()), [])

    def test_downloaded_bilder7_uninstaller_preserves_other_installations_and_data(self):
        import os
        import subprocess
        from unittest.mock import patch
        import install
        import build_user_uninstaller
        if os.getuid() == 0:
            self.skipTest('Uninstaller requires a normal user')
        with tempfile.TemporaryDirectory(prefix='uninstall test ') as temp:
            home = Path(temp) / 'recipient home'
            home.mkdir()
            (home / '.config').mkdir()
            (home / '.config/user-dirs.dirs').write_text('XDG_DESKTOP_DIR="$HOME/My Desktop"\n')
            env = {**os.environ, 'HOME': str(home), 'XDG_CONFIG_HOME': str(home / '.config')}
            with patch.dict(os.environ, env):
                other = install.install(home, credentials=False, installdir=home / 'KBox-Bilder8')
                targets = [install.install(home, credentials=False, installdir=home / directory)
                           for directory in ('Kbox-Bilder7', 'KBox-Bilder7')]
            preserved = {}
            for _, setup, _ in targets:
                for name in ('Box/JPG/photo.jpg', '.state/credentials/private-key'):
                    path = setup.parent / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'preserve')
                for path in setup.parent.rglob('*'):
                    if path.is_file():
                        preserved[path] = path.read_bytes()
            other_files = {p: p.read_bytes() for p in (other[0] / 'run-desktop.sh', other[1], other[2],
                           home / '.local/bin/KBox-Bilder8', home / 'My Desktop/KBox-Bilder8.desktop')}
            package = build_user_uninstaller.build('Bilder7', Path(temp) / 'downloads')
            for _ in range(2):
                subprocess.run(['sh', str(package)], env=env, check=True, capture_output=True)
            for target, _, desktop in targets:
                self.assertFalse((target / 'run-desktop.sh').exists())
                self.assertFalse(desktop.exists())
                self.assertFalse((home / '.local/share/chaosbox/icons' / f'{target.name}.png').exists())
            self.assertFalse((home / '.local/bin/KBox-Bilder7').exists())
            self.assertFalse((home / '.local/bin/chaosbox').exists())
            self.assertFalse((home / 'My Desktop/KBox-Bilder7.desktop').exists())
            self.assertEqual({p: p.read_bytes() for p in preserved}, preserved)
            self.assertEqual({p: p.read_bytes() for p in other_files}, other_files)
            self.assertTrue((home / '.local/share/chaosbox/desktop/app.py').exists())
            with patch.dict(os.environ, env):
                uninstall.uninstall(home, home / 'KBox-Bilder8')
            self.assertFalse((home / '.local/share/chaosbox/desktop/VERSION').exists())
            self.assertFalse((home / '.local/share/chaosbox/desktop/sync_transport.py').exists())
