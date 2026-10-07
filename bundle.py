"""Build a small, relocatable LaTeX source bundle without copying a project tree.

The project root is also the compiler working directory. Relative API paths are
relative to that root. This is a conservative static dependency reader, not a
TeX interpreter; macro-generated dependencies require explicit extra assets and
a subsequent compilation check.
"""

from __future__ import annotations

import os
from pathlib import Path
import re

from safeio import SafeIOError, safe_is_file, safe_read


class BundleError(ValueError):
    """The requested source bundle cannot be assembled safely."""


_RESERVED = {".git", ".agents", ".codex", ".audit-work"}
_TEXT_EXTENSIONS = {".tex", ".sty", ".cls", ".ltx", ".def", ".cfg"}
_GRAPHICS_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".mps", ".eps", ".ps", ".svg")
_LITERAL_ENVIRONMENTS = {"verbatim", "Verbatim", "lstlisting", "minted", "comment", "filecontents", "filecontents*"}
_DEPENDENCY_SPECS = {
    "input": ((".tex",), False, True),
    "include": ((".tex",), False, True),
    "@input": ((".tex",), False, True),
    "InputIfFileExists": ((".tex",), True, True),
    "IfFileExists": ((".tex",), True, True),
    "bibliography": ((".bib",), False, False),
    "addbibresource": ((".bib",), False, False),
    "bibliographystyle": ((".bst",), True, False),
    "includegraphics": (_GRAPHICS_EXTENSIONS, False, False),
    "documentclass": ((".cls",), True, True),
    "usepackage": ((".sty",), True, True),
    "RequirePackage": ((".sty",), True, True),
    "LoadClass": ((".cls",), True, True),
    "LoadClassWithOptions": ((".cls",), True, True),
    "RequirePackageWithOptions": ((".sty",), True, True),
    "lstinputlisting": ((), False, False),
    "verbatiminput": ((), False, False),
    "VerbatimInput": ((), False, False),
}


def _without_comments(source: str) -> str:
    # Consume control symbols together, so escaped '%' and '\\' stay intact.
    token = re.compile(r"%[^\r\n]*(?:\r\n?|\n|$)|\\([A-Za-z@]+|.)\*?")
    environment_header = re.compile(
        r"(?:\s|%[^\r\n]*(?:\r\n?|\n|$))*"
        r"\{((?:%[^\r\n]*(?:\r\n?|\n|$)|[^{}%])*)\}"
    )
    parts = []
    position = 0
    while match := token.search(source, position):
        parts.append(source[position:match.start()])
        position = match.end()
        command = match.group(1)
        if command is None:
            # Drop the comment AND its newline to preserve split filenames.
            continue
        start = match.start()
        if command in {"verb", "Verb"} and position < len(source):
            end = source.find(source[position], position + 1)
            position = len(source) if end < 0 else end + 1
        elif command == "begin":
            header = environment_header.match(source, position)
            if header:
                # Comments in the header are ordinary TeX, outside the body.
                environment = _without_comments(header.group(1))
                if environment in _LITERAL_ENVIRONMENTS:
                    parts.append(source[start:position] + _without_comments(header.group()))
                    start = header.end()
                    marker = "\\end{" + environment + "}"
                    end = source.find(marker, start)
                    position = len(source) if end < 0 else end + len(marker)
        parts.append(source[start:position])
    parts.append(source[position:])
    return "".join(parts)


def _group(source: str, position: int, opening: str = "{") -> tuple[str, int]:
    while position < len(source) and source[position].isspace():
        position += 1
    if position >= len(source) or source[position] != opening:
        raise BundleError("Expected a static braced dependency argument; supply its exact files with --asset.")
    closing = "}" if opening == "{" else "]"
    start = position + 1
    depth = 1
    position += 1
    while position < len(source):
        char = source[position]
        if char == "\\":
            position += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return source[start:position], position + 1
        position += 1
    raise BundleError("Unclosed dependency argument; fix the TeX syntax before bundling.")


def _argument(source: str, position: int, *, bare: bool = False) -> tuple[str, int]:
    while position < len(source) and source[position].isspace():
        position += 1
    while position < len(source) and source[position] == "[":
        _, position = _group(source, position, "[")
        while position < len(source) and source[position].isspace():
            position += 1
    if bare and position < len(source) and source[position] != "{":
        match = re.match(r"[^\s{}]+", source[position:])
        if match:
            return match.group(), position + len(match.group())
    return _group(source, position)


