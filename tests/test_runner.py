"""Outer-loop contracts; fake sessions are explicit and never claim OS proof."""
import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manuscript as app


class ProcessCleanupTests(unittest.TestCase):
    def test_denied_signal_to_finished_group_does_not_mask_cancellation(self):
        process = MagicMock(pid=123)
        process.poll.return_value = 0
        with patch.object(app.os, "killpg", side_effect=PermissionError("exited confined group")):
            app.kill_group(process)
        process.send_signal.assert_not_called()
        process.wait.assert_called_once_with(timeout=5)

    def test_live_child_receives_direct_signal_if_group_signal_is_denied(self):
        process = MagicMock(pid=123)
        process.poll.side_effect = [None, 0]
        with patch.object(app.os, "killpg", side_effect=PermissionError("group denied")), patch.object(app.time, "sleep"):
            app.kill_group(process)
        process.send_signal.assert_called_once_with(app.signal.SIGTERM)


class ProcessExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="thin-process-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def execute(self, script, *, live=True):
        with redirect_stdout(io.StringIO()):
            return app.execute([sys.executable, "-I", "-c", script], cwd=self.root,
                env=app.private_environment(self.root, self.root), timeout=10,
                log_dir=self.root / "logs", live=live)

    def test_full_logs_keep_tool_output_but_summary_keeps_only_turn_state_and_last_message(self):
        script = '''import json, sys
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Earlier message"}}))
for number in range(160):
    print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "x" * 8192}}))
sys.stderr.write('{"type":"turn.failed"}\\n' + "diagnostic" * 16000)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Last message"}}))
sys.stdout.write(json.dumps({"type": "turn.completed"}))
'''
        code, summary = self.execute(script)
        self.assertEqual(code, 0)
        self.assertEqual(summary, {"completed": True, "failed": False, "text": "Last message"})
        lines = (self.root / "logs/events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 163)
        commands = [json.loads(line) for line in lines[1:-2]]
        self.assertEqual(len(commands), 160)
        self.assertTrue(all(event["item"]["aggregated_output"] == "x" * 8192 for event in commands))
        self.assertEqual((self.root / "logs/stderr.log").read_text(),
                         '{"type":"turn.failed"}\n' + "diagnostic" * 16000)

    def test_final_message_without_newline_is_retained(self):
        code, summary = self.execute('''import json, sys
print(json.dumps({"type": "turn.completed"}))
sys.stdout.write(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "No newline"}}))
''')
        self.assertEqual(code, 0)
        self.assertEqual(summary, {"completed": True, "failed": False, "text": "No newline"})

    def test_nonlive_compiler_output_is_logged_without_interpreting_json(self):
        output = '{"type":"turn.failed"}\n{"type":"turn.completed"}'
        code, summary = self.execute(f"import sys; sys.stdout.write({output!r})", live=False)
        self.assertEqual(code, 0)
        self.assertEqual(summary, {"completed": False, "failed": False, "text": ""})
        self.assertEqual((self.root / "logs/stdout.log").read_text(), output)

    def test_log_failure_does_not_replace_execution_failure(self):
        with patch.object(app.selectors.DefaultSelector, "select", side_effect=OSError("read failure")), \
                patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "read failure") as caught:
                self.execute('print("synthetic output")')
        self.assertTrue(any("disk full" in note for note in caught.exception.__notes__))

    def test_codex_requires_a_completed_nonfailed_turn_and_successful_exit(self):
        session = app.Session.__new__(app.Session)
        session.work = self.root
        session.codex = self.root / "unused-codex"
        session.args = argparse.Namespace(model="fixture", reasoning="high", timeout=10)
        session.env = {}
        session.policy = MagicMock()
        session.policy.wrap.side_effect = lambda command: command
        cases = [
            (0, {"completed": False, "failed": False, "text": "Unfinished"}),
            (0, {"completed": True, "failed": True, "text": "Failed"}),
            (1, {"completed": True, "failed": False, "text": "Bad exit"}),
        ]
        for code, summary in cases:
            with self.subTest(code=code, summary=summary), patch.object(app, "execute", return_value=(code, summary)):
                with self.assertRaises(app.AuditError):
                    session.run_codex("Synthetic prompt", self.root / "logs")
        with patch.object(app, "execute", return_value=(0, {"completed": True, "failed": False, "text": "Final message"})):
            self.assertEqual(session.run_codex("Synthetic prompt", self.root / "logs"), "Final message")


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.roots = []

    def prepare(self, session, *_args):
        self.roots.append(session.root)
        session._resources.callback(self.events.append, "proxy closed")
        session._resources.callback(self.fail_cleanup)

    def fail_cleanup(self):
        self.events.append("source close attempted")
        raise OSError("synthetic service close failure")

    def session(self):
        with patch.object(app.Session, "_prepare", lambda session, *args: self.prepare(session, *args)):
            return app.Session(None, Path("unused"), Path("unused"), None)

    def test_cleanup_attempts_every_resource_and_is_idempotent(self):
        session = self.session()
        with self.assertRaisesRegex(OSError, "service close failure"):
            session.close()
        self.assertEqual(self.events, ["source close attempted", "proxy closed"])
        self.assertFalse(session.root.exists())
        session.close()
        self.assertEqual(len(self.events), 2)

    def test_context_preserves_task_error_after_cleanup_failure(self):
        session = self.session()
        original = app.AuditError("original task error")
        with self.assertRaises(app.AuditError) as caught:
            with session:
                raise original
        self.assertIs(caught.exception, original)
        self.assertTrue(any("service close failure" in note for note in original.__notes__))
        self.assertFalse(session.root.exists())
        self.assertEqual(self.events, ["source close attempted", "proxy closed"])

    def test_partial_initialization_cleans_up_without_masking_error(self):
        original = app.AuditError("initialization failed")

        def fail_prepare(session, *args):
            self.prepare(session, *args)
            raise original

        with patch.object(app.Session, "_prepare", fail_prepare):
            with self.assertRaises(app.AuditError) as caught:
                app.Session(None, Path("unused"), Path("unused"), None)
        self.assertIs(caught.exception, original)
        self.assertFalse(self.roots[0].exists())
        self.assertEqual(self.events, ["source close attempted", "proxy closed"])
        self.assertTrue(any("service close failure" in note for note in original.__notes__))


