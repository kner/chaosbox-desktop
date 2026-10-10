"""Markdown source preservation, shared styles and lossless poster wrapping."""
from pathlib import Path
import sys
import unittest

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import markdown_render as md


class MarkdownTests(unittest.TestCase):
    def test_blocks_nested_lists_and_combined_styles(self):
        blocks = md.parse('# Heading\n\nA **bold and *italic*** text.\n\n'
                          '3. first\n   - child\n4. second\n\n> quote\n\n```py\n  x = "**raw**"\n```\n\n---')
        self.assertEqual(blocks[0].heading, 1)
        self.assertTrue(any(span.bold and span.italic and span.text == 'italic' for span in blocks[1].spans))
        self.assertEqual([block.prefix for block in blocks if block.prefix], ['3.', '•', '4.'])
        self.assertEqual(next(block for block in blocks if block.prefix == '•').indent, 1)
        self.assertTrue(blocks[-3].quote)
        self.assertEqual(blocks[-2].spans[0].text, '  x = "**raw**"')
        self.assertTrue(blocks[-2].code)
        self.assertTrue(blocks[-1].rule)

    def test_escaping_links_images_and_literal_html(self):
        source = r'\*literal\* &amp; [Label](https://example.org) ![Alt](https://example.org/photo.png) <b>HTML</b>'
        spans = md.parse(source)[0].spans
        self.assertEqual(''.join(span.text for span in spans), '*literal* & Label Alt <b>HTML</b>')
        self.assertEqual(next(span.link for span in spans if span.link), 'https://example.org')
        self.assertFalse(any(span.bold for span in spans))
        unsafe = md.parse('[Bad](javascript:alert(1))')
        self.assertFalse(any(span.link for block in unsafe for span in block.spans))

    def test_line_breaks_and_inline_code_preserve_content(self):
        blocks = md.parse('line one\nline two  \nline three\n\n`a * b` ~~old~~')
        self.assertEqual(''.join(span.text for span in blocks[0].spans), 'line one\nline two\nline three')
        self.assertTrue(any(span.code and span.text == 'a * b' for span in blocks[1].spans))
        self.assertTrue(any(span.strike and span.text == 'old' for span in blocks[1].spans))

    def test_poster_wrap_retains_long_words_and_style_changes_inside_words(self):
        source = 'Grüße **bold***italic*' + 'verylongword' * 20
        document = md.layout(md.parse(source), 110, 15)
        self.assertEqual(''.join(document.plain_lines).replace(' ', ''),
                         'Grüßebolditalic' + 'verylongword' * 20)
        for line in document.lines:
            self.assertLessEqual(sum(run.width for run in line.runs) + line.indent, 110)
        self.assertTrue(any(run.span.bold for line in document.lines for run in line.runs))
        self.assertTrue(any(run.span.italic for line in document.lines for run in line.runs))

    def test_poster_fit_uses_styled_height_and_fails_instead_of_truncating(self):
        source = '# Large heading\n\n- **First** item\n- *Second* item\n\n```\n  x = 3\n```'
        documents, height = md.fit([source, 'short'], 160, 150, 22, 8)
        self.assertLessEqual(height, 150)
        self.assertEqual(height, max(document.height for document in documents))
        self.assertTrue(any(line.prefix == '•' for line in documents[0].lines))
        image = Image.new('RGB', (200, 190), 'white')
        md.draw_poster(ImageDraw.Draw(image), documents[0], 20, 20, 160)
        self.assertLess(image.getextrema()[0][0], 255)
        self.assertEqual(image.crop((0, 0, 19, 190)).getextrema(), ((255, 255),) * 3)
        self.assertEqual(image.crop((181, 0, 200, 190)).getextrema(), ((255, 255),) * 3)
        with self.assertRaisesRegex(ValueError, 'does not fit'):
            md.fit([source * 100], 40, 20, 12, 8)

    def test_empty_markdown_needs_no_caption_space(self):
        documents, height = md.fit(['', ' \n\n'], 100, 20, 12, 8)
        self.assertEqual(height, 0)
        self.assertTrue(all(document.lines == [] for document in documents))


if __name__ == '__main__':
    unittest.main()
