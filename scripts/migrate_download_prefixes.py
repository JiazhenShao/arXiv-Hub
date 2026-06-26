#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from arxiv_daily.config import ProfileConfig  # noqa: E402
from arxiv_daily.downloader import PaperDownloader  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove manifest-confirmed legacy automatic rating prefixes."
    )
    parser.add_argument("--profile", required=True, type=Path)
    args = parser.parse_args()
    config = ProfileConfig.load(args.profile)
    downloader = PaperDownloader(
        record_dir=config.record_dir,
        library_dir=config.active_library_dir,
        state_dir=config.record_dir / ".state",
    )
    statuses = downloader.migrate_automatic_rating_prefixes()
    migrated = sum(status.state == "migrated" for status in statuses.values())
    conflicts = sum(status.state == "conflict" for status in statuses.values())
    print(f"Managed filename migration: {migrated} renamed, {conflicts} conflicts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
