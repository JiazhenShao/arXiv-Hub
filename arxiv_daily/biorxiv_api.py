from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from .models import Paper


BIORXIV_API = "https://api.biorxiv.org/details"
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
_PAGE_SIZE = 100


class BiorxivError(RuntimeError):
    """The bioRxiv/medRxiv API could not provide verified metadata."""


def _parse_authors(authors_str: str) -> tuple[str, ...]:
    """Parse semicolon-separated author string from bioRxiv API."""
    parts = [part.strip() for part in authors_str.split(";") if part.strip()]
    return tuple(parts) if parts else ("Unknown",)


def _parse_paper(item: dict, server: str) -> Paper | None:
    """Parse a single paper record from the bioRxiv/medRxiv API response."""
    try:
        doi = str(item["doi"]).strip()
        if not doi:
            return None
        version = str(item.get("version", "1")).strip()
        versioned_id = f"{doi}v{version}"
        title = " ".join(str(item.get("title", "")).split())
        abstract = " ".join(str(item.get("abstract", "")).split())
        if not title or not abstract:
            return None
        authors = _parse_authors(str(item.get("authors", "")))
        category = str(item.get("category", "")).strip().lower().replace(" ", "-")
        if not category:
            category = server
        date_str = str(item.get("date", "")).strip()
        try:
            published = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        abs_url = f"https://www.{server}.org/content/{versioned_id}"
        pdf_url = f"{abs_url}.full.pdf"
        return Paper(
            arxiv_id=doi,
            versioned_id=versioned_id,
            title=title,
            abstract=abstract,
            authors=authors,
            categories=(category,),
            primary_category=category,
            published=published,
            updated=published,
            abs_url=abs_url,
            pdf_url=pdf_url,
            source=server,
        )
    except (KeyError, TypeError, ValueError):
        return None


class BiorxivClient:
    """Fetches preprint metadata from the bioRxiv or medRxiv API.

    The ``server`` parameter should be ``"biorxiv"`` or ``"medrxiv"``.
    If ``subjects`` is provided, only papers whose category matches one of
    the given subjects are returned.
    """

    def __init__(
        self,
        server: str = "biorxiv",
        *,
        subjects: list[str] | None = None,
        user_agent: str,
        min_interval_seconds: float = 3.0,
        timeout_seconds: float = 60.0,
        retry_backoffs: tuple[float, ...] = (10.0, 20.0, 40.0),
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        if server not in ("biorxiv", "medrxiv"):
            raise ValueError(f"server must be 'biorxiv' or 'medrxiv', got {server!r}")
        self.server = server
        self.subjects = {s.lower().replace(" ", "-") for s in subjects} if subjects else None
        self.user_agent = user_agent
        self.min_interval_seconds = min_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.retry_backoffs = retry_backoffs
        self.max_attempts = len(retry_backoffs) + 1
        self._clock = clock
        self._sleep = sleeper
        self._opener = opener
        self._last_request_at: float = 0.0

    def _get(self, url: str) -> dict:
        for attempt in range(1, self.max_attempts + 1):
            wait = max(0.0, self._last_request_at + self.min_interval_seconds - self._clock())
            if wait > 0:
                self._sleep(wait)
            request = urllib.request.Request(
                url,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    self._last_request_at = self._clock()
                    raw = response.read()
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                self._last_request_at = self._clock()
                if exc.code in RETRYABLE_HTTP_CODES and attempt < self.max_attempts:
                    backoff = self.retry_backoffs[attempt - 1]
                    self._sleep(backoff)
                    continue
                raise BiorxivError(
                    f"{self.server} request failed with HTTP {exc.code}: {exc.reason}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._last_request_at = self._clock()
                if attempt < self.max_attempts:
                    self._sleep(self.retry_backoffs[attempt - 1])
                    continue
                raise BiorxivError(f"{self.server} request failed: {exc}") from exc
        raise BiorxivError(f"{self.server} request failed after {self.max_attempts} attempts")

    def _papers_from_response(self, data: dict) -> list[Paper]:
        collection = data.get("collection", [])
        papers: list[Paper] = []
        for item in collection:
            paper = _parse_paper(item, self.server)
            if paper is None:
                continue
            if self.subjects is not None and paper.primary_category not in self.subjects:
                continue
            papers.append(paper)
        return papers

    def fetch_candidates(self, start: date, end: date) -> list[Paper]:
        """Fetch all preprints in the given date range (by submission date)."""
        cursor = 0
        papers: list[Paper] = []
        seen_dois: set[str] = set()
        while True:
            url = (
                f"{BIORXIV_API}/{self.server}"
                f"/{start.isoformat()}/{end.isoformat()}/{cursor}/json"
            )
            data = self._get(url)
            messages = data.get("messages", [{}])
            status = messages[0].get("status", "") if messages else ""
            if status != "ok" and not data.get("collection"):
                break
            page = self._papers_from_response(data)
            for paper in page:
                if paper.arxiv_id not in seen_dois:
                    seen_dois.add(paper.arxiv_id)
                    papers.append(paper)
            total = int(messages[0].get("total", 0)) if messages else 0
            count = int(messages[0].get("count", len(page))) if messages else len(page)
            cursor += count
            if cursor >= total or not page:
                break
        return papers

    def fetch_by_ids(self, arxiv_ids: list[str]) -> list[Paper]:
        """Fetch papers by DOI (arxiv_id for bioRxiv/medRxiv papers)."""
        if not arxiv_ids:
            return []
        # Filter to only IDs that look like bioRxiv/medRxiv DOIs
        relevant = [id_ for id_ in arxiv_ids if id_.startswith("10.1101/")]
        papers: list[Paper] = []
        for doi in relevant:
            url = f"{BIORXIV_API}/{self.server}/{urllib.parse.quote(doi, safe='/')}/json"
            try:
                data = self._get(url)
            except BiorxivError:
                continue
            page = self._papers_from_response(data)
            if page:
                # Return the latest version
                latest = max(page, key=lambda p: p.versioned_id)
                papers.append(latest)
        return papers
