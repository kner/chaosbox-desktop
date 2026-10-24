"""Transport of immutable journal entries. No source files or tombstones are deleted."""
from pathlib import Path
import errno
import hashlib
import json
import os
import posixpath
import shlex
import stat
import subprocess
import uuid
import shutil
import tempfile
import signal
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from chaos_sync import digest
from chaos_sync import Store, operation_label, canonical, checksum_manifest, valid_manifest

class RemoteLockError(FileExistsError):
    """A different transfer owns the server lock; never remove it automatically."""


def acquire_sftp_lock(sftp, lock):
    try:
        sftp.mkdir(lock)
    except OSError as error:
        # Some servers return generic SFTP_FAILURE for mkdir(existing directory)
        # rather than an errno. Inspect the path before reporting the real cause.
        try:
            info = sftp.lstat(lock)
        except OSError:
            raise OSError(f'Cannot acquire remote synchronization lock {lock}: {error}') from error
        if stat.S_ISDIR(info.st_mode):
            raise RemoteLockError(
                f'Remote synchronization is busy (transfer.lock): {lock}. '
                'Wait for the other transfer to finish, then retry. '
                'If the lock remains after all clients have stopped, inspect it manually.') from error
        raise OSError(f'Unsafe remote synchronization lock path {lock}: {error}') from error


@contextmanager
def sftp_connection(ssh, cancel, progress=print, setup_timeout=20):
    """Bound channel/subsystem negotiation as well as TCP authentication.

    Paramiko's open_sftp() can wait indefinitely before a channel timeout can
    be installed. Closing the client wakes these internal negotiation waits.
    """
    import paramiko
    client = paramiko.SSHClient()
    stopped, expired = threading.Event(), threading.Event()
    deadline = [time.monotonic() + 45]
    def watch_setup():
        while not stopped.wait(.05):
            if cancel.is_set() or time.monotonic() >= deadline[0]:
                if not cancel.is_set(): expired.set()
                client.close()
                return
    watcher = threading.Thread(target=watch_setup, daemon=True)
    try:
        client.load_host_keys(str(Path(ssh['knownhosts']).expanduser()))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        if cancel.is_set(): raise InterruptedError('Synchronization cancelled')
        progress('Connecting to SSH server …')
        watcher.start()
        try:
            client.connect(ssh['host'], port=int(ssh['port']), username=ssh['user'],
                           key_filename=str(Path(ssh['keyfile']).expanduser()),
                           allow_agent=False, look_for_keys=False,
                           timeout=15, auth_timeout=15, banner_timeout=15)
            deadline[0] = time.monotonic() + setup_timeout
            progress('Opening SFTP channel …')
            sftp = client.open_sftp()
            sftp.get_channel().settimeout(20)
        except Exception as error:
            if cancel.is_set(): raise InterruptedError('Synchronization cancelled') from error
            if expired.is_set(): raise TimeoutError('SSH/SFTP setup timed out; check server availability and SFTP support.') from error
            raise
        finally:
            stopped.set()
            if watcher.ident is not None: watcher.join(timeout=1)
        if cancel.is_set(): raise InterruptedError('Synchronization cancelled')
        if expired.is_set(): raise TimeoutError('SSH/SFTP setup timed out; check server availability and SFTP support.')
        yield sftp
    finally:
        stopped.set()
        client.close()


def byte_progress(label, check, progress):
    last = [time.monotonic()]
    def update(sent, total):
        check()
        now = time.monotonic()
        if sent == total or now - last[0] >= 1:
            progress(f'{label}: {sent / 1048576:.1f} / {total / 1048576:.1f} MiB')
            last[0] = now
    return update

def open_sftp_reader(sftp):
    """An independent channel: Paramiko SFTP clients cannot be shared by threads."""
    import paramiko
    channel=sftp.get_channel().get_transport().open_session(timeout=15)
    timer=threading.Timer(20,channel.close)
    timer.daemon=True
    try:
        channel.settimeout(20)
        timer.start()
        channel.invoke_subsystem('sftp')
        return paramiko.SFTPClient(channel)
    except BaseException:
        channel.close()
        raise
    finally:timer.cancel()


