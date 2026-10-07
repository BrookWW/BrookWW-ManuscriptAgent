# ManuscriptAgent

[![Tests](https://github.com/BrookWW/BrookWW-ManuscriptAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/BrookWW/BrookWW-ManuscriptAgent/actions/workflows/ci.yml)

Audit and revise mathematical LaTeX manuscripts through fresh, isolated Codex sessions. ManuscriptAgent supports repeated full-paper reviews and a model-planned segmented review, saving an editable source revision after every completed audit round.

The bundled [mathematical manuscript skill](skill/mathematical-manuscript-agent/SKILL.md) handles mathematical reasoning, literature research, rewriting, and subagent review when useful. The Python runner handles dependency collection, session isolation, round scheduling, and delivery. Original manuscript files are preserved.

The skill targets the standards of Annals of Mathematics or comparable journals. Its [style preferences](skill/mathematical-manuscript-agent/references/personal-style.md) and LaTeX template reflect author preferences, not official journal requirements. A completed run is not a certificate of mathematical correctness or publication readiness.

## Requirements and setup

- **macOS** with `sandbox-exec`. There is no unconfined fallback or Linux/Windows execution mode.
- **Python 3.11 or newer**. The core runner uses the Python standard library.
- **Codex CLI**, installed and authenticated with the standard OpenAI provider and a regular `auth.json` file in its configured Codex home. Keychain-only authentication and custom model providers are not supported.
- Access to the model and reasoning effort you select.

Clone the repository, open its directory, and check the launcher:

```sh
git clone https://github.com/BrookWW/BrookWW-ManuscriptAgent.git ManuscriptAgent
cd ManuscriptAgent
./manuscript-agent --help
```

The launcher selects an available Python 3.11+ interpreter. Use `--codex /absolute/path/to/codex` if Codex is not available on `PATH`.

A TeX installation is optional: final delivery consists of source files and required assets, without an exported manuscript PDF. PDF reading tools are also optional external dependencies. The runner can use supported system/Homebrew Poppler tools, a known bundled Poppler runtime, or the bundled Python PDF text extractor when available. It skips unsupported tool locations without expanding filesystem access.

## Quick start

Run three full-paper audit-and-revision rounds:

```sh
./manuscript-agent /absolute/path/to/main.tex \
  --mode full \
  --rounds 3 \
  --model gpt-6.1-sol \
  --reasoning xhigh
```

Results are saved under this repository's `runs/<timestamp>-<id>/` directory. Full mode is the default. In full mode, omitting `--rounds` prompts for the count at the terminal.

The model and reasoning effort in these examples are explicit choices, not hard-coded requirements. If omitted, they are read from your local Codex configuration; reasoning falls back to `high` if it is not configured. A model must be specified either in the command or in that configuration.

## Recommended workflow: 3–4 full rounds, then one segmented pass

Begin with **three or four full-paper rounds** to review and revise the manuscript as a whole. Then run **one complete segmented pass** on the latest saved sources to examine focused blocks in detail. This is a recommended workflow, not an empirically established optimal number of reviews.

### 1. Full-paper review

Choose a new output directory so the second stage has a stable input path:

```sh
./manuscript-agent /absolute/path/to/main.tex \
  --mode full \
  --rounds 3 \
  --model gpt-6.1-sol \
  --reasoning xhigh \
  --output /absolute/path/to/audits/paper-full
```

For four rounds, change `--rounds 3` to `--rounds 4`. Every round receives the latest complete manuscript sources in a fresh session, audits the whole paper, and saves a new source revision when it completes successfully.

### 2. One complete segmented pass

After the three-round run finishes, review its final report and start the segmented run from its third revision:

```sh
./manuscript-agent /absolute/path/to/audits/paper-full/revisions/round-0003/main.tex \
  --mode segmented \
  --model gpt-6.1-sol \
  --reasoning xhigh \
  --output /absolute/path/to/audits/paper-segmented
```

If you ran four full rounds, use `revisions/round-0004/main.tex` instead. Replace `main.tex` with your actual entry filename, retaining any subdirectory layout. If the entry is nested within a larger project, also set `--project-root` to the corresponding revision directory and repeat any required `--asset` options.

**Do not add `--rounds 1` to the segmented command.** In segmented mode, one pass means executing the entire model-selected plan once. A separate planning session chooses the blocks and their order; the runner then starts one fresh audit session per block. `--rounds` is incompatible with segmented mode. For example, a five-block plan produces one planning session and five audit sessions.

The plan is saved as `review-plan.md`. Each block session receives the latest complete manuscript, the read-only plan, and its assigned scope. It may consult other passages and repair directly affected material. Planner edits to the manuscript are discarded. The plan stays fixed throughout the run, and the runner does not automatically append a full-paper review. See the [segmented review guide](skill/mathematical-manuscript-agent/references/segmented-review.md).

Both output directories must be new. For another attempt, choose new directory names or omit `--output` to get automatically generated names. Use an actual revision directory when continuing; do not use the `current` symlink as a project root.

## Multi-file manuscripts and assets

`--project-root` sets the LaTeX working directory and the boundary for manuscript dependencies. It defaults to the entry file's directory. Dependencies are discovered from the TeX source and retain their project-relative layout. Use repeatable `--asset` options for required files whose paths cannot be discovered, such as macro-generated asset references:

```sh
./manuscript-agent /absolute/path/to/project/paper/main.tex \
  --project-root /absolute/path/to/project \
  --asset figures/diagram.pdf \
  --asset tables/results.tex \
  --mode full \
  --rounds 3 \
  --model gpt-6.1-sol \
  --reasoning xhigh
```

Here, asset paths are relative to `--project-root`. Only add manuscript dependencies: explicitly supplied assets are carried into subsequent rounds.

Referenced `.bib` files are included automatically. If the manuscript has no external bibliography references, the skill still supplies `references.bib` at the working root; that file may be empty. Unrelated bibliography files, downloads, review notes, caches, and logs are not passed to the next round.

## Command-line options

| Option | Meaning |
|---|---|
| `input` | Entry `.tex` file |
| `--mode full` | Audit the complete paper in each round; default mode |
| `--rounds N` | Exact positive audit-round count in full mode only |
| `--mode segmented` | Plan blocks, then audit each block once in its own session |
| `--project-root PATH` | LaTeX working directory; defaults to the entry directory |
| `--asset PATH` | Required additional dependency; repeatable, relative to the project root or absolute within it |
| `--output PATH` | New run directory; defaults to `runs/<timestamp>-<id>/` |
| `--model NAME` | Override the locally configured Codex model |
| `--reasoning LEVEL` | Override the locally configured reasoning effort |
| `--timeout SECONDS` | Maximum time per Codex session, including planning; default `7200` |
| `--build-timeout SECONDS` | Maximum time per source-PDF text extraction; default `180` |
| `--codex PATH` | Codex executable; default `codex` |

The session timeout is not a total-run budget. More audit rounds or planned blocks can increase total runtime and model usage. Segmented review does not guarantee a lower token count, lower cost, or better mathematical findings.

## Outputs and recovery

| Path inside a run | Contents |
|---|---|
| `deliverables/` | Final complete TeX/Bib sources and required assets after all rounds succeed; no compiled manuscript PDF |
| `revisions/round-0000/` | Original input source bundle |
| `revisions/round-NNNN/` | Complete sources saved after each successful audit round |
| `current` | Symlink to the latest saved revision |
| `archive/round-NNNN/outcome.txt` | Round report, including a runner-generated list of files actually saved |
| `archive/round-NNNN/` | Execution logs, isolation preflight, and available multi-agent diagnostics |
| `review-plan.md` | Segmented mode only: model-selected block schedule |
| `archive/planning/` | Segmented mode only: planning logs, report, preflight, and diagnostics |
| `run.json` | Mode, round counts, status, entry path, and any execution error |

Reports identify the delivered files and rewrite local links to their saved revisions when possible. Links to unsaved temporary material are marked as not delivered. The runner-generated saved-file list is authoritative; a model's delivery claim alone is not.

A failed model session, timeout, interruption, missing TeX/Bib output, or unsafe dependency prevents that round from being counted. Earlier completed revisions remain available, and failed work is preserved where it can be read safely. Inspect `run.json` and that round's archive, then start a new run from the last completed revision. `deliverables/` is exported only after all requested audit rounds finish successfully.

Full mode runs the exact requested number of rounds even when a completed round makes no changes. In segmented mode, the planner is not counted as an audit round. Invalid or unreadable plans stop the run before auditing; the plan's syntax is checked, but its mathematical decomposition is not certified.

## Isolation, local data, and review limits

Every planning or audit session has its own writable manuscript copy, private home, Codex state, and temporary directory. A macOS Seatbelt preflight checks the boundary before the model starts. Earlier archives, the original project, and unrelated user files are denied. The fixed skill, configuration, and review plan are read-only. Previous conversations, memories, plugins, and host bridges are not loaded; native subagents inherit the same session boundary.

Model traffic uses a fixed-destination OpenAI relay. Optional literature downloads use an authenticated HTTPS broker with public-address and redirect checks. The relay restricts network destinations, not HTTP origins behind shared CDN addresses. Some macOS system/runtime files and required preferences services remain shared. The runner does not provide a virtual machine or isolation from model/server-side state, and text deliberately placed in the manuscript carries forward with it.

Trusted startup credentials are copied separately into each session. Agent-written authentication changes are not passed to later sessions or written back to the user's login. If authentication expires, log in again and restart from a completed revision. After each session the runner terminates its process group, closes proxies, and removes private directories. A deliberately detached descendant may outlive its original process group while remaining subject to its original sandbox restrictions.

Run archives can contain manuscript text, model responses, and native session logs. Keep them private. The repository excludes `runs/`, agent state, and caches from version control; do not force-add generated archives. Custom `--output` directories outside `runs/` also need to remain outside published source control or be explicitly ignored.

The best-effort multi-agent observer saves available session JSONL files and `agent-diagnostics.json` in each session's archive before cleanup. It records evidence of agent creation, execution, completion, and result receipt. Observation is bounded and informational: missing or partial logs do not prove that collaboration was absent, and observed collaboration does not certify mathematical quality. Authentication files and the entire private Codex home are not archived by this observer. Diagnostic archives are not passed to later rounds.

The runner verifies orchestration and safe source delivery. Mathematical conclusions, reference accuracy, completeness of proofs, and adherence to editorial instructions still require human review.

## Development and validation

The [Tests workflow](https://github.com/BrookWW/BrookWW-ManuscriptAgent/actions/workflows/ci.yml) runs on every push and pull request, and can also be started manually from the Actions tab. It uses macOS 15 and Python 3.12, enables real kernel-isolation tests, checks the executable launcher, and rejects tracked manuscript runs, agent caches, and credentials. The workflow needs no model credentials and does not make live model calls.

The full test suite is discovered. On a clean GitHub runner, two optional integration checks normally report skips: the native standalone Codex configuration check and the bundled PDF-runtime check. They require locally installed runtimes that CI does not provision. The other kernel-isolation and synthetic full/segmented audit tests still run. Check the test step's log for the actual pass, failure, and skip counts; a green badge is a software-test result, not a mathematical audit.

Run the test suite with Python 3.11 or newer, without generating bytecode caches:

```sh
python3 -B -m unittest discover -s tests -v
```

To include macOS kernel-isolation tests:

```sh
AUDITAGENT_SANDBOX_TESTS=1 python3 -B -m unittest discover -s tests -v
```

Kernel tests require permission to invoke `sandbox-exec` and bind local loopback sockets. Tests use synthetic manuscripts and fake model sessions; they do not run a live mathematical audit. See the [validation notes](docs/validation.md) for recorded results and limitations.
