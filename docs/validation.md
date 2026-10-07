# Validation

## Publication verification — 2026-10-07

```sh
AUDITAGENT_SANDBOX_TESTS=1 python3.12 -B -m unittest discover -s tests -q
PYTHONDONTWRITEBYTECODE=1 ./manuscript-agent --help
```

Result: **227 tests passed, no skips**, in 6.056 seconds, including the
macOS kernel-isolation and synthetic-CLI integration tests. Execution required
permission to invoke the sandbox and bind local loopback sockets. The tests
used synthetic credentials, mocked upstream calls and fake model sessions;
no live model or external service request was made. CLI help also passed.

The public source snapshot excludes manuscript runs, local agent state,
Python caches, credentials and the local Chinese README. Historical local
provenance files are not included in the publication history. Local research
outputs remain outside the published source package. These checks verify
orchestration and packaging, not mathematical correctness.

## Segmented mode — 2026-10-04

```sh
AUDITAGENT_SANDBOX_TESTS=1 python3.12 -B -m unittest discover -s tests -q
```

Result: **227 tests passed, no skips**, in 5.076 seconds. The suite ran with
permission to use macOS `sandbox-exec` and local loopback sockets. No live model
call or external service request was made.

New coverage checks model-selected block counts and order, planning outside the
audit count, discarded planner edits, latest-source handoff, unchanged startup
credentials, and source-only delivery. Missing, malformed, linked, oversized,
or non-UTF-8 plans stop before auditing. Planning interruption or timeout retains
the original revision; a failed audit block retains prior completed revisions.
CLI tests verify the default full mode, automatic counts in segmented mode, and
rejection of a conflicting `--rounds` argument.

The real synthetic-CLI integration now also runs one planning session followed
by two segmented audit sessions. It verifies a readable but unwritable plan,
denial of planning and earlier audit archives, fresh private state, cleanup,
discarded planning edits, and complete TeX/Bib delivery. Existing full-mode and
kernel-isolation tests also pass. Skill validation, CLI help, and whitespace
checks pass.

These tests establish orchestration behavior, not the quality of a model's
decomposition, mathematical findings, or context/token savings. No live
segmented manuscript audit was run.

## Refactor validation — 2026-10-02

The lifecycle, shared safe reader, DNS helpers, bundle discovery and policy-generation refactor was compared with the clean pre-change revision `55ed661`.

```sh
AUDITAGENT_SANDBOX_TESTS=1 python3.12 -B -m unittest discover -s tests -q
```

The baseline passed **161 tests with no skips** in 4.158 seconds. The refactored runner passed **180 tests with no skips** in 4.426 seconds, including the real macOS kernel, loopback and synthetic-CLI integration tests. Suite times are not directly comparable because the new suite includes additional cases. No live model or external service was contacted. CLI help and `git diff --check` also passed.

New regression coverage checks cleanup after partial initialization, completion of remaining cleanup callbacks after one fails, preservation of the original error when logging or cleanup also fails, bounded reads during file mutations, dependency replacement between discovery and copying, dynamic graphics extensions, and DNS deadline/error classification. Failed round snapshots are saved before session cleanup. PDF compilation starts after the last model session closes, and a failed build now snapshots the build workspace. Directory cleanup failures are reported rather than silently ignored; already-published revisions remain available.

Policy strings and wrapped commands were byte-identical across 30 combinations of paths, ports and macOS preference access settings. Model-domain and public-source URL policies remain separate, while their DNS transport and address validation share one implementation.

Local performance checks:

| Workload | Before | After | Method |
|---|---:|---:|---|
| Same three thin integration tests | 1.414 s | 1.224 s | Median of five alternating runs per version, including Python startup, synthetic CLI and local PDF build |
| Bundle with 250 graphics | 51.31 ms | 44.78 ms | Median of seven alternating runs; all 251 output files byte-identical |
| Bounded read, 4 KiB | 0.027 ms | 0.027 ms | Median of 21 alternating cached-file measurements |
| Bounded read, 8 MiB | 0.999 ms | 0.399 ms | Same cached-file method |
| Bounded read, 32 MiB | 8.036 ms | 2.323 ms | Same cached-file method |

The bundle workload reduced safe file checks from 752 to 502 while retaining checks immediately before copying and descriptor-based reads. These local measurements show no observed regression in the measured workloads; they do not establish live-model latency, network performance or mathematical quality.

## Previous thin runner validation — 2026-09-30

The previous receipt/alignment protocol and its validation reports have been removed. These results describe the current thin runner, not a mathematical audit.

Final command on macOS:

```sh
AUDITAGENT_SANDBOX_TESTS=1 python3.12 -B -m unittest discover -s tests -q
```

Result after redundancy removal: **161 tests passed, no skips**, in 4.164 seconds. Local kernel/loopback tests ran with permission to use `sandbox-exec` and local sockets. No live model call or external service request was made.

Coverage includes:

- Real Seatbelt denial of historical source/archive reads, listings, writes and descendant access; links, renames, network ports and narrow CFPreferences access.
- A reproduced configuration-seal bypass through renaming a writable ancestor, now blocked without blocking normal sibling writes.
- Two real isolated sessions driven by a fake CLI: new HOME/CODEX_HOME/TMP, startup-only credentials, source-only TeX/Bib handoff, archive denial and cleanup. Both upstream network entry points were explicitly mocked to fail if called.
- Exact round counts, unchanged rounds, missing bibliography, CLI failure, keyboard interruption, preservation of the last revision and optional PDF failure preserving TeX/Bib.
- Unrelated project/scratch Bib files and review notes excluded from handoff; an empty root `references.bib` accepted for manuscripts without external references.
- No required report, completion marker, reviewer receipt or provenance packet. A harmless stderr message does not reject a finished session.
- Safe dependency copying, bounded public-source helper behavior, shared within-round extraction/Bib work and BibTeX mathematical/Unicode escaping.
- Candidate promotion moves the safely collected directory without changing file inodes; failed pointer publication preserves the previous current revision. The original input is retained only as revision 0000.
- Full runtime logs remain on disk while memory retains only completion/failure status and the last agent message. A final JSON event without a newline is handled.
- Plain PDF text, page ranges and layout remain supported after removing the unused coordinate/XML path. Source caching remains independent of caller mutations after removing the duplicate records list.

The skill passed `quick_validate.py`, and all its relative reference links resolve. The launcher help and `git diff --check` passed.

Limitations: the fake CLI verifies orchestration, not real model instruction-following or mathematical quality. No complete live manuscript audit was run. The OS sandbox protects local round boundaries, not server-side model state or information deliberately included in deliverable sources. A descendant that detaches from the original process group may outlive group cleanup while retaining its old sandbox restrictions; this is not VM-level process teardown. Authentication changes inside a round are intentionally not carried forward.

The pre-change working source, including uncommitted edits, was saved at `../backups/ManuscriptAgent-before-thin-20260930.tar.gz` relative to the repository root. Git internals, run outputs and Python caches were excluded.