def parallel_sftp_reads(items, download, reader_factory, fallback, check):
    readers=[]
    local=threading.local()
    stopped=threading.Event()
    def work(item):
        check()
        if stopped.is_set():raise InterruptedError('Transfer stopped')
        if not hasattr(local,'reader'):
            try:local.reader=reader_factory()
            except Exception:local.reader=None
            if local.reader is not None:
                readers.append(local.reader)
                if stopped.is_set():local.reader.close();raise InterruptedError('Transfer stopped')
        if local.reader is None:return item
        download(item,local.reader,stopped)
        return None
    pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='sftp-read')
    futures=[]
    try:
        futures=[pool.submit(work,item) for item in items]
        for future in as_completed(futures):
            check()
            retry=future.result()
            if retry is not None:download(retry,fallback,stopped)
    finally:
        stopped.set()
        for future in futures:future.cancel()
        for reader in list(readers):reader.close()
        pool.shutdown(wait=True,cancel_futures=True)


def remote_inventory(items):
    return {name:dict(size=item.st_size,mtime=item.st_mtime) for name,item in items.items()
            if name.endswith('.op') and stat.S_ISREG(item.st_mode)
            and hasattr(item,'st_size') and hasattr(item,'st_mtime')}


def read_sftp_manifest(sftp, remote, progress):
    try:
        with sftp.open(posixpath.join(remote,'checksums.json'),'rb') as stream:
            data=stream.read(16777217)
        if len(data)>16777216:raise ValueError('Checksum index too large')
        result=json.loads(data)
        if valid_manifest(result):return result
    except (OSError,ValueError):pass
    progress('Checksum index unavailable; checking complete journal …')
    return None


def publish_sftp_manifest(store, sftp, remote, check, previous=None):
    entries={item.filename:item for item in sftp.listdir_attr(posixpath.join(remote,'ops'))}
    operations=store.operations()
    value=checksum_manifest(operations,remote_inventory(entries))
    if set(value['ops'])!={name for name,item in entries.items() if name.endswith('.op') and stat.S_ISREG(item.st_mode)}:
        raise ValueError('Remote journal changed while publishing checksum index')
    path=store.root/'checksums.json'
    if previous==value:
        check()
        from chaos_sync import atomic
        atomic(path,canonical(value)+'\n')
        return value
    # The checksum only covers immutable operation content, including blob references.
    # Server inventory timestamps are an optional cache guard, excluded from the checksum.
    temporary=posixpath.join(remote,'.checksums-'+uuid.uuid4().hex+'.tmp')
    target=posixpath.join(remote,'checksums.json')
    try:
        check()
        with sftp.open(temporary,'wb') as out:out.write((canonical(value)+'\n').encode())
        check()
        try:sftp.lstat(target)
        except OSError as error:
            if error.errno!=errno.ENOENT:raise
        else:sftp.remove(target)
        sftp.rename(temporary,target)
        atomic_content=canonical(value)+'\n'
        from chaos_sync import atomic
        atomic(path,atomic_content)
    finally:
        try:sftp.remove(temporary)
        except OSError:pass
    return value


def json_projection(store):
    """Materialize current record heads; conflicting paths keep their server copy."""
    operations = store.operations()
    paths = {op['path'] for op in operations.values() if op['kind'] == 'record'}
    # Include legacy empty files for removal; journal tombstones remain intact.
    for source in store.sources(store.data, {'.json'}):
        if source.name in ('records.json', 'cb.json', 'desktop-index.json'):
            continue
        if json.loads(source.read_text(encoding='utf-8-sig')) == []:
            paths.add(source.relative_to(store.data).as_posix())
    protected = set()
    records = {}
    for ident, versions in sorted(store.heads(operations).items()):
        if versions[0]['kind'] != 'record':
            continue
        if len(versions) > 1:
            protected.update(op['path'] for op in operations.values() if op['id'] == ident)
        elif versions[0]['action'] == 'put':
            op = versions[0]
            records.setdefault(op['path'], []).append(op['record'])
    return {path: (canonical(records[path]) + '\n').encode('utf-8') if records.get(path) else None
            for path in sorted(paths - protected)}


