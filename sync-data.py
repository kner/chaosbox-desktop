#!/usr/bin/env python3
"""Synchronize a profile, inspect/resolve conflicts or delete into the shared trash."""
import argparse
import json
from pathlib import Path
import threading
import core
import chaos_sync
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--data',required=True,type=Path,help='Profile directory containing JPG and TXT')
p.add_argument('--install-dir',type=Path)
p.add_argument('--show-target',action='store_true',help='Show this installation ID and effective server destinations without syncing')
p.add_argument('--transport',choices=['ssh','rsync','sftp'])
p.add_argument('--import-journal',type=Path)
p.add_argument('--prepare-only',action='store_true')
p.add_argument('--apply-only',action='store_true')
p.add_argument('--conflicts',action='store_true')
p.add_argument('--devices',action='store_true')
p.add_argument('--retire',metavar='DEVICE_ID')
p.add_argument('--history',metavar='OBJECT_ID')
p.add_argument('--restore',nargs=2,metavar=('OBJECT_ID','REVISION'))
p.add_argument('--whole-box',action='store_true')
p.add_argument('--resolve',nargs=2,metavar=('OBJECT_ID','REVISION'))
p.add_argument('--delete',type=Path)
p.add_argument('--record',type=int)
p.add_argument('--exchange',type=Path,help='Merge with another local journal (offline transport/test)')
a=p.parse_args()
profile=core.Profile('','',a.data/'JPG',a.data/'TXT',a.data/'TXT',a.data/'.indices',[],[])
store=core.sync_store(profile)
with store.locked():
    if a.show_target:
        settings=core.Settings(a.install_dir or a.data.parent);settings.reload()
        configured=next(pr for pr in settings.profiles if pr.data.resolve()==(a.data/'TXT').resolve())
        settings.profile(configured.id)
        print(json.dumps({key:settings.ssh[key] for key in ('installation','datadestination','shelldatadestination')},indent=2))
    elif a.import_journal:
        store.import_from(a.import_journal)
        store.capture()
        print("Conflicts:",len(store.apply()))
        store.acknowledge()
    elif a.prepare_only:
        store.capture()
    elif a.apply_only:
        store.capture()
        conflicts=store.apply()
        store.acknowledge()
        print("Conflicts:",len(conflicts))
    elif a.conflicts:
        print(json.dumps({key:value for key,value in store.heads().items() if len(value)>1 and value[0]["kind"]!="device"},ensure_ascii=False,indent=2))
    elif a.devices:
        print(json.dumps({i:h for i,h in store.heads().items() if h[0]["kind"]=="device"},ensure_ascii=False,indent=2))
    elif a.retire:
        store.device_event(a.retire,"retire")
    elif a.history:
        print(json.dumps([op for op in store.operations().values() if op["id"]==a.history],ensure_ascii=False,indent=2))
    elif a.restore:
        store.restore(*a.restore)
    elif a.resolve:
        store.resolve(*a.resolve)
    elif a.delete:
        if a.whole_box:store.delete_box(a.delete)
        else:store.delete(a.delete,a.record)
    elif a.exchange:
        other=chaos_sync.Store(a.exchange/'JPG',a.exchange/'TXT')
        store.capture()
        for folder,suffix in [('blobs','.blob'),('ops','.op')]:
            for first,second in [(store,other),(other,store)]:
                for source in (first.root/folder).glob('*'+suffix): second.immutable(second.root/folder/source.name,source)
        print('Conflicts:',len(store.apply()))
    else:
        settings=core.Settings(a.install_dir or a.data.parent)
        settings.reload()
        profile=next((pr for pr in settings.profiles if pr.data.resolve()==(a.data/'TXT').resolve()),None)
        if profile is None: raise ValueError('Profile is not configured in setup.ini')
        settings.profile(profile.id)
        if a.transport: settings.ssh['transport']=a.transport
        # release local lock; public upload acquires the same lock
        action=lambda:core.upload(settings,profile,threading.Event(),print)
if 'action' in locals(): action()