class _Bundle:
    def __init__(self, project_root: Path, extra_assets: list[Path]):
        # Keep the lexical root: resolving an attacker-swapped link would hide
        # it before safe_read can enforce O_NOFOLLOW on every component.
        self.root = Path(os.path.abspath(project_root))
        if self.root.is_symlink():
            raise BundleError(f"Project root must not be a symlink: {project_root}")
        if not self.root.is_dir():
            raise BundleError(f"Project root is not a directory: {project_root}")
        self.files: set[Path] = set()
        self.visited: set[tuple[Path, Path]] = set()
        self.graphics_paths: list[Path] = []
        self.graphics_extensions = _GRAPHICS_EXTENSIONS
        self.dynamic_graphics_paths = False
        self.assets = [self.checked_path(asset, required=True) for asset in extra_assets]

    def read(self, relative: Path) -> bytes:
        try:
            return safe_read(self.root, relative)
        except SafeIOError as exc:
            raise BundleError(str(exc)) from exc

    def is_file(self, relative: Path) -> bool:
        try:
            return safe_is_file(self.root, relative)
        except SafeIOError as exc:
            raise BundleError(
                f"Source must be a regular file without symlink or hard links: {relative}. {exc}"
            ) from exc

    def relative_path(self, path: Path) -> Path:
        if ".." in path.parts:
            raise BundleError(f"Parent traversal is not allowed in a bundle path: {path}")
        if path.is_absolute():
            try:
                relative = path.relative_to(self.root)
            except ValueError as exc:
                raise BundleError(f"Dependency is outside the project root: {path}") from exc
        else:
            relative = path
        if any(part in _RESERVED for part in relative.parts):
            raise BundleError(f"Reserved control or history path cannot be bundled: {relative}")
        return relative

    def checked_path(self, path: Path, *, required: bool = False) -> Path:
        relative = self.relative_path(path)
        exists = self.is_file(relative)
        if required and not exists:
            raise BundleError(f"Required source file is missing: {relative}")
        return relative

    def static(self, value: str, command: str, source: Path) -> str | None:
        value = value.strip()
        if "|" in value or "\x00" in value or "\n" in value or "\r" in value:
            raise BundleError(f"Unsupported dependency path in {source}: {value!r}")
        path = Path(value)
        if path.is_absolute():
            raise BundleError(f"Use a project-relative dependency path in {source}: {value}")
        # Check the expression before joining, including paths that do not exist.
        if ".." in path.parts or any(part in _RESERVED for part in path.parts):
            self.relative_path(path)
        if not value or any(char in value for char in "\\#{}~^$"):
            if self.assets:
                return None
            raise BundleError(
                f"Dynamic or ambiguous \\{command} dependency in {source}: {value!r}. "
                "Provide the exact required files with --asset and verify compilation."
            )
        return value

    def dependency(
        self, value: str, extensions: tuple[str, ...], bases: list[Path],
        source: Path, command: str, *, optional: bool = False,
    ) -> Path | None:
        value = self.static(value, command, source)
        if value is None:
            return None
        path = Path(value)
        names = [path] if path.suffix else [Path(value + ext) for ext in extensions] + [path]
        for base in dict.fromkeys(bases):
            for name in names:
                relative = self.relative_path(base / name)
                if self.is_file(relative):
                    self.files.add(relative)
                    return relative
        if command == "includegraphics" and self.dynamic_graphics_paths:
            for asset in self.assets:
                if asset.name in {name.name for name in names}:
                    return asset
        if optional:
            return None
        raise BundleError(
            f"Missing \\{command} dependency {value!r} referenced by {source}. "
            "Correct the path or project root; use --asset for macro-generated dependencies."
        )

    def scan(self, relative: Path, base: Path = Path(".")) -> None:
        key = (relative, base)
        if key in self.visited:
            return
        self.visited.add(key)
        self.files.add(relative)
        raw = self.read(relative)
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            source = raw.decode("latin-1")
        source = _without_comments(source)
        position = 0
        while position < len(source):
            if source[position] != "\\":
                position += 1
                continue
            match = re.match(r"\\([A-Za-z@]+|.)\*?", source[position:])
            if not match:
                break
            command = match.group(1)
            position += len(match.group())
            if command in {"verb", "Verb"}:
                if position < len(source):
                    end = source.find(source[position], position + 1)
                    position = len(source) if end < 0 else end + 1
                continue
            if command == "begin":
                environment, end = _argument(source, position)
                if environment in _LITERAL_ENVIRONMENTS:
                    marker = "\\end{" + environment + "}"
                    finish = source.find(marker, end)
                    position = len(source) if finish < 0 else finish + len(marker)
                continue
            if command == "graphicspath":
                value, position = _argument(source, position)
                paths = []
                cursor = 0
                while cursor < len(value):
                    if not value[cursor:].strip():
                        break
                    if not value[cursor:].lstrip().startswith("{"):
                        # A macro may expand to the complete list of paths.
                        rest = self.static(value[cursor:], command, relative)
                        if rest is not None:
                            raise BundleError(f"Expected braced graphics directories in {relative}.")
                        self.dynamic_graphics_paths = True
                        break
                    item, cursor = _group(value, cursor)
                    item = self.static(item, command, relative)
                    if item is None:
                        self.dynamic_graphics_paths = True
                        continue
                    folder = base / item
                    # Validate directory components using a nonexistent child path.
                    self.checked_path(folder / "__bundle_path_check__")
                    paths.append(folder)
                self.graphics_paths = paths
                continue
            if command == "DeclareGraphicsExtensions":
                value, position = _argument(source, position)
                value = self.static(value, command, relative)
                if value is not None:
                    extensions = tuple(part.strip() for part in value.split(","))
                    if not all(re.fullmatch(r"\.[A-Za-z0-9]+", ext) for ext in extensions):
                        raise BundleError(f"Unsupported graphics extensions in {relative}: {value}")
                    self.graphics_extensions = extensions
                continue
            if command in {"import", "subimport", "inputfrom", "subinputfrom", "includefrom", "subincludefrom"}:
                directory, position = _argument(source, position)
                filename, position = _argument(source, position)
                directory = self.static(directory, command, relative)
                if directory is None:
                    continue
                import_base = (base if command.startswith("sub") else Path(".")) / directory
                child = self.dependency(filename, (".tex",), [import_base], relative, command)
                if child is not None:
                    self.scan(child, import_base)
                continue
            if command not in _DEPENDENCY_SPECS:
                continue
            extensions, optional, recursive = _DEPENDENCY_SPECS[command]
            value, position = _argument(source, position, bare=command in {"input", "include", "@input"})
            values = value.split(",") if command in {"bibliography", "usepackage", "RequirePackage", "RequirePackageWithOptions"} else [value]
            bases = [base, Path(".")]
            if command == "includegraphics":
                extensions = self.graphics_extensions
                bases += self.graphics_paths
            for value in values:
                child = self.dependency(value, extensions, bases, relative, command, optional=optional)
                if child is not None and recursive:
                    self.scan(child, base)


