from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Topic:
    name: str
    weight: float
    phrases: tuple[str, ...]


@dataclass(frozen=True)
class ExtraSource:
    """Configuration for an additional preprint source (biorxiv, medrxiv, chemrxiv)."""

    type: str  # "biorxiv", "medrxiv", or "chemrxiv"
    subjects: tuple[str, ...]  # empty tuple means all subjects


@dataclass(frozen=True)
class ProfileConfig:
    record_dir: Path
    active_library_dir: Path
    archive_library_dir: Path
    categories: dict[str, float]
    topics: tuple[Topic, ...]
    report_limit: int
    relevance_threshold: float
    backfill_days: int
    announcement_max_age_days: int
    history_report_limit: int
    history_library_limit: int
    recency_decay_days: float
    mmr_lambda: float
    base_model: str
    base_revision: str
    adapter_model: str
    adapter_revision: str
    batch_size: int
    user_agent: str
    api_min_interval_seconds: float = 3.0
    api_retry_backoffs: tuple[float, ...] = (10.0, 20.0, 40.0)
    api_retry_deadline_seconds: float = 90.0
    api_timeout_seconds: float = 60.0
    viewer_timezone: str = "America/Chicago"
    search_start_time: time = time(20, 0)
    extra_sources: tuple[ExtraSource, ...] = ()

    @property
    def topic_weights(self) -> dict[str, float]:
        return {topic.name: topic.weight for topic in self.topics}

    @property
    def topic_phrases(self) -> dict[str, tuple[str, ...]]:
        return {topic.name: topic.phrases for topic in self.topics}

    @classmethod
    def load(cls, path: Path) -> "ProfileConfig":
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        paths = data["paths"]
        ranking = data["ranking"]
        model = data["model"]
        source = data["source"]
        viewer = data.get("viewer", {})
        viewer_timezone = str(viewer.get("timezone", "America/Chicago"))
        try:
            ZoneInfo(viewer_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"Unknown viewer timezone: {viewer_timezone}"
            ) from exc
        try:
            search_start_time = time.fromisoformat(
                str(viewer.get("search_time", "20:00"))
            )
        except ValueError as exc:
            raise ValueError("viewer.search_time must use HH:MM format") from exc
        topics = tuple(
            Topic(
                name=str(item["name"]),
                weight=float(item["weight"]),
                phrases=tuple(str(value).lower() for value in item["phrases"]),
            )
            for item in data["topics"]
        )
        valid_source_types = {"biorxiv", "medrxiv", "chemrxiv"}
        extra_sources_raw = data.get("extra_sources", [])
        if not isinstance(extra_sources_raw, list):
            raise ValueError("[extra_sources] must be a list of source tables")
        extra_sources: list[ExtraSource] = []
        for entry in extra_sources_raw:
            if not isinstance(entry, dict):
                raise ValueError("Each [[extra_sources]] entry must be a table")
            src_type = str(entry.get("type", "")).strip().lower()
            if src_type not in valid_source_types:
                raise ValueError(
                    f"extra_sources type must be one of {sorted(valid_source_types)}, "
                    f"got {src_type!r}"
                )
            subjects = tuple(
                str(s).strip() for s in entry.get("subjects", []) if str(s).strip()
            )
            extra_sources.append(ExtraSource(type=src_type, subjects=subjects))

        return cls(
            record_dir=Path(paths["record_dir"]).expanduser(),
            active_library_dir=Path(paths["active_library_dir"]).expanduser(),
            archive_library_dir=Path(paths["archive_library_dir"]).expanduser(),
            categories={
                str(key): float(value)
                for key, value in data["categories"].items()
            },
            topics=topics,
            report_limit=int(ranking["report_limit"]),
            relevance_threshold=float(ranking["relevance_threshold"]),
            backfill_days=int(ranking["backfill_days"]),
            announcement_max_age_days=int(ranking["announcement_max_age_days"]),
            history_report_limit=int(ranking["history_report_limit"]),
            history_library_limit=int(ranking["history_library_limit"]),
            recency_decay_days=float(ranking["recency_decay_days"]),
            mmr_lambda=float(ranking["mmr_lambda"]),
            base_model=str(model["base_model"]),
            base_revision=str(model["base_revision"]),
            adapter_model=str(model["adapter_model"]),
            adapter_revision=str(model["adapter_revision"]),
            batch_size=int(model["batch_size"]),
            user_agent=str(source["user_agent"]),
            api_min_interval_seconds=float(
                source.get("min_interval_seconds", 3.0)
            ),
            api_retry_backoffs=tuple(
                float(value)
                for value in source.get("retry_backoffs", [10.0, 20.0, 40.0])
            ),
            api_retry_deadline_seconds=float(
                source.get("retry_deadline_seconds", 90.0)
            ),
            api_timeout_seconds=float(source.get("timeout_seconds", 60.0)),
            viewer_timezone=viewer_timezone,
            search_start_time=search_start_time,
            extra_sources=tuple(extra_sources),
        )
