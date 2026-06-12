from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .history import RECORD_RE, RATING_RE, extract_arxiv_id
from .models import Paper


MATCHED_INTERESTS_RE = re.compile(
    r"\*\*Matched interests:\*\*\s*(.+)",
    re.IGNORECASE,
)
REPORT_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
VERSION_RE = re.compile(r"v(\d+)$")
MAX_FILENAME_BYTES = 180
MAX_PDF_BYTES = 100 * 1024 * 1024
CONCEPT_TAG_MAP = {
    "nuclear astrophysics": "Nucl-Astro",
    "neutron stars": "NS",
    "field theory and rg methods": "RG",
    "high-energy astronomical phenomena": "HEA",
    "group theory": "Group",
    "equation of state": "EoS",
    "eos": "EoS",
}
CONCEPT_TAG_PHRASES = (
    ("EoS", ("equation of state", "equations of state", " eos ")),
    ("NS", ("neutron star", "neutron-star", "neutron stars")),
    ("RG", ("renormalization group", "renormalisation group", "functional rg")),
)
RATING_TAGS = {
    "high": "!!!",
    "medium": "!!",
    "low": "!",
    "skip": "skip",
    "unrated": "unrated",
}
RATINGS = {"high", "medium", "low", "skip", "unrated"}
SELECTED_CROSS_LISTS = frozenset({"hep-ph", "nucl-th"})


class DownloadError(RuntimeError):
    """A PDF could not be validated and installed safely."""


@dataclass(frozen=True)
class DownloadCandidate:
    paper: Paper
    rating: str
    matched_interests: tuple[str, ...]
    report_date: str

    @property
    def arxiv_id(self) -> str:
        return self.paper.arxiv_id

    @property
    def versioned_id(self) -> str:
        return self.paper.versioned_id


@dataclass(frozen=True)
class DownloadEntry:
    versioned_id: str
    filename: str
    rating: str
    tags: tuple[str, ...]
    report_date: str

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DownloadEntry":
        return cls(
            versioned_id=str(data["versioned_id"]),
            filename=str(data["filename"]),
            rating=str(data["rating"]),
            tags=tuple(str(value) for value in data.get("tags", [])),
            report_date=str(data["report_date"]),
        )


@dataclass(frozen=True)
class DownloadStatus:
    state: str
    message: str
    filename: str = ""


class DownloadManifest:
    def __init__(
        self,
        path: Path,
        entries: dict[str, DownloadEntry] | None = None,
    ) -> None:
        self.path = path
        self.entries = entries or {}

    @classmethod
    def load(cls, path: Path) -> "DownloadManifest":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError, TypeError) as exc:
            raise DownloadError("Download manifest is unreadable") from exc
        if not isinstance(raw, dict):
            raise DownloadError("Download manifest is invalid")
        try:
            entries = {
                str(arxiv_id): DownloadEntry.from_dict(entry)
                for arxiv_id, entry in raw.get("entries", {}).items()
            }
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise DownloadError("Download manifest is invalid") from exc
        return cls(path, entries)

    def set(
        self,
        *,
        arxiv_id: str,
        versioned_id: str,
        filename: str,
        rating: str,
        tags: tuple[str, ...],
        report_date: str,
    ) -> None:
        self.entries[arxiv_id] = DownloadEntry(
            versioned_id=versioned_id,
            filename=filename,
            rating=rating,
            tags=tags,
            report_date=report_date,
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "entries": {
                arxiv_id: {
                    **asdict(entry),
                    "tags": list(entry.tags),
                }
                for arxiv_id, entry in sorted(self.entries.items())
            },
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


def _canonical_rating(value: str) -> str:
    normalized = value.lower()
    if normalized == "strong":
        return "high"
    if normalized == "maybe":
        return "medium"
    return normalized if normalized in RATINGS else "unrated"


def _parse_report_candidates(path: Path) -> dict[str, DownloadCandidate]:
    text = path.read_text(encoding="utf-8")
    matches = list(RECORD_RE.finditer(text))
    candidates: dict[str, DownloadCandidate] = {}
    for index, match in enumerate(matches):
        try:
            paper = Paper.from_dict(json.loads(match.group(1)))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if paper.arxiv_id in candidates:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.end() : end]
        rating_match = RATING_RE.search(section)
        matched_interests = MATCHED_INTERESTS_RE.search(section)
        interests = (
            tuple(
                value.strip()
                for value in matched_interests.group(1).split(",")
                if value.strip()
            )
            if matched_interests
            else ()
        )
        candidates[paper.arxiv_id] = DownloadCandidate(
            paper=paper,
            rating=_canonical_rating(
                rating_match.group(1) if rating_match else "unrated"
            ),
            matched_interests=interests,
            report_date=path.stem,
        )
    return candidates