class LocalSessionTests(unittest.TestCase):
    def test_local_session_does_not_initialize_model_or_network_dependencies(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="thin-local-session-") as directory:
            root = Path(directory)
            source, state = root / "source", root / "state"
            source.mkdir()
            state.mkdir()
            args = argparse.Namespace(codex=str(root / "nonexistent-codex"))
            with ExitStack() as stack:
                for name in ("pdf_tools", "CodexProxy", "SourceFetcher", "codex_runtime_roots"):
                    stack.enter_context(patch.object(app, name, side_effect=AssertionError(f"Unexpected {name}")))
                stack.enter_context(patch.object(app.ssl, "create_default_context", side_effect=AssertionError("Unexpected TLS setup")))
                stack.enter_context(patch.object(app.shutil, "which", side_effect=AssertionError("Unexpected executable discovery")))
                policy = stack.enter_context(patch.object(app, "Seatbelt"))
                session = app.Session(args, state, source, None)
                try:
                    self.assertTrue(session.work.is_dir())
                    self.assertFalse((session.home / ".codex/config.toml").exists())
                    self.assertFalse((session.control / "ca-bundle.pem").exists())
                    self.assertIsNone(policy.call_args.kwargs.get("proxy_port"))
                    self.assertIsNone(policy.call_args.kwargs.get("source_port"))
                finally:
                    session.close()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="thin-publication-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)

    def candidate(self, name, contents):
        candidate = self.state / name
        candidate.mkdir()
        (candidate / "main.tex").write_text(contents)
        return candidate

    def test_publication_moves_trusted_candidate_without_copying_file_bytes(self):
        candidate = self.candidate("candidate", "Synthetic source")
        inode = (candidate / "main.tex").stat().st_ino
        app.promote(self.state, candidate, 1)
        self.assertFalse(candidate.exists())
        self.assertEqual((self.state / "current").resolve(), self.state / "revisions/round-0001")
        self.assertEqual((self.state / "current/main.tex").stat().st_ino, inode)

    def test_failed_current_pointer_update_preserves_previously_published_revision(self):
        app.promote(self.state, self.candidate("first", "First revision"), 0)
        previous = (self.state / "current").resolve()
        candidate = self.candidate("second", "Second revision")
        with patch.object(Path, "replace", side_effect=OSError("Synthetic pointer publication failure")):
            with self.assertRaisesRegex(OSError, "publication failure"):
                app.promote(self.state, candidate, 1)
        self.assertEqual((self.state / "current").resolve(), previous)
        self.assertEqual((self.state / "current/main.tex").read_text(), "First revision")


class RuntimePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="codex-package-test-")
        self.package = Path(self.temporary.name) / "runtime"
        self.executable = self.package / "bin/codex"
        self.executable.parent.mkdir(parents=True)
        self.executable.write_text("synthetic runtime")

    def tearDown(self):
        self.temporary.cleanup()

    def manifest(self, entry="bin/codex"):
        (self.package / "codex-package.json").write_text(json.dumps({
            "layoutVersion": 1, "variant": "codex", "entrypoint": entry,
            "resourcesDir": "codex-resources", "pathDir": "codex-path"}))

    def test_complete_selected_package_without_allowing_its_parent(self):
        self.manifest()
        (self.package / "codex-resources").mkdir()
        sibling = self.package.parent / "prior-session"
        sibling.mkdir()
        roots = app.codex_runtime_roots(self.executable)
        self.assertEqual(roots, [self.package])
        self.assertTrue((self.package / "codex-resources").is_relative_to(roots[0]))
        self.assertFalse(sibling.is_relative_to(roots[0]))

    def test_legacy_binary_retains_only_its_directory(self):
        self.assertEqual(app.codex_runtime_roots(self.executable), [self.executable.parent])

    def test_mismatched_or_traversing_package_is_rejected(self):
        for entry in ("../other/codex", "bin/other", str(self.executable)):
            with self.subTest(entry=entry):
                self.manifest(entry)
                with self.assertRaises(app.AuditError):
                    app.codex_runtime_roots(self.executable)

    def test_corrupt_or_linked_metadata_is_rejected(self):
        metadata = self.package / "codex-package.json"
        metadata.write_text("invalid JSON")
        with self.assertRaises(app.AuditError):
            app.codex_runtime_roots(self.executable)
        metadata.unlink()
        external = self.package.parent / "metadata.json"
        external.write_text('{}')
        metadata.symlink_to(external)
        with self.assertRaises(app.AuditError):
            app.codex_runtime_roots(self.executable)


