from __future__ import annotations

import html
import json
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

from .downloader import DownloadStatus, PaperDownloader
from .history import RATING_WEIGHTS, RECORD_RE, RATING_RE
from .preferences import PreferenceEvidence, PreferenceSignalStore
from .report import format_authors
from .schedule import eligible_digest_cycle, next_digest_start


REPORT_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
DIGEST_CONTEXT_RE = re.compile(r"<!-- arxiv-digest:(\{.*?\}) -->")
EDITABLE_RATINGS = ("high", "medium", "low", "skip", "unrated")
LEGACY_RATINGS = {"strong": "high", "maybe": "medium"}
MAX_REQUEST_BYTES = 4096
_WRITE_LOCK = Lock()
DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_SEARCH_START_TIME = time(20, 0)


@dataclass(frozen=True)
class ViewerPaper:
    arxiv_id: str
    versioned_id: str
    title: str
    abstract: str
    authors: tuple[str, ...]
    categories: tuple[str, ...]
    published: str
    updated: str
    abs_url: str
    pdf_url: str
    rating: str


@dataclass(frozen=True)
class SearchResult:
    status: str
    message: str


@dataclass(frozen=True)
class ReportPagination:
    dates: tuple[str, ...]
    page: int
    total_pages: int
    tokens: tuple[int | None, ...]


