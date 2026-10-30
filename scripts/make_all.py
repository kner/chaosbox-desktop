#!/usr/bin/env python3
"""Install Desktop, deploy Web, synchronize and publish installation APK, Desktop installer and uninstaller."""
import argparse
from pathlib import Path
import sys
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import core
import build_user_installer
import build_user_uninstaller
import sync_transport


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def ensure_server_key(ssh):
    """Authorize the installation key through an existing passwordless SSH login."""
    options = ['-p', str(ssh['port']), '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes',
               '-o', 'UserKnownHostsFile=' + ssh['knownhosts'],
               '-o', 'ConnectTimeout=15']
    target = ssh['user'] + '@' + ssh['host']
    probe = subprocess.run(['/usr/bin/ssh', *options, '-o', 'IdentitiesOnly=yes',
                            '-o', 'IdentityAgent=none', '-i', ssh['keyfile'],
                            target, 'true'], capture_output=True, text=True)
    if probe.returncode == 0:
        return
    public = Path(ssh['keyfile'] + '.pub')
    if not public.is_file():
        raise ValueError(f'Öffentlicher Installationsschlüssel fehlt: {public}')
    print('Installationsschlüssel über bestehende SSH-Vertrauensstellung freischalten …', flush=True)
    # ssh-copy-id uses the normal SSH identities/agent to authorize this new key.
    # BatchMode prevents both password and interactive passphrase prompts.
    try:
        run('ssh-copy-id', '-i', public, *options, target)
    except subprocess.CalledProcessError as error:
        raise ValueError('Keine Anmeldung ohne Passwort möglich. Den öffentlichen '
                         f'Installationsschlüssel {public} auf dem Server autorisieren '
                         'oder eine funktionierende SSH-Anmeldung per Schlüssel/Agent bereitstellen.') from error


def android_setup(settings, android):
    """Return the common setup used by Desktop and Android, with shared sync IDs."""
    text = settings.path.read_text(encoding='utf-8-sig')
    return build_user_installer.bind_sync_targets(text, settings.profiles)


def initial_setup(name):
    """Adapt the first Box title in the bundled template without dropping entries."""
    text = core.english_setup(core.DEFAULT_SETUP.read_text(encoding='utf-8-sig'))
    lines = list(core.logical_lines(text))
    first_title = None
    for section, values, begin, end in core.section_blocks(text):
        if section.casefold() != 'box':
            continue
        first_title = values.get('titel', '')
        title_line = next((index for index in range(begin + 1, end)
                           if '=' in lines[index]
                           and lines[index].split('=', 1)[0].strip().casefold() == 'title'), None)
        if title_line is None:
            lines.insert(begin + 1, 'Title=' + name)
        else:
            key = lines[title_line].split('=', 1)[0]
            lines[title_line] = key + '=' + name
        break
    if first_title:
        for section, _, begin, end in core.section_blocks(text):
            if section.casefold() != 'ssh.' + first_title.casefold():
                continue
            lines[begin] = '[SSH.' + name + ']'
            for index in range(begin + 1, end):
                if '=' in lines[index] and lines[index].split('=', 1)[0].strip().casefold() == 'remoteprofile':
                    key = lines[index].split('=', 1)[0]
                    lines[index] = key + '=' + name.casefold()
    if first_title:
        return '\n'.join(lines).rstrip() + '\n'
    return text.rstrip() + f'\n\n[Box]\nTitle={name}\n'


def upload_file(sftp, source, destination):
    """Stage complete artifacts before making them visible in download listings."""
    import uuid
    parent = str(Path(destination).parent)
    parts = parent.split('/')
    for i in range(1, len(parts) + 1):
        directory = '/'.join(parts[:i])
        if not directory:
            continue
        try:
            sftp.stat(directory)
        except FileNotFoundError:
            sftp.mkdir(directory)
    temporary = destination + '.' + uuid.uuid4().hex + '.tmp'
    try:
        sftp.put(str(source), temporary)
        sftp.posix_rename(temporary, destination)
    finally:
        try:
            sftp.remove(temporary)
        except FileNotFoundError:
            pass


