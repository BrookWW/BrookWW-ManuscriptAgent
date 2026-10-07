"""Process-tree archive isolation using the macOS kernel Seatbelt sandbox.

The caller must use fresh, copied files, close inherited descriptors, and expose
no unsandboxed tools. Network access, if needed, goes through a trusted CONNECT
relay and the bounded public-source service on explicitly selected localhost
ports. There is no unsandboxed fallback.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Sequence


class IsolationError(RuntimeError):
    """The required operating-system isolation could not be established."""


SYSTEM_READ_ROOTS = (
    "/System", "/usr", "/bin", "/sbin", "/Library", "/private/etc",
    "/private/var/db", "/private/var/select", "/opt/homebrew",
)


def _paths(values: Sequence[Path | str]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(Path(value).resolve() for value in values))


def _subpaths(values: Sequence[Path]) -> str:
    return " ".join(f"(subpath {json.dumps(str(value))})" for value in values)


class Seatbelt:
    """Immutable inline policy inherited by Codex and every descendant.

    Metadata traversal outside allowed roots is needed by macOS runtimes. File
    contents and executable mappings are allowlisted; deny_roots additionally
    block every read operation, including metadata, regardless of the allowlist.
    """

    def __init__(
        self,
        read_roots: Sequence[Path | str],
        write_roots: Sequence[Path | str],
        deny_roots: Sequence[Path | str] = (),
        deny_write_roots: Sequence[Path | str] = (),
        proxy_port: int | None = None,
        source_port: int | None = None,
        allow_macos_preferences_sync: bool = False,
    ) -> None:
        if platform.system() != "Darwin" or not os.access("/usr/bin/sandbox-exec", os.X_OK):
            raise IsolationError("Strict isolation requires macOS /usr/bin/sandbox-exec.")
        for name, port in (("proxy_port", proxy_port), ("source_port", source_port)):
            if port is not None and (type(port) is not int or not 1 <= port <= 65535):
                raise ValueError(f"{name} must be between 1 and 65535.")
        if type(allow_macos_preferences_sync) is not bool:
            raise ValueError("allow_macos_preferences_sync must be a boolean.")
        self.read_roots = _paths((*SYSTEM_READ_ROOTS, *read_roots, *write_roots))
        self.write_roots = _paths(write_roots)
        self.deny_roots = _paths(deny_roots)
        self.deny_write_roots = _paths(deny_write_roots)
        if any(root == Path("/") for root in (*self.read_roots, *self.write_roots)):
            raise IsolationError("The filesystem root cannot be an allowed subtree.")
        self.proxy_port = proxy_port
        self.source_port = source_port
        self.allow_macos_preferences_sync = allow_macos_preferences_sync
        device_reads = " ".join(
            f'(literal "{path}")' for path in
            ("/", "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/fd")
        )
        # Descriptor-based no-follow readers must open each directory ancestor.
        # On this macOS, even O_EVTONLY ancestor opens require file-read-data.
        # Grant only each exact directory entry, never its subtree; explicit
        # deny_roots below still forbid archives and their directory listings.
        ancestors = sorted({parent for root in self.read_roots for parent in root.parents}, key=str)
        directory_reads = " ".join(f"(literal {json.dumps(str(path))})" for path in ancestors)
        read_filter = f"(require-any {device_reads} {directory_reads} (subpath \"/dev/fd\") {_subpaths(self.read_roots)})"
        write_filter = f'(require-any (literal "/dev/null") {_subpaths(self.write_roots)})'
        if allow_macos_preferences_sync:
            # Recent Codex builds refresh managed macOS preferences at thread
            # startup. These two daemon endpoints and their exact read-only
            # state-cache objects are sufficient; no preference permission,
            # cache write, general IPC, or filesystem exception is granted.
            # Unexpected future cache names remain denied.
            mach_rules = (
                "(deny mach-priv* mach-task-name mach-task-inspect)\n"
                "(deny mach-lookup (require-not (require-any "
                '(global-name "com.apple.cfprefsd.agent") '
                '(global-name "com.apple.cfprefsd.daemon"))))'
            )
            ipc_rules = (
                "(deny ipc-sysv* iokit* user-preference*)\n"
                "(deny ipc-posix-sem* ipc-posix-shm-write*)\n"
                "(deny ipc-posix-shm-read* (require-not (require-any "
                f'(ipc-posix-name "apple.cfprefs.{os.getuid()}v1") '
                '(ipc-posix-name "apple.cfprefs.daemonv1"))))'
            )
        else:
            mach_rules = "(deny mach-lookup mach-priv* mach-task-name mach-task-inspect)"
            ipc_rules = "(deny ipc-posix* ipc-sysv* iokit* user-preference*)"
        lines = [
            "(version 1)", "(allow default)",
            f"(deny file-read-data file-map-executable (require-not {read_filter}))",
            f"(deny file-write* (require-not {write_filter}))",
            "(deny file-link file-issue-extension file-mount file-unmount)",
            mach_rules,
            "(deny appleevent-send lsopen authorization-right-obtain)",
            ipc_rules,
            "(deny process-info* (require-not (target self)))",
            "(deny signal (require-not (target same-sandbox)))",
            "(deny network-inbound network-bind)",
        ]
        ports = sorted({port for port in (proxy_port, source_port) if port is not None})
        if not ports:
            lines.append("(deny network-outbound)")
        else:
            destinations = " ".join(f'(remote tcp "localhost:{port}")' for port in ports)
            lines.append(f'(deny network-outbound (require-not (require-any {destinations})))')
        if self.deny_roots:
            lines.append(f"(deny file-read* file-map-executable file-write* {_subpaths(self.deny_roots)})")
        if self.deny_write_roots:
            lines.append(f"(deny file-write* {_subpaths(self.deny_write_roots)})")
        # A path-based seal alone can be bypassed by renaming a writable parent
        # directory, moving its protected descendants away from their denied
        # paths. Keep each such ancestor in place while permitting ordinary
        # sibling files (for example Codex session data) to remain writable.
        sealed_ancestors = sorted({
            parent
            for root in (*self.deny_roots, *self.deny_write_roots)
            for parent in root.parents
            if any(parent.is_relative_to(writable) for writable in self.write_roots)
        }, key=str)
        if sealed_ancestors:
            literals = " ".join(f"(literal {json.dumps(str(path))})" for path in sealed_ancestors)
            lines.append(f"(deny file-write-unlink {literals})")
        self.profile = "\n".join(lines) + "\n"

    def wrap(self, argv: Sequence[str]) -> list[str]:
        if not argv:
            raise ValueError("A sandboxed command is required.")
        return ["/usr/bin/sandbox-exec", "-p", self.profile, *map(str, argv)]

    def preflight(
        self,
        work_dir: Path,
        canary_file: Path,
        python_executable: str = sys.executable,
        env: dict[str, str] | None = None,
    ) -> dict[str, bool]:
        """Probe a caller-owned synthetic canary; never inspect real history.

        The caller creates the canary inside a protected archive directory. This
        method tests direct access, a second process, a symlink, link creation,
        renaming, writes, and workspace access. A failed probe aborts execution.
        """
        work_dir, canary_file = work_dir.resolve(), canary_file.resolve()
        if not canary_file.is_file():
            raise IsolationError("Isolation preflight requires an existing synthetic canary.")
        if not any(canary_file.is_relative_to(root) for root in self.deny_roots):
            raise IsolationError("The canary must be inside an explicit denied root.")
        probe_env = env or {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(work_dir),
            "TMPDIR": str(work_dir), "TMPPREFIX": str(work_dir / "zsh"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            result = subprocess.run(
                self.wrap([python_executable, "-I", "-c", _PREFLIGHT, str(work_dir), str(canary_file)]),
                cwd=work_dir, env=probe_env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=30, close_fds=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IsolationError(f"Isolation preflight could not run: {exc}") from exc
        finally:
            moved = canary_file.with_name(canary_file.name + ".probe-moved")
            if moved.exists() and not canary_file.exists():
                moved.rename(canary_file)
            for leftover in (work_dir / ".isolation-link", work_dir / ".isolation-hardlink",
                             work_dir / ".isolation-write", canary_file.with_name(".isolation-write")):
                leftover.unlink(missing_ok=True)
        if result.returncode:
            raise IsolationError(
                "OS isolation preflight failed; no audit was started. "
                "Launch from a normal terminal if this process is already sandboxed. "
                f"Exit {result.returncode}: {result.stderr.strip()[:800]}"
            )
        try:
            checks = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise IsolationError("Isolation preflight returned invalid results.") from exc
        expected = {"direct", "descendant", "symlink", "hardlink", "rename", "archive_write", "workspace_write"}
        if set(checks) != expected or not all(value is True for value in checks.values()):
            raise IsolationError(f"Archive isolation is ineffective: {checks}")
        return checks


_PREFLIGHT = r'''
import errno, json, os, pathlib, subprocess, sys
work, canary = map(pathlib.Path, sys.argv[1:])
def denied(action):
    try:
        action()
        return False
    except OSError as exc:
        return exc.errno in (errno.EACCES, errno.EPERM)
checks = {"direct": denied(canary.read_bytes)}
child_code = "import pathlib,sys; pathlib.Path(sys.argv[1]).read_bytes()"
child = subprocess.run([sys.executable, "-I", "-c", child_code, str(canary)],
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
checks["descendant"] = child.returncode != 0 and b"PermissionError" in child.stderr
link = work / ".isolation-link"
link.symlink_to(canary)
checks["symlink"] = denied(link.read_bytes)
checks["hardlink"] = denied(lambda: os.link(canary, work / ".isolation-hardlink"))
checks["rename"] = denied(lambda: canary.rename(canary.with_name(canary.name + ".probe-moved")))
checks["archive_write"] = denied(lambda: canary.with_name(".isolation-write").write_text("synthetic"))
workspace = work / ".isolation-write"
workspace.write_text("synthetic")
checks["workspace_write"] = workspace.read_text() == "synthetic"
print(json.dumps(checks, sort_keys=True))
'''
