from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .models import Paper


@dataclass
class RecommenderState:
    seen_ids: set[str] = field(default_factory=set)
    last_announcement_date: date | None = None

    @classmethod
    def load(cls, path: Path) -> "RecommenderState":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            last = data.get("last_announcement_date")
            return cls(
                seen_ids={str(value) for value in data.get("seen_ids", [])},
                last_announcement_date=date.fromisoformat(last) if last else None,
            )
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "seen_ids": sorted(self.seen_ids),
                    "last_announcement_date": (
                        self.last_announcement_date.isoformat()
                        if self.last_announcement_date
                        else None
                    ),
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(path)


class MetadataCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.papers: dict[str, Paper] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.papers = {
                    str(key): Paper.from_dict(value)
                    for key, value in data.items()
                }
            except (OSError, ValueError, TypeError, KeyError):
                self.papers = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {key: paper.to_dict() for key, paper in self.papers.items()},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)