def _make_all(name, install_dir, android, web):
    versions = [(p / 'VERSION').read_text().strip() for p in (ROOT, android, web)]
    if len(set(versions)) != 1:
        raise ValueError('VERSION muss in allen drei Projekten identisch sein.')
    for file in (android / 'sign.sh', android / 'configure-adb-sync.py', web / 'scripts/upload-exif.sh'):
        if not file.is_file():
            raise ValueError(f'Datei fehlt: {file}')
    # A named make-all run on a fresh installation owns its initial box name.
    # Seed this before desktop installation so neither the desktop installer nor
    # the APK build can fall back to the generic bundled profiles.
    setup_path = install_dir / 'setup.ini'
    if not setup_path.is_file():
        install_dir.mkdir(parents=True, exist_ok=True)
        core.atomic_write(setup_path, initial_setup(name))
    print(f'[1/6] Desktop installieren: {install_dir}', flush=True)
    run(ROOT / 'scripts/install-desktop.sh', '--installdir', install_dir)
    settings = core.Settings(install_dir)
    settings.reload()
    for profile in settings.profiles:
        if '/' in profile.id or profile.setup_path != settings.path:
            raise ValueError('Der Gesamtbuild unterstützt derzeit Boxen aus der zentralen setup.ini; lokale Box-setups bitte zuerst zentral zusammenführen.')
    setup = android_setup(settings, android)
    if setup != settings.path.read_text(encoding='utf-8-sig'):
        core.atomic_write(settings.path, setup)
        settings.reload()
        setup = settings.path.read_text(encoding='utf-8-sig')
    configurations = [profile.options[2] for profile in settings.profiles]
    ssh = configurations[0]
    target_keys = ('host', 'port', 'user', 'keyfile', 'knownhosts', 'webinstallation')
    if any(any(c.get(key, 'l1' if key == 'webinstallation' else '') != ssh.get(key, 'l1' if key == 'webinstallation' else '') for key in target_keys) for c in configurations):
        raise ValueError('Alle Boxen müssen denselben Webserver und dieselben SSH-Zugangsdaten verwenden.')
    for key in ('keyfile', 'knownhosts'):
        if not Path(ssh[key]).is_file():
            raise ValueError(f'SSH-Datei fehlt: {ssh[key]}')
    web_root = ssh.get('webinstallation', 'l1')
    ensure_server_key(ssh)
    print('[2/6] Web-Anwendung hochladen', flush=True)
    # Use the existing uploader with the installation’s explicit SSH identity.
    with tempfile.TemporaryDirectory(prefix='kbox-ssh-') as temp:
        wrapper = Path(temp) / 'ssh'
        options = ['-p', ssh['port'], '-i', ssh['keyfile'], '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + ssh['knownhosts']]
        wrapper.write_text('#!/bin/sh\nexec /usr/bin/ssh ' + shlex.join(options) + ' "$@"\n')
        wrapper.chmod(0o700)
        import os
        run('bash', web / 'scripts/upload-exif.sh', ssh['user'] + '@' + ssh['host'], web_root,
            env={**os.environ, 'PATH': temp + ':' + os.environ.get('PATH', '')})
    print('[3/6] Alle Boxen synchronisieren', flush=True)
    for profile in settings.profiles:
        settings.profile(profile.id)
        core.upload(settings, profile, threading.Event(), print)
    print('[4/6] Signierte APK mit Installations-Setup bauen', flush=True)
    apk_name = f'KBox-{name}-{versions[0].replace(".", "")}.apk'
    with tempfile.TemporaryDirectory(prefix='kbox-android-') as temp:
        stage = Path(temp) / 'android'
        shutil.copytree(android, stage, ignore=shutil.ignore_patterns('.git', '.gradle', '.tooling', 'build', 'released', '*.apk', 'signing-keys'))
        for directory in ('.tooling', 'signing-keys'):
            (stage / directory).symlink_to(android / directory, target_is_directory=True)
        assets = stage / 'app/src/main/assets'
        assets.mkdir(parents=True, exist_ok=True)
        setup_file = Path(temp) / 'setup.ini'
        setup_file.write_text(setup, encoding='utf-8')
        # Android seeds installation-owned credentials from these asset names.
        shutil.copyfile(ssh['keyfile'], assets / 'android_copy')
        shutil.copyfile(ssh['knownhosts'], assets / 'known_hosts')
        run('bash', stage / 'sign.sh', '--installdir', name,
            env={**os.environ, 'KBOX_SETUP_FILE': str(setup_file)})
        built = stage / 'released' / apk_name
        with zipfile.ZipFile(built) as archive:
            if archive.read('assets/initial/Setup/setup.ini').decode('utf-8') != setup:
                raise ValueError('APK enthält nicht die erwartete setup.ini.')
        released = android / 'released' / apk_name
        released.parent.mkdir(exist_ok=True)
        shutil.copy2(built, released)
    print('[5/6] Desktop-Installer und Deinstaller bauen', flush=True)
    desktop_release = build_user_installer.build(ROOT / 'released', name=name, setup=settings.path)
    uninstaller_release = build_user_uninstaller.build(name, ROOT / 'released')
    print('[6/6] APK, Desktop-Installer und Deinstaller veröffentlichen', flush=True)
    with sync_transport.sftp_connection(ssh, threading.Event(), print) as sftp:
        upload_file(sftp, released, f'{web_root}/storage/apk/{apk_name}')
        for artifact in (desktop_release, desktop_release.with_suffix('.run.sha256'),
                         uninstaller_release, uninstaller_release.with_suffix('.run.sha256')):
            upload_file(sftp, artifact, f'{web_root}/storage/desktop/{artifact.name}')
    print(f'Fertig: {install_dir}\nAPK: {released}\nDesktop-Installer: {desktop_release}\nDesktop-Deinstaller: {uninstaller_release}\nServer: {web_root}/storage/apk/{apk_name}\nDesktop-Download: {web_root}/storage/desktop/{desktop_release.name}')


def make_all(name, install_dir, android, web):
    # Serialize installation updates, Gradle cache use and released/ output.
    import fcntl
    with (ROOT / '.make-all.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _make_all(name, install_dir, android, web)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', help='z.B. Bilder → ~/Kbox-Bilder und Android KBox-Bilder')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', args.name):
        parser.error('NAME: Buchstabe, danach Buchstaben/Ziffern/_/-, maximal 40 Zeichen')
    try:
        make_all(args.name, (Path.home() / f'Kbox-{args.name}').resolve(),
                 ROOT.parent / 'chaosbox-android', ROOT.parent / 'chaosbox-kner-at')
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Abgebrochen: {error}\n')


if __name__ == '__main__':
    main()
