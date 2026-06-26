from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable

from .history import annotation_weight, extract_arxiv_id
from .models import InterestPaper, Paper
from .ranking import build_interest_clusters
from .setup import CategoryPreference, TopicPreference


class SeedScanError(RuntimeError):
    """The read-only seed library could not be scanned safely."""


@dataclass(frozen=True)
class SeedCandidate:
    arxiv_id: str
    weight: float
    observed_date: date
    path: str


@dataclass(frozen=True)
class SeedScanResult:
    candidates: tuple[SeedCandidate, ...]
    counts: dict[str, int]
    records: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class VerifiedSeedResult:
    papers: tuple[Paper, ...]
    candidates: tuple[SeedCandidate, ...]
    counts: dict[str, int]


@dataclass(frozen=True)
class ProfileSuggestion:
    categories: tuple[CategoryPreference, ...]
    topics: tuple[TopicPreference, ...]


def scan_seed_library(
    root: Path,
    *,
    cache_path: Path,
    limit: int,
    reader_factory: Callable[[Path], object] | None = None,
) -> SeedScanResult:
    root = root.expanduser()
    if root.is_symlink() or not root.is_dir():
        raise SeedScanError("Seed PDF folder must be a regular directory")
    if not 25 <= limit <= 500:
        raise ValueError("Seed library limit must be between 25 and 500")
    cached = _load_cache(cache_path)
    current_cache: dict[str, dict[str, object]] = {}
    records: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    identified: list[SeedCandidate] = []

    for path in sorted(root.rglob("*")):
        if path.suffix.lower() != ".pdf":
            continue
        if path.is_symlink():
            record = {"path": str(path), "status": "symlink", "arxiv_id": ""}
            records.append(record)
            counts["symlink"] += 1
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            record = {
                "path": str(path),
                "status": "corrupt",
                "arxiv_id": "",
                "message": str(exc),
            }
            records.append(record)
            counts["corrupt"] += 1
            continue
        key = str(path.resolve())
        signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        previous = cached.get(key)
        if (
            previous is not None
            and previous.get("size") == stat.st_size
            and previous.get("mtime_ns") == stat.st_mtime_ns
        ):
            record = dict(previous)
        else:
            record = _inspect_pdf(path, reader_factory=reader_factory)
            record.update(signature)
        current_cache[key] = record
        records.append(record)
        status = str(record["status"])
        counts[status] += 1
        arxiv_id = str(record.get("arxiv_id", ""))
        if status == "identified" and arxiv_id:
            identified.append(
                SeedCandidate(
                    arxiv_id=arxiv_id,
                    weight=annotation_weight(path.name),
                    observed_date=datetime.fromtimestamp(stat.st_mtime).date(),
                    path=str(path),
                )
            )

    identified.sort(
        key=lambda item: (item.observed_date, item.path),
        reverse=True,
    )
    unique: list[SeedCandidate] = []
    seen: set[str] = set()
    for item in identified:
        if item.arxiv_id in seen:
            counts["identified"] -= 1
            counts["duplicate"] += 1
            for record in records:
                if (
                    record.get("path") == item.path
                    and record.get("status") == "identified"
                ):
                    record["status"] = "duplicate"
                    break
            continue
        seen.add(item.arxiv_id)
        if len(unique) < limit:
            unique.append(item)
    _write_cache(cache_path, current_cache)
    return SeedScanResult(
        candidates=tuple(unique),
        counts=dict(counts),
        records=tuple(records),
    )


def verify_seed_candidates(
    scan: SeedScanResult,
    *,
    fetcher: Callable[[list[str]], list[Paper]],
) -> VerifiedSeedResult:
    fetched = fetcher([item.arxiv_id for item in scan.candidates])
    by_id = {paper.arxiv_id: paper for paper in fetched}
    verified_candidates = tuple(
        item for item in scan.candidates if item.arxiv_id in by_id
    )
    counts = dict(scan.counts)
    counts["unverified"] = len(scan.candidates) - len(verified_candidates)
    counts["verified"] = len(verified_candidates)
    return VerifiedSeedResult(
        papers=tuple(by_id[item.arxiv_id] for item in verified_candidates),
        candidates=verified_candidates,
        counts=counts,
    )


