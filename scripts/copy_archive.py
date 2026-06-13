#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


class ArchiveCopyError(RuntimeError):
    """The archive cannot be copied without risking source or destination data."""


@dataclass(frozen=True)
class ArchiveCopySummary:
    total: int
    pending: int
    existing: int
    copied: int


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ArchiveCopyError("Archive source must be a regular directory")
    paths: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ArchiveCopyError(f"Archive source contains a symlink: {path}")
        if path.is_file():
            paths.append(path)
    return paths


def copy_archive(
    source: Path,
    destination: Path,
    *,
    apply: bool,
) -> ArchiveCopySummary:
    source = source.expanduser()
    destination = destination.expanduser()
    if destination.is_symlink():
        raise ArchiveCopyError("Archive destination must not be a symlink")

    pending: list[tuple[Path, Path]] = []
    existing = 0
    conflicts: list[Path] = []
    source_files = _files(source)
    for source_path in source_files:
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if destination_path.is_symlink():
            conflicts.append(relative)
        elif destination_path.exists():
            if (
                destination_path.is_file()
                and source_path.stat().st_size == destination_path.stat().st_size
                and _digest(source_path) == _digest(destination_path)
            ):
                existing += 1
            else:
                conflicts.append(relative)
        else:
            pending.append((source_path, destination_path))

    if conflicts:
        preview = ", ".join(str(path) for path in conflicts[:5])
        raise ArchiveCopyError(
            f"{len(conflicts)} archive destination conflict(s): {preview}"
        )
    if not apply:
        return ArchiveCopySummary(
            total=len(source_files),
            pending=len(pending),
            existing=existing,
            copied=0,
        )

    copied = 0
    for source_path, destination_path in pending:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination_path.name}.",
            suffix=".copying",
            dir=destination_path.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(source_path, temporary)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            if _digest(source_path) != _digest(temporary):
                raise ArchiveCopyError(
                    f"Copied archive content failed verification: {source_path}"
                )
            os.replace(temporary, destination_path)
            directory_fd = os.open(destination_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            if (
                _digest(source_path) != _digest(destination_path)
                or source_path.stat().st_mtime_ns
                != destination_path.stat().st_mtime_ns
            ):
                raise ArchiveCopyError(
                    f"Installed archive file failed verification: {source_path}"
                )
            copied += 1
        finally:
            temporary.unlink(missing_ok=True)

    return ArchiveCopySummary(
        total=len(source_files),
        pending=0,
        existing=existing,
        copied=copied,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy an arXiv Hub paper archive without overwriting data."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = copy_archive(
            args.source,
            args.destination,
            apply=args.apply,
        )
    except (ArchiveCopyError, OSError) as exc:
        print(f"ARCHIVE COPY FAILED: {exc}")
        return 2
    mode = "copied" if args.apply else "pending"
    value = summary.copied if args.apply else summary.pending
    print(
        f"Archive files: total={summary.total} existing={summary.existing} "
        f"{mode}={value}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
