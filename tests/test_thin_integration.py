"""Real macOS isolation with a synthetic CLI; never starts a model or uses the Internet."""
import argparse
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manuscript as app


_FAKE_CLI = r'''
import errno, json, os, pathlib, re, sys
assert sys.argv[1] == "exec" and "--ephemeral" not in sys.argv
assert "resume" not in sys.argv
prompt = sys.stdin.read()
assert "SKILL.md" in prompt
planning = "This is the planning stage of segmented mode" in prompt
segmented = "Mode: segmented." in prompt
work = pathlib.Path(sys.argv[sys.argv.index("-C") + 1])
home = pathlib.Path(os.environ["HOME"])
temporary = pathlib.Path(os.environ["TMPDIR"])
assert pathlib.Path(os.environ["CODEX_HOME"]) == home / ".codex"
assert (home / ".codex/auth.json").read_bytes() == b"{}"
assert not (home / "private-cache").exists()
assert not (temporary / "private-cache").exists()
assert not (work / "old-review.md").exists()
assert not (work / ".audit-work/private-cache").exists()
entry = work / "main.tex"
source = entry.read_text()
number = int(re.search(r"fixture-round: (\d+)", source).group(1)) + 1
protected = [pathlib.Path(SOURCE_FILE), pathlib.Path(STATE_ROOT) / "isolation-canary.txt"]
if segmented:
    protected.append(pathlib.Path(STATE_ROOT) / "archive/planning/outcome.txt")
if number == 2:
    protected.append(pathlib.Path(STATE_ROOT) / "archive/round-0001/outcome.txt")
for path in protected:
    try:
        path.read_bytes()
    except OSError as exc:
        assert exc.errno in (errno.EACCES, errno.EPERM), (str(path), repr(exc))
    else:
        raise AssertionError("Previous source/archive became readable: " + str(path))
try:
    list(pathlib.Path(STATE_ROOT).iterdir())
except PermissionError:
    pass
else:
    raise AssertionError("Archive directory listing was allowed")
if planning:
    (work / ".audit-work/review-plan.md").write_text(
        "# Review plan\n\n## 1. Main argument\nAudit the argument.\n\n"
        "## 2. Exposition and consistency\nReview the exposition and interfaces.\n")
    entry.write_text("PLANNER EDIT MUST NOT REACH THE AUDIT")
    (home / ".codex/auth.json").write_text('{"history":"discard planning credentials"}')
    (home / "private-cache").write_text("planning private history")
    (work / "old-review.md").write_text("planning notes")
    print(json.dumps({"type":"fixture.checks", "phase":"planning", "work":str(work),
                      "home":str(home), "temporary":str(temporary), "denied":len(protected)}))
    print(json.dumps({"type":"turn.completed"}))
    sys.exit(0)
if segmented:
    plan = pathlib.Path(re.search(r"^Review plan: (.+)$", prompt, re.M).group(1))
    assert "## 1. Main argument" in plan.read_text()
    assert "Current block:\n## " + str(number) + "." in prompt
    try:
        plan.write_text("altered plan")
    except OSError as exc:
        assert exc.errno in (errno.EACCES, errno.EPERM)
    else:
        raise AssertionError("The review plan was writable")
source = re.sub(r"fixture-round: \d+", "fixture-round: " + str(number), source)
if "\\bibliography{refs}" not in source:
    source = source.replace("\\end{document}", "\\bibliography{refs}\n\\end{document}")
entry.write_text(source)
(work / "refs.bib").write_text("@misc{fixture,title={Synthetic round " + str(number) + "}}\n")
(home / ".codex/auth.json").write_text('{"history":"must not enter next round"}')
(home / "private-cache").write_text("previous private home")
(temporary / "private-cache").write_text("previous temporary history")
(work / "old-review.md").write_text("previous review")
(work / ".audit-work/private-cache").write_text("previous scratch history")
(work / ".audit-work/downloaded.bib").write_text("not a manuscript bibliography")
print(json.dumps({"type":"fixture.checks", "round":number, "work":str(work),
                  "home":str(home), "temporary":str(temporary), "denied":len(protected)}))
print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"Synthetic round " + str(number)}}))
print(json.dumps({"type":"turn.completed"}))
'''


@unittest.skipUnless(platform.system() == "Darwin" and os.environ.get("AUDITAGENT_SANDBOX_TESTS") == "1",
                     "requires explicit real macOS sandbox tests from an unsandboxed terminal")
