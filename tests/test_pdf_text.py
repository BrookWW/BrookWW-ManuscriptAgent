import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdf_text import PDFTextError, extract, main


class PDFTextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "article.pdf"
        self.source.write_bytes(b"%PDF-1.4 mocked parser input")
        self.pages = [Mock(), Mock(), Mock()]
        for page, content in zip(self.pages, ("First theorem", "第二个定理", None)):
            page.extract_text.return_value = content
        self.document = MagicMock()
        self.document.__enter__.return_value.pages = self.pages
        self.parser = Mock(return_value=self.document)
        module = SimpleNamespace(open=self.parser)
        patcher = patch.dict(sys.modules, {"pdfplumber": module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plain_text_preserves_unicode_empty_pages_and_page_breaks(self):
        self.assertEqual(extract(self.source), "First theorem\n\f\n第二个定理\n\f\n\n".encode())
        for page in self.pages:
            page.extract_text.assert_called_once_with(layout=False, x_tolerance=2, y_tolerance=3)
            page.close.assert_called_once_with()

    def test_page_range_is_inclusive_and_clamped_to_document(self):
        self.assertEqual(extract(self.source, first=2, last=8, page_breaks=False), "第二个定理\n\n".encode())
        self.pages[0].extract_text.assert_not_called()
        self.pages[0].close.assert_not_called()
        self.pages[1].close.assert_called_once_with()
        self.pages[2].close.assert_called_once_with()

    def test_empty_and_invalid_page_ranges_are_rejected(self):
        for first, last in ((0, None), (4, None), (2, 1)):
            with self.subTest(first=first, last=last), self.assertRaisesRegex(PDFTextError, "page range"):
                extract(self.source, first=first, last=last)
        self.document.__enter__.return_value.pages = []
        with self.assertRaisesRegex(PDFTextError, "page count"):
            extract(self.source)
        for page in self.pages:
            page.extract_text.assert_not_called()

    def test_cli_layout_page_selection_and_default_output(self):
        result = main(["-layout", "-f", "2", "-l", "2", "-nopgbrk", str(self.source)])
        self.assertEqual(result, 0)
        self.assertEqual(self.source.with_suffix(".txt").read_text(), "第二个定理\n")
        self.pages[1].extract_text.assert_called_once_with(layout=True, x_tolerance=2, y_tolerance=3)
        self.pages[0].extract_text.assert_not_called()
        self.pages[2].extract_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
