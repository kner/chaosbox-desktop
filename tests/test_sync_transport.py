import os,sys,tempfile,unittest,threading,stat,json,types,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from chaos_sync import Store,atomic,canonical
import sync_transport

class FileSftp:
    def __init__(self,root):self.root=root
    def path(self,name):return self.root/name.lstrip('/')
    def lstat(self,path):return self.path(path).lstat()
    def mkdir(self,path):self.path(path).mkdir()
    def rmdir(self,path):self.path(path).rmdir()
    def listdir_attr(self,path):return [types.SimpleNamespace(filename=f.name,st_mode=f.lstat().st_mode,st_size=f.stat().st_size,st_mtime=int(f.stat().st_mtime)) for f in self.path(path).iterdir()]
    def open(self,path,mode):return self.path(path).open(mode)
    def put(self,source,path,callback,confirm=True):self.path(path).write_bytes(Path(source).read_bytes());callback(1,1)
    def rename(self,source,target):self.path(source).rename(self.path(target))
    def remove(self,path):self.path(path).unlink()

class Transports(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='sync transport ');self.root=Path(self.temp.name)
        self.a=Store(self.root/'a/JPG',self.root/'a/TXT');self.b=Store(self.root/'b/JPG',self.root/'b/TXT')
        self.server=self.root/'server';self.server.mkdir()
        atomic(self.a.data/'box.json','[{"device":"one","comment":"Grüße 東京"}]')
    def tearDown(self):self.temp.cleanup()
    def assert_received(self):
        self.b.apply();records=json.loads((self.b.data/'box.json').read_text());self.assertEqual(records[0]['comment'],'Grüße 東京')
    def media(self):
        self.a.reader=lambda path:next((dict(v[0]['record']) for v in self.a.heads().values() if v[0]['kind']=='media' and v[0]['action']=='put'),{'device':'photo','comment':''})
        path=self.a.images/'SCHRAUBEN/Grüße photo.jpg'
        atomic(path,b'first image')
        return path

    def test_sftp_publishes_named_media_updates_moves_and_deletes(self):
        source=self.media();sftp=FileSftp(self.server);remote='data/TXT/.chaosbox-sync'
        sync=lambda:sync_transport.sftp_exchange(self.a,sftp,remote,progress=lambda _:None)
        sync();target=self.server/'data/JPG/SCHRAUBEN/Grüße photo.jpg'
        self.assertEqual(target.read_bytes(),b'first image')
        source.write_bytes(b'updated image');sync();self.assertEqual(target.read_bytes(),b'updated image')
        # Preserve object identity when changing the path.
        heads=self.a.heads();op=next(v[0] for v in heads.values() if v[0]['kind']=='media')
        moved=self.a.images/'OTHER/new.jpg';atomic(moved,source.read_bytes());source.unlink()
        sync();self.assertFalse(target.exists());target=self.server/'data/JPG/OTHER/new.jpg'
        self.assertEqual(target.read_bytes(),b'updated image')
        unrelated=self.server/'data/JPG/keep.jpg';unrelated.write_bytes(b'not synchronized')
        self.a.delete(moved);sync();self.assertFalse(target.exists());self.assertEqual(unrelated.read_bytes(),b'not synchronized')

    def test_sftp_preserves_remote_edits_and_rejects_symlink_parent(self):
        source=self.media();sftp=FileSftp(self.server);remote='data/TXT/.chaosbox-sync'
        sync=lambda:sync_transport.sftp_exchange(self.a,sftp,remote,progress=lambda _:None)
        sync();target=self.server/'data/JPG/SCHRAUBEN/Grüße photo.jpg';target.write_bytes(b'remote edit')
        source.write_bytes(b'local edit')
        with self.assertRaisesRegex(ValueError,'Untracked remote JPG'):sync()
        self.assertEqual(target.read_bytes(),b'remote edit')
        target.unlink();target.parent.rmdir();outside=self.root/'outside';outside.mkdir()
        target.parent.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'Unsafe remote directory'):sync()
        self.assertEqual(list(outside.iterdir()),[])
        self.assertFalse((self.server/remote/'transfer.lock').exists())

    def test_conflict_keeps_existing_remote_view(self):
        source=self.media();sftp=FileSftp(self.server);remote='data/TXT/.chaosbox-sync'
        sync_transport.sftp_exchange(self.a,sftp,remote,progress=lambda _:None)
        op=next(v[0] for v in self.a.heads().values() if v[0]['kind']=='media')
        self.a.issue('media',op['path'],dict(op['record'],comment='A'),source,parents=[op['revision']])
        self.a.issue('media',op['path'],dict(op['record'],comment='B'),source,parents=[op['revision']])
        sync_transport.sftp_exchange(self.a,sftp,remote,progress=lambda _:None)
        self.assertEqual((self.server/'data/JPG/SCHRAUBEN/Grüße photo.jpg').read_bytes(),b'first image')
        self.assertTrue(any(len(v)>1 for v in self.a.heads().values()))

    def test_sftp_round_trip_deletion_and_remote_lock(self):
        sftp=FileSftp(self.server)
        sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        sync_transport.sftp_exchange(self.b,sftp,'shared',progress=lambda _:None);self.assert_received()
        self.a.delete(self.a.data/'box.json',0)
        sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        sync_transport.sftp_exchange(self.b,sftp,'shared',progress=lambda _:None);self.b.apply()
        self.assertEqual(json.loads((self.b.data/'box.json').read_text()),[])
        self.assertFalse((self.server/'shared/transfer.lock').exists())
        (self.server/'shared/transfer.lock').mkdir()
        with self.assertRaises(FileExistsError):sync_transport.sftp_exchange(self.a,sftp,'shared')
    def test_generic_server_failure_reports_existing_lock_without_removing_it(self):
        class GenericFailureSftp(FileSftp):
            def mkdir(self,path):
                if self.path(path).exists():raise OSError('Failure')
                super().mkdir(path)
            def rmdir(self,path):raise AssertionError('Must not remove another transfer lock')
        lock=self.server/'shared/transfer.lock';lock.mkdir(parents=True)
        with self.assertRaisesRegex(sync_transport.RemoteLockError,r'busy.*shared/transfer.lock'):
            sync_transport.sftp_exchange(self.a,GenericFailureSftp(self.server),'shared',progress=lambda _:None)
        self.assertTrue(lock.is_dir())
        self.assertFalse(self.a.operations())

    def test_corrupt_journal_does_not_replace_local_data(self):
        sftp=FileSftp(self.server);sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        source=next((self.server/'shared/ops').glob('*.op'));op=json.loads(source.read_text());op['parents']=['a'*64];source.write_text(canonical(op))
        with self.assertRaises(ValueError):sync_transport.sftp_exchange(self.b,sftp,'shared')
        self.assertFalse(self.b.operations());self.assertFalse((self.b.data/'box.json').exists())
    def test_real_rsync_and_ssh_tar_streams(self):
        source=self.media()
        binary=self.root/'bin';binary.mkdir();wrapper=binary/'ssh'
        wrapper.write_text('''#!/bin/sh
while [ "$#" -gt 0 ]; do case "$1" in local-test|tester@local-test) shift; break;; *) shift;; esac; done
if [ "$#" -eq 1 ]; then exec /bin/sh -c "$1"; else exec "$@"; fi
''');wrapper.chmod(0o755)
        with patch.dict(os.environ,PATH=str(binary)+':'+os.environ['PATH']):
            for transport in ('rsync','ssh'):
                remote=self.server/transport/'TXT'
                config=dict(host='local-test',user='tester',port='22',keyfile='unused',knownhosts='unused',datadestination=str(remote))
                sync_transport.shell_exchange(self.a,config,transport,threading.Event(),lambda _:None)
                self.b.reader=self.a.reader
                sync_transport.shell_exchange(self.b,config,transport,threading.Event(),lambda _:None);self.assert_received()
                target=remote.parent/'JPG/SCHRAUBEN/Grüße photo.jpg'
                self.assertEqual(target.read_bytes(),b'first image')
                op=next(v[0] for v in self.a.heads().values() if v[0]['kind']=='media')
                source.write_bytes(b'updated image')
                sync_transport.shell_exchange(self.a,config,transport,threading.Event(),lambda _:None)
                self.assertEqual(target.read_bytes(),b'updated image')
                self.a.delete(source)
                sync_transport.shell_exchange(self.a,config,transport,threading.Event(),lambda _:None)
                self.assertFalse(target.exists())
                atomic(source,b'first image')
                self.a=Store(self.root/transport/'JPG',self.root/transport/'TXT',reader=lambda p:{'device':'photo','comment':''})
                source=self.media()
                atomic(self.a.data/'box.json','[{"device":"one","comment":"Grüße 東京"}]')
                self.assertFalse((remote/'.chaosbox-sync/transfer.lock').exists())
    def test_cancellation_releases_remote_lock(self):
        sftp=FileSftp(self.server);sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        count=[0]
        def check():
            count[0]+=1
            if count[0]>3:raise InterruptedError('cancelled')
        with self.assertRaises(InterruptedError):sync_transport.sftp_exchange(self.b,sftp,'shared',check)
        self.assertFalse((self.server/'shared/transfer.lock').exists())
        self.assertFalse(self.b.operations())
    def test_sftp_reuses_verified_local_blobs_and_reports_progress(self):
        class TrackingSftp(FileSftp):
            def __init__(self,root):super().__init__(root);self.reads=[]
            def open(self,path,mode):
                if mode=='rb':self.reads.append(path)
                return super().open(path,mode)
        self.a.reader=lambda path:{'comment':'movie'}
        atomic(self.a.images/'clip.mp4',b'media bytes')
        sftp=TrackingSftp(self.server)
        sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        sftp.reads.clear();messages=[]
        sync_transport.sftp_exchange(self.a,sftp,'shared',progress=messages.append)
        self.assertFalse(any('/blobs/' in name for name in sftp.reads))
        self.assertFalse(any('/ops/' in name for name in sftp.reads))
        self.assertTrue(any('Reusing local media' in message for message in messages))
        self.assertTrue(any('Scanning local' in message for message in messages))
        # A corrupt local copy is not reused; immutable collisions remain errors.
        blob=next((self.a.root/'blobs').glob('*.blob'));blob.write_bytes(b'broken')
        sftp.reads.clear()
        with self.assertRaisesRegex(ValueError,'Immutable sync collision'):
            sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        self.assertTrue(any('/blobs/' in name for name in sftp.reads))
        self.assertEqual(blob.read_bytes(),b'broken')

    def test_parallel_readers_have_separate_sessions_and_keep_validation(self):
        # Each reader belongs to one worker; sharing a Paramiko session could deadlock.
        lock=threading.Lock();readers=[];active=[0,0]
        class Reader(FileSftp):
            def __init__(self,root):super().__init__(root);self.owner=threading.get_ident();self.closed=False
            def open(self,path,mode):
                assert threading.get_ident()==self.owner
                with lock:active[0]+=1;active[1]=max(active)
                try:time.sleep(.01);return super().open(path,mode)
                finally:
                    with lock:active[0]-=1
            def close(self):self.closed=True
        def factory():
            reader=Reader(self.server);readers.append(reader);return reader
        atomic(self.a.data/'box.json',json.dumps([dict(device=str(i),comment='Grüße 東京') for i in range(20)]))
        sync_transport.sftp_exchange(self.a,FileSftp(self.server),'shared',progress=lambda _:None)
        sync_transport.sftp_exchange(self.b,FileSftp(self.server),'shared',progress=lambda _:None,reader_factory=factory)
        self.assertGreater(active[1],1)
        self.assertLessEqual(len(readers),4)
        self.assertTrue(all(reader.closed for reader in readers))
        self.assertEqual(len(json.loads((self.b.data/'box.json').read_text())),20)
        # Hosts refusing additional channels still work through the primary session.
        (self.server/'shared/checksums.json').unlink()
        def unavailable():raise OSError('channel limit')
        sync_transport.sftp_exchange(self.b,FileSftp(self.server),'shared',progress=lambda _:None,reader_factory=unavailable)
        self.assertFalse((self.server/'shared/transfer.lock').exists())
        corrupt=next((self.server/'shared/ops').glob('*.op'))
        op=json.loads(corrupt.read_text());op['parents']=['a'*64];corrupt.write_text(canonical(op))
        receiver=Store(self.root/'fresh/JPG',self.root/'fresh/TXT')
        with self.assertRaises(ValueError):
            sync_transport.sftp_exchange(receiver,FileSftp(self.server),'shared',progress=lambda _:None,reader_factory=factory)
        self.assertFalse(receiver.operations())
        self.assertTrue(all(reader.closed for reader in readers))
        self.assertFalse((self.server/'shared/transfer.lock').exists())


    def test_parallel_uploads_are_bounded_and_failed_batch_is_resumable(self):
        lock=threading.Lock();writers=[];active=[0,0];fail=[False]
        class Writer(FileSftp):
            def __init__(self,root):super().__init__(root);self.owner=threading.get_ident();self.closed=False
            def put(self,source,path,callback,confirm=True):
                self.assert_owner()
                with lock:active[0]+=1;active[1]=max(active)
                try:
                    time.sleep(.01)
                    if fail[0]:raise OSError('upload interrupted')
                    return super().put(source,path,callback,confirm)
                finally:
                    with lock:active[0]-=1
            def assert_owner(self):assert threading.get_ident()==self.owner
            def rename(self,source,target):self.assert_owner();return super().rename(source,target)
            def close(self):self.closed=True
        def factory():
            writer=Writer(self.server);writers.append(writer);return writer
        atomic(self.a.data/'box.json',json.dumps([dict(device=str(i)) for i in range(32)]))
        fail[0]=True
        with self.assertRaisesRegex(OSError,'upload interrupted'):
            sync_transport.sftp_exchange(self.a,FileSftp(self.server),'shared',progress=lambda _:None,reader_factory=factory)
        self.assertFalse((self.server/'shared/checksums.json').exists())
        self.assertFalse((self.server/'shared/transfer.lock').exists())
        self.assertTrue(all(writer.closed for writer in writers))
        fail[0]=False;writers.clear()
        sync_transport.sftp_exchange(self.a,FileSftp(self.server),'shared',progress=lambda _:None,reader_factory=factory)
        self.assertGreater(active[1],1);self.assertLessEqual(len(writers),4)
        self.assertTrue(all(writer.closed for writer in writers))
        self.assertEqual(len(self.a.operations()),len(list((self.server/'shared/ops').glob('*.op'))))
        # A server that refuses worker channels must still support first upload.
        atomic(self.a.data/'box.json',json.dumps([dict(device=str(i),comment='changed') for i in range(32)]))
        def unavailable():raise OSError('channel limit')
        sync_transport.sftp_exchange(self.a,FileSftp(self.server),'shared',progress=lambda _:None,reader_factory=unavailable)
        self.assertFalse((self.server/'shared/transfer.lock').exists())

    def test_checksum_index_is_equal_and_transfers_only_missing_operations(self):
        class Reads(FileSftp):
            def __init__(self,root):super().__init__(root);self.reads=[]
            def open(self,path,mode):
                if mode=='rb':self.reads.append(path)
                return super().open(path,mode)
        sftp=Reads(self.server)
        sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        first=json.loads((self.server/'shared/checksums.json').read_text())
        self.assertEqual(first,json.loads((self.a.root/'checksums.json').read_text()))
        sftp.reads.clear()
        sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        self.assertFalse(any('/ops/' in path or '/blobs/' in path for path in sftp.reads))
        self.assertEqual(first['checksum'],json.loads((self.server/'shared/checksums.json').read_text())['checksum'])
        sync_transport.sftp_exchange(self.b,sftp,'shared',progress=lambda _:None)
        record=json.loads((self.b.data/'box.json').read_text());record[0]['comment']='changed'
        atomic(self.b.data/'box.json',canonical(record));self.b.capture()
        self.assertNotEqual(first['checksum'],self.b.checksums()['checksum'])
        sync_transport.sftp_exchange(self.b,sftp,'shared',progress=lambda _:None)
        before=set(self.a.operations());sftp.reads.clear()
        sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        downloaded=[Path(path).stem for path in sftp.reads if '/ops/' in path]
        self.assertTrue(downloaded)
        self.assertFalse(set(downloaded)&before)
        self.assertEqual(json.loads((self.a.data/'box.json').read_text())[0]['comment'],'changed')
        self.assertEqual(json.loads((self.server/'shared/checksums.json').read_text())['checksum'],self.a.checksums()['checksum'])

    def test_bad_or_stale_checksum_index_falls_back_to_validation(self):
        sftp=FileSftp(self.server)
        sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        index=self.server/'shared/checksums.json';index.write_text('{broken')
        sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        self.assertTrue(sync_transport.valid_manifest(json.loads(index.read_text())))
        op=next((self.server/'shared/ops').glob('*.op'))
        value=json.loads(op.read_text());value['parents']=['a'*64];op.write_text(canonical(value))
        with self.assertRaises(ValueError):sync_transport.sftp_exchange(self.a,sftp,'shared',progress=lambda _:None)
        self.assertFalse((self.server/'shared/transfer.lock').exists())

    def test_failed_upload_does_not_publish_new_checksum(self):
        sync_transport.sftp_exchange(self.a,FileSftp(self.server),'shared',progress=lambda _:None)
        old=(self.server/'shared/checksums.json').read_bytes()
        records=json.loads((self.a.data/'box.json').read_text());records[0]['comment']='not uploaded'
        atomic(self.a.data/'box.json',canonical(records))
        class BrokenUpload(FileSftp):
            def rename(self,source,target):
                if target.endswith('.op'):raise OSError('transfer interrupted')
                super().rename(source,target)
        with self.assertRaises(OSError):sync_transport.sftp_exchange(self.a,BrokenUpload(self.server),'shared',progress=lambda _:None)
        self.assertEqual((self.server/'shared/checksums.json').read_bytes(),old)
        self.assertNotEqual(self.a.checksums()['checksum'],json.loads(old)['checksum'])

    def test_sftp_setup_timeout_and_cancel_interrupt_blocked_subsystem(self):
        import paramiko
        config=dict(host='local-test',port=22,user='tester',keyfile='key',knownhosts='hosts')
        class BlockedClient:
            def __init__(self):self.closed=threading.Event()
            def load_host_keys(self,path):pass
            def set_missing_host_key_policy(self,policy):pass
            def connect(self,*args,**kwargs):pass
            def open_sftp(self):
                self.closed.wait(2)
                raise EOFError('connection closed')
            def close(self):self.closed.set()
        for cancelled in (False,True):
            with self.subTest(cancelled=cancelled):
                cancel=threading.Event();client=BlockedClient();messages=[]
                timer=threading.Timer(.1,cancel.set) if cancelled else None
                if timer:timer.start()
                started=time.monotonic()
                try:
                    with patch.object(paramiko,'SSHClient',return_value=client):
                        with self.assertRaises(InterruptedError if cancelled else TimeoutError):
                            with sync_transport.sftp_connection(config,cancel,messages.append,
                                                                setup_timeout=2 if cancelled else .1):
                                self.fail('Blocked SFTP setup unexpectedly succeeded')
                finally:
                    if timer:timer.cancel();timer.join()
                self.assertLess(time.monotonic()-started,1)
                self.assertTrue(client.closed.is_set())
                self.assertTrue(any('Opening SFTP channel' in line for line in messages))

    def test_capture_and_apply_can_be_cancelled_between_files(self):
        def cancel():raise InterruptedError('cancelled')
        with self.assertRaises(InterruptedError):self.a.capture(cancel)
        self.assertFalse(self.a.operations())
        self.a.capture()
        with self.assertRaises(InterruptedError):self.a.apply(cancel)
        self.a.apply()

if __name__=='__main__':unittest.main()