class ThinIntegrationTests(unittest.TestCase):
    def test_local_session_preflight_needs_no_codex_or_network_services(self):
        with tempfile.TemporaryDirectory(
            dir="/private/tmp", prefix="thin-local-isolation-"
        ) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            state = root / "check"
            state.mkdir()
            args = argparse.Namespace(codex=str(root / "missing-codex"))

            with ExitStack() as stack:
                for name in (
                    "pdf_tools", "CodexProxy",
                    "SourceFetcher", "codex_runtime_roots"
                ):
                    stack.enter_context(
                        patch.object(
                            app, name,
                            side_effect=AssertionError(f"Unexpected {name}")
                        )
                    )
                stack.enter_context(
                    patch.object(
                        app.ssl, "create_default_context",
                        side_effect=AssertionError("Unexpected TLS setup")
                    )
                )
                session = app.Session(args, state, source, None)
                try:
                    app.probe(session, state, state / "isolation.json")
                finally:
                    session.close()
            self.assertTrue(
                all(json.loads((state / "isolation.json").read_text()).values())
            )

    def test_two_real_isolated_rounds_keep_only_tex_bib_and_fresh_private_state(self):
        self.run_round_fixture(segmented=False)

    def test_segmented_planning_and_rounds_keep_plan_read_only_and_history_isolated(self):
        self.run_round_fixture(segmented=True)

    def run_round_fixture(self, *, segmented):
        with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="thin-integration-") as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            entry = source / "main.tex"
            entry.write_text("\\documentclass{article}\n% fixture-round: 0\n"
                             "\\begin{document}Synthetic input.\\end{document}\n")
            original = entry.read_bytes()
            state = root / "run"
            executable = root / "runtime/bin/fake-codex"
            executable.parent.mkdir(parents=True)
            script = _FAKE_CLI.replace("SOURCE_FILE", repr(str(entry))).replace("STATE_ROOT", repr(str(state)))
            executable.write_text("#!" + sys.executable + "\n" + script)
            executable.chmod(0o700)
            args = argparse.Namespace(input=entry, project_root=source, output=state,
                asset=[], rounds=None if segmented else 2, mode='segmented' if segmented else 'full',
                model="synthetic-no-model", reasoning="high",
                timeout=30, build_timeout=30, codex=str(executable))
            with ExitStack() as stack:
                credentials = stack.enter_context(patch.object(app, "read_credentials", return_value=b"{}"))
                # Even an accidental fake-CLI request cannot open an upstream socket.
                connect = stack.enter_context(patch.object(app.CodexProxy, "_connect",
                    side_effect=OSError("External network disabled in integration fixture")))
                fetch = stack.enter_context(patch.object(app.SourceFetcher, "_retrieve",
                    side_effect=OSError("External network disabled in integration fixture")))
                stack.enter_context(redirect_stdout(io.StringIO()))
                self.assertEqual(app.run(args), 0)
            credentials.assert_called_once_with()
            connect.assert_not_called()
            fetch.assert_not_called()
            status = json.loads((state / "run.json").read_text())
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["completed_rounds"], 2)
            self.assertEqual(status["planned_rounds"], 2)
            self.assertFalse((state / "input").exists())
            self.assertEqual(entry.read_bytes(), original)
            self.assertEqual({p.name for p in (state / "deliverables").iterdir()}, {"main.tex", "refs.bib"})
            self.assertIn("fixture-round: 2", (state / "deliverables/main.tex").read_text())
            self.assertIn("Synthetic round 2", (state / "deliverables/refs.bib").read_text())
            checks = []
            for number in (1, 2):
                round_dir = state / "archive" / f"round-{number:04d}"
                self.assertTrue(all(json.loads((round_dir / "isolation.json").read_text()).values()))
                events = [json.loads(line) for line in (round_dir / "codex/events.jsonl").read_text().splitlines()]
                current = next(event for event in events if event["type"] == "fixture.checks")
                self.assertEqual(current["round"], number)
                self.assertEqual(current["denied"], number + 1 + int(segmented))
                report = (round_dir / "outcome.txt").read_text()
                self.assertIn(f"Synthetic round {number}", report)
                self.assertIn(str(state / "revisions" / f"round-{number:04d}" / "main.tex"), report)
                checks.append(current)
            if segmented:
                self.assertIsNone(status['requested_rounds'])
                planning = state / 'archive/planning'
                self.assertTrue(all(json.loads((planning / 'isolation.json').read_text()).values()))
                events = [json.loads(line) for line in (planning / 'codex/events.jsonl').read_text().splitlines()]
                checks.append(next(event for event in events if event['type'] == 'fixture.checks'))
                self.assertTrue((state / 'review-plan.md').is_file())
                self.assertFalse((state / 'revisions/round-0003').exists())
            for key in ("work", "home", "temporary"):
                self.assertEqual(len({check[key] for check in checks}), len(checks))
                self.assertTrue(all(not Path(check[key]).exists() for check in checks))


if __name__ == "__main__":
    unittest.main()
