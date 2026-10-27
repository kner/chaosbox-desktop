import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import build_user_installer
import core


class UserInstallerTests(unittest.TestCase):
    @unittest.skipIf(os.getuid() == 0, 'Installer requires a normal user')
    def test_downloaded_installer_and_update_preserve_box(self):
        with tempfile.TemporaryDirectory(prefix='chaosbox installer ') as temp:
            root = Path(temp)
            package = build_user_installer.build(root / 'downloads')
            home = root / 'home with spaces'
            home.mkdir()
            env = {**os.environ, 'HOME': str(home)}
            for attempt in range(2):
                subprocess.run(['sh', str(package)], env=env, check=True, capture_output=True)
                launcher = home / '.local/bin/chaosbox'
                self.assertTrue(os.access(launcher, os.X_OK))
                subprocess.run([str(launcher), '--check'], env=env, check=True, capture_output=True)
                shared = home / '.local/share/chaosbox/desktop'
                self.assertEqual((shared / 'VERSION').read_bytes(), (build_user_installer.SOURCE / 'VERSION').read_bytes())
                setup = home / 'ChaosBox/setup.ini'
                if attempt == 0:
                    setup.write_text(setup.read_text() + '\n; preserve custom setup\n')
                    original = setup.read_bytes()
                    key = home / 'ChaosBox/.state/credentials/id_ed25519'
                    original_key = key.read_bytes()
                else:
                    self.assertEqual(setup.read_bytes(), original)
                    self.assertEqual(key.read_bytes(), original_key)

    @unittest.skipIf(os.getuid() == 0, 'Installer requires a normal user')
    def test_named_installer_embeds_setup_and_targets_only_named_box(self):
        with tempfile.TemporaryDirectory(prefix='chaosbox named ') as temp:
            root = Path(temp)
            setup = root / 'setup.ini'
            setup.write_text('[App]\nLanguage=de\n[Box]\nTitle=Fotos\nCategories=Reisen,Familie\n'
                             '[SSH]\nHost=example.org\nUser=images\n'
                             'KeyFile=/original/private/id_ed25519\nKnownHosts=/original/known_hosts\n')
            hosts = root / '.state/credentials/known_hosts'
            hosts.parent.mkdir(parents=True)
            hosts.write_text('# public host identities\n')
            package = build_user_installer.build(root / 'downloads', 'Bilder3', setup)
            source_settings = core.Settings(root)
            source_settings.reload()
            source_ssh = source_settings.profiles[0].options[2]
            home = root / 'recipient home'
            home.mkdir()
            env = {**os.environ, 'HOME': str(home)}
            # An unrelated installation must not divert a named installer.
            generic = build_user_installer.build(root / 'downloads')
            subprocess.run(['sh', str(generic)], env=env, check=True, capture_output=True)
            unrelated = (home / 'ChaosBox/setup.ini').read_bytes()
            for attempt in range(2):
                subprocess.run(['sh', str(package)], env=env, check=True, capture_output=True)
                target = home / 'KBox-Bilder3'
                text = (target / 'setup.ini').read_text()
                recipient = core.Settings(target)
                recipient.reload()
                self.assertNotEqual(recipient.installation_id, source_settings.installation_id)
                self.assertEqual(recipient.profiles[0].options[2]['installation'], source_ssh['installation'])
                self.assertEqual(recipient.profiles[0].options[2]['datadestination'], source_ssh['datadestination'])
                self.assertIn('RemoteProfile=fotos', text)
                self.assertIn('Title=Fotos', text)
                self.assertIn('Host=example.org', text)
                self.assertIn('KeyFile=.state/credentials/id_ed25519', text)
                self.assertNotIn('/original/', text)
                self.assertEqual((target / '.state/credentials/known_hosts').read_bytes(), hosts.read_bytes())
                self.assertEqual((home / 'ChaosBox/setup.ini').read_bytes(), unrelated)
                desktops = list((home / '.local/share/applications').glob('*.desktop'))
                self.assertTrue(any('Name=KBox-Bilder3\n' in p.read_text() for p in desktops))
                command = (home / '.local/bin/chaosbox').read_text()
                import shlex
                launcher = Path(shlex.split(command.splitlines()[1])[1])
                self.assertIn(str(target), launcher.read_text())
                if attempt == 0:
                    (target / 'setup.ini').write_text(text + '\n; custom\n')
                    preserved = (target / 'setup.ini').read_bytes()
                else:
                    self.assertEqual((target / 'setup.ini').read_bytes(), preserved)

    @unittest.skipIf(os.getuid() == 0, 'Installer requires a normal user')
    def test_two_installers_have_independent_desktop_launchers_setups_and_icons(self):
        import shlex
        from PIL import Image
        with tempfile.TemporaryDirectory(prefix='chaosbox multi ') as temp:
            root = Path(temp)
            home = root / 'home'
            (home / '.config').mkdir(parents=True)
            (home / '.config/user-dirs.dirs').write_text('XDG_DESKTOP_DIR="$HOME/Schreibtisch"\n')
            env = {**os.environ, 'HOME': str(home), 'XDG_CONFIG_HOME': str(home / '.config')}
            packages, setups, icons = [], {}, []
            for number in (3, 4):
                name = f'Bilder{number}'
                setup = root / f'{name}.ini'
                setup.write_text(f'[App]\nLanguage=en\n[Box]\nTitle=Photos{number}\n'
                                 f'Categories=Category{number}\n')
                package = build_user_installer.build(root / 'downloads', name, setup)
                packages.append(package)
                subprocess.run(['sh', str(package)], env=env, check=True, capture_output=True)
                target = home / f'KBox-{name}'
                setups[target] = (target / 'setup.ini').read_bytes()
                shortcut = home / 'Schreibtisch' / f'KBox-{name}.desktop'
                self.assertTrue(os.access(shortcut, os.X_OK))
                text = shortcut.read_text()
                self.assertIn(f'Name=KBox-{name}\n', text)
                exec_line = next(line[5:] for line in text.splitlines() if line.startswith('Exec='))
                launcher = Path(shlex.split(exec_line)[0])
                self.assertIn(str(target), launcher.read_text())
                subprocess.run([str(launcher), '--check'], env=env, check=True, capture_output=True)
                subprocess.run([str(home / '.local/bin' / f'KBox-{name}'), '--check'],
                               env=env, check=True, capture_output=True)
                icon = Path(next(line[5:] for line in text.splitlines() if line.startswith('Icon=')))
                with Image.open(icon) as image:
                    icons.append(image.tobytes())
            self.assertNotEqual(*icons)
            subprocess.run(['sh', str(packages[0])], env=env, check=True, capture_output=True)
            for target, content in setups.items():
                self.assertEqual((target / 'setup.ini').read_bytes(), content)
            for number in (3, 4):
                self.assertTrue((home / f'Schreibtisch/KBox-Bilder{number}.desktop').exists())

    def test_export_preserves_profile_overrides_and_multiline_snippets(self):
        import make_all
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup = root / 'setup.ini'
            setup.write_text('[App]\n[Box]\nTitle=Fotos\n[Box]\nTitle=Haus\n'
                             '[SSH]\nInstallation=shared-default\nWebInstallation=l2\n'
                             '[SSH.Haus]\nInstallation=shared-house\nHost=other.example\n'
                             'RemoteProfile=stale\n'
                             '[TextSnippets]\nask="first\n[SSH.Fotos]\nInstallation=snippet\nlast"\n')
            original = setup.read_bytes()
            exported = build_user_installer.installation_setup(setup)
            self.assertEqual(setup.read_bytes(), original)
            self.assertIn('ask="first\n[SSH.Fotos]\nInstallation=snippet\nlast"', exported)
            blocks = core.sections(exported)
            self.assertEqual(blocks['SSH.Fotos']['installation'], 'shared-default')
            self.assertEqual(blocks['SSH.Haus']['installation'], 'shared-house')
            self.assertEqual(blocks['SSH.Haus']['remoteprofile'], 'haus')
            source = core.Settings(root)
            source.reload()
            recipient_root = root / 'another-machine'
            recipient_root.mkdir()
            (recipient_root / 'setup.ini').write_text(exported)
            recipient = core.Settings(recipient_root)
            recipient.reload()
            self.assertEqual([p.options[2]['datadestination'] for p in source.profiles],
                             [p.options[2]['datadestination'] for p in recipient.profiles])
            android = Path(__file__).resolve().parents[2] / 'chaosbox-android'
            apk_blocks = core.sections(make_all.android_setup(source, android))
            for profile in source.profiles:
                section = 'SSH.' + profile.id
                self.assertEqual(blocks[section]['installation'], apk_blocks[section]['installation'])
                self.assertEqual(blocks[section]['remoteprofile'], apk_blocks[section]['remoteprofile'])
