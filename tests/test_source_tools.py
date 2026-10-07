import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from source_tools import SourceToolError, extract_source, fetch_source, generate_zbmath_bib, main, save_source
from safeio import safe_read


class SourceToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="source-tools-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.content = b"@article{x,title={Checked source},author={A. Author}}\n"
        self.record = {"source_id": "a" * 32, "status": "ok", "sha256": hashlib.sha256(self.content).hexdigest(),
                       "size_bytes": len(self.content), "path": ".audit-work/sources/" + "a" * 32 + ".bib",
                       "metadata_path": ".audit-work/sources/" + "a" * 32 + ".json"}

    def test_source_and_metadata_no_clobber_and_readonly_by_default(self):
        save_source(self.work, self.record, self.content)
        self.assertEqual(safe_read(self.work, self.record["path"]), self.content)
        self.assertEqual(json.loads(safe_read(self.work, self.record["metadata_path"])), self.record)
        self.assertEqual((self.work / self.record["path"]).stat().st_mode & 0o222, 0)
        with self.assertRaises(FileExistsError):
            save_source(self.work, self.record, self.content)

    def test_fixed_flat_paths_and_size_limits_are_enforced(self):
        for changes in ({"source_id": "../other"}, {"path": "/tmp/stolen"},
                        {"path": ".audit-work/sources/../report.md"},
                        {"metadata_path": "report.json"}):
            with self.subTest(changes=changes), self.assertRaises(SourceToolError):
                save_source(self.work, {**self.record, **changes}, self.content)
        with mock.patch("source_tools.MAX_SOURCE_BYTES", 3), self.assertRaises(SourceToolError):
            save_source(self.work, self.record, self.content)
        self.assertFalse((self.work / ".audit-work").exists())

    def test_automatic_metadata_is_not_a_model_alignment_gate(self):
        record = {**self.record, "sha256": "outdated", "size_bytes": 1}
        save_source(self.work, record, self.content)
        self.assertEqual(safe_read(self.work, record["path"]), self.content)

    def test_source_directory_symlink_and_hardlink_are_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.work / ".audit-work").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            save_source(self.work, self.record, self.content)
        self.assertEqual(list(outside.iterdir()), [])
        (self.work / ".audit-work").unlink()
        source = self.work / ".audit-work/sources"
        source.mkdir(parents=True)
        original = outside / "original"
        original.write_text("private")
        os.link(original, self.work / self.record["path"])
        with self.assertRaises(FileExistsError):
            save_source(self.work, self.record, self.content)
        self.assertEqual(original.read_text(), "private")

    def test_root_ancestor_symlink_is_rejected(self):
        link = self.root / "alias"
        link.symlink_to(self.work, target_is_directory=True)
        with self.assertRaises(OSError):
            save_source(link, self.record, self.content)

    def test_failed_fetch_is_metadata_only_and_cannot_claim_content(self):
        failed = {**self.record, "status": "error", "sha256": None, "size_bytes": 0, "path": None, "error": "HTTP 403"}
        save_source(self.work, failed, b"")
        self.assertEqual(json.loads(safe_read(self.work, failed["metadata_path"])), failed)
        self.assertEqual(len(list((self.work / ".audit-work/sources").iterdir())), 1)
        with self.assertRaises(SourceToolError):
            save_source(self.work, failed, self.content)

    @mock.patch("source_tools.http.client.HTTPConnection")
    def test_client_ignores_proxy_env_and_uses_exact_loopback_endpoint(self, factory):
        import base64
        record = {**self.record, "requested_url": "https://example.com/item", "thread_id": "c"}
        factory.return_value.getresponse.return_value.status = 200
        factory.return_value.getresponse.return_value.read.return_value = json.dumps({"record": record, "content_base64": base64.b64encode(self.content).decode()}).encode()
        config = {"source_fetch_url": "http://127.0.0.1:12345/fetch", "source_fetch_token": "a" * 43}
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://evil.com", "HTTPS_PROXY": "http://evil.com"}):
            actual, content = fetch_source(config, "https://example.com/item", "c")
        self.assertEqual(actual, record)
        self.assertEqual(content, self.content)
        factory.assert_called_once_with("127.0.0.1", 12345, timeout=60)

    @mock.patch("source_tools.http.client.HTTPConnection")
    def test_client_does_not_accept_external_endpoint_or_mismatched_result(self, factory):
        for endpoint in ("http://localhost:123/fetch", "http://example.com:123/fetch", "https://127.0.0.1:123/fetch", "http://127.0.0.1:123/fetch?path=/secret", "http://user@127.0.0.1:123/fetch"):
            with self.subTest(endpoint=endpoint), self.assertRaises(SourceToolError):
                fetch_source({"source_fetch_url": endpoint, "source_fetch_token": "a" * 43}, "https://example.com/item", "c")
        factory.assert_not_called()
        factory.return_value.getresponse.return_value.status = 200
        factory.return_value.getresponse.return_value.read.return_value = json.dumps({"record": {"thread_id": "someone-else", "requested_url": "https://example.com/other"}, "content_base64": ""}).encode()
        with self.assertRaises(SourceToolError):
            fetch_source({"source_fetch_url": "http://127.0.0.1:123/fetch", "source_fetch_token": "a" * 43}, "https://example.com/item", "c")

    def pdf_fixture(self, body=b"%PDF-1.4 synthetic fixture"):
        record = {**self.record, "path": self.record["path"].replace(".bib", ".pdf"),
                  "sha256": hashlib.sha256(body).hexdigest(), "size_bytes": len(body), "thread_id": "c"}
        save_source(self.work, record, body)
        self.temporary = self.root / "private-temp"
        self.temporary.mkdir()
        return record, {"work_root": str(self.work), "pdf_text_command": ["/sealed/pdftotext"], "build_timeout": 15}

    def test_extract_shares_source_across_agents_and_returns_text_without_receipt(self):
        record, config = self.pdf_fixture()
        content = b"Real article title\nTheorem 2.1. The checked conclusion.\n"
        def run(command, **kwargs):
            self.assertEqual(command[:7], ["/sealed/pdftotext", "-enc", "UTF-8", "-f", "2", "-l", "3"])
            self.assertEqual(Path(command[-2]).read_bytes(), b"%PDF-1.4 synthetic fixture")
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            self.assertEqual(kwargs["timeout"], 15)
            Path(command[-1]).write_bytes(content)
            return subprocess.CompletedProcess(command, 0)
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.temporary), "PYTHONPATH": "/untrusted/imports"}), \
                mock.patch("source_tools.subprocess.run", side_effect=run):
            value = extract_source(config, record["source_id"], first=2, last=3)
        self.assertEqual((value["first_page"], value["last_page"]), (2, 3))
        self.assertEqual(safe_read(self.work, value["text_path"]), content)
        self.assertEqual(set(value), {"source_id", "text_path", "first_page", "last_page"})
        self.assertFalse(list((self.work / ".audit-work/sources").glob("*-text.json")))
        self.assertEqual((self.work / value["text_path"]).stat().st_mode & 0o222, 0)
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_extract_rejects_unknown_paths_and_bad_ranges(self):
        record, config = self.pdf_fixture()
        with mock.patch("source_tools.subprocess.run") as parser:
            for identifier, first, last in (("../source.pdf", 1, None),
                    (record["source_id"], 0, None), (record["source_id"], 2, 1),
                    (record["source_id"], 1, 2001)):
                with self.subTest(identifier=identifier, first=first, last=last), self.assertRaises(SourceToolError):
                    extract_source(config, identifier, first=first, last=last)
            parser.assert_not_called()

    def test_extract_rejects_nonpdf_and_alias_before_parser(self):
        record, config = self.pdf_fixture()
        path = self.work / record["path"]
        path.chmod(0o600)
        path.write_bytes(b"not a PDF")
        with mock.patch("source_tools.subprocess.run") as parser, self.assertRaisesRegex(SourceToolError, "Expected a PDF"):
            extract_source(config, record["source_id"])
        parser.assert_not_called()
        path.unlink()
        path.symlink_to(self.root / "outside.pdf")
        with mock.patch("source_tools.subprocess.run") as parser, self.assertRaises(OSError):
            extract_source(config, record["source_id"])
        parser.assert_not_called()

    def test_extract_rejects_oversized_source_before_parser(self):
        record, config = self.pdf_fixture()
        with mock.patch("source_tools.MAX_SOURCE_BYTES", 5), \
                mock.patch("source_tools.subprocess.run") as parser, self.assertRaisesRegex(SourceToolError, "byte limit"):
            extract_source(config, record["source_id"])
        parser.assert_not_called()

    def test_extract_rejects_hardlinked_source_before_parser(self):
        record, config = self.pdf_fixture()
        os.link(self.work / record["path"], self.root / "alias.pdf")
        with mock.patch("source_tools.subprocess.run") as parser, self.assertRaises(SourceToolError):
            extract_source(config, record["source_id"])
        parser.assert_not_called()

    def test_extract_uses_snapshot_without_requiring_acquisition_receipt(self):
        record, config = self.pdf_fixture()
        (self.work / record["metadata_path"]).unlink()
        original = self.work / record["path"]
        original.chmod(0o600)
        original.write_bytes(b"%PDF-1.4 updated article")
        def run(command, **kwargs):
            self.assertEqual(Path(command[-2]).read_bytes(), b"%PDF-1.4 updated article")
            Path(command[-1]).write_text("Theorem 2.1")
            original.write_bytes(b"%PDF-1.4 next article revision")
            return subprocess.CompletedProcess(command, 0)
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.temporary)}), \
                mock.patch("source_tools.subprocess.run", side_effect=run):
            value = extract_source(config, record["source_id"])
        self.assertEqual(safe_read(self.work, value["text_path"]), b"Theorem 2.1")

    def test_missing_optional_pdf_tool_only_blocks_extraction(self):
        record, config = self.pdf_fixture()
        config["pdf_text_command"] = []
        with mock.patch("source_tools.subprocess.run") as parser, self.assertRaisesRegex(SourceToolError, "unavailable"):
            extract_source(config, record["source_id"])
        parser.assert_not_called()

    def test_extract_empty_oversized_and_timeout_outputs_fail_without_receipt(self):
        record, config = self.pdf_fixture()
        for content in (b"", b"x" * (4 * 1024 * 1024 + 1), b"\xffnot utf8"):
            def run(command, **kwargs):
                Path(command[-1]).write_bytes(content)
                return subprocess.CompletedProcess(command, 0)
            with self.subTest(length=len(content)), mock.patch.dict(os.environ, {"TMPDIR": str(self.temporary)}), \
                    mock.patch("source_tools.subprocess.run", side_effect=run), self.assertRaises(SourceToolError):
                extract_source(config, record["source_id"])
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.temporary)}), \
                mock.patch("source_tools.subprocess.run", side_effect=subprocess.TimeoutExpired("extractor", 1)), self.assertRaisesRegex(SourceToolError, "time limit"):
            extract_source(config, record["source_id"])
        self.assertFalse(list((self.work / ".audit-work/sources").glob("*-text.json")))

    @unittest.skipUnless(platform.system() == "Darwin" and os.environ.get("AUDITAGENT_SANDBOX_TESTS") == "1",
                         "requires an explicit real macOS sandbox test")
    def test_actual_pdf_extract_cli_inherits_strict_os_sandbox(self):
        from isolation import Seatbelt
        root = Path(__file__).resolve().parents[1]
        runtime = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
        if not runtime.is_file():
            self.skipTest("bundled PDF runtime unavailable")
        fixture = self.root / "fixture.pdf"
        generated = subprocess.run([str(runtime), "-I", "-B", "-c",
            "from reportlab.pdfgen import canvas; import sys; p=canvas.Canvas(sys.argv[1],invariant=1); "
            "p.drawString(72,750,'Verified article title. Theorem 1. First page.'); p.showPage(); "
            "p.drawString(72,750,'Theorem 2. Second page conclusion.'); p.save()", str(fixture)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        record, config = self.pdf_fixture(fixture.read_bytes())
        control = self.root / "control"
        control.mkdir()
        for name in ("source_tools.py", "safeio.py", "pdf_text.py"):
            shutil.copyfile(root / name, control / name)
        config["pdf_text_command"] = [str(runtime), "-B", str(control / "pdf_text.py")]
        (control / "round-config.json").write_text(json.dumps(config))
        archive = self.root / "archive"
        archive.mkdir()
        (archive / "canary").write_text("Denied synthetic history")
        policy = Seatbelt(read_roots=[self.work, self.temporary, control, Path(sys.prefix).resolve(),
                                     runtime.resolve().parent.parent],
                          write_roots=[self.work, self.temporary], deny_roots=[archive], deny_write_roots=[control])
        env = {"HOME": str(self.temporary), "TMPDIR": str(self.temporary)}
        result = subprocess.run(policy.wrap([sys.executable, "-B", str(control / "source_tools.py"),
            "extract", record["source_id"], "--first", "2", "--last", "2"]), cwd=self.work,
            env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("SOURCE_TEXT "), result.stdout)
        value = json.loads(result.stdout.removeprefix("SOURCE_TEXT "))
        text = safe_read(self.work, value["text_path"]).decode()
        self.assertIn("Theorem 2. Second page conclusion.", text)
        self.assertNotIn("First page", text)
        denied = subprocess.run(policy.wrap([sys.executable, "-I", "-B", "-c", "import pathlib; pathlib.Path(" + repr(str(archive / "canary")) + ").read_text()"]),
                                cwd=self.work, env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("PermissionError", denied.stderr)


class ZbMathSourceToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="zbmath-tools-test-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.config = {"work_root": str(self.work)}

    def read_with_config(self, root, relative, **kwargs):
        if relative == "round-config.json":
            return json.dumps(self.config).encode()
        return safe_read(root, relative, **kwargs)

    def api_fixture(self, *, documents=1, total=None, url="https://api.zbmath.org/v1/document/12345"):
        document = {"id": 12345, "database": "Zbl", "identifier": "1234.56789", "year": "2024",
                    "title": {"title": "A checked mathematical result"},
                    "contributors": {"authors": [{"name": "Example, Alice"}]},
                    "document_type": {"code": "j"}, "source": {"series": [{"title": "Example Journal"}]}}
        raw = json.dumps({"result": [dict(document, id=12345 + i) for i in range(documents)],
                          "status": {"execution_bool": True, "status_code": 200,
                                     "nr_total_results": documents if total is None else total}}).encode()
        record = {"source_id": "b" * 32, "status": "ok", "sha256": hashlib.sha256(raw).hexdigest(),
                  "size_bytes": len(raw), "path": ".audit-work/sources/" + "b" * 32 + ".txt",
                  "metadata_path": ".audit-work/sources/" + "b" * 32 + ".json", "thread_id": "c",
                  "requested_url": url, "final_url": url}
        return record, raw

    def test_bib_candidate_is_shared_across_agents_without_derivative_receipt(self):
        source, raw = self.api_fixture()
        save_source(self.work, source, raw)
        value = generate_zbmath_bib(self.config, source["source_id"], key="Example2024")
        self.assertEqual(value["document_id"], "12345")
        self.assertEqual(value["origin"], "api_generated")
        self.assertEqual(value["provider"], "zbmath_api")
        self.assertEqual(value["source_url"], source["final_url"])
        content = safe_read(self.work, value["bibtex_path"])
        self.assertIn(b"@article{Example2024", content)
        self.assertFalse(list((self.work / ".audit-work/sources").glob("*-bib.json")))
        self.assertEqual((self.work / value["bibtex_path"]).stat().st_mode & 0o222, 0)

    def test_bib_rejects_nonofficial_source_and_bad_path(self):
        source, raw = self.api_fixture()
        save_source(self.work, source, raw)
        with self.assertRaises(SourceToolError):
            generate_zbmath_bib(self.config, "../file", key="key")
        metadata = self.work / source["metadata_path"]
        metadata.chmod(0o600)
        metadata.write_text(json.dumps({**source, "final_url": "https://example.org/data.json"}))
        with self.assertRaisesRegex(SourceToolError, "official"):
            generate_zbmath_bib(self.config, source["source_id"], key="key")
        self.assertFalse(list((self.work / ".audit-work/sources").glob("*-bib.bib")))

    def test_bib_does_not_gate_on_acquisition_hash_or_owner(self):
        source, raw = self.api_fixture()
        save_source(self.work, {**source, "sha256": "old-digest", "size_bytes": 1}, raw)
        value = generate_zbmath_bib(self.config, source["source_id"], key="Example2024")
        self.assertIn(b"@article{Example2024", safe_read(self.work, value["bibtex_path"]))

    def test_multi_result_search_needs_explicit_document_selection(self):
        source, raw = self.api_fixture(documents=2, url="https://api.zbmath.org/v1/document/_search?search_string=estimate")
        save_source(self.work, source, raw)
        with self.assertRaisesRegex(SourceToolError, "Choose one"):
            generate_zbmath_bib(self.config, source["source_id"], key="key")
        result = generate_zbmath_bib(self.config, source["source_id"], key="key", document_id="12346")
        self.assertEqual(result["document_id"], "12346")
        with self.assertRaisesRegex(SourceToolError, "Choose one"):
            generate_zbmath_bib(self.config, source["source_id"], key="key", document_id="99999")

    def test_partial_search_page_cannot_silently_choose_one(self):
        source, raw = self.api_fixture(total=10, url="https://api.zbmath.org/v1/document/_search?search_string=estimate")
        save_source(self.work, source, raw)
        with self.assertRaisesRegex(SourceToolError, "Choose one"):
            generate_zbmath_bib(self.config, source["source_id"], key="key")

    def test_api_cli_works_without_native_thread_identity(self):
        source, raw = self.api_fixture()
        output = io.StringIO()
        with mock.patch("source_tools.safe_read", side_effect=self.read_with_config), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("source_tools.fetch_source", return_value=(source, raw)) as fetched, \
                mock.patch("sys.stdout", output):
            code = main(["zbmath-record", "12345"])
        self.assertEqual(code, 0)
        fetched.assert_called_once_with(self.config, source["requested_url"])
        lines = output.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("ZBMATH_RESULT "))
        self.assertEqual(json.loads(lines[0].split(" ", 1)[1])["status"], "single")
        self.assertEqual(json.loads(lines[-1].removeprefix("SOURCE_FETCH ")), source)

    def test_expected_remote_denial_is_typed_unavailability_not_crashed_tool(self):
        source, _ = self.api_fixture()
        source.update(status="error", path=None, sha256=None, size_bytes=0,
                      error_code="http_challenge", retryable=False)
        output = io.StringIO()
        with mock.patch("source_tools.safe_read", side_effect=self.read_with_config), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("source_tools.fetch_source", return_value=(source, b"")), \
                mock.patch("sys.stdout", output):
            code = main(["zbmath-search", "doi:10.1234/example"])
        self.assertEqual(code, 0)
        summary = json.loads(output.getvalue().splitlines()[0].split(" ", 1)[1])
        self.assertEqual(summary["status"], "retrieval_unavailable")
        self.assertEqual(summary["error_code"], "http_challenge")
        self.assertEqual(json.loads(safe_read(self.work, source["metadata_path"]))["status"], "error")

    def test_ambiguous_bib_cli_requests_selection_without_writing_candidate(self):
        source, raw = self.api_fixture(documents=2, url="https://api.zbmath.org/v1/document/_search?search_string=estimate")
        save_source(self.work, source, raw)
        output = io.StringIO()
        with mock.patch("source_tools.safe_read", side_effect=self.read_with_config), \
                mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "c"}), mock.patch("sys.stdout", output):
            code = main(["zbmath-bib", source["source_id"], "--key", "Example2024"])
        self.assertEqual(code, 0)
        self.assertNotIn("SOURCE_BIB", output.getvalue())
        self.assertEqual(json.loads(output.getvalue().removeprefix("ZBMATH_RESULT "))["status"], "selection_required")
        self.assertFalse(list((self.work / ".audit-work/sources").glob("*-bib.bib")))

    def test_malformed_local_key_and_unparseable_json_fail(self):
        source, raw = self.api_fixture()
        save_source(self.work, source, raw)
        for key in ("../unsafe", "key\n@article{evil"):
            with self.subTest(key=key), mock.patch("source_tools.safe_read", side_effect=self.read_with_config), \
                    mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "c"}), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(["zbmath-bib", source["source_id"], "--key", key]), 2)
        path = self.work / source["path"]
        path.chmod(0o600)
        path.write_bytes(b"not JSON")
        with mock.patch("source_tools.safe_read", side_effect=self.read_with_config), \
                mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "c"}), mock.patch("sys.stderr", io.StringIO()):
            with mock.patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(main(["zbmath-bib", source["source_id"], "--key", "Example2024"]), 0)
            self.assertIn("bibliography_unavailable", output.getvalue())


if __name__ == "__main__":
    unittest.main()
