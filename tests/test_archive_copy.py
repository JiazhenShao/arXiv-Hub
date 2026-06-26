from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scripts.copy_archive import ArchiveCopyError, copy_archive


class ArchiveCopyTests(unittest.TestCase):
    def test_dry_run_reports_pending_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "paper.pdf").write_bytes(b"%PDF-source")

            summary = copy_archive(source, destination, apply=False)

            self.assertEqual(summary.pending, 1)
            self.assertEqual(summary.copied, 0)
            self.assertFalse(destination.exists())

    def test_apply_copies_recursively_and_preserves_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            paper = source / "paper.pdf"
            paper.write_bytes(b"%PDF-source")
            os.utime(paper, (1_600_000_000, 1_600_000_000))

            summary = copy_archive(source, destination, apply=True)

            copied = destination / "paper.pdf"
            self.assertEqual(summary.copied, 1)
            self.assertEqual(copied.read_bytes(), paper.read_bytes())
            self.assertEqual(copied.stat().st_mtime_ns, paper.stat().st_mtime_ns)
            self.assertEqual(paper.read_bytes(), b"%PDF-source")

    def test_identical_existing_file_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "paper.pdf").write_bytes(b"same")
            existing = destination / "paper.pdf"
            existing.write_bytes(b"same")
            before = existing.stat().st_mtime_ns

            summary = copy_archive(source, destination, apply=True)

            self.assertEqual(summary.existing, 1)
            self.assertEqual(summary.copied, 0)
            self.assertEqual(existing.stat().st_mtime_ns, before)

    def test_conflict_fails_before_copying_any_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "a.pdf").write_bytes(b"new")
            (source / "conflict.pdf").write_bytes(b"source")
            (destination / "conflict.pdf").write_bytes(b"different")

            with self.assertRaises(ArchiveCopyError):
                copy_archive(source, destination, apply=True)

            self.assertFalse((destination / "a.pdf").exists())
            self.assertEqual(
                (destination / "conflict.pdf").read_bytes(),
                b"different",
            )

    def test_symlinked_source_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            target = root / "outside.pdf"
            target.write_bytes(b"outside")
            (source / "paper.pdf").symlink_to(target)

            with self.assertRaises(ArchiveCopyError):
                copy_archive(source, destination, apply=False)


if __name__ == "__main__":
    unittest.main()
