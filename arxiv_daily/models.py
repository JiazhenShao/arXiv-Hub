from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class Paper:
    arxiv_id: str
    versioned_id: str
    title: str
    abstract: str
    authors: tuple[str, ...]
    categories: tuple[str, ...]
    primary_category: str
    published: datetime
    updated: datetime
    abs_url: str
    pdf_url: str

    def with_version(self, versioned_id: str) -> "Paper":
        return replace(
            self,
            versioned_id=versioned_id,
            abs_url=f"https://arxiv.org/abs/{versioned_id}",
            pdf_url=f"https://arxiv.org/pdf/{versioned_id}",
        )

    def with_dates(self, published: datetime, updated: datetime) -> "Paper":
        return replace(self, published=published, updated=updated)

    def to_dict(self) -> dict[str, Any]:
        return {
            "arxiv_id": self.arxiv_id,
            "versioned_id": self.versioned_id,
            "title": self.title,
            "abstract": self.abstract,
            "authors": list(self.authors),
            "categories": list(self.categories),
            "primary_category": self.primary_category,
            "published": self.published.isoformat(),
            "updated": self.updated.isoformat(),
            "abs_url": self.abs_url,
            "pdf_url": self.pdf_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Paper":
        return cls(
            arxiv_id=str(data["arxiv_id"]),
            versioned_id=str(data["versioned_id"]),
            title=str(data["title"]),
            abstract=str(data["abstract"]),
            authors=tuple(str(value) for value in data["authors"]),
            categories=tuple(str(value) for value in data["categories"]),
            primary_category=str(data["primary_category"]),
            published=datetime.fromisoformat(str(data["published"])),
            updated=datetime.fromisoformat(str(data["updated"])),
            abs_url=str(data["abs_url"]),
            pdf_url=str(data["pdf_url"]),
        )


@dataclass(frozen=True)
class LibraryItem:
    arxiv_id: str
    weight: float
    observed_date: date
    path: str
    source: str = "library"
    explicit: bool = False
    fingerprint: str = ""
    changed_at: datetime | None = None


@dataclass(frozen=True)
class InterestPaper:
    paper: Paper
    weight: float
    observed_date: date | None
    source: str = "report"
    explicit: bool = True
    fingerprint: str = ""
    changed_at: datetime | None = None


@dataclass(frozen=True)
class InterestCluster:
    centroid: tuple[float, ...]
    weight: float
    member_ids: tuple[str, ...]


@dataclass(frozen=True)
class RankedPaper:
    paper: Paper
    score: float
    tier: str
    matched_topics: tuple[str, ...]
    semantic_score: float
    priority_score: float
    lexical_score: float
    recency_score: float
    why: str