def suggest_profile(
    papers: Iterable[Paper],
    embeddings: dict[str, list[float]],
) -> ProfileSuggestion:
    paper_list = list(papers)
    if not paper_list:
        return ProfileSuggestion(categories=(), topics=())
    category_counts: Counter[str] = Counter()
    for paper in paper_list:
        for index, category in enumerate(paper.categories):
            category_counts[category] += 2 if index == 0 else 1
    maximum = max(category_counts.values())
    categories = tuple(
        CategoryPreference(name, round(count / maximum, 3))
        for name, count in sorted(
            category_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    )
    interests = [
        InterestPaper(paper=paper, weight=1.0, observed_date=None)
        for paper in paper_list
    ]
    clusters = build_interest_clusters(interests, embeddings)
    by_id = {paper.arxiv_id: paper for paper in paper_list}
    topics: list[TopicPreference] = []
    for cluster in clusters:
        members = [by_id[value] for value in cluster.member_ids]
        phrases = _cluster_phrases(members)
        if not phrases:
            continue
        topics.append(
            TopicPreference(
                name=phrases[0],
                weight=round(cluster.weight, 3),
                phrases=phrases,
            )
        )
    return ProfileSuggestion(categories=categories, topics=tuple(topics))


def _inspect_pdf(
    path: Path,
    *,
    reader_factory: Callable[[Path], object] | None,
) -> dict[str, object]:
    filename_id = extract_arxiv_id(path.name)
    if filename_id:
        return {
            "path": str(path),
            "status": "identified",
            "arxiv_id": filename_id,
            "source": "filename",
        }
    try:
        if reader_factory is None:
            from pypdf import PdfReader

            reader = PdfReader(path, strict=False)
        else:
            reader = reader_factory(path)
        if bool(getattr(reader, "is_encrypted", False)):
            return {
                "path": str(path),
                "status": "encrypted",
                "arxiv_id": "",
            }
        metadata = getattr(reader, "metadata", {}) or {}
        metadata_text = " ".join(str(value) for value in metadata.values())
        metadata_id = extract_arxiv_id(metadata_text)
        if metadata_id:
            return {
                "path": str(path),
                "status": "identified",
                "arxiv_id": metadata_id,
                "source": "metadata",
            }
        page_text = " ".join(
            str(page.extract_text() or "")
            for page in list(getattr(reader, "pages", ()))[:2]
        )
        page_id = extract_arxiv_id(page_text)
        if page_id:
            return {
                "path": str(path),
                "status": "identified",
                "arxiv_id": page_id,
                "source": "text",
            }
        return {
            "path": str(path),
            "status": "unrecognized",
            "arxiv_id": "",
        }
    except Exception as exc:
        return {
            "path": str(path),
            "status": "corrupt",
            "arxiv_id": "",
            "message": str(exc)[:200],
        }


def _cluster_phrases(papers: list[Paper]) -> tuple[str, ...]:
    from sklearn.feature_extraction.text import TfidfVectorizer

    documents = [f"{paper.title}. {paper.abstract}" for paper in papers]
    vectorizer = TfidfVectorizer(
        stop_words="english",
        ngram_range=(1, 2),
        max_features=300,
    )
    try:
        matrix = vectorizer.fit_transform(documents)
    except ValueError:
        return ()
    terms = vectorizer.get_feature_names_out()
    scores = matrix.sum(axis=0).A1
    ranked = sorted(
        (
            (float(score) * (1.15 if " " in str(term) else 1.0), str(term))
            for term, score in zip(terms, scores, strict=True)
        ),
        key=lambda item: (-item[0], item[1]),
    )
    phrases: list[str] = []
    for _, term in ranked:
        if any(term in existing or existing in term for existing in phrases):
            continue
        phrases.append(term)
        if len(phrases) == 6:
            break
    return tuple(phrases)


def _load_cache(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            str(key): dict(value)
            for key, value in payload.get("files", {}).items()
        }
    except (OSError, TypeError, ValueError):
        return {}


def _write_cache(path: Path, values: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "files": values}, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
