"""ChaosBox Sync v1: immutable operations and content-addressed media, no clock ordering.
The journal lives in TXT/.chaosbox-sync (Android: configured JSON folder).
Transport must union ops/ and blobs/, never mirror-delete them.
"""
from pathlib import Path, PurePosixPath
from contextlib import contextmanager
import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime,timezone

SYNC_KEYS = {'id', 'revision', 'base_revision'}

RECORD_KEYS={'box','anzahl','device','alias','category','comment','package','count','pack','created','modified','id','revision'}

def records_from(value):
    if isinstance(value,list):records=value
    elif isinstance(value,dict):records=[value] if RECORD_KEYS.intersection(value) else [value[key] for key in sorted(value)]
    else:raise ValueError('Expected JSON records')
    if any(not isinstance(record,dict) for record in records):raise ValueError('Invalid JSON records')
    return records

def checksum_manifest(operations, inventory=None):
    entries={op['revision']+'.op':digest(canonical(op).encode()) for op in operations.values()}
    result=dict(schema=1,algorithm='sha256',checksum=digest(canonical(entries).encode()),ops=entries)
    if inventory is not None:result['inventory']=inventory
    return result


def valid_manifest(value):
    if not isinstance(value,dict) or value.get('schema')!=1 or value.get('algorithm')!='sha256':return False
    entries=value.get('ops')
    if not isinstance(entries,dict):return False
    for name,checksum in entries.items():
        if not isinstance(name,str) or not name.endswith('.op') or not 1<=len(name[:-3])<=64 or any(c not in '0123456789abcdef-' for c in name[:-3]):return False
        if not isinstance(checksum,str) or len(checksum)!=64 or any(c not in '0123456789abcdef' for c in checksum):return False
    return value.get('checksum')==digest(canonical(entries).encode())


def operation_label(op):
    """A human-readable object name; revisions and blob hashes are internal."""
    if op.get('kind')=='device':
        return 'Geräteabmeldung' if op.get('action')=='retire' else 'Gerätebestätigung'
    record=op.get('record') or {}
    text=lambda value:' '.join(str(value or '').split())
    box=text(record.get('box'))
    device=text(record.get('device') or record.get('alias'))
    path=PurePosixPath(op.get('path') or '')
    if not box and op.get('kind')=='record':box=path.stem
    label=('Box '+box) if box else ''
    if device:label+=(' – ' if label else '')+device
    if op.get('kind')=='media' and path.name:
        label+=(' ('+path.name+')') if label else path.name
    return text(label or path.name or 'Datensatz')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(data):
    if isinstance(data, Path):
        with data.open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()
    return hashlib.sha256(data).hexdigest()

def legacy_id(kind, path, position=0):
    return str(uuid.UUID(bytes=hashlib.md5(f'{kind}:{path}:{position}'.encode()).digest(), version=3))

def normalized(record):
    result=dict(record)
    result['anzahl']=max(0,int(result.get('count',result.get('anzahl',result.get('Anzahl',0))) or 0))
    result['package']=result.get('pack',result.get('package',''))
    result['comment']=result.get('comment',result.get('kommentar',result.get('Kommentar','')))
    if result['comment'] is None:result['comment']=''
    category=next((v for k,v in result.items() if k.casefold() in ('category','kategorie')),'')
    result['category']=(', '.join(map(str,category)) if isinstance(category,list) else str(category or '')).upper()
    for key in ('box','device','alias','created'):result.setdefault(key,'')
    for key in list(result):
        if key in ('count','pack','Anzahl','kommentar','Kommentar','modified') or (key!='category' and key.casefold() in ('category','kategorie')):result.pop(key)
    return result

def clean(record):
    return {k:v for k,v in normalized(record).items() if k not in SYNC_KEYS|{'path'}}

def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f'Symlink refused: {path}')
    temporary = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as out:
            if isinstance(data, Path):
                with data.open('rb') as source: shutil.copyfileobj(source,out)
            else: out.write(data if isinstance(data, bytes) else data.encode('utf-8'))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