class FakeSession:
    """Model fixture only: this class makes no operating-system isolation claim."""

    def __init__(self, harness, args, state, source_root, credential_bytes):
        self.h = harness
        self.root = Path(tempfile.mkdtemp(dir=harness.root, prefix="session-"))
        self.work, self.home = self.root / "manuscript", self.root / "home"
        self.temp, self.control = self.root / "tmp", self.root / "control"
        for directory in (self.work, self.home / ".codex", self.temp, self.control):
            directory.mkdir(parents=True)
        self.auth = credential_bytes
        self.closed = False
        if credential_bytes is not None:
            (self.home / ".codex/auth.json").write_bytes(credential_bytes)
        harness.sessions.append(self)

    def close(self):
        self.closed = True
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def prepare_round(self, resources, entry, final_round):
        self.h.final_rounds.append(final_round)
        return "Use the fixture skill."

    def run_codex(self, prompt, log):
        self.h.calls += 1
        number = self.h.calls
        self.h.inputs.append({str(path.relative_to(self.work)): path.read_bytes()
                              for path in self.work.rglob("*") if path.is_file()})
        self.h.auth_inputs.append((self.home / ".codex/auth.json").read_bytes())
        self.h.clean_private_directories.append(
            not (self.home / "private-cache").exists() and not any(self.temp.iterdir()))
        (self.home / ".codex/auth.json").write_text('{"history":"previous-round"}')
        (self.home / "private-cache").write_text("private round history")
        (self.temp / "private-cache").write_text("private temporary history")
        scratch = self.work / ".audit-work"
        scratch.mkdir()
        (scratch / "notes.txt").write_text("must not reach the next round")
        (scratch / "downloaded.bib").write_text("must not become a delivered bibliography")
        (scratch / "main.pdf").write_bytes(b"temporary compilation output")
        (self.work / "scratch").mkdir()
        (self.work / "scratch/unrelated.bib").write_text("unrelated bibliography")
        (self.work / "old-review.md").write_text("must not reach the next round")
        if self.h.empty_bib_fallback:
            (self.work / "references.bib").write_bytes(b"")
        if number == 1:
            entry = self.work / "main.tex"
            entry.write_text(entry.read_text() + "\n% fixture revision\n")
        if number == self.h.interrupt_round:
            raise KeyboardInterrupt
        if number == self.h.fail_model_round:
            raise app.AuditError("synthetic CLI failure")
        if number == self.h.missing_bib_round:
            (self.work / "refs.bib").unlink()
        if number == self.h.stderr_round:
            log.mkdir(parents=True)
            (log / "stderr.log").write_text("Synthetic nonfatal diagnostic.\n")
        # Deliberately no report, receipts, completion marker, or review packets.
        return "" if self.h.empty_outcome else (
            "Updated the manuscript; fixture outcome.\n"
            f"[TeX]({self.work / 'main.tex'})\n[Bib]({self.work / 'refs.bib'})\n"
            f"[PDF]({scratch / 'main.pdf'})\n")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="thin-runner-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.tex = self.source / "main.tex"
        self.tex.write_text("\\documentclass{article}\n\\begin{document}\\input{section}\n"
                            "\\bibliography{refs}\n\\end{document}\n")
        (self.source / "section.tex").write_text("A synthetic section.")
        (self.source / "refs.bib").write_text("@misc{fixture,title={Synthetic fixture}}\n")
        (self.source / "old").mkdir()
        (self.source / "old/extra.bib").write_text("must not become manuscript input")
        (self.source / "unrelated.txt").write_text("Not manuscript input.")
        self.initial = self.tex.read_bytes()
        self.state = self.root / "run"
        self.sessions, self.inputs, self.auth_inputs = [], [], []
        self.clean_private_directories, self.final_rounds = [], []
        self.calls = 0
        self.fail_model_round = self.missing_bib_round = self.stderr_round = self.interrupt_round = -1
        self.empty_outcome = self.empty_bib_fallback = False
        self.credentials = b'{"auth_mode":"fixture","token":"trusted-startup"}'
        self.args = argparse.Namespace(input=self.tex, project_root=self.source, output=self.state,
            asset=[], rounds=3, model="fixture-model", reasoning="high",
            timeout=30, build_timeout=30, codex="unused")

    def invoke(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(app, "Session", side_effect=lambda *a: FakeSession(self, *a)))
            # Kernel proofs belong to test_isolation and the real fake-CLI integration.
            stack.enter_context(patch.object(app, "probe", return_value=None))
            self.credential_reader = stack.enter_context(
                patch.object(app, "read_credentials", return_value=self.credentials))
            stack.enter_context(redirect_stdout(io.StringIO()))
            return app.run(self.args)

    def read_state(self):
        return json.loads((self.state / "run.json").read_text())

    def assert_last_revision(self, number):
        self.assertEqual(self.read_state()["completed_rounds"], number)
        self.assertEqual((self.state / "current").resolve().name, f"round-{number:04d}")
        self.assertFalse((self.state / "revisions" / f"round-{number+1:04d}").exists())

    def test_exact_rounds_include_unchanged_revisions_without_protocol_artifacts(self):
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.calls, 3)
        self.assert_last_revision(3)
        self.assertEqual(self.inputs[1], self.inputs[2])
        self.assertEqual(self.final_rounds, [False, False, True])
        self.assertEqual(len(self.sessions), 3)
        self.assertEqual(self.tex.read_bytes(), self.initial)
        self.assertTrue(all(session.closed for session in self.sessions))
        self.assertEqual(self.read_state()["status"], "completed")
        self.assertEqual({p.name for p in (self.state / "deliverables").iterdir()},
                         {"main.tex", "section.tex", "refs.bib"})
        self.assertFalse((self.state / "input").exists())
        self.assertEqual((self.state / "revisions/round-0000/main.tex").read_bytes(), self.initial)

    def test_only_manuscript_sources_and_dependencies_reach_next_round(self):
        self.assertEqual(self.invoke(), 0)
        for files in self.inputs:
            self.assertEqual(set(files), {"main.tex", "section.tex", "refs.bib"})
        self.assertTrue(all(self.clean_private_directories))
        self.assertEqual(len({session.root for session in self.sessions}), 3)
        self.assertIn(b"fixture revision", self.inputs[1]["main.tex"])

    def test_every_round_gets_original_credentials_not_agent_auth_changes(self):
        self.assertEqual(self.invoke(), 0)
        self.credential_reader.assert_called_once_with()
        self.assertEqual(self.auth_inputs, [self.credentials] * 3)

    def test_agent_diagnostic_failure_does_not_change_success_or_round_count(self):
        with patch('agent_observer.observe', side_effect=RuntimeError('diagnostic failure')):
            self.assertEqual(self.invoke(), 0)
        self.assert_last_revision(3)
        self.assertEqual(self.read_state()['status'], 'completed')
        self.assertTrue(all(session.closed for session in self.sessions))

    def test_agent_diagnostic_failure_does_not_replace_original_cli_failure(self):
        self.fail_model_round = 2
        with patch('agent_observer.observe', side_effect=RuntimeError('diagnostic failure')):
            with self.assertRaisesRegex(app.AuditError, 'synthetic CLI failure'):
                self.invoke()
        self.assert_last_revision(1)
        self.assertTrue(all(session.closed for session in self.sessions))

    def test_agent_report_disk_error_does_not_change_run_success(self):
        original = Path.write_text

        def fail_diagnostic(path, *args, **kwargs):
            if path.name == 'agent-diagnostics.json.tmp':
                raise OSError('synthetic diagnostic disk failure')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'write_text', fail_diagnostic):
            self.assertEqual(self.invoke(), 0)
        self.assert_last_revision(3)
        self.assertEqual(self.read_state()['status'], 'completed')

    def test_agent_observer_import_error_does_not_change_run_success(self):
        with patch.dict(sys.modules, {'agent_observer': None}):
            self.assertEqual(self.invoke(), 0)
        self.assert_last_revision(3)

    def test_agent_diagnostics_are_collected_before_cleanup_even_on_failure(self):
        original = FakeSession.run_codex

        def with_rollout(session, prompt, log):
            folder = session.home / '.codex/sessions'
            folder.mkdir()
            (folder / 'root.jsonl').write_text(json.dumps({
                'type': 'session_meta', 'payload': {'id': 'fixture-root', 'source': 'exec'}
            }) + '\n')
            return original(session, prompt, log)

        self.fail_model_round = 2
        with patch.object(FakeSession, 'run_codex', with_rollout):
            with self.assertRaisesRegex(app.AuditError, 'synthetic CLI failure'):
                self.invoke()
        for number in (1, 2):
            folder = self.state / 'archive' / f'round-{number:04d}'
            self.assertTrue((folder / 'native-rollouts/sessions/root.jsonl').is_file())
            report = json.loads((folder / 'agent-diagnostics.json').read_text())
            self.assertTrue(report['informational_only'])
        self.assertTrue(all(not session.root.exists() for session in self.sessions))

    def test_empty_natural_language_outcome_and_missing_report_do_not_block(self):
        self.empty_outcome = True
        self.assertEqual(self.invoke(), 0)
        for number in range(1, 4):
            directory = self.state / "archive" / f"round-{number:04d}"
            report = (directory / "outcome.txt").read_text()
            self.assertIn("Saved source files", report)
            self.assertIn(str(self.state / "revisions" / f"round-{number:04d}" / "main.tex"), report)
            self.assertFalse((directory / "report.md").exists())

    def test_missing_bib_does_not_count_failed_round_or_use_scratch_bib(self):
        self.missing_bib_round = 2
        with self.assertRaises((app.AuditError, app.BundleError)):
            self.invoke()
        self.assertEqual(self.calls, 2)
        self.assert_last_revision(1)
        self.assertTrue(((self.state / "current") / "refs.bib").is_file())
        self.assertTrue((self.state / "archive/round-0002/failed-workspace/main.tex").is_file())
        report = (self.state / "archive/round-0002/outcome.txt").read_text()
        self.assertIn("No source revision confirmed", report)
        self.assertNotIn("Saved source files", report)
        self.assertNotIn(str(self.sessions[1].work), report)
        self.assertTrue(all(session.closed for session in self.sessions))

    def test_unreferenced_empty_root_bibliography_is_preserved(self):
        self.empty_bib_fallback = True
        self.args.rounds = 2
        self.tex.write_text(self.tex.read_text().replace("\\bibliography{refs}\n", ""))
        (self.source / "refs.bib").unlink()
        self.assertEqual(self.invoke(), 0)
        self.assertNotIn("references.bib", self.inputs[0])
        self.assertEqual(self.inputs[1]["references.bib"], b"")
        self.assertEqual({p.name for p in (self.state / "deliverables").iterdir()},
                         {"main.tex", "section.tex", "references.bib"})
        self.assertEqual((self.state / "deliverables/references.bib").read_bytes(), b"")

    def test_failed_cli_stops_without_retry_and_preserves_last_complete_sources(self):
        self.fail_model_round = 2
        with self.assertRaisesRegex(app.AuditError, "synthetic CLI"):
            self.invoke()
        self.assertEqual(self.calls, 2)
        self.assert_last_revision(1)
        self.assertEqual(self.read_state()["status"], "failed")
        self.assertTrue(all(session.closed for session in self.sessions))

    def test_successful_codex_stderr_does_not_block(self):
        self.stderr_round = 1
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.read_state()["completed_rounds"], 3)
        self.assertIn("Synthetic", (self.state / "archive/round-0001/codex/stderr.log").read_text())

    def test_interruption_preserves_last_complete_revision_and_closes_session(self):
        self.interrupt_round = 2
        with self.assertRaises(KeyboardInterrupt):
            self.invoke()
        self.assertEqual(self.calls, 2)
        self.assert_last_revision(1)
        self.assertEqual(self.read_state()["status"], "interrupted")
        self.assertTrue(all(session.closed for session in self.sessions))

    def test_reports_link_to_fixed_revisions_after_temporary_files_are_removed(self):
        self.assertEqual(self.invoke(), 0)
        for number, session in enumerate(self.sessions, 1):
            self.assertFalse(session.root.exists())
            report = (self.state / "archive" / f"round-{number:04d}" / "outcome.txt").read_text()
            revision = self.state / "revisions" / f"round-{number:04d}"
            self.assertIn(f"[TeX](<{revision / 'main.tex'}>)", report)
            self.assertIn(f"[Bib](<{revision / 'refs.bib'}>)", report)
            self.assertIn("PDF (temporary file; not delivered)", report)
            self.assertNotIn(str(session.work), report)
            self.assertNotIn("/current/", report)
            self.assertTrue((revision / "main.tex").is_file())
            self.assertTrue((revision / "refs.bib").is_file())
        self.assertFalse((self.state / "deliverables/main.pdf").exists())

    def test_source_pdf_assets_are_still_delivered(self):
        self.tex.write_text(self.tex.read_text() + "\n\\includegraphics{diagram.pdf}\n")
        (self.source / "diagram.pdf").write_bytes(b"required figure")
        self.assertEqual(self.invoke(), 0)
        self.assertEqual((self.state / "deliverables/diagram.pdf").read_bytes(), b"required figure")
        self.assertFalse((self.state / "deliverables/main.pdf").exists())

    def test_cli_rejects_removed_pdf_options_and_keeps_extraction_timeout(self):
        args = app.parser().parse_args([str(self.tex), "--build-timeout", "60"])
        self.assertEqual(args.build_timeout, 60)
        for options in (["--pdf"], ["--engine", "xelatex"]):
            with self.subTest(options=options), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    app.parser().parse_args([str(self.tex), *options])
                self.assertEqual(caught.exception.code, 2)

    def test_failed_status_write_preserves_task_error_and_workspace(self):
        self.fail_model_round = 2
        write_text = Path.write_text

        def fail_error_status(path, data, *args, **kwargs):
            if path.name == "run.json.tmp" and '"status": "failed"' in data:
                raise OSError("synthetic status disk failure")
            return write_text(path, data, *args, **kwargs)

        with patch.object(Path, "write_text", fail_error_status):
            with self.assertRaisesRegex(app.AuditError, "synthetic CLI") as caught:
                self.invoke()
        self.assertTrue(any("status disk failure" in note for note in caught.exception.__notes__))
        self.assertEqual((self.state / "current").resolve().name, "round-0001")
        self.assertTrue((self.state / "archive/round-0002/failed-workspace/main.tex").is_file())
        self.assertTrue(all(session.closed for session in self.sessions))


if __name__ == "__main__":
    unittest.main()
