"""Descriptor-based reads across the untrusted-workspace / trusted-parent boundary."""

from __future__ import annotations

import os
import errno
from pathlib import Path
import stat


class SafeIOError(RuntimeError):
    """An unsafe file, alias, or concurrent modification was encountered."""


_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _root_fd(root: Path) -> int:
    # Walk from / instead of merely protecting the final path component.
    path = Path(os.path.abspath(root))
    fd = os.open("/", _DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _signature(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _read_at(directory: int, name: str, *, max_bytes: int | None = None) -> bytes:
    fd = os.open(name, _FILE, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SafeIOError(f"Expected a regular file with one link: {name}")
        if max_bytes is not None and before.st_size > max_bytes:
            raise SafeIOError(f"File exceeds its byte limit: {name}")
        remaining = before.st_size
        chunks = []
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise SafeIOError(f"File changed while being read: {name}")
            chunks.append(chunk)
            remaining -= len(chunk)
        # Bounded callers require EOF as well as an unchanged stat signature.
        # Read at most one extra byte, even if the file grows during the read.
        if max_bytes is not None and os.read(fd, 1):
            raise SafeIOError(f"File changed while being read: {name}")
        if _signature(os.fstat(fd)) != _signature(before):
            raise SafeIOError(f"File changed while being read: {name}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def safe_read(root: Path, relative: str | Path, *, max_bytes: int | None = None) -> bytes:
    """Read an unaliased regular file, optionally enforcing a byte limit.

    Oversized files are rejected before reading. A file that changes during
    the read is rejected, and bounded reads never consume more than the limit
    plus one byte used to detect growth.
    """
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SafeIOError(f"Expected a nonempty relative path without traversal: {relative}")
    fd = None
    try:
        fd = _root_fd(root)
        for component in relative.parts[:-1]:
            child = os.open(component, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return _read_at(fd, relative.name, max_bytes=max_bytes)
    except OSError as exc:
        raise SafeIOError(f"Unsafe or inaccessible source {relative}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)


def safe_is_file(root: Path, relative: str | Path) -> bool:
    """Check a file through pinned descriptors without reading its contents.

    Missing paths return False; links, special files, and traversal fail closed.
    A subsequent read must still use safe_read because names may change.
    """
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SafeIOError(f"Expected a nonempty relative path without traversal: {relative}")
    directory = file_fd = None
    try:
        directory = _root_fd(root)
        for component in relative.parts[:-1]:
            child = os.open(component, _DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        file_fd = os.open(relative.name, _FILE, dir_fd=directory)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SafeIOError(f"Expected a regular file with one link: {relative}")
        return True
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return False
        raise SafeIOError(f"Unsafe or inaccessible source {relative}: {exc}") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory is not None:
            os.close(directory)


def safe_copy_tree(source: Path, destination: Path) -> None:
    """Snapshot into a new trusted destination using only anchored source FDs.

    The destination and its ancestors must be controlled by the caller and denied
    to the source process. A partial snapshot is never a completed revision.
    """
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise SafeIOError(f"Snapshot destination already exists: {destination}")
    root = None
    try:
        root = _root_fd(source)
        destination.mkdir(parents=True, exist_ok=False)
        _copy_directory(root, destination)
    except OSError as exc:
        raise SafeIOError(f"Could not safely snapshot {source}: {exc}") from exc
    finally:
        if root is not None:
            os.close(root)


def _copy_directory(source_fd: int, destination: Path) -> None:
    before = os.fstat(source_fd)
    names = sorted(os.listdir(source_fd))
    for name in names:
        info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        target = destination / name
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, _DIRECTORY, dir_fd=source_fd)
            try:
                if os.fstat(child).st_ino != info.st_ino:
                    raise SafeIOError(f"Directory changed during snapshot: {name}")
                target.mkdir()
                _copy_directory(child, target)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            # A concurrent symlink replacement fails O_NOFOLLOW. A hardlink
            # replacement is rejected by fstat before any bytes are read.
            target.write_bytes(_read_at(source_fd, name))
        else:
            raise SafeIOError(f"Symlink or special file in snapshot: {name}")
    after = os.fstat(source_fd)
    if (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise SafeIOError("Directory changed while being snapshotted.")