def sftp_json_projection(store, sftp, remote, mkdirs, check, progress):
    progress('Updating current JSON files in remote TXT/ …')
    data = posixpath.dirname(remote.rstrip('/')) or '.'
    desired = json_projection(store)
    # Check all managed paths before replacing any file; unrelated paths are untouched.
    for relative in desired:
        check()
        target = posixpath.join(data, relative)
        mkdirs(posixpath.dirname(target))
        try:
            info = sftp.lstat(target)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
        else:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Unsafe remote JSON: ' + target)
    for relative, content in desired.items():
        check()
        target = posixpath.join(data, relative)
        if content is None:
            try: sftp.remove(target)
            except OSError as error:
                if error.errno != errno.ENOENT: raise
            continue
        try:
            with sftp.open(target, 'rb') as stream:
                if stream.read() == content:
                    continue
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
        temporary = target + '.' + uuid.uuid4().hex + '.tmp'
        try:
            with sftp.open(temporary, 'wb') as stream:
                stream.write(content)
            check()
            if hasattr(sftp, 'posix_rename'):
                try:
                    sftp.posix_rename(temporary, target)
                    continue
                except OSError as error:
                    if error.errno not in (errno.ENOSYS, errno.EOPNOTSUPP) and 'unsupported' not in str(error).lower():
                        raise
            # Servers without the atomic overwrite extension require remove + rename.
            sftp_remove_if_present(sftp, target)
            sftp.rename(temporary, target)
        finally:
            sftp_remove_if_present(sftp, temporary)


def shell_json_projection(store, remote):
    q = shlex.quote
    data = posixpath.dirname(remote.rstrip('/')) or '.'
    desired = json_projection(store)
    lines = ['set -eu']
    checked = set()
    for relative in desired:
        target = posixpath.join(data, relative)
        directory = posixpath.dirname(target)
        parents = []
        while directory not in ('', '/', '.'):
            parents.append(directory)
            directory = posixpath.dirname(directory)
        for directory in reversed(parents):
            if directory not in checked:
                checked.add(directory)
                quoted = q(directory)
                lines.append('[ ! -L ' + quoted + ' ] && { [ -d ' + quoted + ' ] || mkdir -- ' + quoted + '; }')
        quoted = q(target)
        lines.append('[ ! -L ' + quoted + ' ] && { [ ! -e ' + quoted + ' ] || [ -f ' + quoted + ' ]; }')
    for relative, content in desired.items():
        target = q(posixpath.join(data, relative))
        if content is None:
            lines.append('rm -f -- ' + target)
            continue
        temporary = q(posixpath.join(data, relative) + '.' + uuid.uuid4().hex + '.tmp')
        lines.append('printf ' + q('%s') + ' ' + q(content.decode('utf-8')) + ' > ' + temporary)
        lines.append('if cmp -s -- ' + temporary + ' ' + target + '; then rm -- ' + temporary
                     + '; else mv -f -- ' + temporary + ' ' + target + '; fi')
    return '\n'.join(lines)


def media_projection(store):
    """Current media plus known historical bytes; conflicts keep their existing view."""
    operations=store.operations()
    desired={}; known={}; protected=set()
    for op in operations.values():
        if op['kind']=='media' and op['action']=='put':
            known.setdefault(op['path'],set()).add(op['blob'])
    for versions in store.heads(operations).values():
        if versions[0]['kind']!='media':continue
        if len(versions)>1:
            ident=versions[0]['id']
            protected.update(op['path'] for op in operations.values() if op['id']==ident)
        elif versions[0]['action']=='put':
            op=versions[0];path=op['path']
            if path in desired:raise ValueError('Media filename conflict: '+path)
            desired[path]=op['blob']
    for path in protected:desired.pop(path,None)
    return desired,known,protected


def media_checksum(store):
    """Same media-only journal checksum used by Android's v1 checkpoint."""
    media = {revision: op for revision, op in store.operations().items() if op['kind'] == 'media'}
    return hashlib.sha256(canonical(media).encode('utf-8')).hexdigest()


def sftp_media_inventory(sftp, images, paths, mkdirs, check):
    """One listing per managed folder; no media content crosses the network."""
    folders = {}
    inventory = {}
    for path in sorted(paths):
        check()
        parent = posixpath.dirname(posixpath.join(images, path))
        if parent not in folders:
            mkdirs(parent)
            folders[parent] = {item.filename: item for item in sftp.listdir_attr(parent)}
        item = folders[parent].get(posixpath.basename(path))
        if item is not None and not stat.S_ISREG(item.st_mode):
            raise ValueError('Unsafe remote media: ' + path)
        inventory[path] = None if item is None else [item.st_size, int(item.st_mtime), item.st_mode]
    return hashlib.sha256(canonical(inventory).encode('utf-8')).hexdigest()


