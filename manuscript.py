#!/usr/bin/env python3
"""Thin skill-driven manuscript runner with isolated rounds and TeX/Bib delivery."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from urllib.parse import quote, unquote, urlsplit

from bundle import BundleError, create_bundle
from isolation import IsolationError, Seatbelt, SYSTEM_READ_ROOTS
from network import CodexProxy
from source_fetch import SourceFetcher
from safeio import SafeIOError, safe_copy_tree, safe_read, safe_is_file


HERE = Path(__file__).resolve().parent
SKILL = HERE / "skill/mathematical-manuscript-agent"


class AuditError(RuntimeError):
    pass


def save_json(path: Path, value: dict, *, primary_error: BaseException | None = None) -> None:
    try:
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        temp.replace(path)
    except OSError as exc:
        if primary_error is None:
            raise
        primary_error.add_note(f"Could not save {path}: {exc}")


def close_resources(resources: ExitStack, primary_error: BaseException | None = None) -> None:
    """Attempt every registered cleanup without replacing an active error."""
    try:
        resources.close()
    except BaseException as exc:
        if primary_error is None:
            raise
        primary_error.add_note(f"Cleanup also failed: {exc}")


def copy_tree(source: Path, destination: Path) -> None:
    """Copy bytes, never archive aliases, links, devices, or FIFOs."""
    safe_copy_tree(source, destination)


def kill_group(process: subprocess.Popen) -> None:
    # Descendants may still exist after the main process has exited.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            # macOS may deny a second group signal after the confined leader
            # has exited. Do not turn an already-finished cancellation into an
            # unrelated permission failure. A live direct child still gets its
            # explicit signal; do not silently leave that process running.
            if process.poll() is None:
                try:
                    process.send_signal(sig)
                except ProcessLookupError:
                    break
            else:
                break
        if sig == signal.SIGTERM:
            time.sleep(0.15)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def execute(command: list[str], *, cwd: Path, env: dict[str, str],
            timeout: float, log_dir: Path, stdin: str = "", live: bool = False) -> tuple[int, dict]:
    """Only pipes cross the boundary; the unsandboxed parent writes all logs."""
    log_dir.mkdir(parents=True, exist_ok=True)
    summary = {"completed": False, "failed": False, "text": ""}

    def consume(line: bytes) -> None:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(event, dict):
            return
        kind, item = event.get("type"), event.get("item", {})
        if kind == "turn.completed":
            summary["completed"] = True
        elif kind == "turn.failed":
            summary["failed"] = True
        if kind in {"error", "turn.failed"}:
            print(f"  Codex error: {str(event)[:500]}", flush=True)
        elif kind == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            message = item.get("text", "")
            if isinstance(message, str):
                summary["text"] = message
                print("  " + message[:1500], flush=True)

    started = time.monotonic()
    last_progress = started
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               close_fds=True, start_new_session=True)
    resources = ExitStack()
    # Close pipes even if terminating the process group fails.
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream:
            resources.callback(stream.close)
    resources.callback(kill_group, process)
    pending = b""
    primary_error = None
    try:
        assert process.stdin and process.stdout and process.stderr
        try:
            process.stdin.write(stdin.encode())
            process.stdin.close()
        except BrokenPipeError:
            pass
        with selectors.DefaultSelector() as selector, \
                (log_dir / ("events.jsonl" if live else "stdout.log")).open("wb") as out, \
                (log_dir / "stderr.log").open("wb") as err:
            for stream, name, log in [(process.stdout, "stdout", out), (process.stderr, "stderr", err)]:
                selector.register(stream, selectors.EVENT_READ, (name, log))
            while selector.get_map():
                if time.monotonic() - started > timeout:
                    raise AuditError(f"Process timed out after {timeout:g} seconds; see {log_dir}")
                if live and time.monotonic() - last_progress >= 60:
                    print(f"  Codex is running ({int(time.monotonic() - started)} seconds elapsed).", flush=True)
                    last_progress = time.monotonic()
                for key, _ in selector.select(timeout=0.25):
                    name, log = key.data
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        if live and name == "stdout" and pending:
                            consume(pending)
                            pending = b""
                        selector.unregister(key.fileobj)
                        continue
                    log.write(chunk)
                    log.flush()
                    if live and name == "stdout":
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            consume(line)
            code = process.wait(timeout=5)
        save_json(log_dir / "process.json", {"exit_code": code,
                  "elapsed_seconds": round(time.monotonic() - started, 2)})
        return code, summary
    except BaseException as exc:
        primary_error = exc
        save_json(log_dir / "process.json", {"exit_code": process.poll(),
                  "status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                  "error": str(exc) or type(exc).__name__,
                  "elapsed_seconds": round(time.monotonic() - started, 2)}, primary_error=exc)
        raise
    finally:
        close_resources(resources, primary_error)


def local_defaults() -> tuple[str | None, str]:
    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    try:
        config = tomllib.loads((source / "config.toml").read_text())
    except (OSError, ValueError):
        config = {}
    if config.get("model_provider") not in (None, "openai"):
        raise AuditError("This version supports the standard OpenAI Codex provider only.")
    return config.get("model"), config.get("model_reasoning_effort", "high")


def private_environment(home: Path, temp: Path) -> dict[str, str]:
    # Do not inherit proxies, shell startup injection, API keys, old sessions,
    # plugin/daemon sockets, Python paths, or parent Codex task identifiers.
    return {"HOME": str(home), "CODEX_HOME": str(home / ".codex"),
            "TMPDIR": str(temp), "TMP": str(temp), "TEMP": str(temp),
            # zsh heredocs use TMPPREFIX independently of TMPDIR. Keep their
            # temporary files inside the existing session write boundary.
            "TMPPREFIX": str(temp / "zsh"),
            "PATH": ":".join([str(Path(sys.executable).parent), "/usr/bin", "/bin",
                              "/usr/sbin", "/sbin", "/Library/TeX/texbin"]),
            "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TERM": "dumb",
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "TEXMFHOME": str(home / "texmf"), "TEXMFVAR": str(home / "texmf-var"),
            "TEXMFCONFIG": str(home / "texmf-config")}


def codex_runtime_roots(executable: Path) -> list[Path]:
    """Allow the selected runtime package, never the user's Codex state tree."""
    executable = executable.resolve()
    package = executable.parent.parent
    manifest = package / "codex-package.json"
    if not manifest.exists():
        return [executable.parent]
    if manifest.is_symlink() or not manifest.is_file():
        raise AuditError("Codex package metadata must be a regular file.")
    try:
        metadata = json.loads(manifest.read_text())
        entry = Path(metadata["entrypoint"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AuditError("Could not validate Codex package metadata.") from exc
    if (metadata.get("layoutVersion") != 1 or metadata.get("variant") != "codex"
            or entry.is_absolute() or ".." in entry.parts
            or (package / entry).resolve() != executable
            or not executable.is_relative_to(package)):
        raise AuditError("Codex package metadata does not identify the selected executable.")
    return [package]



def pdf_tools(control: Path) -> tuple[dict, list[Path]]:
    """Optional reading tools; TeX/Bib delivery has no PDF-tool dependency."""
    runtime = Path.home().resolve() / '.cache/codex-runtimes/codex-primary-runtime/dependencies'
    poppler = runtime / 'native/poppler'
    found, roots = {}, []
    for name in ('pdftotext', 'pdftoppm', 'pdfinfo'):
        path = shutil.which(name)
        candidates = ([Path(path)] if path else []) + [poppler / 'bin' / name]
        for candidate in candidates:
            candidate = candidate.resolve()
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            # Discover commands freely, but never infer permissions from their
            # parent directories. Check real targets, including symlink targets.
            if any(candidate.is_relative_to(root) for root in SYSTEM_READ_ROOTS):
                pass  # Already readable under the existing OS policy.
            elif candidate.is_relative_to(poppler):
                roots.append(poppler)
            else:
                continue
            found[name] = str(candidate)
            break
        else:
            if path:
                print(f'Ignoring unsupported PDF tool: {path}; use a standard system installation '
                      'or the bundled PDF runtime.', file=sys.stderr)
    command = [found['pdftotext']] if 'pdftotext' in found else []
    python_root = runtime / 'python'
    python = python_root / 'bin/python3'
    if (not command and python.is_file() and python.resolve().is_relative_to(python_root)
            and list(python_root.glob('lib/python*/site-packages/pdfplumber/__init__.py'))):
        command = [str(python.resolve()), '-B', str(control / 'pdf_text.py')]
        roots.append(python_root.resolve())
    return {**found, 'pdf_text_command': command}, list(dict.fromkeys(roots))


class Session:
    """One fresh private home and process-tree OS boundary per round."""

    def __init__(self, args, state: Path, source_root: Path, credential_bytes: bytes | None):
        self.args = args
        self._resources = ExitStack()
        self.root = Path(tempfile.mkdtemp(prefix='manuscript-agent-', dir='/private/tmp')).resolve()
        self._resources.callback(shutil.rmtree, self.root)
        self.proxy = self.source_fetcher = None
        try:
            self._prepare(state, source_root, credential_bytes)
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def _prepare(self, state, source_root, credential_bytes):
        self.work, self.home = self.root / 'manuscript', self.root / 'home'
        self.temp, self.control = self.root / 'tmp', self.root / 'control'
        for path in (self.work, self.home, self.temp, self.control):
            path.mkdir(parents=True)
        self.env = private_environment(self.home, self.temp)
        policy = dict(
            read_roots=[self.work, self.home, self.temp, self.control, Path(sys.prefix).resolve()],
            write_roots=[self.work, self.home, self.temp],
            deny_roots=[state, source_root], deny_write_roots=[self.control])
        if credential_bytes is not None:
            roots, config = self._prepare_codex(credential_bytes)
            policy['read_roots'].extend(roots)
            policy['deny_write_roots'].append(config)
            policy.update(allow_macos_preferences_sync=True,
                          proxy_port=self.proxy.port, source_port=self.source_fetcher.port)
        self.policy = Seatbelt(**policy)

    def _prepare_codex(self, credential_bytes):
        self.tools, tool_roots = pdf_tools(self.control)
        for name in ('pdftoppm', 'pdfinfo'):
            if name in self.tools:
                self.env['MANUSCRIPT_' + name.upper()] = self.tools[name]
        ca_source = os.environ.get('CODEX_CA_CERTIFICATE') or os.environ.get('SSL_CERT_FILE')
        trust = ssl.create_default_context(cafile=ca_source)
        ca_bundle = self.control / 'ca-bundle.pem'
        ca_bundle.write_text(''.join(ssl.DER_cert_to_PEM_cert(cert)
                                    for cert in trust.get_ca_certs(binary_form=True)))
        if not ca_bundle.stat().st_size:
            raise AuditError('No trusted CA certificates are available for isolated Codex.')
        self.env['CODEX_CA_CERTIFICATE'] = self.env['SSL_CERT_FILE'] = str(ca_bundle)
        self.codex = Path(shutil.which(self.args.codex) or self.args.codex).resolve()
        if not self.codex.is_file():
            raise AuditError(f'Codex executable not found: {self.args.codex}')
        config = self.home / '.codex/config.toml'
        config.parent.mkdir()
        config.write_text('''cli_auth_credentials_store = "file"
            allow_login_shell = false
            project_doc_max_bytes = 0
            web_search = "live"
            check_for_update_on_startup = false
            [memories]
            use_memories = false
            generate_memories = false
            [features]
            multi_agent = true
            memories = false
            apps = false
            plugins = false
            hooks = false
            browser_use = false
            browser_use_external = false
            computer_use = false
            in_app_browser = false
            remote_control = false
            worktrees = false
            shell_snapshot = false
            workspace_dependencies = false
        ''')
        auth = self.home / '.codex/auth.json'
        auth.write_bytes(credential_bytes)
        auth.chmod(0o600)
        self.proxy, self.source_fetcher = CodexProxy(), SourceFetcher()
        for service in (self.proxy, self.source_fetcher):
            # Register before entering so partially started services also close.
            self._resources.callback(service.close)
            service.__enter__()
        for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
            self.env[key] = self.proxy.url
        return [*codex_runtime_roots(self.codex), *tool_roots], config

    def prepare_round(self, resources: Path, entry: str, final_round: bool, *,
                      planning: bool = False, plan: str | None = None,
                      block: str | None = None) -> str:
        copy_tree(resources, self.control / 'skill')
        for name in ('source_tools.py', 'safeio.py', 'zbmath.py', 'pdf_text.py'):
            shutil.copyfile(HERE / name, self.control / name)
        save_json(self.control / 'round-config.json', {
            'work_root': str(self.work), 'build_timeout': self.args.build_timeout,
            **self.tools, **self.source_fetcher.config})
        scope = 'Audit and revise the complete manuscript.'
        if plan is not None:
            plan_path = self.control / 'review-plan.md'
            plan_path.write_text(plan, encoding='utf-8')
            scope = f'Review plan: {plan_path}\nCurrent block:\n{block}'
        template = 'plan.txt' if planning else 'round.txt'
        prompt = (HERE / 'prompts' / template).read_text()
        substitutions = {'{{ENTRYPOINT}}': shlex.quote(entry),
                         '{{RESOURCE_ROOT}}': str(self.control / 'skill'),
                         '{{PYTHON}}': sys.executable,
                         '{{SOURCE_TOOL}}': str(self.control / 'source_tools.py'),
                         '{{FINAL_ROUND}}': str(final_round).lower(),
                         '{{MODE}}': 'segmented' if planning or plan is not None else 'full',
                         '{{SCOPE}}': scope}
        # Substitute only template fields, never placeholders inside model text.
        return re.sub(r'\{\{[A-Z_]+\}\}', lambda match: substitutions[match.group()], prompt)

    def run_codex(self, prompt: str, log: Path) -> str:
        final = self.work / '.audit-work/final.txt'
        final.parent.mkdir(exist_ok=True)
        command = [str(self.codex), 'exec', '--skip-git-repo-check',
                   # The entire CLI already runs inside our OS sandbox.
                   '--dangerously-bypass-approvals-and-sandbox', '--json', '--color', 'never',
                   '-C', str(self.work), '--model', self.args.model,
                   '-c', 'model_reasoning_effort=' + json.dumps(self.args.reasoning),
                   '-o', str(final), '-']
        code, summary = execute(self.policy.wrap(command), cwd=self.work, env=self.env,
                               timeout=self.args.timeout, log_dir=log, stdin=prompt, live=True)
        if code or summary['failed']:
            raise AuditError(f'Codex did not finish (exit {code}); see {log}')
        if not summary['completed']:
            raise AuditError(f'Codex ended without a completed turn; see {log}')
        # Natural-language outcome only; no marker, JSON schema, or receipt.
        if safe_is_file(self.work, '.audit-work/final.txt'):
            return safe_read(self.work, '.audit-work/final.txt').decode(errors='replace')
        return summary['text']

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        close_resources(self._resources, exc)

    def close(self):
        self._resources.close()


def probe(session: Session, state: Path, log: Path) -> None:
    # Test the actual kernel boundary without consuming a model turn.
    canary = state / 'isolation-canary.txt'
    if not canary.exists():
        canary.write_text(uuid.uuid4().hex)
    checks = session.policy.preflight(session.work, canary, sys.executable, session.env)
    save_json(log, checks)


def collect_candidate(work: Path, entry: str, destination: Path, extras: list[str]) -> None:
    if not safe_read(work, entry).strip():
        raise AuditError('The output TeX entry is empty.')
    create_bundle(work / entry, work, destination, [work / name for name in extras])
    # Only bibliography dependencies cross rounds. A manuscript with no external
    # citations may deliver an empty, explicitly named references.bib instead.
    if not any(destination.rglob('*.bib')):
        if not safe_is_file(work, 'references.bib'):
            raise AuditError('No bibliography output: reference a .bib from TeX or provide references.bib.')
        (destination / 'references.bib').write_bytes(safe_read(work, 'references.bib'))


def promote(state: Path, candidate: Path, number: int) -> Path:
    """Move a safely collected candidate, then atomically publish its pointer."""
    revision = state / 'revisions' / f'round-{number:04d}'
    revision.parent.mkdir(exist_ok=True)
    if revision.exists() or revision.is_symlink():
        raise AuditError(f'Revision already exists: {revision}')
    candidate.rename(revision)
    link = state / '.current-next'
    link.symlink_to(revision.relative_to(state), target_is_directory=True)
    link.replace(state / 'current')
    return revision


def format_outcome(outcome: str, work: Path, revision: Path | None = None) -> str:
    """List published files and repair model links using only that inventory."""
    files = {path.relative_to(revision).as_posix(): path
             for path in sorted(revision.rglob('*')) if path.is_file()} if revision else {}

    def file_link(label: str, path: Path, suffix: str = '') -> str:
        label = label.replace('[', r'\[').replace(']', r'\]')
        return f'[{label}](<{quote(path.as_posix(), safe="/:")}{suffix}>)'

    def rewrite(match: re.Match) -> str:
        label, target = match.group(1), match.group(2).strip('<>')
        target = re.sub(r'\\([()\\ ])', r'\1', target)
        if target.startswith('#'):
            return match.group()
        if target.startswith('file://'):
            try:
                url = urlsplit(target)
            except ValueError:
                return f'{label} (invalid file link; not delivered)'
            if url.netloc not in ('', 'localhost'):
                return match.group()
            target = url.path + ('#' + url.fragment if url.fragment else '')
        target, separator, fragment = target.partition('#')
        suffix = separator + fragment
        line = re.search(r':\d+(?::\d+)?$', target)
        if line:
            suffix = line.group() + suffix
            target = target[:line.start()]
        if re.match(r'[A-Za-z][A-Za-z0-9+.-]*:', target):
            return match.group()  # Web, email and other non-file links.
        path = Path(unquote(target))
        # macOS may display /private/tmp as /tmp. Avoid resolving model paths.
        if str(work).startswith('/private/tmp/') and str(path).startswith('/tmp/'):
            path = Path('/private' + str(path))
        if path.is_absolute():
            if not path.is_relative_to(work.parent):
                return match.group()
            if not path.is_relative_to(work):
                return f'{label} (temporary file; not delivered)'
            path = path.relative_to(work)
        saved = files.get(path.as_posix())
        if saved is None:
            return f'{label} (temporary file; not delivered)'
        return file_link(label, saved, suffix)

    # Inline Markdown links, including angle-bracket paths and filename parentheses.
    links = r'!?\[([^\]\n]*)\]\(\s*(<[^>\n]+>|(?:\\.|[^()\s]|\([^()\n]*\))+)(?:[ \t]+"[^"\n]*")?\s*\)'
    notes = re.sub(links, rewrite, outcome)
    if revision is None:
        heading = 'No source revision confirmed. Diagnostic notes only.'
    else:
        heading = 'Saved source files (authoritative delivery list):\n\n' + '\n'.join(
            '- ' + file_link(name, path) for name, path in files.items())
    return (heading + '\n\nModel notes (delivery claims are not authoritative; '
            'temporary files are not delivered):\n\n' + notes + '\n')


def read_credentials() -> bytes:
    source = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    try:
        return safe_read(source, 'auth.json')
    except SafeIOError as exc:
        raise AuditError('A regular Codex auth.json is required; run codex login first.') from exc


def save_failed_workspace(work: Path, destination: Path, status: dict) -> None:
    """Keep diagnostic files before the session's context removes its workspace."""
    try:
        copy_tree(work, destination)
    except (OSError, SafeIOError) as exc:
        status['snapshot_error'] = str(exc)


def observe_agents(session, round_dir: Path) -> None:
    """Optional diagnostics cannot change the result of the manuscript round."""
    try:
        from agent_observer import observe
        result = observe(session.home / '.codex', round_dir)
        print(f"  Multi-agent observation: {result['status']}; "
              f"{result.get('created_count', 0)} created, "
              f"{result.get('completed_count', 0)} completed. "
              f"Details: {round_dir / 'agent-diagnostics.json'}", flush=True)
    except Exception:
        # Even import, unexpected parser, or console errors are non-blocking.
        pass


def export_deliverables(state: Path, status: dict) -> None:
    """Export the last successful source revision and its required assets."""
    delivery = state / 'deliverables'
    copy_tree((state / 'current').resolve(), delivery)
    status['deliverables'] = str(delivery)


def parse_review_plan(plan: str) -> list[str]:
    """Read ordered Markdown blocks; their mathematical scope belongs to the model."""
    headings = []
    offset = 0
    fence = None
    for line in plan.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if marker:
            delimiter, tail = marker.groups()
            if fence is None:
                fence = delimiter
            elif delimiter[0] == fence[0] and len(delimiter) >= len(fence) and not tail.strip():
                fence = None
        elif fence is None:
            heading = re.match(r'^##[ \t]+([0-9]+)\.[ \t]+\S', line)
            if heading:
                headings.append((int(heading.group(1)), offset))
        offset += len(line)
    if not headings or [number for number, _ in headings] != list(range(1, len(headings) + 1)):
        raise AuditError('Review plan must contain consecutive Markdown headings '
                         '"## 1. Title", "## 2. Title", and so on. No audit rounds were started.')
    starts = [position for _, position in headings] + [len(plan)]
    return [plan[start:end].strip() for start, end in zip(starts, starts[1:])]


def plan_review(args, state: Path, source_root: Path, credentials: bytes,
                resources: Path, entry: str, status: dict) -> tuple[str, list[str]]:
    """Use a separate isolated session; carry forward only its short review plan."""
    directory = state / 'archive/planning'
    directory.mkdir()
    status.update(status='planning')
    save_json(state / 'run.json', status)
    print('Planning: fresh isolated session to choose audit blocks.', flush=True)
    with Session(args, state, source_root, credentials) as session:
        try:
            session.work.rmdir()
            copy_tree((state / 'current').resolve(), session.work)
            probe(session, state, directory / 'isolation.json')
            prompt = session.prepare_round(resources, entry, False, planning=True)
            outcome = session.run_codex(prompt, directory / 'codex')
            (directory / 'outcome.txt').write_text(format_outcome(outcome, session.work))
            plan = safe_read(session.work, '.audit-work/review-plan.md', max_bytes=64 * 1024).decode('utf-8')
            blocks = parse_review_plan(plan)
            (state / 'review-plan.md').write_text(plan, encoding='utf-8')
            return plan, blocks
        except BaseException:
            save_failed_workspace(session.work, directory / 'failed-workspace', status)
            raise
        finally:
            observe_agents(session, directory)


def run(args) -> int:
    mode = getattr(args, 'mode', 'full')
    if mode == 'segmented' and args.rounds is not None:
        raise AuditError('--rounds is only available in full mode; segmented mode uses the model plan.')
    input_path = args.input.absolute()
    source_root = (args.project_root or input_path.parent).absolute()
    if input_path.is_symlink() or source_root.is_symlink():
        raise AuditError('The input and project root must not be symlinks.')
    source_root = source_root.resolve()

    state = args.output.absolute()
    state.mkdir(parents=True, mode=0o700, exist_ok=False)
    state = state.resolve()
    archive = state / 'archive'
    archive.mkdir()
    status = {'status': 'preparing', 'mode': mode, 'requested_rounds': args.rounds,
              'planned_rounds': args.rounds, 'completed_rounds': 0,
              'active_round': None, 'entry': None, 'model': args.model, 'reasoning': args.reasoning}
    save_json(state / 'run.json', status)
    try:
        # Reuse only trusted startup credentials, never state written by a prior round.
        credentials = read_credentials()
        extras = [p if p.is_absolute() else source_root / p for p in args.asset]
        extra_rel = [p.relative_to(source_root).as_posix() for p in extras]
        baseline = state / 'input'
        entry = create_bundle(input_path, source_root, baseline, extras)
        status['entry'] = entry
        promote(state, baseline, 0)
        resources = state / 'skill'
        copy_tree(SKILL, resources)

        plan, blocks = None, []
        rounds = args.rounds
        if mode == 'segmented':
            plan, blocks = plan_review(args, state, source_root, credentials, resources, entry, status)
            rounds = len(blocks)
            status['planned_rounds'] = rounds
            save_json(state / 'run.json', status)
            print(f'Planned {rounds} audit blocks. Review plan: {state / "review-plan.md"}', flush=True)

        for number in range(1, rounds + 1):
            status.update(status='running', active_round=number)
            save_json(state / 'run.json', status)
            round_dir = archive / f'round-{number:04d}'
            round_dir.mkdir()
            print(f'Round {number}/{rounds}: fresh isolated skill session.', flush=True)
            with Session(args, state, source_root, credentials) as session:
                try:
                    # Only manuscript dependencies enter the writable workspace.
                    session.work.rmdir()
                    copy_tree((state / 'current').resolve(), session.work)
                    probe(session, state, round_dir / 'isolation.json')
                    if mode == 'segmented':
                        prompt = session.prepare_round(resources, entry, number == rounds,
                                                       plan=plan, block=blocks[number - 1])
                    else:
                        prompt = session.prepare_round(resources, entry, number == rounds)
                    outcome = session.run_codex(prompt, round_dir / 'codex')
                    report = round_dir / 'outcome.txt'
                    report.write_text(format_outcome(outcome, session.work))
                    candidate = round_dir / 'candidate'
                    collect_candidate(session.work, entry, candidate, extra_rel)
                    revision = promote(state, candidate, number)
                    # Completion records publication, not a proof of mathematical correctness.
                    status['completed_rounds'] = number
                    save_json(state / 'run.json', status)
                    report.write_text(format_outcome(outcome, session.work, revision))
                except BaseException:
                    save_failed_workspace(session.work, round_dir / 'failed-workspace', status)
                    raise
                finally:
                    observe_agents(session, round_dir)

        export_deliverables(state, status)
        status.update(status='completed', active_round=None)
        save_json(state / 'run.json', status)
    except BaseException as exc:
        status.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                      error=str(exc) or type(exc).__name__)
        save_json(state / 'run.json', status, primary_error=exc)
        raise
    print(f'Finished {rounds} rounds. TeX/Bib: {state / "deliverables"}', flush=True)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path, help='Entry .tex file; the original is never modified')
    p.add_argument('--mode', choices=['full', 'segmented'], default='full',
                   help='Full-paper rounds (default), or model-planned blocks with one round per block')
    p.add_argument('--rounds', type=int, help='Exact number of audit rounds in full mode; incompatible with segmented mode')
    p.add_argument('--project-root', type=Path, help='LaTeX working directory (default: entry directory)')
    p.add_argument('--output', type=Path, help='New output directory (default: runs/<timestamp>-<id>)')
    p.add_argument('--asset', type=Path, action='append', default=[], help='Necessary dependency relative to project root; repeatable')
    p.add_argument('--model', help='Codex model (default: configured model)')
    p.add_argument('--reasoning', help='Reasoning effort (default: configured effort)')
    p.add_argument('--timeout', type=float, default=7200, help='Seconds per Codex session')
    p.add_argument('--build-timeout', type=float, default=180, help='Seconds per source PDF text extraction')
    p.add_argument('--codex', default='codex', help='Codex executable')
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.mode == 'segmented' and args.rounds is not None:
            raise AuditError('--rounds is only available in full mode; segmented mode uses the model plan.')
        if sys.platform != 'darwin':
            raise AuditError('Strict isolation supports macOS only; no unconfined fallback.')
        if args.timeout <= 0 or args.build_timeout <= 0:
            raise AuditError('Timeouts must be positive.')
        args.output = args.output or HERE / 'runs' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        model, reasoning = local_defaults()
        args.model, args.reasoning = args.model or model, args.reasoning or reasoning
        if not args.model:
            raise AuditError('Specify --model or configure a default model in Codex.')
        if args.mode == 'full':
            if args.rounds is None:
                args.rounds = int(input('Number of isolated rounds: '))
            if args.rounds < 1:
                raise AuditError('Rounds must be positive.')
        return run(args)
    except KeyboardInterrupt as exc:
        print('Interrupted. The last completed source revision is preserved.', file=sys.stderr)
        for note in getattr(exc, '__notes__', ()):
            print(note, file=sys.stderr)
        return 130
    except (AuditError, IsolationError, BundleError, SafeIOError, OSError, ValueError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        for note in getattr(exc, '__notes__', ()):
            print(note, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
