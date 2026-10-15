import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core
class InstallationSyncTests(unittest.TestCase):
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
    def test_explicit_custom_server_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            s=core.Settings(folder);s.path.write_text('[Box]\nTitel=Parts\n[SSH]\nHost=custom.test\nDataDestination=/srv/unique/TXT\n');s.reload();s.profile(s.default)
            self.assertEqual(s.ssh['datadestination'],'/srv/unique/TXT')
