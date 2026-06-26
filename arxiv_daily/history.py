from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import InterestPaper, LibraryItem, Paper


MODERN_ID_RE = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)")
LEGACY_ID_RE = re.compile(
    r"(?P<archive>[a-z-]+)[_/](?P<number>\d{7})(?:v\d+)?",
    re.IGNORECASE,
)
LEGACY_ARCHIVES = frozenset(
    {
        "acc-phys",
        "adap-org",
        "alg-geom",
        "ao-sci",
        "astro-ph",
        "atom-ph",
        "bayes-an",
        "chao-dyn",
        "chem-ph",
        "cmp-lg",
        "comp-gas",
        "cond-mat",
        "cs",
        "dg-ga",
        "funct-an",
        "gr-qc",
        "hep-ex",
        "hep-lat",
        "hep-ph",
        "hep-th",
        "math",
        "math-ph",
        "mtrl-th",
        "nlin",
        "nucl-ex",
        "nucl-th",
        "patt-sol",
        "physics",
        "plasm-ph",
        "q-alg",
        "q-bio",
        "q-fin",
        "quant-ph",
        "solv-int",
        "stat",
        "supr-con",
    }
)
RECORD_RE = re.compile(r"<!-- arxiv-record:(\{.*?\}) -->")
RATING_RE = re.compile(
    r"\*\*Interest:\*\*\s*(high|medium|low|strong|maybe|skip|unrated)",
    re.IGNORECASE,
)
DATED_FOLDER_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3})\s+(?P<year>\d{4})"
)
RATING_WEIGHTS = {
    "high": 4.0,
    "medium": 2.0,
    "low": 0.5,
    "strong": 4.0,
    "maybe": 2.0,
    "skip": -3.0,
    "unrated": 0.25,
}


def extract_arxiv_id(value: str) -> str | None:
    modern = MODERN_ID_RE.search(value)
    if modern:
        return modern.group(1)
    legacy = LEGACY_ID_RE.search(value)
    if legacy and legacy.group("archive").lower() in LEGACY_ARCHIVES:
        return f"{legacy.group('archive').lower()}/{legacy.group('number')}"
    return None


def annotation_marker(filename: str) -> str | None:
    explicit = re.search(
        r"\[(high|medium|low|skip|unrated)\]",
        filename,
        re.IGNORECASE,
    )
    if explicit:
        return explicit.group(1).lower()
    if "[!!!]" in filename:
        return "!!!"
    if "[!!]" in filename:
        return "!!"
    if "[!]" in filename:
        return "!"
    return None


def annotation_weight(filename: str) -> float:
    marker = annotation_marker(filename)
    if marker in RATING_WEIGHTS:
        return RATING_WEIGHTS[marker]
    if marker == "!!!":
        return 4.0
    if marker == "!!":
        return 3.0
    if marker == "!":
        return 2.0
    return 1.0


def _observed_date(path: Path) -> date:
    for parent in path.parents:
        match = DATED_FOLDER_RE.search(parent.name)
        if match:
            try:
                return datetime.strptime(
                    f"{match.group('day')} {match.group('month')} {match.group('year')}",
                    "%d %b %Y",
                ).date()
            except ValueError:
                pass
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def scan_library(roots: Iterable[Path], limit: int) -> list[LibraryItem]:
    items: list[LibraryItem] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.pdf"):
            arxiv_id = extract_arxiv_id(path.name)
            if arxiv_id is None:
                continue
            marker = annotation_marker(path.name)
            stat = path.stat()
            items.append(
                LibraryItem(
                    arxiv_id=arxiv_id,
                    weight=annotation_weight(path.name),
                    observed_date=_observed_date(path),
                    path=str(path),
                    explicit=marker is not None,
                    fingerprint=marker or "unmarked",
                    changed_at=datetime.fromtimestamp(
                        stat.st_ctime,
                        tz=timezone.utc,
                    ),
                )
            )
    items.sort(
        key=lambda item: (
            item.changed_at or datetime.min.replace(tzinfo=timezone.utc),
            item.path,
        ),
        reverse=True,
    )
    unique: list[LibraryItem] = []
    seen: set[str] = set()
    for item in items:
        if item.arxiv_id in seen:
            continue
        seen.add(item.arxiv_id)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def _report_date(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


def parse_report_ratings(text: str) -> dict[str, str]:
    matches = list(RECORD_RE.finditer(text))
    ratings: dict[str, str] = {}
    for index, match in enumerate(matches):
        try:
            metadata = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        arxiv_id = str(metadata.get("arxiv_id", ""))
        if not arxiv_id:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        rating_match = RATING_RE.search(text, match.end(), end)
        ratings[arxiv_id] = (
            rating_match.group(1).lower() if rating_match else "unrated"
        )
    return ratings


def parse_report_history(record_dir: Path, limit: int) -> list[InterestPaper]:
    history: list[InterestPaper] = []
    paths = sorted(record_dir.glob("????-??-??.md"), reverse=True)
    for path in paths:
        text = path.read_text(encoding="utf-8")
        changed_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        matches = list(RECORD_RE.finditer(text))
        for index, match in enumerate(matches):
            try:
                paper = Paper.from_dict(json.loads(match.group(1)))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(text)
            )
            rating_match = RATING_RE.search(text, match.end(), end)
            rating = rating_match.group(1).lower() if rating_match else "unrated"
            history.append(
                InterestPaper(
                    paper=paper,
                    weight=RATING_WEIGHTS[rating],
                    observed_date=_report_date(path),
                    fingerprint=f"{path.name}:{rating}",
                    changed_at=changed_at,
                )
            )
            if len(history) >= limit:
                return history
    return history


def report_arxiv_ids(
    record_dir: Path,
    *,
    exclude_date: date | None = None,
) -> set[str]:
    seen: set[str] = set()
    for path in record_dir.glob("????-??-??.md"):
        if exclude_date is not None and path.stem == exclude_date.isoformat():
            continue
        text = path.read_text(encoding="utf-8")
        for match in RECORD_RE.finditer(text):
            try:
                arxiv_id = str(json.loads(match.group(1)).get("arxiv_id", ""))
            except json.JSONDecodeError:
                continue
            if arxiv_id:
                seen.add(arxiv_id)
    return seen