def sftp_checkpoint_matches(sftp, path, receipt, check):
    try:
        with sftp.open(path, 'rb') as stream:
            check()
            content = stream.read(4097)
        return len(content) <= 4096 and json.loads(content) == receipt
    except OSError as error:
        if error.errno != errno.ENOENT:
            raise
    except (ValueError, UnicodeError):
        pass
    return False


def sftp_remove_if_present(sftp, path):
    try:
        sftp.remove(path)
    except OSError as error:
        if error.errno != errno.ENOENT:
            raise


def sftp_media_projection(store, sftp, remote, mkdirs, check, progress):
    desired,known,protected=media_projection(store)
    images=posixpath.join(posixpath.dirname(posixpath.dirname(remote.rstrip('/'))),'JPG')
    progress('Checking remote media inventory …')
    mkdirs(images)
    managed = set(known) - protected
    checkpoint = posixpath.join(remote, 'media-checkpoint.json')
    receipt = dict(schema=1, images=images, media_checksum=media_checksum(store),
                   inventory_transport='sftp',
                   inventory_checksum=sftp_media_inventory(sftp, images, managed, mkdirs, check))
    if sftp_checkpoint_matches(sftp, checkpoint, receipt, check):
        progress('Media journal and remote inventory unchanged; skipping remote JPG checks and transfers.')
        return
    # A failed publication must never leave a valid receipt behind.
    sftp_remove_if_present(sftp, checkpoint)
    progress('Updating current media in remote JPG/ …')
    def remote_hash(path):
        try:info=sftp.lstat(path)
        except OSError as error:
            if error.errno==errno.ENOENT:return None
            raise
        if not stat.S_ISREG(info.st_mode):raise ValueError('Unsafe remote media: '+path)
        import hashlib
        value=hashlib.sha256()
        with sftp.open(path,'rb') as stream:
            while True:
                check();chunk=stream.read(65536)
                if not chunk:break
                value.update(chunk)
        return value.hexdigest()
    # Preflight every managed path before publishing or removing anything.
    current={}
    for index, path in enumerate(sorted(managed), 1):
        progress(f'Checking remote media {index}/{len(managed)}: {path}')
        check();target=posixpath.join(images,path)
        mkdirs(posixpath.dirname(target))
        current[path]=remote_hash(target)
        if path in desired and current[path] is not None and current[path] not in known[path]:
            raise ValueError('Untracked remote JPG file; retained: '+path)
    for path,blob in sorted(desired.items()):
        check()
        if current[path]==blob:continue
        source=store.root/'blobs'/(blob+'.blob')
        if digest(source)!=blob:raise ValueError('Corrupt media payload: '+path)
        target=posixpath.join(images,path);temporary=target+'.'+uuid.uuid4().hex+'.tmp'
        try:
            sftp.put(str(source),temporary,callback=byte_progress('Publishing JPG/'+path,check,progress))
            check()
            renamed=False
            if hasattr(sftp,'posix_rename'):
                try:sftp.posix_rename(temporary,target);renamed=True
                except OSError as error:
                    if error.errno not in (errno.ENOSYS,errno.EOPNOTSUPP) and 'unsupported' not in str(error).lower():raise
            if not renamed:
                if current[path] is not None:sftp.remove(target)
                sftp.rename(temporary,target)
        finally:
            try:sftp.remove(temporary)
            except OSError:pass
    for path,value in current.items():
        if path not in desired and value in known[path]:
            check();sftp.remove(posixpath.join(images,path))

    receipt['inventory_checksum'] = sftp_media_inventory(sftp, images, managed, mkdirs, check)
    temporary = posixpath.join(remote, '.media-checkpoint-' + uuid.uuid4().hex + '.tmp')
    try:
        with sftp.open(temporary, 'wb') as stream:
            stream.write((canonical(receipt) + '\n').encode('utf-8'))
        check()
        sftp.rename(temporary, checkpoint)
    finally:
        sftp_remove_if_present(sftp, temporary)


