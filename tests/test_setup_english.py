from pathlib import Path
import tempfile
import unittest

import core


class EnglishSetupTests(unittest.TestCase):
    def test_translation_preserves_values_and_snippets(self):
        text = ('[App]\nFelder=Box,Quantity,Device,Alias,Category,Comment,Package\n'
                'Kategorie=Familie\n[Box]\nTitel=Bilder\n; Kategorie=Example\n'
                '[TextSnippets]\nTitel="Titel=unchanged\n[Box]\nKategorie=still text"\n')
        result = core.english_setup(text)
        self.assertIn('Fields=Box,Quantity', result)
        self.assertIn('Categories=Familie', result)
        self.assertIn('Title=Bilder', result)
        self.assertIn('; Categories=Example', result)
        self.assertIn('Titel="Titel=unchanged\n[Box]\nKategorie=still text"', result)
        self.assertEqual(core.english_setup(result), result)

    def test_save_and_category_edit_write_english_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'setup.ini').write_text('[App]\nKategorie=Inherited\n[Box]\nTitel=Photos\n')
            settings = core.Settings(root)
            settings.reload()
            settings.remember_category(settings.profiles[0], 'Travel')
            result = settings.path.read_text()
            self.assertIn('Title=Photos', result)
            self.assertIn('Categories=INHERITED, TRAVEL', result)
            self.assertNotIn('Titel=', result)
            self.assertNotIn('Kategorie=', result)
            self.assertEqual(settings.profiles[0].id, 'Photos')

    def test_windows_newlines_preserved(self):
        self.assertEqual(core.english_setup('[Box]\r\nTitel=Photos\r\n'),
                         '[Box]\r\nTitle=Photos\r\n')
