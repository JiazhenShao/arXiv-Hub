from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, TypeVar

from filelock import FileLock

from .models import Paper


ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"
OPENSEARCH = "http://a9.com/-/spec/opensearch/1.1/"
VERSION_RE = re.compile(r"v(\d+)$")
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
DOWNLOAD_ID_RE = re.compile(
    r"(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})v\d+",
    re.IGNORECASE,
)
T = TypeVar("T")


class ArxivError(RuntimeError):
    """The official arXiv API could not provide verified metadata."""


class ArxivMetadataError(ArxivError):
    """An Atom response was malformed or omitted required metadata."""


def _text(element: ET.Element | None, field: str) -> str:
    if element is None or element.text is None:
        raise ArxivMetadataError(f"Missing required arXiv field: {field}")
    value = " ".join(element.text.split())
    if not value:
        raise ArxivMetadataError(f"Empty required arXiv field: {field}")
    return value


def _parse_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArxivMetadataError(f"Invalid {field}: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def versionless_id(value: str) -> str:
    raw = value.rstrip("/").rsplit("/", 1)[-1]
    return VERSION_RE.sub("", raw)


def parse_atom(payload: bytes) -> list[Paper]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ArxivMetadataError("Malformed arXiv Atom response") from exc

    papers: list[Paper] = []
    for entry in root.findall(f"{{{ATOM}}}entry"):
        raw_id = _text(entry.find(f"{{{ATOM}}}id"), "id")
        versioned_id = raw_id.rstrip("/").rsplit("/", 1)[-1]
        arxiv_id = versionless_id(versioned_id)
        title = _text(entry.find(f"{{{ATOM}}}title"), "title")
        abstract = _text(entry.find(f"{{{ATOM}}}summary"), "summary")
        published = _parse_datetime(
            _text(entry.find(f"{{{ATOM}}}published"), "published"),
            "published",
        )
        updated = _parse_datetime(
            _text(entry.find(f"{{{ATOM}}}updated"), "updated"),
            "updated",
        )
        authors = tuple(
            _text(author.find(f"{{{ATOM}}}name"), "author/name")
            for author in entry.findall(f"{{{ATOM}}}author")
        )
        if not authors:
            raise ArxivMetadataError(f"Paper {versioned_id} has no authors")
        categories = tuple(
            value
            for category in entry.findall(f"{{{ATOM}}}category")
            if (value := category.attrib.get("term", "").strip())
        )
        if not categories:
            raise ArxivMetadataError(f"Paper {versioned_id} has no categories")
        primary = entry.find(f"{{{ARXIV}}}primary_category")
        primary_category = (
            primary.attrib.get("term", "").strip() if primary is not None else ""
        )
        if not primary_category:
            primary_category = categories[0]

        abs_url = ""
        pdf_url = ""
        for link in entry.findall(f"{{{ATOM}}}link"):
            href = link.attrib.get("href", "").strip()
            if link.attrib.get("rel") == "alternate":
                abs_url = href
            if link.attrib.get("title") == "pdf":
                pdf_url = href
        if not abs_url:
            abs_url = f"https://arxiv.org/abs/{versioned_id}"
        if not pdf_url:
            pdf_url = f"https://arxiv.org/pdf/{versioned_id}"
        if "arxiv.org/" not in abs_url or "arxiv.org/" not in pdf_url:
            raise ArxivMetadataError(f"Paper {versioned_id} has non-arXiv links")

        papers.append(
            Paper(
                arxiv_id=arxiv_id,
                versioned_id=versioned_id,
                title=title,
                abstract=abstract,
                authors=authors,
                categories=categories,
                primary_category=primary_category,
                published=published,
                updated=updated,
                abs_url=abs_url.replace("http://", "https://"),
                pdf_url=pdf_url.replace("http://", "https://"),
            )
        )
    return papers


def atom_total_results(payload: bytes) -> int:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ArxivMetadataError("Malformed arXiv Atom response") from exc
    element = root.find(f"{{{OPENSEARCH}}}totalResults")
    if element is None or element.text is None:
        return len(root.findall(f"{{{ATOM}}}entry"))
    try:
        return int(element.text.strip())
    except ValueError as exc:
        raise ArxivMetadataError("Invalid opensearch:totalResults") from exc


def _version_number(versioned_id: str) -> int:
    match = VERSION_RE.search(versioned_id)
    return int(match.group(1)) if match else 0


def dedupe_papers(papers: Iterable[Paper]) -> list[Paper]:
    by_id: dict[str, Paper] = {}
    order: list[str] = []
    for paper in papers:
        if paper.arxiv_id not in by_id:
            order.append(paper.arxiv_id)
            by_id[paper.arxiv_id] = paper
        elif _version_number(paper.versioned_id) > _version_number(
            by_id[paper.arxiv_id].versioned_id
        ):
            by_id[paper.arxiv_id] = paper
    return [by_id[arxiv_id] for arxiv_id in order]


class ArxivClient:
    def __init__(
        self,
        categories: Iterable[str],
        *,
        endpoint: str = "https://export.arxiv.org/api/query",
        user_agent: str,
        min_interval_seconds: float = 3.0,
        timeout_seconds: float = 60.0,
        max_results: int = 200,
        state_dir: Path | None = None,
        retry_backoffs: tuple[float, ...] = (10.0, 20.0, 40.0),
        retry_deadline_seconds: float = 90.0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        opener: Callable[..., object] = urllib.request.urlopen,
        allowed_download_hosts: set[str] | None = None,
        allowed_download_schemes: set[str] | None = None,
    ) -> None:
        self.categories = tuple(categories)
        self.endpoint = endpoint
        self.user_agent = user_agent
        self.min_interval_seconds = min_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.max_results = max_results
        self.retry_backoffs = retry_backoffs
        self.retry_deadline_seconds = retry_deadline_seconds
        self.max_attempts = len(retry_backoffs) + 1
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleeper
        self._opener = opener
        self.allowed_download_hosts = allowed_download_hosts or {
            "arxiv.org",
            "export.arxiv.org",
        }
        self.allowed_download_schemes = allowed_download_schemes or {"https"}
        self._memory_state = {"last_request_at": 0.0, "not_before": 0.0}
        if state_dir is None:
            self._throttle_path = None
            self._request_lock: FileLock | threading.Lock = threading.Lock()
        else:
            state_dir.mkdir(parents=True, exist_ok=True)
            self._throttle_path = state_dir / "arxiv-api-throttle.json"
            self._request_lock = FileLock(
                str(state_dir / "arxiv-api-throttle.lock")
            )

    def _read_throttle_state(self, now: float) -> dict[str, float]:
        if self._throttle_path is None:
            raw = self._memory_state
        else:
            try:
                raw = json.loads(self._throttle_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                raw = {}
        state: dict[str, float] = {}
        maximum_future = now + self.retry_deadline_seconds
        for key in ("last_request_at", "not_before"):
            try:
                value = float(raw.get(key, 0.0))
            except (TypeError, ValueError, AttributeError):
                value = 0.0
            if not math.isfinite(value) or value < 0 or value > maximum_future:
                value = 0.0
            state[key] = value
        return state

    def _write_throttle_state(self, state: dict[str, float]) -> None:
        if self._throttle_path is None:
            self._memory_state = dict(state)
            return
        payload = json.dumps(state, sort_keys=True) + "\n"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._throttle_path.parent,
                prefix=f".{self._throttle_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, self._throttle_path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def _remaining(self, started_at: float) -> float:
        return self.retry_deadline_seconds - (self._monotonic() - started_at)

    def _sleep_before(self, target: float, started_at: float) -> None:
        delay = max(0.0, target - self._clock())
        if delay <= 0:
            return
        if delay > self._remaining(started_at):
            raise ArxivError(
                "arXiv retry deadline reached before another request could start"
            )
        self._sleep(delay)

    def _retry_after_seconds(
        self,
        error: urllib.error.HTTPError,
        fallback: float,
    ) -> float:
        value = error.headers.get("Retry-After") if error.headers else None
        if not value:
            return fallback
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return fallback
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, retry_at.timestamp() - self._clock())

    def _retry_delay(self, exc: BaseException, attempt: int) -> float:
        fallback = self.retry_backoffs[attempt - 1]
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
            return self._retry_after_seconds(exc, fallback)
        return fallback

    def _terminal_error(self, exc: BaseException, attempts: int) -> ArxivError:
        deadline = f"{self.retry_deadline_seconds:g}"
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code == 429:
                return ArxivError(
                    f"arXiv rate limit persisted after {deadline} seconds "
                    f"({attempts} attempts)"
                )
            if exc.code in RETRYABLE_HTTP_CODES:
                return ArxivError(
                    f"arXiv remained temporarily unavailable after {deadline} "
                    f"seconds ({attempts} attempts; HTTP {exc.code})"
                )
            return ArxivError(
                f"arXiv request failed with HTTP {exc.code}: {exc.reason}"
            )
        return ArxivError(
            f"arXiv request failed after {deadline} seconds "
            f"({attempts} attempts): {exc}"
        )

    def _execute(
        self,
        request: urllib.request.Request,
        consume: Callable[[object], T],
    ) -> T:
        started_at = self._monotonic()
        last_error: BaseException | None = None
        attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            attempts = attempt
            retry_delay = 0.0
            retryable = False
            with self._request_lock:
                now = self._clock()
                state = self._read_throttle_state(now)
                target = max(
                    state["last_request_at"] + self.min_interval_seconds,
                    state["not_before"],
                )
                try:
                    self._sleep_before(target, started_at)
                    remaining = self._remaining(started_at)
                    if remaining <= 0:
                        raise ArxivError(
                            "arXiv retry deadline reached before request"
                        )
                    timeout = min(self.timeout_seconds, remaining)
                    with self._opener(request, timeout=timeout) as response:
                        result = consume(response)
                except urllib.error.HTTPError as exc:
                    last_error = exc
                    retryable = exc.code in RETRYABLE_HTTP_CODES
                    if retryable and attempt < self.max_attempts:
                        retry_delay = self._retry_delay(exc, attempt)
                    exc.close()
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    last_error = exc
                    retryable = True
                    if attempt < self.max_attempts:
                        retry_delay = self._retry_delay(exc, attempt)
                else:
                    completed_at = self._clock()
                    self._write_throttle_state(
                        {
                            "last_request_at": completed_at,
                            "not_before": 0.0,
                        }
                    )
                    return result

                completed_at = self._clock()
                can_retry = (
                    retryable
                    and attempt < self.max_attempts
                    and retry_delay <= self._remaining(started_at)
                )
                self._write_throttle_state(
                    {
                        "last_request_at": completed_at,
                        "not_before": (
                            completed_at + retry_delay if can_retry else 0.0
                        ),
                    }
                )
            if not can_retry:
                assert last_error is not None
                raise self._terminal_error(last_error, attempts) from last_error

        assert last_error is not None
        raise self._terminal_error(last_error, attempts) from last_error

    def _request(self, parameters: dict[str, str | int]) -> bytes:
        url = f"{self.endpoint}?{urllib.parse.urlencode(parameters)}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "application/atom+xml"},
        )
        return self._execute(request, lambda response: response.read())

    def download_pdf(
        self,
        versioned_id: str,
        destination: Path,
        *,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        if not DOWNLOAD_ID_RE.fullmatch(versioned_id):
            raise ArxivError("Invalid versioned arXiv ID for PDF download")
        safe_id = urllib.parse.quote(versioned_id, safe="/.-")
        self.download_pdf_url(
            f"https://arxiv.org/pdf/{safe_id}",
            destination,
            max_bytes=max_bytes,
        )

    def download_pdf_url(
        self,
        url: str,
        destination: Path,
        *,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self._validate_download_url(url, redirect=False)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "application/pdf"},
        )

        def stream(response: object) -> None:
            final_url = response.geturl()
            self._validate_download_url(final_url, redirect=True)
            content_type = response.headers.get("Content-Type", "")
            content_type = content_type.split(";", 1)[0].strip().lower()
            if content_type != "application/pdf":
                raise ArxivError("arXiv PDF response has an invalid content type")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    announced_size = int(content_length)
                except ValueError as exc:
                    raise ArxivError("arXiv PDF response has invalid length") from exc
                if announced_size > max_bytes:
                    raise ArxivError("arXiv PDF exceeds the configured size limit")

            destination.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            signature = b""
            try:
                with destination.open("wb") as handle:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ArxivError(
                                "arXiv PDF exceeds the configured size limit"
                            )
                        if len(signature) < 5:
                            signature += chunk[: 5 - len(signature)]
                        handle.write(chunk)
                    if signature != b"%PDF-":
                        raise ArxivError("Downloaded file is not a valid PDF")
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
                raise

        self._execute(request, stream)

    def _validate_download_url(self, url: str, *, redirect: bool) -> None:
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme.lower() not in self.allowed_download_schemes
            or (parsed.hostname or "").lower() not in self.allowed_download_hosts
        ):
            noun = "redirect" if redirect else "URL"
            raise ArxivError(f"arXiv PDF {noun} is not approved")

    def fetch_candidates(self, start: date, end: date) -> list[Paper]:
        category_query = " OR ".join(f"cat:{category}" for category in self.categories)
        date_query = (
            f"submittedDate:[{start:%Y%m%d}0000 TO {end:%Y%m%d}2359]"
        )
        parameters: dict[str, str | int] = {
                "search_query": f"({category_query}) AND {date_query}",
                "start": 0,
                "max_results": self.max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
        }
        papers: list[Paper] = []
        expected_total: int | None = None
        start_index = 0
        while expected_total is None or start_index < expected_total:
            parameters["start"] = start_index
            payload = self._request(parameters)
            page = parse_atom(payload)
            page_total = atom_total_results(payload)
            if expected_total is None:
                expected_total = page_total
            elif page_total != expected_total:
                raise ArxivMetadataError(
                    "arXiv pagination total changed during retrieval"
                )
            if not page:
                if start_index < expected_total:
                    raise ArxivMetadataError(
                        "arXiv pagination ended before totalResults"
                    )
                break
            papers.extend(page)
            start_index += len(page)
        return dedupe_papers(papers)

    def fetch_by_ids(self, arxiv_ids: list[str]) -> list[Paper]:
        if not arxiv_ids:
            return []
        papers: list[Paper] = []
        for offset in range(0, len(arxiv_ids), 100):
            payload = self._request(
                {
                    "id_list": ",".join(arxiv_ids[offset : offset + 100]),
                    "max_results": 100,
                }
            )
            papers.extend(parse_atom(payload))
        return dedupe_papers(papers)