def shell_media_projection(store, remote):
    desired,known,protected=media_projection(store)
    images=posixpath.join(posixpath.dirname(posixpath.dirname(remote.rstrip('/'))),'JPG')
    q=shlex.quote
    lines=['set -eu', 'command -v sha256sum >/dev/null', 'command -v stat >/dev/null']
    checked_directories=set()
    def directories(path):
        parts=[]
        while path not in ('','/','.'):
            parts.append(path);path=posixpath.dirname(path)
        for directory in reversed(parts):
            if directory in checked_directories:continue
            checked_directories.add(directory)
            quoted=q(directory)
            lines.append('[ ! -L '+quoted+' ] && { [ -d '+quoted+' ] || mkdir -- '+quoted+'; }')
    # Validate parent directories and file types before trusting cached inventory.
    directories(images)
    for path in sorted(set(known)-protected):
        target=posixpath.join(images,path);directories(posixpath.dirname(target));target=q(target)
        lines.append('[ ! -L '+target+' ] && { [ ! -e '+target+' ] || [ -f '+target+' ]; }')
    lines.extend(['media_inventory() {', 'items=$(', ':'])
    for path in sorted(set(known)-protected):
        target=q(posixpath.join(images,path))
        lines.append('if [ -e '+target+' ]; then stat -c '+q('%n:%s:%y:%z:%f')+' -- '+target+' || exit 1; else printf '+q('%s\n')+' '+q('missing:'+path)+'; fi')
    lines.extend([') || return 1', 'printf '+q('%s')+' "$items" | sha256sum', '}'])
    checkpoint=q(remote+'/media-checkpoint.json')
    receipt=dict(schema=1, images=images, media_checksum=media_checksum(store),
                 inventory_transport='shell', inventory_checksum='__INVENTORY__')
    prefix,suffix=canonical(receipt).split('__INVENTORY__')
    receipt_command='receipt=$(printf '+q('%s%s%s')+' '+q(prefix)+' "$inventory" '+q(suffix)+')'
    lines.extend(['inventory=$(media_inventory)', 'inventory=${inventory%% *}', receipt_command,
        'if [ ! -L '+checkpoint+' ] && [ -f '+checkpoint+' ] && [ "$(cat -- '+checkpoint+')" = "$receipt" ]; then printf '+q('%s\n')+' '+q('Media journal and remote inventory unchanged; skipping remote JPG checks and transfers.')+'; exit 0; fi',
        'rm -f -- '+checkpoint])
    # Validate contents and payloads before changing the current view.
    for path in sorted(set(known)-protected):
        target=posixpath.join(images,path);directories(posixpath.dirname(target));target=q(target)
        lines.append('[ ! -L '+target+' ] && { [ ! -e '+target+' ] || [ -f '+target+' ]; }')
        if path in desired:
            allowed='|'.join(sorted(known[path]))
            lines.append('if [ -f '+target+' ]; then h=$(sha256sum -- '+target+'); h=${h%% *}; case "$h" in '+allowed+') ;; *) echo '+q('Untracked remote JPG file; retained: '+path)+' >&2; exit 1;; esac; fi')
            source=q(remote+'/blobs/'+desired[path]+'.blob')
            lines.append('[ ! -L '+source+' ] && [ -f '+source+' ] && h=$(sha256sum -- '+source+') && [ "${h%% *}" = '+q(desired[path])+' ]')
    for path in sorted(set(known)-protected):
        target=posixpath.join(images,path);quoted=q(target)
        if path in desired:
            source=q(remote+'/blobs/'+desired[path]+'.blob');temporary=q(target+'.'+uuid.uuid4().hex+'.tmp')
            lines.append('h=""; if [ -f '+quoted+' ]; then h=$(sha256sum -- '+quoted+'); h=${h%% *}; fi')
            lines.append('if [ "$h" != '+q(desired[path])+' ]; then cp -- '+source+' '+temporary+' && mv -f -- '+temporary+' '+quoted+'; fi')
        else:
            allowed='|'.join(sorted(known[path]))
            lines.append('if [ -f '+quoted+' ]; then h=$(sha256sum -- '+quoted+'); h=${h%% *}; case "$h" in '+allowed+') rm -- '+quoted+';; esac; fi')
    temporary=q(remote+'/.media-checkpoint-'+uuid.uuid4().hex+'.tmp')
    lines.extend(['inventory=$(media_inventory)', 'inventory=${inventory%% *}', receipt_command,
                  'printf '+q('%s\n')+' "$receipt" > '+temporary+' && mv -f -- '+temporary+' '+checkpoint])
    return '\n'.join(lines)


