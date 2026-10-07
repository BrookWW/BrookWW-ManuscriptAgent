"""Segmented scheduling with synthetic sessions; no live model calls."""
import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manuscript as app


PLAN = "# Review plan\n\nStart with the supporting argument.\n\n" \
       "## 1. Supporting lemma\nAudit section.tex and the lemma used by the main result.\n\n" \
       "## 2. Main result and consistency\nCheck the main proof and affected introductory claims.\n"


class PlanParsingTests(unittest.TestCase):
    def test_preserves_scope_and_order_without_counting_overview_or_nested_headings(self):
        plan = PLAN.replace('Audit section.tex', '### Focus\nAudit section.tex')
        blocks = app.parse_review_plan(plan)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[0].startswith('## 1. Supporting lemma'))
        self.assertIn('### Focus\nAudit section.tex', blocks[0])
        self.assertNotIn('Main result', blocks[0])
        self.assertTrue(blocks[1].startswith('## 2. Main result'))

    def test_code_examples_do_not_add_rounds(self):
        plan = PLAN + '\n```text\n## 3. Example only\n```\n'
        self.assertEqual(len(app.parse_review_plan(plan)), 2)
        with self.assertRaises(app.AuditError):
            app.parse_review_plan('```markdown\n' + PLAN + '\n```')

    def test_rejects_empty_or_ambiguous_round_order(self):
        for plan in ('', '# Overview only', '## 2. First block',
                     '## 1. First\n## 1. Duplicate', '## 1. First\n## 3. Skipped'):
            with self.subTest(plan=plan), self.assertRaises(app.AuditError):
                app.parse_review_plan(plan)


class SyntheticSession:
    """Use real prompt assembly, but replace all model work with fixture output."""

    def __init__(self, harness, args, state, source_root, credentials):
        self.h, self.args = harness, args
        self.root = Path(tempfile.mkdtemp(dir=harness.root, prefix='session-'))
        self.work, self.home = self.root / 'manuscript', self.root / 'home'
        self.temp, self.control = self.root / 'tmp', self.root / 'control'
        for directory in (self.work, self.home / '.codex', self.temp, self.control):
            directory.mkdir(parents=True)
        (self.home / '.codex/auth.json').write_bytes(credentials)
        self.tools = {}
        self.source_fetcher = argparse.Namespace(config={})
        harness.sessions.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        shutil.rmtree(self.root)

    def prepare_round(self, resources, entry, final_round, **kwargs):
        self.planning = kwargs.get('planning', False)
        self.block = kwargs.get('block')
        self.final_round = final_round
        return REAL_PREPARE(self, resources, entry, final_round, **kwargs)

    def run_codex(self, prompt, log):
        h = self.h
        h.prompts.append(prompt)
        h.auth_inputs.append((self.home / '.codex/auth.json').read_bytes())
        (self.home / '.codex/auth.json').write_text('untrusted refreshed credentials')
        if self.planning and h.planning_error:
            raise h.planning_error
        if not self.planning:
            h.inputs.append({str(path.relative_to(self.work)): path.read_bytes()
                             for path in self.work.rglob('*') if path.is_file()})
            h.blocks.append(self.block)
            h.final_flags.append(self.final_round)
            h.plans.append((self.control / 'review-plan.md').read_text())
        scratch = self.work / '.audit-work'
        scratch.mkdir()
        (scratch / 'notes.md').write_text('Session history must not reach another session.')
        (self.work / 'old-review.md').write_text('This is not a source dependency.')
        if self.planning:
            # Neither planner edits nor scratch files may become the first audit input.
            (self.work / 'main.tex').write_text('PLANNER EDIT MUST BE DISCARDED')
            target = scratch / 'review-plan.md'
            if h.plan_kind == 'symlink':
                target.symlink_to(h.root / 'outside.md')
            elif h.plan_kind != 'missing':
                target.write_bytes(h.plan_bytes)
            return 'The review plan is ready.'
        number = len(h.inputs)
        (self.work / 'section.tex').write_text(f'Revised block {number}.\n')
        if number == h.fail_round:
            raise app.AuditError('synthetic block failure')
        return f'Reviewed block {number}.'


class SegmentedRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp', prefix='segmented-tests-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.entry = self.source / 'main.tex'
        self.entry.write_text('\\documentclass{article}\n\\begin{document}\n'
                              '\\input{section}\n\\bibliography{refs}\n\\end{document}\n')
        (self.source / 'section.tex').write_text('Original argument.\n')
        (self.source / 'refs.bib').write_text('@misc{fixture,title={Fixture}}\n')
        self.original = self.entry.read_bytes()
        self.state = self.root / 'run'
        self.args = argparse.Namespace(input=self.entry, project_root=self.source,
            output=self.state, asset=[], rounds=None, mode='segmented', model='fixture',
            reasoning='high', timeout=30, build_timeout=30, codex='unused')
        self.sessions, self.inputs, self.prompts, self.blocks = [], [], [], []
        self.plans, self.final_flags, self.auth_inputs = [], [], []
        self.plan_bytes, self.plan_kind = PLAN.encode(), 'regular'
        self.fail_round = -1
        self.planning_error = None

    def invoke(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(app, 'Session', side_effect=lambda *a: SyntheticSession(self, *a)))
            stack.enter_context(patch.object(app, 'probe', return_value=None))
            stack.enter_context(patch.object(app, 'read_credentials', return_value=b'{}'))
            stack.enter_context(redirect_stdout(io.StringIO()))
            return app.run(self.args)

    def status(self):
        return json.loads((self.state / 'run.json').read_text())

    def test_model_plan_drives_rounds_and_only_sources_and_plan_cross_sessions(self):
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(len(self.sessions), 3)
        self.assertEqual(self.blocks, app.parse_review_plan(PLAN))
        self.assertEqual(self.final_flags, [False, True])
        self.assertEqual(self.plans, [PLAN, PLAN])
        self.assertEqual(self.auth_inputs, [b'{}'] * 3)
        self.assertEqual(self.inputs[0]['main.tex'], self.original)
        self.assertEqual(self.inputs[0]['section.tex'], b'Original argument.\n')
        self.assertEqual(self.inputs[1]['section.tex'], b'Revised block 1.\n')
        self.assertTrue(all(set(files) == {'main.tex', 'section.tex', 'refs.bib'} for files in self.inputs))
        self.assertEqual((self.state / 'review-plan.md').read_text(), PLAN)
        self.assertEqual(self.status()['planned_rounds'], 2)
        self.assertIsNone(self.status()['requested_rounds'])
        self.assertEqual(self.status()['completed_rounds'], 2)
        self.assertEqual(self.status()['status'], 'completed')
        self.assertEqual(self.status()['mode'], 'segmented')
        self.assertIsNone(self.status()['active_round'])
        self.assertEqual(self.entry.read_bytes(), self.original)
        self.assertEqual((self.state / 'revisions/round-0000/main.tex').read_bytes(), self.original)
        self.assertEqual((self.state / 'deliverables/section.tex').read_text(), 'Revised block 2.\n')
        self.assertEqual({p.name for p in (self.state / 'deliverables').iterdir()},
                         {'main.tex', 'section.tex', 'refs.bib'})
        self.assertTrue((self.state / 'archive/planning/outcome.txt').is_file())
        self.assertFalse((self.state / 'revisions/round-0003').exists())
        self.assertTrue(all(not session.root.exists() for session in self.sessions))
        for block, prompt in zip(self.blocks, self.prompts[1:]):
            self.assertIn('Current block:\n' + block, prompt)
            self.assertIn('Mode: segmented.', prompt)

    def test_plan_text_is_not_reinterpreted_as_prompt_template_fields(self):
        self.plan_bytes = b'## 1. Literal template notation\nCheck {{ENTRYPOINT}} in the examples.\n'
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.status()['completed_rounds'], 1)
        self.assertIn('Check {{ENTRYPOINT}} in the examples.', self.prompts[-1])

    def test_invalid_missing_linked_or_oversized_plan_starts_no_audit_rounds(self):
        for kind, contents in (('regular', b'No numbered blocks.'), ('missing', b''),
                               ('symlink', b''), ('regular', b'## 1. Block\n' + b'x' * 65536),
                               ('regular', b'\xff')):
            with self.subTest(kind=kind, size=len(contents)):
                self.state = self.root / f'run-{len(self.sessions)}'
                self.args.output = self.state
                self.plan_kind, self.plan_bytes = kind, contents
                with self.assertRaises((app.AuditError, app.SafeIOError, UnicodeError)):
                    self.invoke()
                self.assertEqual(self.inputs, [])
                self.assertEqual(self.status()['status'], 'failed')
                self.assertEqual(self.status()['completed_rounds'], 0)
                self.assertIsNone(self.status()['planned_rounds'])
                self.assertEqual((self.state / 'current').resolve().name, 'round-0000')
                self.assertFalse((self.state / 'archive/round-0001').exists())
                self.assertTrue(all(not session.root.exists() for session in self.sessions))

    def test_failed_block_preserves_last_revision_and_original_plan(self):
        self.fail_round = 2
        with self.assertRaisesRegex(app.AuditError, 'synthetic block failure'):
            self.invoke()
        self.assertEqual(self.status()['completed_rounds'], 1)
        self.assertEqual(self.status()['active_round'], 2)
        self.assertEqual((self.state / 'current').resolve().name, 'round-0001')
        self.assertEqual((self.state / 'current/section.tex').read_text(), 'Revised block 1.\n')
        self.assertEqual((self.state / 'archive/round-0002/failed-workspace/section.tex').read_text(),
                         'Revised block 2.\n')
        self.assertEqual((self.state / 'review-plan.md').read_text(), PLAN)
        self.assertFalse((self.state / 'deliverables').exists())

    def test_planning_interruption_or_timeout_preserves_original_revision(self):
        for error in (KeyboardInterrupt(), app.AuditError('Process timed out')):
            with self.subTest(error=type(error).__name__):
                self.state = self.root / f'run-{len(self.sessions)}'
                self.args.output = self.state
                self.planning_error = error
                with self.assertRaises(type(error)):
                    self.invoke()
                self.assertEqual(self.inputs, [])
                self.assertEqual(self.status()['completed_rounds'], 0)
                self.assertEqual(self.status()['status'],
                                 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed')
                self.assertEqual((self.state / 'current/main.tex').read_bytes(), self.original)
                self.assertTrue((self.state / 'archive/planning/failed-workspace/main.tex').is_file())
                self.assertTrue(all(not session.root.exists() for session in self.sessions))


REAL_PREPARE = app.Session.prepare_round


class ModeCLITests(unittest.TestCase):
    def test_full_is_default_and_keeps_the_round_prompt(self):
        with patch.object(app.sys, 'platform', 'darwin'), \
             patch.object(app, 'local_defaults', return_value=('fixture', 'high')), \
             patch('builtins.input', return_value='3') as ask, \
             patch.object(app, 'run', return_value=0) as run:
            self.assertEqual(app.main(['main.tex']), 0)
        ask.assert_called_once()
        self.assertEqual(run.call_args.args[0].mode, 'full')
        self.assertEqual(run.call_args.args[0].rounds, 3)

    def test_segmented_mode_never_prompts_for_round_count(self):
        with patch.object(app.sys, 'platform', 'darwin'), \
             patch.object(app, 'local_defaults', return_value=('fixture', 'high')), \
             patch('builtins.input', side_effect=AssertionError('Unexpected round prompt')), \
             patch.object(app, 'run', return_value=0) as run:
            self.assertEqual(app.main(['main.tex', '--mode', 'segmented']), 0)
        self.assertIsNone(run.call_args.args[0].rounds)

    def test_conflicting_round_count_is_rejected_before_work(self):
        with patch.object(app, 'run') as run, \
             patch.object(app, 'local_defaults') as defaults, \
             redirect_stderr(io.StringIO()) as error:
            self.assertEqual(app.main(['main.tex', '--mode', 'segmented', '--rounds', '2']), 1)
        run.assert_not_called()
        defaults.assert_not_called()
        self.assertIn('--rounds is only available in full mode', error.getvalue())


if __name__ == '__main__':
    unittest.main()