def collect_report_papers(
    record_dir: Path,
    report_date: str,
) -> dict[str, DownloadCandidate]:
    if not REPORT_DATE_RE.fullmatch(report_date):
        raise DownloadError("Invalid report date")
    path = record_dir / f"{report_date}.md"
    if not path.is_file() or path.is_symlink():
        raise DownloadError(f"No regular Markdown report exists for {report_date}")
    try:
        return _parse_report_candidates(path)
    except OSError as exc:
        raise DownloadError(f"Report {report_date} could not be read") from exc


def collect_effective_papers(record_dir: Path) -> dict[str, DownloadCandidate]:
    effective: dict[str, DownloadCandidate] = {}
    for path in sorted(record_dir.glob("????-??-??.md"), reverse=True):
        if not path.is_file() or path.is_symlink():
            continue
        for arxiv_id, candidate in _parse_report_candidates(path).items():
            effective.setdefault(arxiv_id, candidate)
    return effective


def candidate_tags(candidate: DownloadCandidate) -> tuple[str, ...]:
    return candidate_concept_tags(candidate) + candidate_category_tags(candidate)


def candidate_concept_tags(candidate: DownloadCandidate) -> tuple[str, ...]:
    tags: list[str] = []

    def add(tag: str) -> bool:
        if tag and tag not in tags:
            tags.append(tag)
        return len(tags) == 3

    for interest in candidate.matched_interests:
        normalized = interest.strip().lower()
        if normalized == "category prior only":
            continue
        tag = CONCEPT_TAG_MAP.get(normalized)
        if tag is None:
            continue
        if add(tag):
            return tuple(tags)
    searchable = f" {candidate.paper.title} {candidate.paper.abstract} ".lower()
    for tag, phrases in CONCEPT_TAG_PHRASES:
        if any(phrase in searchable for phrase in phrases) and add(tag):
            return tuple(tags)
    return tuple(tags)


def candidate_category_tags(candidate: DownloadCandidate) -> tuple[str, ...]:
    primary = candidate.paper.primary_category
    tags = [primary]
    for category in candidate.paper.categories:
        if (
            category != primary
            and category in SELECTED_CROSS_LISTS
            and category not in tags
        ):
            tags.append(category)
    return tuple(tags)


def _sanitize_title(value: str) -> str:
    title = unicodedata.normalize("NFKC", value)
    previous = None
    while previous != title:
        previous = title
        title = re.sub(
            r"\\[A-Za-z]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}",
            r"\1",
            title,
        )
    title = re.sub(r"\\[A-Za-z]+", "", title)
    title = title.translate(str.maketrans({"$": "", "{": "", "}": "", "~": " "}))
    title = re.sub(r"[\x00-\x1f/:*?\"<>|]+", " - ", title)
    title = re.sub(r"\s+", " ", title).strip(" .-")
    return title or "Untitled arXiv Paper"


def _safe_versioned_id(value: str) -> str:
    return value.replace("/", "_")


