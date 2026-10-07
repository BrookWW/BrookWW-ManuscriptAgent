import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safeio import SafeIOError, safe_copy_tree, safe_read, safe_is_file


class SafeIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "nested").mkdir()
        (self.source / "nested/paper.tex").write_bytes(b"current manuscript")
        self.secret = self.root / "synthetic-archive.txt"
        self.secret.write_bytes(b"old synthetic history")

    def tearDown(self):
        self.temp.cleanup()

    def test_normal_read_and_snapshot(self):
        self.assertTrue(safe_is_file(self.source, "nested/paper.tex"))
        self.assertFalse(safe_is_file(self.source, "nested/missing.tex"))
        self.assertEqual(safe_read(self.source, "nested/paper.tex"), b"current manuscript")
        target = self.root / "snapshot"
        safe_copy_tree(self.source, target)
        self.assertEqual((target / "nested/paper.tex").read_bytes(), b"current manuscript")
        self.assertNotEqual((target / "nested/paper.tex").stat().st_ino,
                            (self.source / "nested/paper.tex").stat().st_ino)

    def test_byte_limit_accepts_empty_and_exact_size_files(self):
        for content in (b"", b"current manuscript"):
            with self.subTest(content=content):
                (self.source / "bounded").write_bytes(content)
                self.assertEqual(safe_read(self.source, "bounded", max_bytes=len(content)), content)

    def test_byte_limit_rejects_oversized_file_before_reading(self):
        with patch("safeio.os.read") as read, self.assertRaisesRegex(SafeIOError, "byte limit"):
            safe_read(self.source, "nested/paper.tex", max_bytes=3)
        read.assert_not_called()

    def test_short_reads_are_collected_in_full(self):
        read = os.read
        for limit in (None, len(b"current manuscript")):
            with self.subTest(limit=limit), patch("safeio.os.read", side_effect=lambda fd, size: read(fd, min(size, 3))):
                self.assertEqual(safe_read(self.source, "nested/paper.tex", max_bytes=limit), b"current manuscript")

    def test_mutations_during_bounded_and_unbounded_reads_are_rejected(self):
        victim = self.source / "nested/paper.tex"
        hardlink = self.source / "new-link"
        content = b"current manuscript"
        read = os.read
        for limit in (None, len(content)):
            for change in ("grow", "shrink", "rewrite", "hardlink"):
                victim.write_bytes(content)
                modified = False

                def mutate(fd, size):
                    nonlocal modified
                    chunk = read(fd, size)
                    if not modified:
                        modified = True
                        if change == "hardlink":
                            os.link(victim, hardlink)
                        else:
                            before = victim.stat()
                            replacement = {"grow": content + b"x", "shrink": b"", "rewrite": b"x" * len(content)}
                            victim.write_bytes(replacement[change])
                            os.utime(victim, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
                    return chunk

                with self.subTest(limit=limit, change=change), patch("safeio.os.read", side_effect=mutate):
                    with self.assertRaisesRegex(SafeIOError, "changed"):
                        safe_read(self.source, "nested/paper.tex", max_bytes=limit)
                hardlink.unlink(missing_ok=True)

    def test_symlink_in_file_directory_or_root_is_rejected(self):
        (self.source / "alias").symlink_to(self.secret)
        (self.source / "alias-dir").symlink_to(self.root, target_is_directory=True)
        root_alias = self.root / "root-alias"
        root_alias.symlink_to(self.source, target_is_directory=True)
        for root, relative in [(self.source, "alias"), (self.source, "alias-dir/synthetic-archive.txt"),
                               (root_alias, "nested/paper.tex")]:
            for limit in (None, 1024):
                with self.subTest(root=root, relative=relative, limit=limit), self.assertRaises(SafeIOError):
                    safe_read(root, relative, max_bytes=limit)
            with self.assertRaises(SafeIOError):
                safe_is_file(root, relative)
        with self.assertRaises(SafeIOError):
            safe_copy_tree(self.source, self.root / "snapshot")

    def test_hardlinks_special_files_and_traversal_are_rejected(self):
        os.link(self.secret, self.source / "hardlink")
        os.mkfifo(self.source / "pipe")
        for name in ["hardlink", "pipe", "../synthetic-archive.txt", str(self.secret)]:
            for limit in (None, 1024):
                with self.subTest(name=name, limit=limit), self.assertRaises(SafeIOError):
                    safe_read(self.source, name, max_bytes=limit)

    def test_symlink_swap_between_stat_and_open_never_reads_archive(self):
        original_stat = os.stat
        victim = self.source / "nested/paper.tex"
        swapped = False
        def swap(path, *args, **kwargs):
            nonlocal swapped
            result = original_stat(path, *args, **kwargs)
            if path == "paper.tex" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                victim.unlink()
                victim.symlink_to(self.secret)
            return result
        with patch("safeio.os.stat", side_effect=swap):
            with self.assertRaises(SafeIOError):
                safe_copy_tree(self.source, self.root / "snapshot")
        self.assertTrue(swapped)
        self.assertFalse((self.root / "snapshot/nested/paper.tex").exists())

    def test_directory_swap_between_stat_and_open_is_rejected(self):
        original_stat = os.stat
        def swap(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == "nested" and kwargs.get("dir_fd") is not None:
                (self.source / "nested").rename(self.source / "moved")
                (self.source / "nested").symlink_to(self.root, target_is_directory=True)
            return result
        with patch("safeio.os.stat", side_effect=swap):
            with self.assertRaises(SafeIOError):
                safe_copy_tree(self.source, self.root / "snapshot")


if __name__ == "__main__":
    unittest.main()
