#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")

from arxiv_daily.arxiv_api import ArxivClient  # noqa: E402
from arxiv_daily.config import ProfileConfig  # noqa: E402
from arxiv_daily.embedding import Specter2Embedder  # noqa: E402
from arxiv_daily.pipeline import DailyPipeline, PipelineError  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a verified, personalized daily arXiv digest."
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today(),
        help="Local run date in YYYY-MM-DD form (default: today).",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=(
            Path.home()
            / "Library"
            / "Application Support"
            / "arXiv Hub"
            / "profile.toml"
        ),
        help="Path to the recommender profile.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the complete report without writing records or state.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Back up and regenerate an existing dated report while preserving ratings.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = ProfileConfig.load(args.profile)
    source = ArxivClient(
        config.categories,
        user_agent=config.user_agent,
        min_interval_seconds=config.api_min_interval_seconds,
        timeout_seconds=config.api_timeout_seconds,
        state_dir=config.record_dir / ".state",
        retry_backoffs=config.api_retry_backoffs,
        retry_deadline_seconds=config.api_retry_deadline_seconds,
    )
    embedder = Specter2Embedder(
        base_model=config.base_model,
        base_revision=config.base_revision,
        adapter_model=config.adapter_model,
        adapter_revision=config.adapter_revision,
        batch_size=config.batch_size,
    )
    pipeline = DailyPipeline(config=config, source=source, embedder=embedder)
    try:
        result = pipeline.run(args.date, dry_run=args.dry_run, force=args.force)
    except PipelineError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    if result.report_text:
        print(result.report_text, end="")
    elif result.report_path:
        print(
            f"WRITTEN: {result.report_path} "
            f"({result.selected_count} verified papers)"
        )
    else:
        print(f"SKIPPED: {result.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
