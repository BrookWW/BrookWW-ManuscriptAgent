"""Delivery reports must point only to files that survived source publication."""
from pathlib import Path
import re
import sys
import tempfile
import unittest
from urllib.parse import quote, unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manuscript import format_outcome


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="outcome-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "session/manuscript"
        self.revision = self.root / "output with spaces/revisions/round-0001"
        self.revision.mkdir(parents=True)
        for name in ("main.tex", "refs.bib", "figures/图 (one).pdf"):
            path = self.revision / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("saved file")

    def test_only_existing_files_are_listed_and_linked(self):
        outcome = (f"[TeX]({self.work}/main.tex)\n"
                   "[Bib](refs.bib)\n[PDF](.audit-work/main.pdf)\n"
                   "[Notes](notes.txt)\n[Fake](../manuscript/main.tex)\n"
                   "[Web](https://example.org/paper.pdf)\n"
                   "[Port](https://example.org:443)\n"
                   "[Section](#conclusion)\nMathematical discussion is unchanged.")
        report = format_outcome(outcome, self.work, self.revision)
        manifest, notes = report.split("Model notes", 1)
        self.assertEqual(manifest.count("\n- "), 3)
        self.assertNotIn("main.pdf", manifest)
        self.assertIn("PDF (temporary file; not delivered)", notes)
        self.assertIn("Notes (temporary file; not delivered)", notes)
        self.assertIn("Fake (temporary file; not delivered)", notes)
        self.assertIn("[Web](https://example.org/paper.pdf)", notes)
        self.assertIn("[Port](https://example.org:443)", notes)
        self.assertIn("[Section](#conclusion)", notes)
        self.assertIn("Mathematical discussion is unchanged.", notes)
        for target in re.findall(r"\]\(<([^>]+)>\)", report):
            self.assertTrue(Path(unquote(target)).is_file(), target)

    def test_common_local_link_forms_and_line_numbers(self):
        main = self.work / "main.tex"
        aliases = (str(main), str(main).removeprefix("/private"), main.as_uri(),
                   "main.tex", "./main.tex")
        for target in aliases:
            with self.subTest(target=target):
                report = format_outcome(f"[Source](<{target}:12>)", self.work, self.revision)
                saved = quote(str(self.revision / "main.tex"), safe="/:")
                self.assertIn(f"[Source](<{saved}:12>)", report)
        image = self.work / "figures/图 (one).pdf"
        for target in (f"<{image}>", image.as_uri(), quote(str(image), safe="/")):
            with self.subTest(target=target):
                report = format_outcome(f"[Figure]({target})", self.work, self.revision)
                saved = quote(str(self.revision / "figures/图 (one).pdf"), safe="/:")
                self.assertIn(f"[Figure](<{saved}>)", report)
        report = format_outcome("[Source](main.tex#L12)", self.work, self.revision)
        self.assertIn("main.tex#L12>)", report)

    def test_missing_session_files_and_similar_path_prefixes_are_not_mapped(self):
        outside = self.root / "elsewhere/main.tex"
        outcome = (f"[Home]({self.work.parent}/home/check.txt)\n"
                   f"[Sibling]({self.work}-old/main.tex)\n"
                   f"[Outside]({outside})\n")
        report = format_outcome(outcome, self.work, self.revision)
        self.assertIn("Home (temporary file; not delivered)", report)
        self.assertIn("Sibling (temporary file; not delivered)", report)
        self.assertIn(f"[Outside]({outside})", report)

    def test_unpublished_round_does_not_claim_files_even_if_a_revision_directory_exists(self):
        report = format_outcome(f"[TeX]({self.work}/main.tex)", self.work)
        self.assertIn("No source revision confirmed", report)
        self.assertNotIn("Saved source files", report)
        self.assertNotIn(str(self.revision), report)
        self.assertIn("TeX (temporary file; not delivered)", report)


if __name__ == "__main__":
    unittest.main()