def sftp_exchange(store, sftp, remote, check=lambda: None, progress=print, reader_factory=None, initialize=False):
    directories = set()
    def mkdirs(path):
        if path in ('','/','.') or path in directories: return
        mkdirs(posixpath.dirname(path))
        try:
            info=sftp.lstat(path)
            if not stat.S_ISDIR(info.st_mode): raise ValueError('Unsafe remote directory: '+path)
        except OSError as e:
            if e.errno!=errno.ENOENT: raise
            mkdirs(posixpath.dirname(path));sftp.mkdir(path)
        directories.add(path)
    progress('Preparing remote journal and acquiring transfer lock …')
    check()
    mkdirs(remote)
    lock=posixpath.join(remote,'transfer.lock')
    acquire_sftp_lock(sftp,lock)
    try:
        protected=posixpath.join(remote,'.htaccess')
        try:sftp.lstat(protected)
        except OSError as e:
            if e.errno!=errno.ENOENT:raise
            with sftp.open(protected,'wb') as out:out.write(b'Require all denied\n')
        server_manifest=read_sftp_manifest(sftp,remote,progress)
        local_manifest=store.checksums()
        remote_files={}
        names={'ops':{},'blobs':{}}
        def remember_name(op):
            label=operation_label(op)
            names['ops'][op['revision']+'.op']=label
            if op.get('blob'):names['blobs'][op['blob']+'.blob']=label
        for op in store.operations().values():remember_name(op)
        with tempfile.TemporaryDirectory(prefix='.incoming-',dir=store.root) as temporary:
            staging=Path(temporary)
            # Read descriptions first so incoming media can be named before download.
            for folder,suffix in [('ops','.op'),('blobs','.blob')]:
                check();directory=posixpath.join(remote,folder);mkdirs(directory)
                (staging/folder).mkdir()
                progress('Listing remote '+('changes' if folder=='ops' else 'media')+' …')
                remote_files[folder]={item.filename:item for item in sftp.listdir_attr(directory)}
                reusable={}
                if folder=='ops' and server_manifest is not None:
                    inventory=remote_inventory(remote_files[folder])
                    names_present={name for name,item in remote_files[folder].items() if name.endswith('.op') and stat.S_ISREG(item.st_mode)}
                    if set(server_manifest['ops'])==names_present and server_manifest.get('inventory')==inventory and set(inventory)==names_present:
                        reusable={name for name,value in server_manifest['ops'].items() if local_manifest['ops'].get(name)==value}
                        if reusable:progress(f'Checksum index: {len(reusable)} unchanged changes; downloading differences only …')
                    else:progress('Checksum index is stale; checking complete journal …')
                items=[(index,name,item) for index,(name,item) in enumerate(remote_files[folder].items(),1)
                       if name.endswith(suffix) and stat.S_ISREG(item.st_mode) and name not in reusable]
                def download(entry,reader,stopped):
                    index,name,item=entry
                    check()
                    if stopped.is_set():raise InterruptedError('Transfer stopped')
                    if name[:-len(suffix)].strip('0123456789abcdef-'):raise ValueError('Unsafe journal filename')
                    description=names[folder].get(name,'Änderungsdatensatz' if folder=='ops' else 'Mediendatei')
                    local=store.root/folder/name
                    if folder=='blobs' and local.is_file() and not local.is_symlink() and digest(local)==name[:-len(suffix)]:
                        progress(f'Reusing local media {index}/{len(remote_files[folder])}: {description}')
                        return
                    label=f'Downloading {"change" if folder=="ops" else "media"} {index}/{len(remote_files[folder])}: {description}'
                    progress(label)
                    report=byte_progress(label,check,progress)
                    received=0
                    source=staging/folder/name
                    with reader.open(posixpath.join(directory,name),'rb') as stream,source.open('wb') as out:
                        while True:
                            check()
                            if stopped.is_set():raise InterruptedError('Transfer stopped')
                            chunk=stream.read(65536)
                            if not chunk:break
                            out.write(chunk)
                            received+=len(chunk)
                            if folder=='blobs':report(received,getattr(item,'st_size',received))
                    if folder=='ops':
                        op=json.loads(source.read_text(encoding='utf-8'))
                        store.validate(op)
                        if source.stem!=op['revision']:raise ValueError('Operation filename mismatch')
                        remember_name(op)
                        if description!=names[folder][name]:
                            progress(f'Received change {index}/{len(remote_files[folder])}: '+names[folder][name])
                factory=reader_factory
                if factory is None and hasattr(sftp,'get_channel'):
                    factory=lambda:open_sftp_reader(sftp)
                if folder=='ops' and factory is not None and len(items)>4:
                    progress('Downloading changes over up to 4 parallel SFTP channels …')
                    parallel_sftp_reads(items,download,factory,sftp,check)
                else:
                    for entry in items:download(entry,sftp,threading.Event())
            progress('Validating received journal …')
            store.import_from(staging,check,progress)
        if initialize: store.initialize_from_local(check,progress)
        else: store.capture(check,progress)
        store.apply(check,progress)
        store.acknowledge()
        for op in store.operations().values():remember_name(op)
        for folder,suffix in [('blobs','.blob'),('ops','.op')]:
            directory=posixpath.join(remote,folder)
            pending=[source for source in sorted((store.root/folder).glob('*'+suffix))
                     if source.name not in remote_files[folder]]
            def upload(entry,writer,stopped):
                index,source=entry
                check()
                if stopped.is_set():raise InterruptedError('Transfer stopped')
                temporary=posixpath.join(directory,'.'+uuid.uuid4().hex+'.tmp')
                published=False
                try:
                    description=names[folder].get(source.name,'Änderungsdatensatz' if folder=='ops' else 'Mediendatei')
                    label=f'Uploading {"change" if folder=="ops" else "media"} {index}/{len(pending)}: {description}'
                    progress(label)
                    # The complete temporary file is closed before its atomic publication.
                    # No redundant stat after put, or remove after a successful rename.
                    writer.put(str(source),temporary,callback=byte_progress(label,check,progress),confirm=False)
                    check()
                    if stopped.is_set():raise InterruptedError('Transfer stopped')
                    writer.rename(temporary,posixpath.join(directory,source.name));published=True
                finally:
                    if not published:
                        try:writer.remove(temporary)
                        except OSError:pass
            items=list(enumerate(pending,1))
            factory=reader_factory
            if factory is None and hasattr(sftp,'get_channel'):
                factory=lambda:open_sftp_reader(sftp)
            if folder=='ops' and factory is not None and len(items)>4:
                progress('Uploading changes over up to 4 parallel SFTP channels …')
                parallel_sftp_reads(items,upload,factory,sftp,check)
            else:
                for entry in items:upload(entry,sftp,threading.Event())
        check()
        sftp_media_projection(store,sftp,remote,mkdirs,check,progress)
        sftp_json_projection(store,sftp,remote,mkdirs,check,progress)
        manifest=publish_sftp_manifest(store,sftp,remote,check,server_manifest)
        progress('Local and server checksum agree; journal synchronized.')
        progress('Immutable journal exchanged over SFTP.')
    finally:
        progress('Releasing remote transfer lock …')
        sftp.rmdir(lock)

