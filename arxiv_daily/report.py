from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from .history import parse_report_ratings
from .models import RankedPaper


class ExistingReportError(RuntimeError):
    """A dated report already exists and force was not requested."""


def format_authors(authors: tuple[str, ...], limit: int = 3) -> str:
    meaningful = tuple(
        author for author in authors if any(character.isalnum() for character in author)
    )
    visible = ", ".join(meaningful[:limit])
    return f"{visible}, et al." if len(meaningful) > limit else visible


def _quote_abstract(abstract: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in abstract.splitlines())


def render_report(
    *,
    run_date: date,
    announcement_date: date,
    query_start: date,
    fetched_at: datetime,
    candidate_count: int,
    model_label: str,
    selected: list[RankedPaper],
) -> str:
    lines = [
        f"# Daily Preprint Recommendations — {run_date.isoformat()}",
        "",
        f"- **Announcement batch:** {announcement_date.isoformat()}",
        f"- **Query window:** {query_start.isoformat()} to {run_date.isoformat()}",
        f"- **Fetched at:** {fetched_at.astimezone(timezone.utc).isoformat()}",
        f"- **Verified unseen candidates:** {candidate_count}",
        f"- **Embedding model:** `{model_label}`",
        f"- **Selected papers:** {len(selected)}",
        "",
        "Papers below were retrieved from official preprint server APIs (arXiv, bioRxiv, chemRxiv).",
        "Titles and abstracts are copied verbatim from that metadata; selection explanations are deterministic score summaries.",
        "",
    ]
    for index, item in enumerate(selected, start=1):
        paper = item.paper
        metadata = json.dumps(paper.to_dict(), ensure_ascii=True, separators=(",", ":"))
        authors = format_authors(paper.authors)
        categories = ", ".join(f"`{value}`" for value in paper.categories)
        matched = ", ".join(item.matched_topics) or "category prior only"
        source_label = {
            "biorxiv": "bioRxiv",
            "medrxiv": "medRxiv",
            "chemrxiv": "chemRxiv",
        }.get(paper.source, "arXiv")
        lines.extend(
            [
                f"## {index}. [{paper.title}]({paper.abs_url})",
                "",
                f"<!-- arxiv-record:{metadata} -->",
                f"- **{source_label}:** [`{paper.versioned_id}`]({paper.abs_url}) · [PDF]({paper.pdf_url})",
                f"- **Submitted:** {paper.published.date().isoformat()} · **Updated:** {paper.updated.date().isoformat()}",
                f"- **Authors:** {authors}",
                f"- **Categories:** {categories}",
                f"- **Tier:** {item.tier}",
                f"- **Relevance:** {round(item.score * 100)}/100",
                f"- **Matched interests:** {matched}",
                f"- **Why selected:** {item.why}",
                "- **Interest:** unrated <!-- edit to high, medium, low, skip, or unrated -->",
                "",
                "**Abstract (verbatim from arXiv):**",
                "",
                _quote_abstract(paper.abstract),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _carry_ratings(text: str, ratings: dict[str, str]) -> str:
    if not ratings:
        return text
    marker_re = re.compile(r"<!-- arxiv-record:(\{.*?\}) -->")
    matches = list(marker_re.finditer(text))
    chunks: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        chunks.append(text[cursor : match.end()])
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.end() : end]
        try:
            arxiv_id = str(json.loads(match.group(1))["arxiv_id"])
        except (KeyError, json.JSONDecodeError):
            arxiv_id = ""
        rating = ratings.get(arxiv_id)
        if rating:
            section = re.sub(
                r"(\*\*Interest:\*\*\s*)"
                r"(high|medium|low|strong|maybe|skip|unrated)",
                rf"\g<1>{rating}",
                section,
                count=1,
                flags=re.IGNORECASE,
            )
        chunks.append(section)
        cursor = end
    if not matches:
        return text
    return "".join(chunks)


def write_report_atomic(path: Path, text: str, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise ExistingReportError(f"Report already exists: {path}")
    if path.exists():
        original = path.read_text(encoding="utf-8")
        text = _carry_ratings(text, parse_report_ratings(original))
        backup_dir = path.parent / ".state" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(path, backup_dir / f"{path.stem}.{stamp}.md")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
