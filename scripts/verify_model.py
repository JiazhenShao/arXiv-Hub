#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from arxiv_daily.config import ProfileConfig  # noqa: E402
from arxiv_daily.embedding import Specter2Embedder  # noqa: E402
from arxiv_daily.models import Paper  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the pinned SPECTER2 model.")
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
    )
    return parser.parse_args()


def main() -> int:
    config = ProfileConfig.load(parse_args().profile)
    embedder = Specter2Embedder(
        base_model=config.base_model,
        base_revision=config.base_revision,
        adapter_model=config.adapter_model,
        adapter_revision=config.adapter_revision,
        batch_size=1,
    )
    paper = Paper(
        arxiv_id="verification",
        versioned_id="verificationv1",
        title="Neutron-star matter and renormalization-group methods",
        abstract="A local model verification input with no external paper metadata.",
        authors=("Local Verification",),
        categories=("nucl-th",),
        primary_category="nucl-th",
        published=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        abs_url="https://arxiv.org/",
        pdf_url="https://arxiv.org/",
    )
    vectors = embedder.embed([paper])
    vector = vectors["verification"]
    if len(vector) < 100:
        raise RuntimeError("SPECTER2 returned an unexpectedly short vector")
    if "proximity" not in str(embedder._model.active_adapters):
        raise RuntimeError("SPECTER2 proximity adapter is not active")
    print(f"MODEL READY: {embedder.label} ({len(vector)} dimensions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