def shell_exchange(store, ssh, transport, cancel, progress=print, initialize=False):
    """rsync over SSH or tar streams over SSH. Paths are quoted, keys/hosts are explicit."""
    host=ssh['host']; user=ssh['user']; port=str(ssh['port'])
    if any(c in host+user for c in '\r\n') or host.startswith('-') or user.startswith('-'): raise ValueError('Invalid SSH host/user')
    args=['ssh','-p',port,'-i',str(Path(ssh['keyfile']).expanduser()),'-o','ConnectTimeout=15','-o','ServerAliveInterval=10','-o','ServerAliveCountMax=2','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
          '-o','UserKnownHostsFile='+str(Path(ssh['knownhosts']).expanduser()),user+'@'+host]
    remote=posixpath.join(ssh.get('shelldatadestination',ssh['datadestination']),'.chaosbox-sync')
    if not remote or '\n' in remote: raise ValueError('Invalid remote path')
    def run(command, cancel_ok=True, **kwargs):
        process=subprocess.Popen(command,start_new_session=True,**kwargs)
        try:
            while True:
                try:
                    code=process.wait(timeout=.2); break
                except subprocess.TimeoutExpired:
                    if cancel_ok and cancel.is_set(): raise ValueError('Synchronization cancelled')
            if code: raise ValueError(f'{transport} exited with status {code}')
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait()
    q=shlex.quote
    progress('Connecting over '+transport+' and acquiring transfer lock …')
    run(args+['mkdir -p -- '+q(remote)+'/blobs '+q(remote)+'/ops && mkdir -- '+q(remote+'/transfer.lock')])
    try:
        protected=remote+'/.htaccess'
        run(args+['if [ ! -e '+q(protected)+' ]; then printf \'%s\\n\' \'Require all denied\' > '+q(protected)+'; fi'])
        # Work in staging so transport failures never expose partial operations.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='chaosbox-sync-') as temporary:
            staging=Path(temporary)
            progress('Downloading remote journal over '+transport+' …')
            if transport=='rsync':
                run(['rsync','-rt','--timeout=60','--include=/ops/***','--include=/blobs/***','--exclude=*','--protect-args','-e',shlex.join(args[:-1]),'--',args[-1]+':'+remote+'/',str(staging)+'/'])
            else:
                with (staging/'download.tar').open('wb') as out:
                    run(args+['tar -C '+q(remote)+' -cf - ops blobs'],stdout=out)
                import tarfile
                with tarfile.open(staging/'download.tar') as archive:
                    for member in archive.getmembers():
                        path=Path(member.name)
                        if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk(): raise ValueError('Unsafe remote archive')
                        if member.isfile() and (len(path.parts)!=2 or path.parts[0] not in ('ops','blobs')): raise ValueError('Unexpected journal entry')
                    archive.extractall(staging,filter='data')
            def check():
                if cancel.is_set(): raise InterruptedError('Synchronization cancelled')
            progress('Validating received journal …')
            store.import_from(staging,check,progress)
            if initialize: store.initialize_from_local(check,progress)
            else: store.capture(check,progress)
            store.apply(check,progress)
            store.acknowledge()
            progress('Uploading local journal over '+transport+' …')
            if transport=='rsync':
                for folder in ('blobs','ops'):
                    (store.root/folder).mkdir(exist_ok=True)
                    run(['rsync','-rt','--timeout=60','--ignore-existing','--delay-updates','--protect-args','-e',shlex.join(args[:-1]),'--',str(store.root/folder)+'/',args[-1]+':'+remote+'/'+folder+'/'])
            else:
                # Extract complete immutable files in staging, then publish by rename server-side.
                import tarfile
                upload=staging/'upload.tar'
                with tarfile.open(upload,'w') as archive:
                    for folder in ('blobs','ops'):
                        (store.root/folder).mkdir(exist_ok=True)
                        archive.add(store.root/folder,arcname=folder)
                target=remote+'/.incoming-'+uuid.uuid4().hex
                command='mkdir -- '+q(target)+' && tar -C '+q(target)+' -xf - && '
                command+='for d in blobs ops; do for f in '+q(target)+'/$d/*; do [ -f "$f" ] || continue; b=${f##*/}; '
                command+='if [ -e '+q(remote)+'/$d/"$b" ]; then cmp -s "$f" '+q(remote)+'/$d/"$b" || exit 1; else mv -- "$f" '+q(remote)+'/$d/"$b" || exit 1; fi; done; done; rm -r -- '+q(target)
                with upload.open('rb') as inp: run(args+[command],stdin=inp)
        progress('Updating current media in remote JPG/ …')
        with tempfile.TemporaryFile() as script:
            script.write(shell_media_projection(store,remote).encode('utf-8'));script.seek(0)
            run(args+['sh -s'],stdin=script)
        progress('Updating current JSON files in remote TXT/ …')
        with tempfile.TemporaryFile() as script:
            script.write(shell_json_projection(store,remote).encode('utf-8'));script.seek(0)
            run(args+['sh -s'],stdin=script)
        manifest=store.checksums()
        source=store.root/'checksums.json'
        temporary=remote+'/.checksums-'+uuid.uuid4().hex+'.tmp'
        with source.open('rb') as inp:
            run(args+['cat > '+q(temporary)+' && mv -f -- '+q(temporary)+' '+q(remote+'/checksums.json')],stdin=inp)
        progress('Local and server checksum agree; journal synchronized.')
        progress('Immutable journal exchanged over '+transport+'.')
    finally: run(args+['rmdir -- '+q(remote+'/transfer.lock')],cancel_ok=False)
