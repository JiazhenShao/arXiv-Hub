from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import Callable

from .models import Paper


CHEMRXIV_API = "https://chemrxiv.org/engage/chemrxiv/public-api/v1/items"
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
_PAGE_SIZE = 50  # chemRxiv API max per page


class ChemrxivError(RuntimeError):
    """The chemRxiv API could not provide verified metadata."""


def _parse_paper(item: dict) -> Paper | None:
    """Parse a single item record from the chemRxiv API response."""
    try:
        content_id = str(item.get("id", "")).strip()
        doi = str(item.get("doi", "")).strip()
        paper_id = doi if doi else content_id
        if not paper_id:
            return None
        versioned_id = paper_id
        title = " ".join(str(item.get("title", "")).split())
        abstract = " ".join(str(item.get("abstract", "")).split())
        if not title or not abstract:
            return None
        authors_raw = item.get("authors", [])
        if isinstance(authors_raw, list):
            authors = tuple(
                f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
                for a in authors_raw
                if isinstance(a, dict)
            ) or ("Unknown",)
        else:
            authors = ("Unknown",)
        subject = str(item.get("subject", "")).strip().lower().replace(" ", "-")
        if not subject:
            subject = "chemistry"
        date_str = str(item.get("publishedDate", item.get("submittedDate", ""))).strip()
        try:
            # chemRxiv dates are ISO 8601 with timezone
            published = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        abs_url = f"https://chemrxiv.org/engage/chemrxiv/article-details/{content_id}"
        # Try to get the PDF URL from the asset field
        pdf_url = ""
        asset = item.get("asset", {})
        if isinstance(asset, dict):
            original = asset.get("original", {})
            if isinstance(original, dict):
                pdf_url = str(original.get("url", "")).strip()
        if not pdf_url:
            pdf_url = abs_url
        return Paper(
            arxiv_id=paper_id,
            versioned_id=versioned_id,
            title=title,
            abstract=abstract,
            authors=authors,
            categories=(subject,),
            primary_category=subject,
            published=published,
            updated=published,
            abs_url=abs_url,
            pdf_url=pdf_url,
            source="chemrxiv",
        )
    except (KeyError, TypeError, ValueError):
        return None


class ChemrxivClient:
    """Fetches preprint metadata from the chemRxiv public API.

    If ``subjects`` is provided, only papers whose subject matches one of
    the given subjects are returned.
    """

    def __init__(
        self,
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
                    self._sleep(self.retry_backoffs[attempt - 1])
                    continue
                raise ChemrxivError(
                    f"chemRxiv request failed with HTTP {exc.code}: {exc.reason}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._last_request_at = self._clock()
                if attempt < self.max_attempts:
                    self._sleep(self.retry_backoffs[attempt - 1])
                    continue
                raise ChemrxivError(f"chemRxiv request failed: {exc}") from exc
        raise ChemrxivError(f"chemRxiv request failed after {self.max_attempts} attempts")

    def _papers_from_response(self, data: dict) -> list[Paper]:
        hits = data.get("itemHits", [])
        papers: list[Paper] = []
        for hit in hits:
            item = hit.get("item", hit) if isinstance(hit, dict) else hit
            paper = _parse_paper(item)
            if paper is None:
                continue
            if self.subjects is not None and paper.primary_category not in self.subjects:
                continue
            papers.append(paper)
        return papers

    def fetch_candidates(self, start: date, end: date) -> list[Paper]:
        """Fetch all chemRxiv preprints in the given date range."""
        skip = 0
        papers: list[Paper] = []
        seen_ids: set[str] = set()
        while True:
            params = urllib.parse.urlencode({
                "sort": "published-date-desc",
                "limit": _PAGE_SIZE,
                "skip": skip,
                "dateFrom": start.isoformat(),
                "dateTo": end.isoformat(),
            })
            url = f"{CHEMRXIV_API}?{params}"
            data = self._get(url)
            page = self._papers_from_response(data)
            total = int(data.get("totalCount", 0))
            for paper in page:
                if paper.arxiv_id not in seen_ids:
                    seen_ids.add(paper.arxiv_id)
                    papers.append(paper)
            skip += _PAGE_SIZE
            if skip >= total or not page:
                break
        return papers

    def fetch_by_ids(self, arxiv_ids: list[str]) -> list[Paper]:
        """Fetch chemRxiv papers by their DOI or content ID."""
        if not arxiv_ids:
            return []
        # Filter to chemRxiv DOIs (10.26434/...) or UUID-like content IDs
        relevant = [
            id_ for id_ in arxiv_ids
            if id_.startswith("10.26434/") or (len(id_) > 30 and "-" in id_)
        ]
        papers: list[Paper] = []
        for paper_id in relevant:
            # Try looking up by searching for the DOI
            params = urllib.parse.urlencode({"term": paper_id, "limit": 1})
            url = f"{CHEMRXIV_API}?{params}"
            try:
                data = self._get(url)
            except ChemrxivError:
                continue
            page = self._papers_from_response(data)
            if page:
                papers.append(page[0])
        return papers
