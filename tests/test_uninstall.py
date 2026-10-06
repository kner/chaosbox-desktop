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
            for name in ("app.py", "core.py", "setup.ini", "chaosbox.svg", "chaosbox.png"):
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