def managed_filename(
    candidate: DownloadCandidate,
    *,
    versioned_id: str | None = None,
) -> str:
    concept_tags = candidate_concept_tags(candidate)
    category_tags = candidate_category_tags(candidate)
    rating_tag = RATING_TAGS[candidate.rating]
    prefix = f"[{rating_tag}]" + "".join(f"[{tag}]" for tag in concept_tags)
    safe_id = _safe_versioned_id(versioned_id or candidate.versioned_id)
    category_suffix = "".join(f"[{tag}]" for tag in category_tags)
    suffix = f" - {category_suffix}{safe_id}.pdf"
    title = _sanitize_title(candidate.paper.title)
    while len(f"{prefix} {title}{suffix}".encode("utf-8")) > MAX_FILENAME_BYTES:
        title = title[:-1].rstrip()
        if not title:
            raise DownloadError("Filename metadata exceeds safe length")
    return f"{prefix} {title}{suffix}"


def _version_number(value: str) -> int:
    match = VERSION_RE.search(value)
    return int(match.group(1)) if match else 0


class PaperDownloader:
    def __init__(
        self,
        *,
        record_dir: Path,
        library_dir: Path,
        state_dir: Path,
        fetcher: Callable[[DownloadCandidate, Path], object] | None = None,
    ) -> None:
        self.record_dir = record_dir
        self.library_dir = library_dir
        self.state_dir = state_dir
        self.fetcher = fetcher
        self.manifest_path = state_dir / "downloads.json"

    def _matching_files(self, arxiv_id: str) -> list[Path]:
        if not self.library_dir.exists():
            return []
        return sorted(
            path
            for path in self.library_dir.glob("*.pdf")
            if path.is_file()
            and not path.is_symlink()
            and extract_arxiv_id(path.name) == arxiv_id
        )

    def _cleanup_temporary_files(self) -> None:
        if not self.library_dir.exists():
            return
        for path in self.library_dir.glob(".arxiv-download-*.tmp"):
            if path.is_file() and not path.is_symlink():
                path.unlink()

    def reconcile(self, report_date: str) -> dict[str, DownloadStatus]:
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_temporary_files()
        manifest = DownloadManifest.load(self.manifest_path)
        candidates = collect_report_papers(self.record_dir, report_date)
        effective = collect_effective_papers(self.record_dir)
        statuses: dict[str, DownloadStatus] = {}
        changed = False

        for arxiv_id, candidate in candidates.items():
            authoritative = effective.get(arxiv_id)
            if (
                authoritative is not None
                and authoritative.report_date != candidate.report_date
            ):
                statuses[arxiv_id] = DownloadStatus(
                    "superseded",
                    "Superseded by newer report "
                    f"{authoritative.report_date}.",
                )
                continue
            entry = manifest.entries.get(arxiv_id)
            if entry is None:
                matches = self._matching_files(arxiv_id)
                if len(matches) > 1:
                    statuses[arxiv_id] = DownloadStatus(
                        "conflict",
                        "Multiple matching PDFs require manual cleanup.",
                    )
                    continue
                if len(matches) == 1:
                    existing = matches[0]
                    try:
                        _validate_pdf(existing)
                    except DownloadError as exc:
                        statuses[arxiv_id] = DownloadStatus(
                            "failed",
                            f"Existing matching file was not adopted: {exc}",
                            existing.name,
                        )
                        continue
                    existing_version = _filename_version(existing.name)
                    managed_version = existing_version or candidate.versioned_id
                    desired = managed_filename(
                        candidate,
                        versioned_id=managed_version,
                    )
                    destination = self.library_dir / desired
                    if existing != destination:
                        if destination.exists():
                            statuses[arxiv_id] = DownloadStatus(
                                "conflict",
                                "Managed filename already exists.",
                            )
                            continue
                        os.replace(existing, destination)
                    manifest.set(
                        arxiv_id=arxiv_id,
                        versioned_id=managed_version,
                        filename=desired,
                        rating=candidate.rating,
                        tags=candidate_tags(candidate),
                        report_date=candidate.report_date,
                    )
                    changed = True
                    statuses[arxiv_id] = DownloadStatus(
                        "existing",
                        "Existing PDF adopted.",
                        desired,
                    )
                    continue
                statuses[arxiv_id] = DownloadStatus(
                    "pending" if candidate.rating == "high" else "not-downloaded",
                    "Ready to download." if candidate.rating == "high" else "Not downloaded.",
                )
                continue

            current = self.library_dir / entry.filename
            if not current.is_file() or current.is_symlink():
                manifest.entries.pop(arxiv_id, None)
                changed = True
                statuses[arxiv_id] = DownloadStatus(
                    "pending" if candidate.rating == "high" else "not-downloaded",
                    "Managed PDF is missing.",
                )
                continue
            try:
                _validate_pdf(current)
            except DownloadError as exc:
                statuses[arxiv_id] = DownloadStatus(
                    "failed",
                    f"Managed PDF failed validation: {exc}",
                    current.name,
                )
                continue

            local_version = entry.versioned_id
            desired = managed_filename(candidate, versioned_id=local_version)
            if current.name != desired:
                destination = self.library_dir / desired
                if destination.exists() and destination != current:
                    statuses[arxiv_id] = DownloadStatus(
                        "conflict",
                        "Managed filename already exists.",
                        current.name,
                    )
                    continue
                os.replace(current, destination)
                current = destination
                changed = True
            manifest.set(
                arxiv_id=arxiv_id,
                versioned_id=local_version,
                filename=current.name,
                rating=candidate.rating,
                tags=candidate_tags(candidate),
                report_date=candidate.report_date,
            )
            changed = True
            newer_available = (
                _version_number(candidate.versioned_id)
                > _version_number(local_version)
            )
            statuses[arxiv_id] = DownloadStatus(
                "pending" if newer_available and candidate.rating == "high" else "downloaded",
                "Newer version ready to download."
                if newer_available and candidate.rating == "high"
                else "PDF is in the reading library.",
                current.name,
            )

        if changed:
            manifest.save()
        return statuses

    def download_high(
        self,
        report_date: str,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, DownloadStatus]:
        statuses = self.reconcile(report_date)
        candidates = collect_report_papers(self.record_dir, report_date)
        manifest = DownloadManifest.load(self.manifest_path)
        pending = [
            (arxiv_id, candidate)
            for arxiv_id, candidate in candidates.items()
            if candidate.rating == "high"
            and statuses.get(arxiv_id, DownloadStatus("", "")).state == "pending"
        ]
        for index, (arxiv_id, candidate) in enumerate(pending, start=1):
            if progress is not None:
                progress(index, len(pending), arxiv_id)
            old_entry = manifest.entries.get(arxiv_id)
            old_path = (
                self.library_dir / old_entry.filename
                if old_entry is not None
                else None
            )
            filename = managed_filename(candidate)
            destination = self.library_dir / filename
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".arxiv-download-",
                suffix=".tmp",
                dir=self.library_dir,
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                if self.fetcher is None:
                    raise DownloadError("PDF downloader is not configured")
                self.fetcher(candidate, temporary)
                _validate_pdf(temporary)
                if destination.exists() and destination != old_path:
                    raise DownloadError("Destination filename already exists")
                os.replace(temporary, destination)
                if (
                    old_path is not None
                    and old_path != destination
                    and old_path.is_file()
                ):
                    old_path.unlink()
                manifest.set(
                    arxiv_id=arxiv_id,
                    versioned_id=candidate.versioned_id,
                    filename=filename,
                    rating=candidate.rating,
                    tags=candidate_tags(candidate),
                    report_date=candidate.report_date,
                )
                manifest.save()
                statuses[arxiv_id] = DownloadStatus(
                    "downloaded",
                    "PDF downloaded.",
                    filename,
                )
            except Exception as exc:
                statuses[arxiv_id] = DownloadStatus(
                    "failed",
                    str(exc),
                    old_entry.filename if old_entry else "",
                )
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        return statuses


def _filename_version(filename: str) -> str | None:
    match = re.search(
        r"((?:\d{4}\.\d{4,5}|[a-z-]+[_/]\d{7})v\d+)",
        filename,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).replace("_", "/", 1) if "." not in match.group(1) else match.group(1)


def _validate_pdf(path: Path) -> None:
    size = path.stat().st_size
    if size <= 5:
        raise DownloadError("Downloaded PDF is empty")
    if size > MAX_PDF_BYTES:
        raise DownloadError("Downloaded PDF exceeds 100 MB")
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise DownloadError("Downloaded file is not a valid PDF")
