"""Transport of immutable journal entries. No source files or tombstones are deleted."""
from pathlib import Path
import errno
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


def sftp_exchange(store, sftp, remote, check=lambda: None, progress=print, reader_factory=None):
    def mkdirs(path):
        if path in ('','/','.'): return
        try:
            info=sftp.lstat(path)
            if not stat.S_ISDIR(info.st_mode): raise ValueError('Unsafe remote directory: '+path)
        except OSError as e:
            if e.errno!=errno.ENOENT: raise
            mkdirs(posixpath.dirname(path));sftp.mkdir(path)
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
        store.capture(check,progress)
        store.apply(check,progress)
        store.acknowledge()
        for op in store.operations().values():remember_name(op)
        for folder,suffix in [('blobs','.blob'),('ops','.op')]:
            directory=posixpath.join(remote,folder)
            pending=sorted((store.root/folder).glob('*'+suffix))
            for index,source in enumerate(pending,1):
                check()
                if source.name in remote_files[folder]:continue
                temporary=posixpath.join(directory,'.'+uuid.uuid4().hex+'.tmp')
                try:
                    description=names[folder].get(source.name,'Änderungsdatensatz' if folder=='ops' else 'Mediendatei')
                    label=f'Uploading {"change" if folder=="ops" else "media"} {index}/{len(pending)}: {description}'
                    progress(label)
                    sftp.put(str(source),temporary,callback=byte_progress(label,check,progress))
                    check();sftp.rename(temporary,posixpath.join(directory,source.name))
                finally:
                    try:sftp.remove(temporary)
                    except OSError:pass
        check()
        manifest=publish_sftp_manifest(store,sftp,remote,check,server_manifest)
        progress('Local and server checksum agree; journal synchronized.')
        progress('Immutable journal exchanged over SFTP.')
    finally:
        progress('Releasing remote transfer lock …')
        sftp.rmdir(lock)

def shell_exchange(store, ssh, transport, cancel, progress=print):
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
            store.capture(check,progress)
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
        manifest=store.checksums()
        source=store.root/'checksums.json'
        temporary=remote+'/.checksums-'+uuid.uuid4().hex+'.tmp'
        with source.open('rb') as inp:
            run(args+['cat > '+q(temporary)+' && mv -f -- '+q(temporary)+' '+q(remote+'/checksums.json')],stdin=inp)
        progress('Local and server checksum agree; journal synchronized.')
        progress('Immutable journal exchanged over '+transport+'.')
    finally: run(args+['rmdir -- '+q(remote+'/transfer.lock')],cancel_ok=False)
