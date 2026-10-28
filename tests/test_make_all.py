import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import core
import make_all

ANDROID = Path(__file__).resolve().parents[2] / 'chaosbox-android'


class MakeAllTests(unittest.TestCase):
    def test_export_binds_each_box_and_preserves_snippets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'setup.ini').write_text('[App]\nLanguage=de\nImageWidth=1800\n[Box]\nTitel=Bilder\neditor=/usr/bin/gimp\n[Box]\nTitel=Haus\n[Poster]\nPOSTER-COLS=3\n'
                '[SSH]\nKeyFile=/private/key\nKnownHosts=/private/hosts\n'
                '[SSH.Haus]\nInstallation=shared-house\n[TextSnippets]\nask="line 1\n[SSH]\nline 3"\n')
            settings = core.Settings(root)
            settings.reload()
            text = make_all.android_setup(settings, ANDROID)
            blocks = {key.casefold(): value for key, value in core.sections(text).items()}
            self.assertEqual(blocks['ssh.bilder']['installation'], settings.installation_id)
            self.assertEqual(blocks['ssh.haus']['installation'], 'shared-house')
            self.assertEqual(blocks['ssh.haus']['remoteprofile'], 'haus')
            self.assertNotIn('/private', text)
            self.assertEqual(text.count('[Box]'), 2)
            self.assertIn('; Language=de', text)
            self.assertIn('; editor=/usr/bin/gimp', text)
            self.assertIn('; POSTER-COLS=3', text)
            self.assertEqual(blocks['app']['imagewidth'], '1800')
            self.assertNotIn('poster', blocks)
            self.assertNotIn('[ImageSize]', text)
            self.assertIn('ask="line 1\n[SSH]\nline 3"', text)

    def test_upload_stages_before_atomic_publish(self):
        sftp = Mock()
        make_all.upload_file(sftp, Path('/tmp/example.apk'), 'l1/storage/apk/example.apk')
        staging = sftp.put.call_args.args[1]
        self.assertTrue(staging.endswith('.tmp'))
        sftp.posix_rename.assert_called_once_with(staging, 'l1/storage/apk/example.apk')
        sftp.remove.assert_called_once_with(staging)

    def test_failed_transfer_does_not_publish(self):
        sftp = Mock()
        sftp.put.side_effect = OSError('connection lost')
        with self.assertRaises(OSError):
            make_all.upload_file(sftp, Path('/tmp/example.apk'), 'l1/storage/apk/example.apk')
        sftp.posix_rename.assert_not_called()
        sftp.remove.assert_called_once()

    def test_pipeline_order_and_apk_setup(self):
        import contextlib
        import zipfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / 'Kbox-Bilder5'
            install.mkdir()
            credentials = install / '.state/credentials'
            credentials.mkdir(parents=True)
            for file in ('id_ed25519', 'known_hosts'):
                (credentials / file).write_text('test credential')
            (install / 'setup.ini').write_text('[App]\n[Box]\nTitel=Bilder\n')
            android, web = root / 'android', root / 'web'
            android.mkdir()
            (web / 'scripts').mkdir(parents=True)
            for project in (android, web):
                (project / 'VERSION').write_text((make_all.ROOT / 'VERSION').read_text())
            for file in ('sign.sh', 'configure-adb-sync.py'):
                (android / file).write_bytes((ANDROID / file).read_bytes())
            (web / 'scripts/upload-exif.sh').touch()
            events = []

            def copy_sources(source, stage, **kwargs):
                (stage / 'app/src/main').mkdir(parents=True)

            def command(*args, **kwargs):
                if str(args[0]).endswith('install-desktop.sh'):
                    events.append('install')
                elif str(args[1]).endswith('upload-exif.sh'):
                    events.append('web')
                    wrapper = Path(kwargs['env']['PATH'].split(':')[0]) / 'ssh'
                    self.assertIn('BatchMode=yes', wrapper.read_text())
                else:
                    events.append('apk')
                    stage = Path(args[1]).parent
                    assets = stage / 'app/src/main/assets'
                    self.assertEqual((assets / 'android_copy').read_text(), 'test credential')
                    self.assertEqual((assets / 'known_hosts').read_text(), 'test credential')
                    self.assertFalse((assets / 'initial/Setup/setup.ini').exists())
                    setup = Path(kwargs['env']['KBOX_SETUP_FILE']).read_text()
                    self.assertIn('RemoteProfile=bilder', setup)
                    output = stage / 'released'
                    output.mkdir()
                    version = (make_all.ROOT / 'VERSION').read_text().strip().replace('.', '')
                    with zipfile.ZipFile(output / f'KBox-Bilder5-{version}.apk', 'w') as archive:
                        archive.writestr('assets/initial/Setup/setup.ini', setup)

            original_uninstaller_build = make_all.build_user_uninstaller.build
            original_build = make_all.build_user_installer.build
            published = []

            def desktop_build(output, name, setup):
                import io
                import json
                import tarfile
                events.append('desktop')
                self.assertEqual(name, 'Bilder5')
                self.assertEqual(setup, install / 'setup.ini')
                self.assertEqual(output, make_all.ROOT / 'released')
                artifact = original_build(root / 'desktop-released', name=name, setup=setup)
                data = artifact.read_bytes()
                with tarfile.open(fileobj=io.BytesIO(data[data.index(b'\x1f\x8b'):]), mode='r:gz') as archive:
                    defaults = json.loads(archive.extractfile('install-defaults.json').read())
                    self.assertEqual(defaults['directory'], 'KBox-Bilder5')
                    self.assertIn('Title=Bilder', archive.extractfile('setup.ini').read().decode())
                    self.assertNotIn('id_ed25519', archive.getnames())
                return artifact

            def uninstaller_build(name, output):
                events.append('uninstaller')
                self.assertEqual(name, 'Bilder5')
                self.assertEqual(output, make_all.ROOT / 'released')
                artifact = original_uninstaller_build(name, root / 'desktop-released')
                self.assertEqual(artifact.name, 'KBox-Bilder5_uninstall.run')
                self.assertIn('KBox-Bilder5', artifact.read_text())
                return artifact

            def publish(sftp, source, destination):
                events.append('publish')
                self.assertTrue(source.is_file())
                published.append(destination)

            with patch.object(make_all, 'ensure_server_key'), \
                 patch.object(make_all, 'run', side_effect=command), \
                 patch.object(make_all.shutil, 'copytree', side_effect=copy_sources), \
                 patch.object(core, 'upload', side_effect=lambda *args: events.append('sync')), \
                 patch.object(make_all.sync_transport, 'sftp_connection', return_value=contextlib.nullcontext(Mock())), \
                 patch.object(make_all.build_user_installer, 'build', side_effect=desktop_build), \
                 patch.object(make_all.build_user_uninstaller, 'build', side_effect=uninstaller_build), \
                 patch.object(make_all, 'upload_file', side_effect=publish):
                make_all._make_all('Bilder5', install, android, web)
            self.assertEqual(events, ['install', 'web', 'sync', 'apk', 'desktop', 'uninstaller',
                                      'publish', 'publish', 'publish', 'publish', 'publish'])
            version = (make_all.ROOT / 'VERSION').read_text().strip()
            self.assertEqual(published, [
                f'l1/storage/apk/KBox-Bilder5-{version.replace(".", "")}.apk',
                f'l1/storage/desktop/KBox-Bilder5_{version}_user.run',
                f'l1/storage/desktop/KBox-Bilder5_{version}_user.run.sha256',
                'l1/storage/desktop/KBox-Bilder5_uninstall.run',
                'l1/storage/desktop/KBox-Bilder5_uninstall.run.sha256'])

    def test_installer_seeds_sibling_host_keys_and_preserves_existing_file(self):
        import install
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            destination = home / 'Kbox-Bilder1'
            install.install(home, installdir=destination)
            hosts = destination / '.state/credentials/known_hosts'
            bundled = ANDROID / 'app/src/main/assets/known_hosts'
            self.assertEqual(hosts.read_bytes(), bundled.read_bytes())
            self.assertEqual(hosts.stat().st_mode & 0o777, 0o600)
            hosts.write_text('existing trusted host keys\n')
            install.install(home, installdir=destination)
            self.assertEqual(hosts.read_text(), 'existing trusted host keys\n')

    def test_authorized_key_needs_no_bootstrap(self):
        from unittest.mock import patch
        ssh = dict(host='server', port='22', user='user', keyfile='/key', knownhosts='/hosts')
        with patch.object(make_all.subprocess, 'run', return_value=Mock(returncode=0)) as command, patch.object(make_all, 'run') as bootstrap:
            make_all.ensure_server_key(ssh)
        self.assertIn('BatchMode=yes', command.call_args.args[0])
        bootstrap.assert_not_called()

    def test_new_key_uses_existing_trust_without_password(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            public = Path(temp) / 'key.pub'
            public.write_text('ssh-ed25519 test')
            ssh = dict(host='server', port='22', user='user', keyfile=str(public.with_suffix('')), knownhosts='/hosts')
            with patch.object(make_all.subprocess, 'run', return_value=Mock(returncode=255)), patch.object(make_all, 'run') as bootstrap:
                make_all.ensure_server_key(ssh)
            args = bootstrap.call_args.args
            self.assertEqual(args[0], 'ssh-copy-id')
            self.assertIn('BatchMode=yes', args)
            self.assertIn(public, args)

    def test_export_matches_android_parser(self):
        import shutil
        import subprocess
        if not shutil.which('javac') or not shutil.which('java'):
            self.skipTest('JDK required for cross-platform setup verification')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = core.Settings(root / 'installation')
            settings.ensure()
            setup = root / 'setup.ini'
            setup.write_text(make_all.android_setup(settings, ANDROID))
            source = root / 'SetupCheck.java'
            source.write_text(r'''package local.sshcopy;
import java.nio.file.*;
import java.util.Locale;
public class SetupCheck {
 public static void main(String[] args) throws Exception {
  AppProfiles config=AppProfiles.parse(Files.readString(Path.of(args[0])));
  for (AppProfiles.Profile profile:config.profiles)
   System.out.println(profile.id+"\t"+profile.imageWidth+"\t"+String.join(",",profile.fields)
      +"\t"+String.join(",",profile.categories).toLowerCase(Locale.ROOT));
 }
}
''')
            sources = ANDROID / 'app/src/main/java/local/sshcopy'
            subprocess.run(['javac', '-d', str(root), str(sources / 'AppProfiles.java'),
                            str(sources / 'TextSnippets.java'), str(source)], check=True, capture_output=True)
            result = subprocess.run(['java', '-cp', str(root), 'local.sshcopy.SetupCheck', str(setup)],
                                    check=True, capture_output=True, text=True)
            expected = ['\t'.join((profile.id, str(profile.options[0]), ','.join(profile.labels),
                                  ','.join(profile.categories).lower())) for profile in settings.profiles]
            self.assertEqual(result.stdout.splitlines(), expected)
