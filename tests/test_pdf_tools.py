"""PDF discovery must not turn executable locations into broad read grants."""
from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manuscript as app


class PDFToolDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="pdf-discovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.runtime = self.home / ".cache/codex-runtimes/codex-primary-runtime/dependencies"
        self.poppler = self.runtime / "native/poppler"
        self.system = self.root / "system"
        for patcher in (patch.object(Path, "home", return_value=self.home),
                        patch.object(app, "SYSTEM_READ_ROOTS", (str(self.system),))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def discover(self, paths=None):
        errors = io.StringIO()
        with patch.object(app.shutil, "which", side_effect=(paths or {}).get), redirect_stderr(errors):
            tools, roots = app.pdf_tools(self.root / "control")
        return tools, roots, errors.getvalue()

    def test_home_bin_tools_never_grant_home_access(self):
        for name in ("pdftotext", "pdftoppm", "pdfinfo"):
            with self.subTest(name=name):
                executable = self.executable(self.home / "bin" / name)
                tools, roots, errors = self.discover({name: str(executable)})
                self.assertEqual(tools, {"pdf_text_command": []})
                self.assertEqual(roots, [])
                self.assertIn("Ignoring unsupported PDF tool", errors)

    def test_system_tools_and_links_to_them_need_no_extra_grants(self):
        target = self.executable(self.system / "Cellar/poppler/1/bin/pdftotext")
        for entry in (self.system / "bin/pdftotext", self.home / "bin/pdftotext"):
            with self.subTest(entry=entry):
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.symlink_to(target)
                tools, roots, errors = self.discover({"pdftotext": str(entry)})
                self.assertEqual(tools["pdf_text_command"], [str(target)])
                self.assertEqual(roots, [])
                self.assertEqual(errors, "")

    def test_system_symlink_cannot_hide_a_home_tool(self):
        target = self.executable(self.home / "bin/pdftotext")
        link = self.system / "bin/pdftotext"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        tools, roots, _ = self.discover({"pdftotext": str(link)})
        self.assertEqual(tools, {"pdf_text_command": []})
        self.assertEqual(roots, [])

    def test_runtime_wrappers_use_known_poppler_entrypoints(self):
        paths = {}
        for name in ("pdftotext", "pdftoppm", "pdfinfo"):
            self.executable(self.poppler / "bin" / name)
            paths[name] = str(self.executable(self.runtime / "bin/override" / name))
        tools, roots, errors = self.discover(paths)
        self.assertEqual(roots, [self.poppler])
        self.assertEqual(errors, "")
        for name in paths:
            self.assertEqual(tools[name], str(self.poppler / "bin" / name))
        self.assertEqual(tools["pdf_text_command"], [tools["pdftotext"]])

    def test_unknown_path_does_not_block_bundled_python_text_fallback(self):
        custom = self.executable(self.home / "bin/pdftotext")
        python_root = self.runtime / "python"
        python = self.executable(python_root / "bin/python3")
        package = python_root / "lib/python3.12/site-packages/pdfplumber/__init__.py"
        package.parent.mkdir(parents=True)
        package.touch()
        tools, roots, _ = self.discover({"pdftotext": str(custom)})
        self.assertEqual(tools["pdf_text_command"],
                         [str(python), "-B", str(self.root / "control/pdf_text.py")])
        self.assertEqual(roots, [python_root])

    def test_runtime_directory_symlinks_cannot_grant_home_access(self):
        self.executable(self.home / "bin/pdftotext")
        self.executable(self.home / "bin/python3")
        package = self.home / "lib/python3.12/site-packages/pdfplumber/__init__.py"
        package.parent.mkdir(parents=True)
        package.touch()
        self.poppler.parent.mkdir(parents=True)
        self.poppler.symlink_to(self.home, target_is_directory=True)
        (self.runtime / "python").symlink_to(self.home, target_is_directory=True)
        tools, roots, _ = self.discover()
        self.assertEqual(tools, {"pdf_text_command": []})
        self.assertEqual(roots, [])


if __name__ == "__main__":
    unittest.main()