def paginate_report_dates(
    dates: Sequence[str],
    requested_page: str | int | None,
    page_size: int = 7,
) -> ReportPagination:
    if page_size <= 0:
        raise ValueError("Page size must be positive")
    try:
        parsed_page = int(requested_page) if requested_page is not None else 1
    except (TypeError, ValueError):
        parsed_page = 1
    total_pages = max(1, (len(dates) + page_size - 1) // page_size)
    page = min(max(parsed_page, 1), total_pages)
    start = (page - 1) * page_size
    visible = tuple(dates[start : start + page_size])
    if total_pages <= 7:
        tokens: tuple[int | None, ...] = tuple(range(1, total_pages + 1))
    elif page <= 4:
        tokens = (1, 2, 3, 4, 5, None, total_pages - 1, total_pages)
    elif page >= total_pages - 3:
        tokens = (
            1,
            2,
            None,
            total_pages - 4,
            total_pages - 3,
            total_pages - 2,
            total_pages - 1,
            total_pages,
        )
    else:
        tokens = (1, None, page - 1, page, page + 1, None, total_pages)
    return ReportPagination(visible, page, total_pages, tokens)


@dataclass(frozen=True)
class ViewerReportContext:
    digest_date: date
    announcement_at: datetime
    submission_start: date
    submission_end: date
    searched_at: datetime
    viewer_timezone: str


def _full_date(value: date) -> str:
    return value.strftime("%A, %B %-d, %Y")


def _short_date(value: date) -> str:
    return value.strftime("%A, %B %-d")


def parse_report_context(
    report_text: str,
    report_date: str,
) -> ViewerReportContext | None:
    match = DIGEST_CONTEXT_RE.search(report_text)
    if match is None:
        return None
    try:
        metadata = json.loads(match.group(1))
        context = ViewerReportContext(
            digest_date=date.fromisoformat(str(metadata["digest_date"])),
            announcement_at=datetime.fromisoformat(
                str(metadata["announcement_at"])
            ),
            submission_start=date.fromisoformat(
                str(metadata["submission_start"])
            ),
            submission_end=date.fromisoformat(str(metadata["submission_end"])),
            searched_at=datetime.fromisoformat(str(metadata["searched_at"])),
            viewer_timezone=str(metadata["viewer_timezone"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Report contains invalid digest metadata") from exc
    if context.digest_date.isoformat() != report_date:
        raise ValueError("Report digest date does not match its filename")
    return context


def _canonical_rating(value: str) -> str:
    normalized = value.lower()
    return LEGACY_RATINGS.get(normalized, normalized)


def list_report_dates(record_dir: Path) -> list[str]:
    return sorted(
        (
            path.stem
            for path in record_dir.glob("????-??-??.md")
            if REPORT_DATE_RE.fullmatch(path.stem)
            and path.is_file()
            and not path.is_symlink()
        ),
        reverse=True,
    )


def parse_viewer_papers(text: str) -> list[ViewerPaper]:
    matches = list(RECORD_RE.finditer(text))
    papers: list[ViewerPaper] = []
    for index, match in enumerate(matches):
        try:
            metadata = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ValueError("Report contains invalid embedded metadata") from exc
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        rating_match = RATING_RE.search(text, match.end(), end)
        rating = _canonical_rating(
            rating_match.group(1) if rating_match else "unrated"
        )
        try:
            papers.append(
                ViewerPaper(
                    arxiv_id=str(metadata["arxiv_id"]),
                    versioned_id=str(metadata["versioned_id"]),
                    title=str(metadata["title"]),
                    abstract=str(metadata["abstract"]),
                    authors=tuple(str(value) for value in metadata["authors"]),
                    categories=tuple(str(value) for value in metadata["categories"]),
                    published=str(metadata["published"]),
                    updated=str(metadata["updated"]),
                    abs_url=str(metadata["abs_url"]),
                    pdf_url=str(metadata["pdf_url"]),
                    rating=rating,
                )
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("Report metadata is incomplete") from exc
    return papers


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def _rating_buttons(paper: ViewerPaper, *, interactive: bool) -> str:
    buttons = []
    for rating in EDITABLE_RATINGS:
        selected = " is-selected" if rating == paper.rating else ""
        disabled = "" if interactive else " disabled"
        buttons.append(
            f'<button class="rating rating-{rating}{selected}" '
            f'data-rating="{rating}"{disabled}>{rating.title()}</button>'
        )
    return "\n".join(buttons)


def generate_html_companion(
    report_text: str,
    *,
    report_date: str,
    interactive: bool,
    token: str = "",
    asset_prefix: str = ".state/viewer-assets/katex",
) -> str:
    if not REPORT_DATE_RE.fullmatch(report_date):
        raise ValueError("Invalid report date")
    papers = parse_viewer_papers(report_text)
    context = parse_report_context(report_text, report_date)
    display_date = _full_date(date.fromisoformat(report_date))
    if context is None:
        report_context = (
            '<aside class="legacy-notice"><strong>Legacy date note</strong>'
            "<span>This report predates the reading-date system. Its filename "
            "represented the newest submission date used by the old search, "
            "not an arXiv announcement date.</span></aside>"
        )
    else:
        eastern = context.announcement_at.astimezone(
            ZoneInfo("America/New_York")
        )
        local = context.announcement_at.astimezone(
            ZoneInfo(context.viewer_timezone)
        )
        announcement_time = eastern.strftime("%-I:%M %p %Z")
        if local.utcoffset() != eastern.utcoffset():
            announcement_time += f" ({local.strftime('%-I:%M %p %Z')})"
        searched = context.searched_at.astimezone(
            ZoneInfo(context.viewer_timezone)
        )
        report_context = (
            '<dl class="report-context">'
            f"<div><dt>Digest for</dt><dd>{_escape(display_date)}</dd></div>"
            "<div><dt>arXiv announcement</dt>"
            f"<dd>{_escape(_full_date(eastern.date()))} at "
            f"{_escape(announcement_time)}</dd></div>"
            "<div><dt>Submissions searched</dt>"
            f"<dd>{context.submission_start.isoformat()} to "
            f"{context.submission_end.isoformat()}</dd></div>"
            "<div><dt>Search performed</dt>"
            f"<dd>{_escape(_full_date(searched.date()))} at "
            f"{_escape(searched.strftime('%-I:%M %p %Z'))}</dd></div>"
            "</dl>"
        )
    config = json.dumps(
        {"interactive": interactive, "token": token, "date": report_date},
        ensure_ascii=True,
    ).replace("<", "\\u003c")
    status = (
        '<span id="server-status" class="server-status online">Interactive</span>'
        if interactive
        else '<span id="server-status" class="server-status offline">'
        "Read-only offline copy</span>"
    )
    notice = (
        ""
        if interactive
        else '<p class="offline-notice">'
        "Open with ArXiv Go.command to change ratings.</p>"
    )
    close_button = (
        f'<a class="all-reports" href="/?token={quote(token)}">All reports</a>'
        '<div class="search-control">'
        '<button id="start-search" class="start-search" disabled>'
        "Start searching</button>"
        '<span id="search-status" class="search-status" aria-live="polite">'
        "Checking availability...</span></div>"
        '<div class="download-control">'
        '<button id="download-high" class="download-high" disabled>'
        "Download High</button>"
        '<span id="download-status" class="download-status" aria-live="polite">'
        "Checking downloads...</span></div>"
        '<button id="close-server" class="close-server">Close Server</button>'
        if interactive
        else ""
    )
    cards = []
    for index, paper in enumerate(papers, start=1):
        authors = _escape(format_authors(paper.authors))
        safe_versioned_id = quote(paper.versioned_id, safe="/.-")
        abs_url = f"https://arxiv.org/abs/{safe_versioned_id}"
        pdf_url = f"https://arxiv.org/pdf/{safe_versioned_id}"
        categories = " ".join(
            f"<span class=\"category\">{_escape(category)}</span>"
            for category in paper.categories
        )
        cards.append(
            f"""
<article class="paper" data-arxiv-id="{_escape(paper.arxiv_id)}"
         data-current-rating="{_escape(paper.rating)}">
  <div class="paper-number">{index:02d}</div>
  <div class="paper-main">
    <div class="paper-heading">
      <h2>{_escape(paper.title)}</h2>
      <div class="score-links">
        <a href="{_escape(pdf_url)}" target="_blank"
           rel="noopener noreferrer">PDF</a>
        <a href="{_escape(abs_url)}" target="_blank" rel="noopener noreferrer">{_escape(paper.versioned_id)}</a>
      </div>
    </div>
    <p class="authors">{authors}</p>
    <div class="categories">{categories}</div>
    <div class="rating-row" role="group"
         aria-label="Interest rating for {_escape(paper.title)}">
      {_rating_buttons(paper, interactive=interactive)}
      <span class="save-state" aria-live="polite"></span>
      <span class="download-state" aria-live="polite"></span>
    </div>
    <details>
      <summary>Abstract</summary>
      <p class="abstract">{_escape(paper.abstract)}</p>
    </details>
  </div>
</article>"""
        )
    asset = _escape(asset_prefix.rstrip("/"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daily arXiv Recommendations — {_escape(report_date)}</title>
  <link rel="stylesheet" href="{asset}/katex.min.css">
  <style>
    :root {{
      --ink: #17211d;
      --muted: #627069;
      --paper: #fbf8ef;
      --panel: rgba(255, 255, 255, 0.78);
      --line: #d8d4c7;
      --accent: #b9472f;
      --green: #246b50;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at 8% 5%, #f0d9ad 0, transparent 25rem),
        linear-gradient(135deg, #f8f3e5, #e7eee8);
      font-family: Georgia, "Times New Roman", serif;
      line-height: 1.55;
    }}
    .shell {{ max-width: 1120px; margin: auto; padding: 48px 24px 96px; }}
    header {{
      display: flex; justify-content: space-between; gap: 24px;
      align-items: flex-start; margin-bottom: 32px;
    }}
    h1 {{ font-size: clamp(2rem, 5vw, 4.6rem); line-height: .94; margin: 0; }}
    .eyebrow {{
      font: 700 .72rem/1.2 ui-monospace, SFMono-Regular, monospace;
      letter-spacing: .16em; text-transform: uppercase; color: var(--accent);
      margin-bottom: 12px;
    }}
    .toolbar {{ display: flex; flex-direction: column; align-items: flex-end; gap: 12px; }}
    .server-status, .offline-notice {{
      font: 600 .78rem/1.3 ui-monospace, SFMono-Regular, monospace;
    }}
    .search-control, .download-control {{
      display: flex; flex-direction: column; align-items: flex-end; gap: 5px;
    }}
    .search-status, .download-status, .download-state {{
      color: var(--muted);
      font: 600 .7rem/1.3 ui-monospace, SFMono-Regular, monospace;
    }}
    .start-search {{
      border: 1px solid var(--green); background: #f1fbf5; color: var(--green);
      padding: 9px 14px; border-radius: 999px; cursor: pointer; font-weight: 700;
    }}
    .download-high {{
      border: 1px solid #916515; background: #fff9e8; color: #76500f;
      padding: 9px 14px; border-radius: 999px; cursor: pointer; font-weight: 700;
    }}
    .start-search:disabled, .download-high:disabled {{
      cursor: not-allowed; opacity: .55;
    }}
    .online {{ color: var(--green); }} .offline {{ color: var(--muted); }}
    .offline-notice {{
      padding: 12px 16px; border: 1px solid var(--line);
      background: rgba(255,255,255,.55); margin: 0 0 28px;
    }}
    .legacy-notice {{
      display: grid; gap: 4px; margin: 0 0 22px; padding: 14px 16px;
      border: 1px solid #d8bd77; border-radius: 12px; background: #fff4d8;
      color: #665528;
    }}
    .legacy-notice strong {{ color: var(--ink); }}
    .report-context {{
      display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px; margin: 0 0 24px;
    }}
    .report-context div {{
      padding: 13px 15px; border: 1px solid var(--line);
      border-radius: 12px; background: rgba(255, 255, 255, .66);
    }}
    .report-context dt {{
      color: var(--muted); font: 700 .68rem/1.2 ui-monospace, SFMono-Regular,
      monospace; text-transform: uppercase; letter-spacing: .05em;
    }}
    .report-context dd {{ margin: 6px 0 0; font-weight: 700; }}
    .close-server {{
      border: 1px solid #8e3224; background: #fff6f2; color: #8e3224;
      padding: 9px 14px; border-radius: 999px; cursor: pointer; font-weight: 700;
    }}
    .all-reports {{
      font: 700 .78rem/1 ui-monospace, SFMono-Regular, monospace;
      color: var(--accent);
    }}
    .paper {{
      display: grid; grid-template-columns: 54px 1fr; gap: 18px;
      background: var(--panel); border: 1px solid var(--line);
      border-radius: 18px; padding: 22px; margin: 16px 0;
      box-shadow: 0 18px 50px rgba(57, 66, 60, .07);
    }}
    .paper-number {{
      font: 700 1rem/1 ui-monospace, SFMono-Regular, monospace;
      color: var(--accent); padding-top: 8px;
    }}
    .paper-heading {{ display: flex; justify-content: space-between; gap: 20px; }}
    h2 {{ font-size: clamp(1.2rem, 2.3vw, 1.65rem); line-height: 1.2; margin: 0; }}
    a {{ color: inherit; text-decoration-color: #c7977e; text-underline-offset: 4px; }}
    .score-links {{
      flex: 0 0 auto; display: flex; gap: 8px; align-items: flex-start;
      font: .72rem/1.3 ui-monospace, SFMono-Regular, monospace;
    }}
    .authors {{ color: var(--muted); margin: 9px 0; }}
    .categories {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .category {{
      font: 700 .68rem/1 ui-monospace, SFMono-Regular, monospace;
      padding: 5px 8px; border: 1px solid var(--line); border-radius: 999px;
    }}
    .rating-row {{ display: flex; flex-wrap: wrap; gap: 7px; margin: 18px 0 10px; }}
    .rating {{
      border: 1px solid var(--line); background: #fff; color: var(--ink);
      padding: 7px 11px; border-radius: 8px; cursor: pointer; font-weight: 700;
    }}
    .rating:disabled {{ cursor: not-allowed; opacity: .58; }}
    .rating-high.is-selected {{ background: #235f46; color: white; border-color: #235f46; }}
    .rating-medium.is-selected {{ background: #447761; color: white; border-color: #447761; }}
    .rating-low.is-selected {{ background: #c3a851; color: #181711; border-color: #a68a31; }}
    .rating-skip.is-selected {{ background: #9d3d32; color: white; border-color: #9d3d32; }}
    .rating-unrated.is-selected {{ background: #777; color: white; border-color: #777; }}
    .save-state {{
      align-self: center; color: var(--muted);
      font: .72rem/1 ui-monospace, SFMono-Regular, monospace;
    }}
    details {{ border-top: 1px solid var(--line); margin-top: 16px; padding-top: 12px; }}
    summary {{ cursor: pointer; font-weight: 700; color: var(--accent); }}
    .abstract {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
    @media (max-width: 700px) {{
      .shell {{ padding: 28px 14px 70px; }}
      header, .paper-heading {{ flex-direction: column; }}
      .toolbar {{ align-items: flex-start; }}
      .report-context {{ grid-template-columns: 1fr; }}
      .paper {{ grid-template-columns: 1fr; padding: 17px; }}
      .paper-number {{ display: none; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <div class="eyebrow">Verified paper digest</div>
        <h1>Daily arXiv<br>Recommendations</h1>
        <p>{_escape(display_date)} · {len(papers)} papers</p>
      </div>
      <div class="toolbar">{status}{close_button}</div>
    </header>
    {notice}
    {report_context}
    {"".join(cards)}
  </main>
  <script src="{asset}/katex.min.js"></script>
  <script src="{asset}/contrib/auto-render.min.js"></script>
  <script>
    window.ARXIV_VIEWER = {config};
    if (window.renderMathInElement) {{
      renderMathInElement(document.body, {{
        delimiters: [
          {{left: "$$", right: "$$", display: true}},
          {{left: "\\\\[", right: "\\\\]", display: true}},
          {{left: "$", right: "$", display: false}},
          {{left: "\\\\(", right: "\\\\)", display: false}}
        ],
        throwOnError: false
      }});
    }}
    const viewer = window.ARXIV_VIEWER;
    async function post(path, payload) {{
      const response = await fetch(`${{path}}?token=${{encodeURIComponent(viewer.token)}}`, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(payload)
      }});
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }}
    if (viewer.interactive) {{
      const searchButton = document.getElementById("start-search");
      const searchStatus = document.getElementById("search-status");
      const downloadButton = document.getElementById("download-high");
      const downloadStatus = document.getElementById("download-status");
      const closeButton = document.getElementById("close-server");
      let searchStartedHere = false;
      let searchBusy = false;
      let downloadBusy = false;
      function refreshCloseState() {{
        closeButton.disabled = searchBusy || downloadBusy;
      }}
      async function refreshSearchStatus() {{
        try {{
          const response = await fetch(
            `/api/search/status?token=${{encodeURIComponent(viewer.token)}}`
          );
          if (!response.ok) throw new Error(await response.text());
          const status = await response.json();
          searchBusy = Boolean(status.busy);
          refreshCloseState();
          searchButton.textContent = status.button_label || "Start searching";
          searchButton.disabled = !status.enabled;
          searchStatus.textContent = status.message;
          if (searchStartedHere && status.state === "ready") {{
            window.location.href = `/?token=${{encodeURIComponent(viewer.token)}}`;
          }}
          if (status.state === "running") {{
            window.setTimeout(refreshSearchStatus, 1000);
          }}
        }} catch (error) {{
          searchButton.disabled = true;
          searchStatus.textContent = "Search status unavailable";
        }}
      }}
      searchButton.addEventListener("click", async () => {{
        searchButton.disabled = true;
        searchStatus.textContent = "Starting search...";
        searchStartedHere = true;
        try {{
          await post("/api/search/start", {{}});
          await refreshSearchStatus();
        }} catch (error) {{
          searchStatus.textContent = "Search could not start";
          await refreshSearchStatus();
        }}
      }});
      async function refreshDownloadStatus() {{
        try {{
          const response = await fetch(
            `/api/download/status?date=${{encodeURIComponent(viewer.date)}}&token=${{encodeURIComponent(viewer.token)}}`
          );
          if (!response.ok) throw new Error(await response.text());
          const status = await response.json();
          downloadBusy = Boolean(status.busy);
          refreshCloseState();
          downloadButton.disabled = !status.enabled;
          downloadButton.textContent = `Download High (${{status.pending}})`;
          downloadStatus.textContent = status.message;
          document.querySelectorAll(".paper").forEach((paper) => {{
            const paperStatus = status.papers[paper.dataset.arxivId];
            const target = paper.querySelector(".download-state");
            target.textContent = paperStatus ? paperStatus.label : "";
          }});
          if (status.state === "running") {{
            window.setTimeout(refreshDownloadStatus, 1000);
          }}
        }} catch (error) {{
          downloadButton.disabled = true;
          downloadStatus.textContent = "Download status unavailable";
        }}
      }}
      downloadButton.addEventListener("click", async () => {{
        downloadButton.disabled = true;
        downloadStatus.textContent = "Starting downloads...";
        try {{
          await post("/api/download/start", {{date: viewer.date}});
          await refreshDownloadStatus();
        }} catch (error) {{
          downloadStatus.textContent = "Downloads could not start";
          await refreshDownloadStatus();
        }}
      }});
      refreshSearchStatus();
      refreshDownloadStatus();
      window.setInterval(refreshSearchStatus, 30000);
      window.setInterval(refreshDownloadStatus, 30000);
      document.querySelectorAll(".rating").forEach((button) => {{
        button.addEventListener("click", async () => {{
          const paper = button.closest(".paper");
          const state = paper.querySelector(".save-state");
          state.textContent = "Saving…";
          try {{
            const result = await post("/api/rating", {{
              date: viewer.date,
              arxiv_id: paper.dataset.arxivId,
              rating: button.dataset.rating
            }});
            paper.dataset.currentRating = button.dataset.rating;
            paper.querySelectorAll(".rating").forEach((item) =>
              item.classList.toggle("is-selected", item === button));
            state.textContent = result.message || "Saved";
            await refreshDownloadStatus();
          }} catch (error) {{
            state.textContent = "Not saved";
          }}
        }});
      }});
      closeButton.addEventListener("click", async () => {{
        closeButton.disabled = true;
        try {{
          await post("/api/shutdown", {{}});
          document.getElementById("server-status").textContent = "Server closed";
          document.getElementById("server-status").className = "server-status offline";
          document.querySelectorAll(".rating").forEach((button) => button.disabled = true);
          searchButton.disabled = true;
          downloadButton.disabled = true;
        }} catch (error) {{
          document.getElementById("server-status").textContent =
            "Wait for the active job to finish before closing.";
          refreshCloseState();
          await refreshSearchStatus();
          await refreshDownloadStatus();
        }}
      }});
    }}
  </script>
</body>
</html>
"""


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def write_html_companion(markdown_path: Path, *, asset_prefix: str) -> Path:
    html_path = markdown_path.with_suffix(".html")
    text = markdown_path.read_text(encoding="utf-8")
    rendered = generate_html_companion(
        text,
        report_date=markdown_path.stem,
        interactive=False,
        asset_prefix=asset_prefix,
    )
    write_text_atomic(html_path, rendered)
    return html_path


def update_report_rating(
    record_dir: Path,
    *,
    report_date: str,
    arxiv_id: str,
    rating: str,
) -> None:
    if not REPORT_DATE_RE.fullmatch(report_date):
        raise ValueError("Invalid report date")
    if rating not in EDITABLE_RATINGS:
        raise ValueError("Invalid rating")
    path = record_dir / f"{report_date}.md"
    if not path.is_file() or path.is_symlink():
        raise ValueError("Unknown report")
    with _WRITE_LOCK:
        text = path.read_text(encoding="utf-8")
        matches = list(RECORD_RE.finditer(text))
        target_index = None
        for index, match in enumerate(matches):
            try:
                metadata = json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid report metadata") from exc
            if str(metadata.get("arxiv_id", "")) == arxiv_id:
                target_index = index
                break
        if target_index is None:
            raise ValueError("Unknown paper")
        match = matches[target_index]
        end = (
            matches[target_index + 1].start()
            if target_index + 1 < len(matches)
            else len(text)
        )
        section = text[match.end() : end]
        rating_match = RATING_RE.search(section)
        if rating_match is None:
            raise ValueError("Paper has no editable rating field")
        section = (
            section[: rating_match.start(1)]
            + rating
            + section[rating_match.end(1) :]
        )
        updated = text[: match.end()] + section + text[end:]
        write_text_atomic(path, updated)
        changed_at = datetime.now(timezone.utc)
        signal_store = PreferenceSignalStore(
            record_dir / ".state" / "preference-signals.json",
            now=lambda: changed_at,
        )
        signal_store.resolve(
            arxiv_id,
            report=PreferenceEvidence(
                source="report",
                weight=RATING_WEIGHTS[rating],
                explicit=True,
                fingerprint=f"{path.name}:{rating}",
                fallback_changed_at=changed_at,
            ),
            library=None,
        )
        signal_store.save()


def generate_all_html(record_dir: Path) -> list[Path]:
    written = []
    for report_date in list_report_dates(record_dir):
        written.append(
            write_html_companion(
                record_dir / f"{report_date}.md",
                asset_prefix=".state/viewer-assets/katex",
            )
        )
    return written


def _safe_asset_path(assets_dir: Path, relative_path: str) -> Path | None:
    if not relative_path or relative_path.startswith(("/", ".")):
        return None
    root = assets_dir.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def create_server(
    *,
    record_dir: Path,
    assets_dir: Path,
    token: str,
    port: int = 0,
    now_provider: Callable[[], datetime] | None = None,
    search_runner: Callable[[date], SearchResult] | None = None,
    downloader: PaperDownloader | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    search_start_time: time = DEFAULT_SEARCH_START_TIME,
    supervisor_handoff_url: str | None = None,
) -> ThreadingHTTPServer:
    record_dir = record_dir.resolve()
    assets_dir = assets_dir.resolve()
    viewer_timezone = ZoneInfo(timezone_name)
    now_provider = now_provider or (lambda: datetime.now(viewer_timezone))
    state_lock = Lock()
    download_io_lock = Lock()
    active_job = {"kind": "", "date": ""}
    search_state = {
        "date": "",
        "state": "idle",
        "message": "Ready to search.",
    }
    download_states: dict[str, dict[str, object]] = {}

    def status_payload(statuses: dict[str, DownloadStatus]) -> dict[str, object]:
        return {
            arxiv_id: {
                "state": status.state,
                "label": {
                    "pending": "Pending",
                    "downloaded": "Downloaded",
                    "existing": "Existing file",
                    "failed": "Failed",
                    "conflict": "Conflict",
                    "superseded": "Superseded by newer report",
                    "not-downloaded": "",
                }.get(status.state, status.state.title()),
                "message": status.message,
                "filename": status.filename,
            }
            for arxiv_id, status in statuses.items()
        }

    def validate_report_date(report_date: str) -> Path:
        if not REPORT_DATE_RE.fullmatch(report_date):
            raise ValueError("Invalid report date")
        path = record_dir / f"{report_date}.md"
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"No Markdown report exists for {report_date}")
        return path

    def reconcile_downloads(report_date: str) -> dict[str, DownloadStatus]:
        validate_report_date(report_date)
        if server.downloader is None:
            return {}
        with download_io_lock:
            return server.downloader.reconcile(report_date)

    def reconciled_download_state(report_date: str) -> dict[str, object]:
        reconciled = reconcile_downloads(report_date)
        pending = any(
            item.state in {"pending", "failed"}
            for item in reconciled.values()
        )
        return {
            "state": "idle",
            "message": (
                f"Ready to download High papers from {report_date}."
                if pending
                else f"All High papers from {report_date} are already present."
            ),
            "papers": status_payload(reconciled),
        }

    def ensure_download_state(report_date: str) -> dict[str, object]:
        validate_report_date(report_date)
        with state_lock:
            existing = download_states.get(report_date)
            if existing is not None:
                return existing
        try:
            refreshed = reconciled_download_state(report_date)
        except Exception as exc:
            print(f"[viewer] download reconciliation failed for {report_date}: {exc}")
            refreshed = {
                "state": "failed",
                "message": f"Download library needs attention for {report_date}.",
                "papers": {},
            }
        with state_lock:
            return download_states.setdefault(report_date, refreshed)

    def current_viewer_time() -> datetime:
        current = now_provider()
        if current.tzinfo is None:
            return current.replace(tzinfo=viewer_timezone)
        return current.astimezone(viewer_timezone)

    def search_status() -> dict[str, object]:
        now = current_viewer_time()
        cycle = eligible_digest_cycle(now, search_start_time)
        cycle_text = cycle.digest_date.isoformat()
        digest_label = _short_date(cycle.digest_date)
        button_label = f"Search for {digest_label} digest"
        with state_lock:
            if (
                search_state["date"] != cycle_text
                and search_state["state"] != "running"
            ):
                search_state.update(
                    date=cycle_text,
                    state="idle",
                    message=(
                        f"{digest_label} digest is ready. It corresponds to "
                        f"the {_full_date(cycle.announcement_at.date())} "
                        "arXiv announcement."
                    ),
                )
            report_path = record_dir / f"{cycle_text}.md"
            if report_path.is_file() and not report_path.is_symlink():
                next_start = next_digest_start(now, search_start_time)
                return {
                    "date": cycle_text,
                    "state": "ready",
                    "enabled": False,
                    "message": (
                        f"{cycle.digest_date.strftime('%A')}'s digest is "
                        "complete. Next digest becomes available "
                        f"{next_start.strftime('%A at %-I:%M %p')} "
                        f"{timezone_name}."
                    ),
                    "button_label": button_label,
                    "busy": bool(active_job["kind"]),
                }
            state = search_state["state"]
            return {
                "date": cycle_text,
                "state": state,
                "enabled": state != "running" and not active_job["kind"],
                "message": search_state["message"],
                "button_label": button_label,
                "busy": bool(active_job["kind"]),
            }

    def current_download_status(report_date: str) -> dict[str, object]:
        ensure_download_state(report_date)
        with state_lock:
            download_state = download_states[report_date]
            papers = dict(download_state["papers"])
            pending = sum(
                1
                for item in papers.values()
                if isinstance(item, dict)
                and item.get("state") in {"pending", "failed"}
            )
            state = str(download_state["state"])
            return {
                "state": state,
                "date": report_date,
                "pending": pending,
                "enabled": (
                    server.downloader is not None
                    and pending > 0
                    and not active_job["kind"]
                    and state != "running"
                ),
                "message": download_state["message"],
                "papers": papers,
                "busy": bool(active_job["kind"]),
            }

    def start_search() -> tuple[HTTPStatus, dict[str, object]]:
        now = current_viewer_time()
        cycle = eligible_digest_cycle(now, search_start_time)
        cycle_text = cycle.digest_date.isoformat()
        digest_label = _short_date(cycle.digest_date)
        with state_lock:
            report_path = record_dir / f"{cycle_text}.md"
            if report_path.is_file() and not report_path.is_symlink():
                return HTTPStatus.CONFLICT, {
                    "ok": False,
                    "message": f"Report for {cycle_text} already exists.",
                }
            if search_state["state"] == "running":
                return HTTPStatus.CONFLICT, {
                    "ok": False,
                    "message": "A search is already running.",
                }
            if active_job["kind"]:
                return HTTPStatus.CONFLICT, {
                    "ok": False,
                    "message": f"A {active_job['kind']} job is already running.",
                }
            active_job.update(kind="search", date=cycle_text)
            search_state.update(
                date=cycle_text,
                state="running",
                message=(
                    f"Searching verified arXiv metadata for the "
                    f"{digest_label} digest..."
                ),
            )

        def worker() -> None:
            try:
                result = server.search_runner(cycle.digest_date)
            except Exception as exc:
                print(f"[viewer] daily search failed: {exc}")
                result = SearchResult(
                    "failed",
                    "Search failed unexpectedly; no report was written.",
                )
            states = {
                "written": "ready",
                "already-exists": "ready",
                "skipped": "skipped",
                "failed": "failed",
            }
            state = states.get(result.status, "failed")
            message = (
                result.message
                if result.status in states
                else "Search failed unexpectedly; no report was written."
            )
            with state_lock:
                search_state.update(
                    date=cycle_text,
                    state=state,
                    message=message,
                )
                active_job.update(kind="", date="")

        Thread(target=worker, daemon=True).start()
        return HTTPStatus.ACCEPTED, {
            "ok": True,
            "message": "Search started.",
        }

    def start_download(report_date: str) -> tuple[HTTPStatus, dict[str, object]]:
        try:
            validate_report_date(report_date)
        except ValueError as exc:
            return HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "message": str(exc),
            }
        if server.downloader is None:
            return HTTPStatus.SERVICE_UNAVAILABLE, {
                "ok": False,
                "message": "PDF downloader is unavailable.",
            }
        with state_lock:
            if active_job["kind"]:
                return HTTPStatus.CONFLICT, {
                    "ok": False,
                    "message": f"A {active_job['kind']} job is already running.",
                }
            active_job.update(kind="download", date=report_date)
        try:
            reconciled = reconcile_downloads(report_date)
        except Exception as exc:
            print(f"[viewer] download reconciliation failed for {report_date}: {exc}")
            with state_lock:
                active_job.update(kind="", date="")
            return HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False,
                "message": (
                    f"Download library reconciliation failed for {report_date}."
                ),
            }
        with state_lock:
            pending = sum(item.state == "pending" for item in reconciled.values())
            download_state = download_states.setdefault(report_date, {})
            download_state.update(
                state="idle",
                message=f"Ready to download High papers from {report_date}.",
                papers=status_payload(reconciled),
            )
            if not pending:
                active_job.update(kind="", date="")
                return HTTPStatus.CONFLICT, {
                    "ok": False,
                    "message": (
                        f"All High papers from {report_date} are already present."
                    ),
                }
            download_state.update(
                state="running",
                message=f"Downloading 0/{pending} from {report_date}...",
            )

        def progress(index: int, total: int, arxiv_id: str) -> None:
            with state_lock:
                download_state = download_states[report_date]
                papers = dict(download_state["papers"])
                item = dict(papers.get(arxiv_id, {}))
                item.update(state="downloading", label="Downloading")
                papers[arxiv_id] = item
                download_state.update(
                    message=(
                        f"Downloading {index}/{total} from {report_date}..."
                    ),
                    papers=papers,
                )

        def worker() -> None:
            try:
                with download_io_lock:
                    result = server.downloader.download_high(
                        report_date,
                        progress=progress,
                    )
                failed = sum(item.state == "failed" for item in result.values())
                conflicts = sum(item.state == "conflict" for item in result.values())
                state = "failed" if failed or conflicts else "complete"
                message = (
                    f"Downloads for {report_date} finished with "
                    f"{failed + conflicts} item(s) needing attention."
                    if failed or conflicts
                    else f"All High papers from {report_date} are downloaded."
                )
            except Exception as exc:
                print(f"[viewer] paper download failed: {exc}")
                result = {}
                state = "failed"
                message = (
                    f"Downloads for {report_date} failed; "
                    "existing PDFs were preserved."
                )
            with state_lock:
                download_state = download_states[report_date]
                download_state.update(
                    state=state,
                    message=message,
                    papers=status_payload(result)
                    if result
                    else download_state["papers"],
                )
                active_job.update(kind="", date="")

        Thread(target=worker, daemon=True).start()
        return HTTPStatus.ACCEPTED, {
            "ok": True,
            "message": f"Downloads started for {report_date}.",
        }

    class ViewerHandler(BaseHTTPRequestHandler):
        server_version = "DailyArxivViewer/1.0"

        def log_message(self, format: str, *args: object) -> None:
            print(f"[viewer] {self.address_string()} - {format % args}")

        def _query_token(self) -> str:
            return parse_qs(urlparse(self.path).query).get("token", [""])[0]

        def _authorized(self) -> bool:
            return self._query_token() == token

        def _same_origin(self) -> bool:
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin", "")
            expected = f"http://{host}"
            return (
                host.startswith("127.0.0.1:")
                and origin == expected
            )

        def _send(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; "
                             "style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

        def _read_json(self) -> dict[str, str] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "Invalid content length")
                return None
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Invalid body size")
                return None
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
                return None
            if not isinstance(payload, dict):
                self._error(HTTPStatus.BAD_REQUEST, "JSON object required")
                return None
            return {str(key): str(value) for key, value in payload.items()}

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/assets/"):
                host = self.headers.get("Host", "")
                if not host.startswith("127.0.0.1:"):
                    self._error(HTTPStatus.NOT_FOUND, "Not found")
                    return
                relative_path = parsed.path.removeprefix("/assets/")
                asset_path = _safe_asset_path(assets_dir, relative_path)
                if asset_path is None:
                    self._error(HTTPStatus.NOT_FOUND, "Unknown asset")
                    return
                content_type = {
                    ".css": "text/css; charset=utf-8",
                    ".js": "text/javascript; charset=utf-8",
                    ".woff2": "font/woff2",
                    ".woff": "font/woff",
                    ".ttf": "font/ttf",
                }.get(asset_path.suffix, "application/octet-stream")
                self._send(HTTPStatus.OK, asset_path.read_bytes(), content_type)
                return
            if not self._authorized():
                self._error(HTTPStatus.NOT_FOUND, "Not found")
                return
            if parsed.path == "/api/search/status":
                body = json.dumps(search_status()).encode("utf-8")
                self._send(
                    HTTPStatus.OK,
                    body,
                    "application/json; charset=utf-8",
                )
                return
            if parsed.path == "/api/download/status":
                report_date = parse_qs(parsed.query).get("date", [""])[0]
                try:
                    payload = current_download_status(report_date)
                except ValueError as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                body = json.dumps(payload).encode("utf-8")
                self._send(
                    HTTPStatus.OK,
                    body,
                    "application/json; charset=utf-8",
                )
                return
            if parsed.path == "/":
                dates = list_report_dates(record_dir)
                requested_page = parse_qs(parsed.query).get("page", ["1"])[0]
                pagination = paginate_report_dates(dates, requested_page)
                links = "".join(
                    '<li><a class="report-link" '
                    f'href="/report/{report_date}?token={quote(token)}">'
                    f"<span>{_escape(_full_date(date.fromisoformat(report_date)))}</span>"
                    "<strong>Open report →</strong></a></li>"
                    for report_date in pagination.dates
                )
                pagination_html = ""
                if pagination.total_pages > 1:
                    def page_link(page: int, label: str, css_class: str = "") -> str:
                        class_attr = f' class="{css_class}"' if css_class else ""
                        return (
                            f'<a{class_attr} href="/?page={page}&amp;token={quote(token)}">'
                            f"{label}</a>"
                        )

                    nav_items: list[str] = []
                    if pagination.page == 1:
                        nav_items.append('<span class="disabled">Newer</span>')
                    else:
                        nav_items.append(page_link(pagination.page - 1, "Newer"))
                    for page_token in pagination.tokens:
                        if page_token is None:
                            nav_items.append('<span class="ellipsis">...</span>')
                        elif page_token == pagination.page:
                            nav_items.append(
                                f'<span class="current" aria-current="page">{page_token}</span>'
                            )
                        else:
                            nav_items.append(page_link(page_token, str(page_token)))
                    if pagination.page == pagination.total_pages:
                        nav_items.append('<span class="disabled">Older</span>')
                    else:
                        nav_items.append(page_link(pagination.page + 1, "Older"))
                    pagination_html = (
                        '<nav class="pagination" aria-label="Report pages">'
                        + "".join(nav_items)
                        + "</nav>"
                    )
                reports = (
                    f"<ol>{links}</ol>{pagination_html}"
                    if links
                    else (
                        '<div class="empty-state"><strong>No reports yet</strong>'
                        "<span>Use Start searching when it becomes available.</span>"
                        "</div>"
                    )
                )
                body = (
                    "<!doctype html><html><head><meta charset=\"utf-8\">"
                    "<meta name=\"viewport\" content=\"width=device-width\">"
                    "<title>Daily arXiv Viewer</title>"
                    "<style>"
                    ":root{--ink:#17211d;--accent:#a13d2b;--line:#d8d4c7}"
                    "*{box-sizing:border-box}body{margin:0;color:var(--ink);"
                    "background:radial-gradient(circle at 8% 5%,#f0d9ad 0,"
                    "transparent 25rem),linear-gradient(135deg,#f8f3e5,#e7eee8);"
                    "font-family:Georgia,serif;min-height:100vh}"
                    "main{max-width:850px;margin:auto;padding:64px 22px 100px}"
                    ".eyebrow{font:700 .72rem ui-monospace;letter-spacing:.16em;"
                    "text-transform:uppercase;color:var(--accent)}"
                    "h1{font-size:clamp(2.8rem,8vw,6rem);line-height:.9;margin:"
                    "16px 0 34px}ol{list-style:none;padding:0;margin:0}"
                    "li{margin:12px 0}.report-link{display:flex;justify-content:"
                    "space-between;align-items:center;padding:20px 22px;border:"
                    "1px solid var(--line);border-radius:14px;background:"
                    "rgba(255,255,255,.72);color:inherit;text-decoration:none;"
                    "box-shadow:0 15px 40px rgba(57,66,60,.06)}"
                    ".report-link span{font-size:1.45rem}.report-link strong{"
                    "font:700 .75rem ui-monospace;color:var(--accent)}"
                    ".pagination{display:flex;flex-wrap:wrap;justify-content:"
                    "center;gap:6px;margin:28px 0 0}.pagination a,.pagination "
                    "span{display:grid;place-items:center;min-width:42px;height:"
                    "42px;padding:0 12px;border:1px solid var(--line);border-radius:"
                    "10px;background:rgba(255,255,255,.72);color:var(--ink);"
                    "text-decoration:none;font:700 .8rem ui-monospace}.pagination "
                    ".current{background:var(--ink);border-color:var(--ink);color:"
                    "#fff}.pagination .disabled{opacity:.42}.pagination .ellipsis{"
                    "border-color:transparent;background:transparent}"
                    ".empty-state{display:grid;gap:7px;padding:24px;border:"
                    "1px dashed var(--line);border-radius:14px;color:#627069;"
                    "background:rgba(255,255,255,.46)}.empty-state strong{"
                    "color:var(--ink);font-size:1.35rem}"
                    ".actions{display:grid;grid-template-columns:repeat(3,"
                    "minmax(0,1fr));gap:18px;align-items:start;margin:0 0 30px}"
                    ".action-column{display:grid;grid-template-rows:auto "
                    "minmax(1.25rem,auto);gap:8px;min-width:0}"
                    ".primary-action{width:100%;min-height:58px;padding:10px "
                    "18px;border-radius:999px;cursor:pointer;font:700 1rem/1.2 "
                    "Georgia,serif}#close-server{border:1px solid #8e3224;"
                    "background:#fff6f2;color:#8e3224}"
                    "#configure-interests{border:1px solid #9b7425;"
                    "background:#fffaf0;color:#755619}"
                    "#start-search{border:1px solid #246b50;"
                    "background:#f1fbf5;color:#246b50}"
                    "#start-search:disabled{"
                    "cursor:not-allowed;opacity:.55}"
                    "#search-status,#status{"
                    "overflow-wrap:anywhere;font:600 "
                    ".72rem/1.35 ui-monospace;color:#627069}"
                    "#status{font:600 .75rem ui-monospace;color:#627069;"
                    "margin:0}@media(max-width:620px){.actions{"
                    "grid-template-columns:1fr}}</style></head><body><main>"
                    "<div class=\"eyebrow\">Private localhost viewer</div>"
                    "<h1>Daily arXiv<br>Reports</h1>"
                    '<div class="actions">'
                    '<div class="action-column search-control">'
                    '<button id="start-search" class="primary-action" disabled>'
                    "Start searching</button>"
                    '<span id="search-status" aria-live="polite">'
                    "Checking availability...</span></div>"
                    '<div class="action-column configure-control">'
                    '<button id="configure-interests" class="primary-action">'
                    "Configure interests</button>"
                    '<span aria-hidden="true"></span></div>'
                    '<div class="action-column close-control">'
                    '<button id="close-server" class="primary-action">'
                    "Close Server</button>"
                    '<span id="status" aria-live="polite"></span></div></div>'
                    f"{reports}"
                    f"""<script>
                    const token = {json.dumps(token)};
                    const supervisorHandoffUrl =
                      {json.dumps(supervisor_handoff_url)};
                    const searchButton = document.getElementById("start-search");
                    const searchStatus = document.getElementById("search-status");
                    const configureButton =
                      document.getElementById("configure-interests");
                    const closeButton = document.getElementById("close-server");
                    let searchStartedHere = false;
                    let searchBusy = false;
                    function refreshCloseState() {{
                      configureButton.disabled = searchBusy;
                      closeButton.disabled = searchBusy;
                    }}
                    async function searchPost() {{
                      const response = await fetch(
                        `/api/search/start?token=${{encodeURIComponent(token)}}`,
                        {{
                          method: "POST",
                          headers: {{"Content-Type": "application/json"}},
                          body: "{{}}"
                        }}
                      );
                      if (!response.ok) throw new Error(await response.text());
                      return response.json();
                    }}
                    async function refreshSearchStatus() {{
                      try {{
                        const response = await fetch(
                          `/api/search/status?token=${{encodeURIComponent(token)}}`
                        );
                        if (!response.ok) throw new Error(await response.text());
                        const status = await response.json();
                        searchBusy = Boolean(status.busy);
                        refreshCloseState();
                        searchButton.textContent =
                          status.button_label || "Start searching";
                        searchButton.disabled = !status.enabled;
                        searchStatus.textContent = status.message;
                        if (searchStartedHere && status.state === "ready") {{
                          window.location.href = `/?page=1&token=${{encodeURIComponent(token)}}`;
                          return;
                        }}
                        if (status.state === "running") {{
                          window.setTimeout(refreshSearchStatus, 1000);
                        }}
                      }} catch (error) {{
                        searchButton.disabled = true;
                        searchStatus.textContent = "Search status unavailable";
                      }}
                    }}
                    searchButton.addEventListener("click", async () => {{
                      searchButton.disabled = true;
                      searchStatus.textContent = "Starting search...";
                      searchStartedHere = true;
                      try {{
                        await searchPost();
                        await refreshSearchStatus();
                      }} catch (error) {{
                        searchStatus.textContent = "Search could not start";
                        await refreshSearchStatus();
                      }}
                    }});
                    refreshSearchStatus();
                    window.setInterval(refreshSearchStatus, 30000);
                    configureButton.addEventListener(
                      "click", async () => {{
                        configureButton.disabled = true;
                        try {{
                          const response = await fetch(
                            `/api/configure?token=${{encodeURIComponent(token)}}`,
                            {{
                              method: "POST",
                              headers: {{"Content-Type": "application/json"}},
                              body: "{{}}"
                            }}
                          );
                          if (!response.ok) throw new Error(await response.text());
                          searchButton.disabled = true;
                          closeButton.disabled = true;
                          if (supervisorHandoffUrl) {{
                            window.location.replace(supervisorHandoffUrl);
                          }}
                        }} catch (error) {{
                          document.getElementById("status").textContent =
                            "Wait for the active job to finish before configuring.";
                          await refreshSearchStatus();
                        }}
                      }}
                    );
                    closeButton.addEventListener(
                      "click", async () => {{
                        closeButton.disabled = true;
                        try {{
                          const response = await fetch(
                            `/api/shutdown?token=${{encodeURIComponent(token)}}`,
                            {{
                            method: "POST",
                            headers: {{"Content-Type": "application/json"}},
                            body: "{{}}"
                            }}
                          );
                          if (!response.ok) throw new Error(await response.text());
                          document.getElementById("status").textContent =
                            "Server closed";
                          searchButton.disabled = true;
                        }} catch (error) {{
                          document.getElementById("status").textContent =
                            "Wait for the active job to finish before closing.";
                          refreshCloseState();
                          await refreshSearchStatus();
                        }}
                      }}
                    );
                    </script></main></body></html>"""
                ).encode("utf-8")
                self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
                return
            if parsed.path.startswith("/report/"):
                report_date = parsed.path.removeprefix("/report/")
                if not REPORT_DATE_RE.fullmatch(report_date):
                    self._error(HTTPStatus.NOT_FOUND, "Unknown report")
                    return
                path = record_dir / f"{report_date}.md"
                if not path.is_file() or path.is_symlink():
                    self._error(HTTPStatus.NOT_FOUND, "Unknown report")
                    return
                body = generate_html_companion(
                    path.read_text(encoding="utf-8"),
                    report_date=report_date,
                    interactive=True,
                    token=token,
                    asset_prefix="/assets",
                )
                self._send(
                    HTTPStatus.OK,
                    body.encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            self._error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:
            if not self._authorized():
                self._error(HTTPStatus.NOT_FOUND, "Not found")
                return
            if not self._same_origin():
                self._error(HTTPStatus.FORBIDDEN, "Origin rejected")
                return
            parsed = urlparse(self.path)
            payload = self._read_json()
            if payload is None:
                return
            if parsed.path == "/api/rating":
                response_message = "Saved"
                try:
                    update_report_rating(
                        record_dir,
                        report_date=payload.get("date", ""),
                        arxiv_id=payload.get("arxiv_id", ""),
                        rating=payload.get("rating", ""),
                    )
                    write_html_companion(
                        record_dir / f"{payload.get('date', '')}.md",
                        asset_prefix=".state/viewer-assets/katex",
                    )
                except ValueError as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                if server.downloader is not None:
                    with state_lock:
                        job_running = bool(active_job["kind"])
                    if job_running:
                        response_message = "Saved; filename update pending."
                    else:
                        try:
                            report_date = payload.get("date", "")
                            refreshed = reconciled_download_state(report_date)
                            with state_lock:
                                download_states[report_date] = refreshed
                                download_states[report_date]["message"] = (
                                    f"Download library updated for {report_date}."
                                )
                        except Exception as exc:
                            print(f"[viewer] rating reconciliation failed: {exc}")
                            response_message = "Saved; filename update pending."
                response = json.dumps(
                    {"ok": True, "message": response_message}
                ).encode("utf-8")
                self._send(
                    HTTPStatus.OK,
                    response,
                    "application/json; charset=utf-8",
                )
                return
            if parsed.path == "/api/search/start":
                status, response_payload = start_search()
                self._send(
                    status,
                    json.dumps(response_payload).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            if parsed.path == "/api/download/start":
                status, response_payload = start_download(
                    payload.get("date", "")
                )
                self._send(
                    status,
                    json.dumps(response_payload).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            if parsed.path == "/api/shutdown":
                with state_lock:
                    if active_job["kind"]:
                        self._error(
                            HTTPStatus.CONFLICT,
                            f"Cannot close while {active_job['kind']} is running",
                        )
                        return
                response = json.dumps({"ok": True}).encode("utf-8")
                self._send(
                    HTTPStatus.OK,
                    response,
                    "application/json; charset=utf-8",
                )
                Thread(target=self.server.shutdown, daemon=True).start()
                return
            if parsed.path == "/api/configure":
                with state_lock:
                    if active_job["kind"]:
                        self._error(
                            HTTPStatus.CONFLICT,
                            f"Cannot configure while {active_job['kind']} is running",
                        )
                        return
                self.server.action = "configure"
                response = json.dumps({"ok": True}).encode("utf-8")
                self._send(
                    HTTPStatus.OK,
                    response,
                    "application/json; charset=utf-8",
                )
                Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._error(HTTPStatus.NOT_FOUND, "Not found")

    server = ThreadingHTTPServer(("127.0.0.1", port), ViewerHandler)
    server.search_runner = search_runner or (
        lambda run_date: SearchResult(
            "failed",
            "Search is unavailable; no report was written.",
        )
    )
    server.downloader = downloader
    server.action = "shutdown"
    return server