class Store:
    def __init__(self, images, data, reader=None, writer=None):
        self.images, self.data = Path(images), Path(data)
        self.root = self.data / '.chaosbox-sync'
        self.reader, self.writer = reader, writer
        self.root.mkdir(parents=True, exist_ok=True)
        device = self.root / 'device'
        if not device.exists(): atomic(device, str(uuid.uuid4()))
        self.device = device.read_text().strip()

    @contextmanager
    def locked(self):
        lock = self.root / 'local.lock'
        try: lock.mkdir()
        except FileExistsError: raise ValueError(f'Synchronization already running; inspect stale lock manually: {lock}')
        try: yield self
        finally: lock.rmdir()

    def checksums(self):
        value=checksum_manifest(self.operations())
        content=canonical(value)+'\n'
        target=self.root/'checksums.json'
        if not target.exists() or target.read_text(encoding='utf-8')!=content:atomic(target,content)
        return value

    def operations(self):
        if getattr(self,"_capture_operations",None) is not None:return self._capture_operations
        result = {}
        for file in sorted((self.root / 'ops').glob('*.op')):
            op = json.loads(file.read_text(encoding='utf-8'))
            self.validate(op)
            if file.stem != op['revision']: raise ValueError('Operation filename mismatch')
            result[op['revision']] = op
        return result

    @staticmethod
    def validate(op):
        uuid.UUID(op['id'])
        if op.get('schema') != 1 or op.get('kind') not in ('record', 'media', 'device') or op.get('action') not in ('put', 'delete', 'ack', 'retire'):
            raise ValueError('Unsupported sync operation')
        for revision in [op['revision'], *op['parents']]:
            if not isinstance(revision, str) or not revision or len(revision)>64 or any(c not in '0123456789abcdef-' for c in revision):
                raise ValueError('Invalid revision')
        path = PurePosixPath(op['path'])
        if path.is_absolute() or not path.parts or any(p in ('', '.', '..') or p.startswith('.') for p in path.parts) or '\\' in op['path'] or any(ord(c)<32 for c in op['path']):
            raise ValueError('Unsafe sync path')
        if op['kind']=='device':
            if op['action'] not in ('ack','retire') or op['path']!='devices/'+op['id']+'.json' or not isinstance(op.get('seen'),list):raise ValueError('Invalid device acknowledgment')
            return
        if op['action'] not in ('put','delete'):raise ValueError('Invalid object action')
        allowed = ('.jpg', '.jpeg', '.mp4') if op['kind']=='media' else ('.json',)
        if path.suffix.lower() not in allowed: raise ValueError('Unsupported sync file')
        if op['action']=='put':
            if not isinstance(op['record'], dict) or op['record'].get('id')!=op['id'] or op['record'].get('revision')!=op['revision']:
                raise ValueError('Operation identity mismatch')
            if op['kind']=='media' and (len(op['blob'])!=64 or any(c not in '0123456789abcdef' for c in op['blob'])):
                raise ValueError('Invalid blob')

    def import_from(self, directory, check=lambda: None, progress=lambda _: None):
        directory=Path(directory)
        combined=self.operations()
        existing=set(combined)
        incoming=[]
        for file in (directory/'ops').glob('*.op'):
            check()
            if file.is_symlink():raise ValueError('Journal symlink')
            op=json.loads(file.read_text(encoding='utf-8'));self.validate(op)
            if file.stem!=op['revision']:raise ValueError('Operation filename mismatch')
            if op['revision'] in combined and canonical(combined[op['revision']])!=canonical(op):raise ValueError('Immutable operation collision')
            combined[op['revision']]=op
            if op['revision'] not in existing:incoming.append(op)
        self.heads(combined)
        verified=set()
        for op in combined.values():
            check()
            if op.get('blob') and op['blob'] not in verified:
                progress('Checking media payload: '+operation_label(op))
                source=directory/'blobs'/(op['blob']+'.blob')
                if not source.exists():source=self.root/'blobs'/(op['blob']+'.blob')
                if source.is_symlink() or not source.is_file() or digest(source)!=op['blob']:raise ValueError('Missing or corrupt payload: '+operation_label(op))
                local=self.root/'blobs'/(op['blob']+'.blob')
                if source!=local and local.exists() and (local.is_symlink() or digest(local)!=op['blob']):
                    raise ValueError('Immutable sync collision: '+local.name)
                verified.add(op['blob'])
        check()
        # Restore missing local blobs before publishing any newly received operations.
        for blob in verified:
            local=self.root/'blobs'/(blob+'.blob')
            if not local.exists():atomic(local,directory/'blobs'/(blob+'.blob'))
        # Publish complete validated history without cancellation between parents and children.
        for op in incoming:
            payload=None
            if op.get('blob'):
                payload=directory/'blobs'/(op['blob']+'.blob')
                if not payload.exists():payload=self.root/'blobs'/(op['blob']+'.blob')
            self.publish(op,payload)

    def heads(self, operations=None):
        operations = self.operations() if operations is None else operations
        referenced = set()
        for op in operations.values():
            for parent in op['parents']:
                if parent not in operations: raise ValueError(f'Incomplete journal: missing revision {parent}')
                if operations[parent]['id']!=op['id'] or operations[parent]['kind']!=op['kind']: raise ValueError('Cross-object parent')
                referenced.add(parent)
        remaining={revision:len(op['parents']) for revision,op in operations.items()}
        children={}
        for revision,op in operations.items():
            for parent in op['parents']:children.setdefault(parent,[]).append(revision)
        ready=[r for r,count in remaining.items() if count==0];processed=0
        while ready:
            revision=ready.pop();processed+=1
            for child in children.get(revision,[]):
                remaining[child]-=1
                if remaining[child]==0:ready.append(child)
        if processed!=len(operations):raise ValueError('Cyclic journal history')
        groups = {}
        for revision, op in operations.items():
            if revision not in referenced: groups.setdefault(op['id'], []).append(op)
        return groups

    def publish(self, op, payload=None):
        self.validate(op)
        if payload is not None:
            if digest(payload)!=op['blob']: raise ValueError('Blob digest mismatch')
            self.immutable(self.root / 'blobs' / (op['blob']+'.blob'), payload)
        self.immutable(self.root / 'ops' / (op['revision']+'.op'), canonical(op).encode())
        if getattr(self,"_capture_operations",None) is not None:self._capture_operations[op['revision']]=op

    @staticmethod
    def immutable(path, content):
        if path.exists():
            if (digest(path)!=digest(content) if isinstance(content,Path) else path.read_bytes()!=content): raise ValueError(f'Immutable sync collision: {path.name}')
        else: atomic(path, content)

    def issue(self, kind, relative, record, payload=None, deleted=False, parents=None, position=0, target=None):
        ident = record.get('id') or legacy_id(kind, relative, position)
        previous = record.get('revision', '')
        operations = self.operations()
        # Legacy migration is deterministic across devices and languages.
        baseline = not previous and not any(op['id']==ident for op in operations.values()) and not deleted
        seed = 'baseline:' + ident + ':' + canonical(clean(record)) + ':' + (digest(payload) if payload is not None else '')
        revision = digest(seed.encode()) if baseline else str(uuid.uuid4())
        if parents is None:
            parent=previous if previous in operations else record.get('base_revision','') if previous else ''
            ancestor=operations.get(parent)
            parents=[parent] if ancestor and ancestor['id']==ident and ancestor['kind']==kind else []
        updated = dict(record)
        updated.update(normalized(record))
        updated.update(id=ident,revision=revision)
        updated.pop('modified', None)
        updated.pop('base_revision',None)
        if len(parents)==1:updated['base_revision']=parents[0]
        elif not baseline:updated['base_revision']=''
        if payload is not None and not baseline and target is not None and self.writer is not None:
            self.writer(target, updated)
            payload = Path(target)
        op = dict(schema=1, id=ident, revision=revision, parents=parents, kind=kind,
                  path=relative, action='delete' if deleted else 'put', device_id='migration' if baseline else self.device,issued_at='' if baseline else datetime.now(timezone.utc).isoformat())
        if not deleted:
            op['record'] = updated
            if payload is not None: op['blob'] = digest(payload)
        self.publish(op, payload)
        return updated

    def sources(self, root, extensions):
        if not root.exists(): return []
        return sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink()
                      and not any(part.startswith('.') for part in p.relative_to(root).parts)
                      and p.suffix.lower() in extensions)

    def capture(self, check=lambda: None, progress=lambda _: None):
        self._capture_operations=self.operations()
        try:self._capture(check,progress)
        finally:self._capture_operations=None
        self.checksums()

    def _capture(self, check, progress):
        """Missing files NEVER imply deletion. Capture local edits before receiving history."""
        operations = self.operations()
        materialized = self.materialized()
        labels={(op['kind'],op['path']):operation_label(op) for op in operations.values()}
        progress('Scanning local changes …')
        for kind, root, extensions in [('record', self.data, {'.json'}), ('media', self.images, {'.jpg','.jpeg','.mp4'})]:
            sources=self.sources(root, extensions)
            for index,path in enumerate(sources,1):
                check()
                relative = path.relative_to(root).as_posix()
                label=labels.get((kind,relative),operation_label(dict(kind=kind,path=relative)))
                progress(f'Checking local {kind} {index}/{len(sources)}: {label}')
                if path.name in ('records.json','cb.json','desktop-index.json'): continue
                if kind=='media':
                    if self.reader is None: raise ValueError('Media reader required')
                    records = [self.reader(path)]
                    payload = path
                else:
                    raw=json.loads(path.read_text(encoding='utf-8-sig'))
                    records = records_from(raw)
                    if any(not isinstance(r,dict) for r in records): raise ValueError('Invalid JSON records')
                    payload = None
                payload_digest = digest(payload) if payload is not None else None
                changed = False
                for i, record in enumerate(records):
                    check()
                    ident=record.get('id') or legacy_id(kind,relative,i)
                    previous=operations.get(record.get('revision'))
                    candidates=[previous] if previous else [op for op in operations.values() if op['id']==ident and op['action']=='put']
                    same=next((op for op in candidates if op is not None and op['action']=='put' and clean(op['record'])==clean(record)
                               and op['path']==relative and (payload is None or (op.get('blob')==payload_digest or materialized.get(op['revision'])==payload_digest))),None)
                    if same:
                        if kind=='record' and not record.get('id'):
                            records[i]=dict(record,id=ident,revision=same['revision']); changed=True
                        continue
                    records[i]=self.issue(kind,relative,record,payload,position=i,target=path if kind=='media' else None)
                    changed |= kind=='record'
                    operations=self.operations()
                if changed: atomic(path,canonical(records)+'\n')

    def initialize_from_local(self, check=lambda: None, progress=lambda _: None):
        """Make the visible local files authoritative over all received journal heads."""
        snapshot = {}
        sources = []
        for kind, root, extensions in [('record', self.data, {'.json'}),
                                       ('media', self.images, {'.jpg', '.jpeg', '.mp4'})]:
            for path in self.sources(root, extensions):
                check()
                if path.name in ('records.json', 'cb.json', 'desktop-index.json'):
                    continue
                relative = path.relative_to(root).as_posix()
                records = (records_from(json.loads(path.read_text(encoding='utf-8-sig')))
                           if kind == 'record' else [self.reader(path)] if self.reader else None)
                if records is None:
                    raise ValueError('Media reader required')
                sources.append((kind, relative, path))
                for position, record in enumerate(records):
                    ident = record.get('id') or legacy_id(kind, relative, position)
                    uuid.UUID(ident)
                    if ident in snapshot:
                        raise ValueError('Duplicate local record identity: ' + relative)
                    canonical(clean(record))  # Validate before issuing any reset operations.
                    snapshot[ident] = (kind, relative, dict(record, id=ident))
        heads = self.heads()
        for ident, (kind, relative, record) in snapshot.items():
            if any(op['kind'] != kind for op in heads.get(ident, [])):
                raise ValueError('Record identity used for different kinds: ' + relative)
        backup = self.root / 'reset-backups' / str(uuid.uuid4())
        backup.mkdir(parents=True)
        progress('Saving reset recovery backup: ' + str(backup))
        if (self.root / 'ops').exists():
            shutil.copytree(self.root / 'ops', backup / 'ops')
        for kind, relative, path in sources:
            check()
            target = backup / kind / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        # Prepare the full reset before publishing: cancellation cannot leave half a reset.
        prepared = Store(self.images, backup / 'prepared')
        prepared.device = self.device
        prepared._capture_operations = self.operations().copy()
        # Resolve every known branch, including deletions, against the local snapshot.
        for ident, (kind, relative, record) in snapshot.items():
            check()
            payload = backup / kind / relative if kind == 'media' else None
            prepared.issue(kind, relative, record, payload,
                       parents=[op['revision'] for op in heads.get(ident, [])])
        for ident, versions in heads.items():
            check()
            if ident in snapshot or versions[0]['kind'] == 'device':
                continue
            op = versions[0]
            prepared.issue(op['kind'], op['path'], {'id': ident}, deleted=True,
                       parents=[version['revision'] for version in versions])
        check()
        for folder, suffix in [('blobs', '.blob'), ('ops', '.op')]:
            for source in (prepared.root / folder).glob('*' + suffix):
                self.immutable(self.root / folder / source.name, source)
        shutil.rmtree(backup / 'prepared')
        self.checksums()
        progress('Local files established as the synchronization baseline.')

    def embed_media(self,path):
        path=Path(path);relative=path.relative_to(self.images).as_posix()
        for ops in self.heads().values():
            if len(ops)!=1:continue
            op=ops[0]
            if op['kind']=='media' and op['path']==relative and op['action']=='put' and self.writer:
                self.writer(path,op['record']);self.remember(op,path);return

    def materialized(self):
        file=self.root/'materialized.json'
        return json.loads(file.read_text()) if file.exists() else {}

    def remember(self, op, path):
        cache=self.materialized();cache[op['revision']]=digest(path)
        atomic(self.root/'materialized.json',canonical(cache))

    def safe_path(self, kind, relative):
        root = self.images if kind=='media' else self.data
        path=root / relative
        if not path.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [path,*path.parents] if p!=root.parent):
            raise ValueError('Symlink or escaping materialization path')
        return path

    def apply(self, check=lambda: None, progress=lambda _: None):
        hashes={}
        def file_digest(path):
            info=path.stat()
            stamp=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
            if path not in hashes or hashes[path][0]!=stamp:
                value=digest(path)
                after=path.stat()
                if stamp!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    raise ValueError('Media changed during synchronization: '+path.name)
                hashes[path]=(stamp,value)
            return hashes[path][1]
        operations=self.operations()
        heads=self.heads(operations)
        history={}
        for old in operations.values():history.setdefault(old['id'],[]).append(old)
        materialized=self.materialized()
        conflicts={ident:ops for ident,ops in heads.items() if len(ops)>1 and ops[0]['kind']!='device'}
        atomic(self.root/'conflicts.json',canonical(conflicts)+'\n')
        destinations={}
        for ident,versions in heads.items():
            if len(versions)==1 and versions[0]['kind']=='media' and versions[0]['action']=='put':
                path=versions[0]['path']
                if path in destinations and destinations[path]!=ident:raise ValueError('Media filename conflict; rename one version before synchronization: '+path)
                destinations[path]=ident
        # A changed path is a move, not a second object. Remove only copies carrying this ID.
        progress('Applying synchronized changes …')
        for index,(ident, ops) in enumerate(heads.items(),1):
            check()
            if len(ops)!=1: continue
            op=ops[0]
            if op['kind']=='device':continue
            progress(f'Applying {index}/{len(heads)}: '+operation_label(op))
            historical=history[ident]
            for old in historical:
                if old['kind']!='media' or (op['action']=='put' and old['path']==op['path']): continue
                if old['path'] in destinations and destinations[old['path']] != ident: continue
                path=self.safe_path('media',old['path'])
                if path.exists():
                    # Only known versions can be removed automatically; edited bytes become a conflict at capture.
                    if file_digest(path) in ({h.get('blob') for h in historical} | {materialized.get(h['revision']) for h in historical}):
                        self.trash(path, ident)
            if op['kind']=='media' and op['action']=='put':
                blob=self.root/'blobs'/(op['blob']+'.blob')
                payload=blob
                if file_digest(payload)!=op['blob']: raise ValueError('Corrupt or incomplete media payload')
                target=self.safe_path('media',op['path'])
                if target.exists() and file_digest(target) not in {op['blob'],materialized.get(op['revision'])}:
                    known=({h.get('blob') for h in historical} | {materialized.get(h['revision']) for h in historical})
                    if file_digest(target) not in known: raise ValueError(f'Untracked file collision: {target}')
                    self.trash(target,ident)
                acceptable={op['blob'],materialized.get(op['revision'])}
                if not target.exists() or file_digest(target) not in acceptable: atomic(target,payload)
                if self.writer is not None and materialized.get(op['revision'])!=file_digest(target):
                    self.writer(target,op['record']);self.remember(op,target)
                    materialized[op['revision']]=self.materialized()[op['revision']]
        # Merge records individually; unrelated edits in the same JSON file survive.
        paths={op['path'] for op in operations.values() if op['kind']=='record'}
        protected={op['path'] for op in operations.values() if op['kind']=='record' and op['id'] in conflicts}
        for source in self.sources(self.data, {'.json'}):
            if source.name not in ('records.json','cb.json','desktop-index.json') and json.loads(source.read_text(encoding='utf-8-sig')) == []:
                paths.add(source.relative_to(self.data).as_posix())
        record_heads={}
        for ident,ops in sorted(heads.items()):
            op=ops[0]
            if ident not in conflicts and op['kind']=='record' and op['action']=='put':
                record_heads.setdefault(op['path'],[]).append(op['record'])
        for relative in paths:
            check()
            progress('Merging JSON: '+relative)
            path=self.safe_path('record',relative)
            existing=[]
            if path.exists():
                value=json.loads(path.read_text(encoding='utf-8-sig'))
                existing=records_from(value)
            keep=[]
            for i, record in enumerate(existing):
                ident=record.get('id') or legacy_id('record',relative,i)
                if ident not in heads or ident in conflicts: keep.append(record)
            keep.extend(record_heads.get(relative,[]))
            if not keep:
                if path.exists() and relative not in protected: self.trash(path,'records')
                continue
            if existing!=keep:
                if path.exists(): self.trash(path,'records')
                atomic(path,canonical(keep)+'\n')
        self.checksums()
        return conflicts

    def trash(self, path, ident):
        target=self.root/'trash'/str(uuid.uuid4())/path.name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.move(str(path),target)

    def delete(self, path, selected=None):
        path=Path(path)
        if path.is_symlink(): raise ValueError('Symbolic deletion target')
        if selected is not None:
            value=json.loads(path.read_text(encoding='utf-8-sig'))
            records=records_from(value)
            if not 0 <= selected < len(records): raise ValueError('The selected record no longer exists')
        self.capture()
        if selected is None:
            relative=path.relative_to(self.images).as_posix()
            candidates=[op for ops in self.heads().values() for op in ops if op['kind']=='media' and op['path']==relative]
            if not candidates: raise ValueError('Media is not registered')
            record=candidates[0]['record']
            self.issue('media',relative,record,deleted=True)
        else:
            records=json.loads(path.read_text())
            if not 0 <= selected < len(records): raise ValueError("The selected record no longer exists")
            record=records[selected]
            self.issue('record',path.relative_to(self.data).as_posix(),record,deleted=True)
        return self.apply()

    def acknowledge(self):
        operations=self.operations();heads=self.heads(operations)
        seen=sorted(revision for revision,op in operations.items() if op['kind']!='device')
        previous=heads.get(self.device,[])
        if any(op['action']=='retire' for op in previous):raise ValueError('This device was retired. Register a new device identity before synchronizing.')
        if any(op.get('seen')==seen for op in previous):return
        self.device_event(self.device,'ack',seen)

    def device_event(self,ident,action,seen=None):
        uuid.UUID(ident)
        previous=self.heads().get(ident,[])
        if action=='retire' and (not previous or previous[0]['kind']!='device'):raise ValueError('Unknown device')
        op=dict(schema=1,id=ident,revision=str(uuid.uuid4()),parents=[p['revision'] for p in previous],kind='device',
                path='devices/'+ident+'.json',action=action,device_id=self.device,seen=seen or [],issued_at=datetime.now(timezone.utc).isoformat())
        self.publish(op)

    def delete_box(self,path):
        self.capture()
        path=Path(path)
        records=json.loads(path.read_text(encoding='utf-8-sig'))
        relative=path.relative_to(self.data).as_posix()
        for record in records:self.issue('record',relative,record,deleted=True)
        return self.apply()

    def restore(self,ident,revision):
        self.capture()
        operation=self.operations().get(revision)
        if operation is None or operation['id']!=ident or operation['action']!='put':raise ValueError('Choose a historical put revision')
        payload=self.root/'blobs'/(operation['blob']+'.blob') if operation.get('blob') else None
        parents=[op['revision'] for op in self.heads().get(ident,[])]
        self.issue(operation['kind'],operation['path'],operation['record'],payload,parents=parents)
        return self.apply()

    def resolve(self, ident, revision):
        self.capture()
        heads=self.heads().get(ident,[])
        selected=next((op for op in heads if op['revision']==revision),None)
        if selected is None: raise ValueError('Choose an existing conflict revision')
        record=selected.get('record',dict(id=ident,revision=revision))
        payload=(self.root/'blobs'/(selected['blob']+'.blob')) if selected.get('blob') else None
        self.issue(selected['kind'],selected['path'],record,payload,selected['action']=='delete',[op['revision'] for op in heads])
        return self.apply()
