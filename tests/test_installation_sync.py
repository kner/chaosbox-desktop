import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core
class InstallationSyncTests(unittest.TestCase):
    def test_explicit_installation_and_external_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'desktop';root.mkdir()
            key=Path(folder)/'id_ed25519';hosts=Path(folder)/'known_hosts'
            settings=core.Settings(root)
            settings.path.write_text('[Box]\nTitel=Haushalt\n[SSH]\nInstallation=shared-123\n'
                                     f'KeyFile={key}\nKnownHosts={hosts}\n')
            settings.reload();settings.profile(settings.default)
            self.assertEqual(settings.ssh['installation'],'shared-123')
            self.assertEqual(settings.ssh['datadestination'],'l1/storage/installations/shared-123/profiles/haushalt/TXT')
            self.assertEqual(settings.ssh['keyfile'],str(key))
            self.assertEqual(settings.ssh['knownhosts'],str(hosts))
            self.assertNotEqual(settings.installation_id,'shared-123')
            settings.assert_current()

    def test_profile_overrides_and_relative_or_home_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=core.Settings(folder)
            settings.path.write_text('[Box]\nTitel=Haushalt\n[Box]\nTitel=BilderBox\n'
                                     '[SSH]\nInstallation=shared\nKeyFile=keys/private\n'
                                     'KnownHosts=~/.ssh/known_hosts\n'
                                     '[SSH.Haushalt]\nInstallation=household\n')
            settings.reload();settings.profile('Haushalt')
            self.assertEqual(settings.ssh['installation'],'household')
            self.assertEqual(settings.ssh['keyfile'],str(Path(folder)/'keys/private'))
            self.assertEqual(settings.ssh['knownhosts'],str((Path.home()/'.ssh/known_hosts').resolve()))
            settings.profile('BilderBox')
            self.assertEqual(settings.ssh['installation'],'shared')

    def test_invalid_remote_installations_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=core.Settings(folder)
            for identifier in ('..','../other','/absolute','a/b','a\\b','bad name'):
                settings.path.write_text('[Box]\nTitel=Parts\n[SSH]\nInstallation='+identifier+'\n')
                with self.assertRaisesRegex(ValueError,'Invalid SSH Installation'):
                    settings.reload()

    def test_directories_and_profiles_never_share_default_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            first=core.Settings(Path(folder)/'one/ChaosBox');first.ensure(default_box='ChaosBox')
            second=core.Settings(Path(folder)/'two/ChaosBox');second.root.mkdir(parents=True)
            second.path.write_bytes(first.path.read_bytes());second.reload()
            self.assertNotEqual(first.installation_id,second.installation_id)
            self.assertEqual(first.installation_id,core.Settings(first.root).installation_id)
            one=first.profile(first.default);two=second.profile(second.default)
            self.assertNotEqual(first.ssh['datadestination'],second.ssh['datadestination'])
            self.assertIn('/installations/'+second.installation_id+'/profiles/',second.ssh['datadestination'])
            targets=[]
            for p in first.profiles:
                first.profile(p.id);targets.append(first.ssh['datadestination'])
            self.assertEqual(len(targets),len(set(targets)))
    def test_old_defaults_and_copied_scoped_paths_are_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            s=core.Settings(folder);s.root.mkdir(exist_ok=True)
            for destination in ('chaosbox-data/TXT','chaosbox-data/installations/other/profiles/chaosbox/TXT'):
                s.path.write_text('[Box]\nTitel=ChaosBox\n[SSH]\nDataDestination='+destination+'\n')
                s.reload();s.profile(s.default)
                self.assertIn('/installations/'+s.installation_id+'/',s.ssh['datadestination'])
                self.assertEqual(s.ssh['shelldatadestination'],'/kunden/homepages/13/d983197478/htdocs/'+s.ssh['datadestination'])
    def test_custom_destinations_cannot_restore_old_data(self):
        with tempfile.TemporaryDirectory() as folder:
            s=core.Settings(folder);s.path.write_text('[Box]\nTitel=Parts\n[SSH]\nHost=custom.test\nDataDestination=/srv/old/TXT\n');s.reload();s.profile(s.default)
            self.assertIn('/installations/'+s.installation_id+'/',s.ssh['datadestination'])
    def test_delete_and_recreate_same_directory_has_fresh_namespace(self):
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'CBHaushalt'
            first=core.Settings(root);first.ensure(default_box='ChaosBox')
            old_id=first.installation_id;old_setup=first.path.read_bytes()
            first.profile(first.default);old_destination=first.ssh['datadestination']
            self.assertEqual(core.Settings(root).installation_id,old_id)
            shutil.rmtree(root)
            second=core.Settings(root);second.ensure(default_box='ChaosBox')
            self.assertNotEqual(second.installation_id,old_id)
            second.path.write_bytes(old_setup);second.reload();second.profile(second.default)
            self.assertNotEqual(second.ssh['datadestination'],old_destination)
            self.assertFalse(any(root.rglob('*.op')))
    def test_external_setup_and_state_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'CBHaushalt'
            for kwargs in ({'state_dir':Path(folder)/'outside'},{'setup':Path(folder)/'old.ini'}):
                with self.assertRaises(ValueError):core.Settings(root,**kwargs)
    def test_open_client_cannot_reconnect_after_directory_is_deleted(self):
        import shutil,threading
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'CBHaushalt';settings=core.Settings(root);settings.ensure(default_box='ChaosBox')
            profile=settings.profile(settings.default)
            shutil.rmtree(root)
            with self.assertRaisesRegex(ValueError,'removed or reset'):
                core.upload(settings,profile,threading.Event())
            self.assertFalse(root.exists())
            fresh=core.Settings(root);fresh.ensure(default_box='ChaosBox')
            with self.assertRaises(ValueError):settings.assert_current()
    def test_external_index_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'CBHaushalt';root.mkdir();outside=Path(folder)/'outside';outside.mkdir()
            (root/'.indices').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError):core.Settings(root)
    def test_sync_target_is_inside_website_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=core.Settings(folder);settings.ensure(default_box='ChaosBox')
            self.assertEqual(settings.ssh['datadestination'],'l1/storage/installations/'+settings.installation_id+'/profiles/chaosbox/TXT')
            self.assertTrue(settings.ssh['shelldatadestination'].endswith('/htdocs/'+settings.ssh['datadestination']))
            settings.path.write_text('[Box]\nTitel=Parts\n[SSH]\nWebInstallation=../outside\n')
            with self.assertRaises(ValueError):settings.reload()