def create_bundle(
    entry: Path, project_root: Path, destination: Path,
    extra_assets: list[Path] | None = None,
) -> str:
    """Copy the static source closure into an empty destination and return entry.

    Paths returned and copied preserve their layout relative to project_root.
    Run the TeX compiler from destination using the returned POSIX entry path.
    Explicit extra files are copied individually; TeX/class/style extra files
    are also scanned. No directory, symlink, history, or control tree is copied.
    """
    destination = Path(destination)
    if destination.is_symlink():
        raise BundleError(f"Destination must not be a symlink: {destination}")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise BundleError(f"Destination must be an empty directory: {destination}")
    bundle = _Bundle(Path(project_root), [Path(asset) for asset in (extra_assets or [])])
    entry_relative = bundle.checked_path(Path(entry), required=True)
    if entry_relative.suffix.lower() != ".tex":
        raise BundleError(f"Entry must be a .tex file: {entry}")
    bundle.scan(entry_relative)
    for asset in bundle.assets:
        bundle.files.add(asset)
        if asset.suffix.lower() in _TEXT_EXTENSIONS:
            bundle.scan(asset)
    destination.mkdir(parents=True, exist_ok=True)
    for relative in sorted(bundle.files):
        # Recheck after discovery, immediately before copying.
        bundle.checked_path(relative, required=True)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bundle.read(relative))
    return entry_relative.as_posix()
