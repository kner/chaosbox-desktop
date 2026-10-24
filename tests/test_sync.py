import unittest,tempfile,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from chaos_sync import Store,atomic,canonical

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.a=Store(self.root/'a/JPG',self.root/'a/TXT')
        self.b=Store(self.root/'b/JPG',self.root/'b/TXT')
        self.path=self.a.data/'box.json'
        atomic(self.path,'[{"box":"B","device":"one","comment":"Grüße 東京"},{"box":"B","device":"two"}]')
    def tearDown(self): self.temp.cleanup()
    def exchange(self):
        for first,second in [(self.a,self.b),(self.b,self.a)]:
            for folder,suffix in [('blobs','.blob'),('ops','.op')]:
                for source in (first.root/folder).glob('*'+suffix): second.immutable(second.root/folder/source.name,source.read_bytes())
        self.a.apply();self.b.apply()
    def test_renamed_identity_does_not_remove_another_objects_current_path(self):
        first='00000000-0000-4000-8000-000000000001'
        second='00000000-0000-4000-8000-000000000002'
        payload=self.root/'payload.jpg';payload.write_bytes(b'same bytes')
        self.a.issue('media','shared.jpg',{'id':first},payload)
        record=self.a.issue('media','shared.jpg',{'id':second},payload)
        self.a.issue('media','renamed.jpg',record,payload,parents=[record['revision']])
        self.a.apply()
        self.assertEqual((self.a.images/'shared.jpg').read_bytes(),b'same bytes')
        self.assertEqual((self.a.images/'renamed.jpg').read_bytes(),b'same bytes')
        self.a.apply()
        self.assertTrue((self.a.images/'shared.jpg').exists())

    def test_null_comment_matches_empty_without_losing_literal_text(self):
        from chaos_sync import normalized
        for key in ('comment','kommentar','Kommentar'):
            for value in (None,'','null','Grüße 東京'):
                with self.subTest(key=key,value=value):
                    self.assertEqual(normalized({key:value})['comment'], '' if value is None else value)
        atomic(self.path,'[{"box":"B","comment":null}]')
        self.a.capture();self.exchange()
        before=self.a.operations()
        records=json.loads(self.path.read_text());records[0]['comment']=''
        atomic(self.path,canonical(records));self.a.capture();self.exchange()
        self.assertEqual(before,self.a.operations())
        self.assertFalse({k:v for k,v in self.a.heads().items() if len(v)>1})

    def test_local_initialization_replaces_received_history_and_resumes_sync(self):
        self.a.capture(); self.exchange()
        original = json.loads(self.path.read_text())
        remote = self.b.data / 'box.json'
        records = json.loads(remote.read_text())
        records[0]['comment'] = 'remote edit'
        atomic(remote, canonical(records)); self.b.capture()
        # Offline local organization removes one record and moves the other.
        original[0]['comment'] = 'local authoritative'
        self.path.unlink()
        moved = self.a.data / 'organized.json'
        atomic(moved, canonical(original[:1]))
        self.a.import_from(self.b.root)
        self.a.initialize_from_local(); self.a.apply(); self.exchange()
        self.assertFalse(any(len(v) > 1 for v in self.a.heads().values()))
        self.assertEqual(json.loads(moved.read_text())[0]['comment'], 'local authoritative')
        self.assertEqual(json.loads((self.b.data / 'organized.json').read_text()), json.loads(moved.read_text()))
        self.assertFalse(remote.exists())
        self.assertTrue(list((self.a.root / 'reset-backups').glob('*/record/organized.json')))
        before = self.a.operations(); self.a.capture()
        self.assertEqual(before, self.a.operations())
        records = json.loads(moved.read_text()); records[0]['comment'] = 'normal edit'
        atomic(moved, canonical(records)); self.a.capture(); self.exchange()
        self.assertEqual(json.loads((self.b.data / 'organized.json').read_text())[0]['comment'], 'normal edit')

    def test_cancelled_initialization_does_not_publish_partial_reset(self):
        self.a.capture()
        before = self.a.operations()
        def cancel_during_preparation():
            for folder in (self.a.root / 'reset-backups').glob('*/prepared/.chaosbox-sync/ops'):
                if list(folder.glob('*.op')):
                    raise InterruptedError('Cancelled')
        with self.assertRaises(InterruptedError):
            self.a.initialize_from_local(check=cancel_during_preparation)
        self.assertEqual(before, self.a.operations())

    def test_empty_initialization_deletes_known_records(self):
        self.a.capture(); self.exchange(); self.path.unlink()
        self.a.initialize_from_local(); self.a.apply(); self.exchange()
        self.assertTrue(all(v[0]['action'] == 'delete' for v in self.a.heads().values()))
        self.assertFalse((self.b.data / 'box.json').exists())

    def test_initialization_rejects_duplicate_ids_before_changing_history(self):
        self.a.capture()
        atomic(self.a.data / 'duplicate.json', self.path.read_text())
        before = self.a.operations()
        with self.assertRaisesRegex(ValueError, 'Duplicate local'):
            self.a.initialize_from_local()
        self.assertEqual(before, self.a.operations())

    def test_initial_idempotent(self):
        self.a.capture();self.exchange();before=self.a.operations();self.b.capture();self.exchange()
        self.assertEqual(before,self.b.operations());self.assertEqual(len(before),2)
        self.assertEqual(json.loads(self.path.read_text()),json.loads((self.b.data/'box.json').read_text()))
    def test_independent_records_merge(self):
        self.a.capture();self.exchange()
        for store,index,text in [(self.a,0,'A'),(self.b,1,'B')]:
            path=store.data/'box.json';records=json.loads(path.read_text());records[index]['comment']=text
            atomic(path,canonical(records));store.capture()
        self.exchange()
        self.assertFalse({k:v for k,v in self.a.heads().items() if len(v)>1})
        self.assertEqual({r['comment'] for r in json.loads(self.path.read_text())},{'A','B'})
    def test_offline_delete_vs_edit_retains_conflict(self):
        self.a.capture();self.exchange()
        self.a.delete(self.path,0)
        path=self.b.data/'box.json';records=json.loads(path.read_text());records[0]['comment']='offline edit'
        atomic(path,canonical(records));self.b.capture();self.exchange()
        conflicts={k:v for k,v in self.a.heads().items() if len(v)>1}
        self.assertEqual(len(conflicts),1)
        ident,heads=next(iter(conflicts.items()));chosen=next(h for h in heads if h['action']=='put')
        self.a.resolve(ident,chosen['revision']);self.exchange()
        self.assertEqual(len(self.a.heads()[ident]),1)
        self.assertIn('offline edit',self.path.read_text())
    def test_deleted_missing_file_does_not_resurrect(self):
        self.a.capture();self.exchange();self.a.delete(self.path,0);self.b.capture();self.exchange()
        self.b.capture();self.exchange()
        self.assertEqual(len(json.loads(self.path.read_text())),1)
        self.assertTrue(list((self.a.root/'trash').rglob('box.json')))
    def test_absence_is_not_delete(self):
        self.a.capture();self.exchange();self.path.unlink();self.a.capture();self.exchange()
        self.assertEqual(len(json.loads(self.path.read_text())),2)
    def test_traversal_rejected(self):
        self.a.capture();op=next(iter(self.a.operations().values()));op['path']='../outside.json'
        with self.assertRaises(ValueError):self.a.publish(op)
    def test_partial_history_rejected(self):
        self.a.capture();op=next(iter(self.a.operations().values()));op['parents']=['a'*64]
        atomic(self.a.root/'ops'/(op['revision']+'.op'),canonical(op))
        with self.assertRaises(ValueError):self.a.apply()
    def test_whole_box_delete_and_restore(self):
        self.a.capture();self.exchange();before=json.loads(self.path.read_text());self.a.delete_box(self.path);self.exchange()
        self.assertFalse(self.path.exists())
        old=next(op for op in self.a.operations().values() if op['id']==before[0]['id'] and op['action']=='put')
        self.a.restore(old['id'],old['revision']);self.exchange()
        self.assertEqual(len(json.loads(self.path.read_text())),1)
    def test_legacy_empty_files_are_trashed_but_reserved_files_stay(self):
        empty=self.a.data/'nested/empty.json';atomic(empty,' [ ] \n')
        reserved=self.a.data/'records.json';atomic(reserved,'[]')
        self.a.capture();before=self.a.operations();self.a.apply()
        self.assertFalse(empty.exists());self.assertTrue(reserved.exists())
        self.assertEqual(before,self.a.operations())
        self.assertTrue(list((self.a.root/'trash').rglob('empty.json')))

    def test_whole_box_stays_absent_after_stale_client_sync(self):
        self.a.capture();self.exchange();self.a.delete_box(self.path)
        self.b.capture();self.exchange();self.b.capture();self.exchange()
        self.assertFalse(self.path.exists());self.assertFalse((self.b.data/'box.json').exists())
        self.assertTrue(all(v[0]['action']=='delete' for v in self.a.heads().values()))

    def test_empty_conflict_path_is_preserved(self):
        self.a.capture();op=next(iter(self.a.operations().values()))
        self.a.issue('record','box.json',dict(op['record'],comment='A'),parents=[op['revision']])
        self.a.issue('record','box.json',op['record'],deleted=True,parents=[op['revision']])
        other=next(v[0] for i,v in self.a.heads().items() if i!=op['id'])
        self.a.issue('record','box.json',other['record'],deleted=True)
        atomic(self.path,'[]');self.a.apply()
        self.assertEqual(self.path.read_text(),'[]')

    def test_cycles_rejected(self):
        self.a.capture();operations=self.a.operations();revision,op=next(iter(operations.items()));op['parents']=[revision]
        atomic(self.a.root/'ops'/(revision+'.op'),canonical(op))
        with self.assertRaises(ValueError):self.a.apply()
    def test_device_acknowledgment_is_stable_and_retirement_is_explicit(self):
        self.a.capture();self.a.apply();self.a.acknowledge();before=len(self.a.operations());self.a.acknowledge()
        self.assertEqual(len(self.a.operations()),before)
        self.a.device_event(self.a.device,'retire')
        with self.assertRaises(ValueError):self.a.acknowledge()
        object_id=next(op['id'] for op in self.a.operations().values() if op['kind']=='record')
        with self.assertRaises(ValueError):self.a.device_event(object_id,'retire')
    def test_unknown_edit_base_remains_a_conflict(self):
        self.a.capture();self.exchange()
        records=json.loads(self.path.read_text());records[0].pop('revision');records[0]['comment']='lost metadata edit'
        atomic(self.path,canonical(records));self.a.capture()
        self.assertEqual(len([versions for versions in self.a.heads().values() if len(versions)>1]),1)
    def test_interrupted_metadata_publication_recovers_from_base_revision(self):
        import uuid
        self.a.capture();records=json.loads(self.path.read_text());old=records[0]['revision']
        records[0]['base_revision']=old;records[0]['revision']=str(uuid.uuid4());records[0]['comment']='recovered save'
        atomic(self.path,canonical(records));self.a.capture();self.a.apply()
        ident=records[0]['id'];self.assertEqual(len(self.a.heads()[ident]),1)
        self.assertEqual(self.a.heads()[ident][0]['record']['comment'],'recovered save')
    def test_partial_and_keyed_records_are_valid_and_deterministic(self):
        atomic(self.a.data/'category.json','{"category":"Werkzeug"}')
        atomic(self.a.data/'grouped.json','{"z":{"category":"Werkzeug"},"a":{"alias":"Reserve"}}')
        self.a.capture();self.exchange()
        self.assertEqual(json.loads((self.b.data/'category.json').read_text())[0]['category'],'WERKZEUG')
        self.assertIn('Reserve',{record['alias'] for record in json.loads((self.b.data/'grouped.json').read_text())})
if __name__=='__main__':unittest.main()
