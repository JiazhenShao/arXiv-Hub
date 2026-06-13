#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import secrets
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.parse import quote


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from arxiv_daily.config import ProfileConfig  # noqa: E402
from arxiv_daily.arxiv_api import ArxivClient  # noqa: E402
from arxiv_daily.downloader import PaperDownloader  # noqa: E402
from arxiv_daily.viewer import (  # noqa: E402
    SearchResult,
    create_server,
    generate_all_html,
    list_report_dates,
)
from scripts.browser_bridge import atomic_write_destination  # noqa: E402


CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
CONFIGURE_EXIT_CODE = 75


def select_report_date(record_dir: Path, requested: str | None) -> str | None:
    dates = list_report_dates(record_dir)
    if requested is not None:
        if requested not in dates:
            raise ValueError(f"No Markdown report exists for {requested}")
        return requested
    return None


def viewer_url(port: int, token: str, report_date: str | None) -> str:
    path = f"/report/{report_date}" if report_date else "/"
    return f"http://127.0.0.1:{port}{path}?token={quote(token)}"


def run_daily_if_missing(
    *,
    record_dir: Path,
    profile_path: Path,
    run_date: date,
    python_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> SearchResult:
    report_path = record_dir / f"{run_date.isoformat()}.md"
    if report_path.is_file() and not report_path.is_symlink():
        return SearchResult("already-exists", "Today's report is ready.")
    result = runner(
        [
            str(python_path),
            str(SKILL_ROOT / "scripts" / "run_daily.py"),
            "--date",
            run_date.isoformat(),
            "--profile",
            str(profile_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(
            result.stderr,
            file=sys.stderr,
            end="" if result.stderr.endswith("\n") else "\n",
        )
    if result.returncode != 0:
        return SearchResult("failed", _failure_message(result.stderr))
    if report_path.is_file():
        return SearchResult("written", "Today's report is ready.")
    return SearchResult("skipped", "No new digest was created.")


def _failure_message(stderr: str) -> str:
    cleaned = CONTROL_CHAR_RE.sub("", stderr)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    detail = lines[-1] if lines else ""
    detail = re.sub(r"^FAILED:\s*", "", detail, flags=re.IGNORECASE)
    if "rate limit" in detail.lower():
        duration = re.search(
            r"rate limit persisted after ([0-9.]+) seconds",
            detail,
            flags=re.IGNORECASE,
        )
        seconds = duration.group(1) if duration else "the retry deadline"
        return (
            f"arXiv rate limit persisted after {seconds} seconds; "
            "no report was written."
        ) if duration else (
            "arXiv rate limit persisted through the retry deadline; "
            "no report was written."
        )
    if detail:
        return f"Search failed: {detail[:240]}"
    return "Search failed; no report was written."


def make_daily_runner(
    *,
    record_dir: Path,
    profile_path: Path,
    python_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Callable[[date], SearchResult]:
    def run(run_date: date) -> SearchResult:
        return run_daily_if_missing(
            record_dir=record_dir,
            profile_path=profile_path,
            run_date=run_date,
            python_path=python_path,
            runner=runner,
        )

    return run


def make_downloader(config: ProfileConfig) -> PaperDownloader:
    client = ArxivClient(
        config.categories,
        user_agent=config.user_agent,
        min_interval_seconds=config.api_min_interval_seconds,
        timeout_seconds=config.api_timeout_seconds,
        state_dir=config.record_dir / ".state",
        retry_backoffs=config.api_retry_backoffs,
        retry_deadline_seconds=config.api_retry_deadline_seconds,
    )
    return PaperDownloader(
        record_dir=config.record_dir,
        library_dir=config.active_library_dir,
        state_dir=config.record_dir / ".state",
        fetcher=lambda candidate, destination: client.download_pdf(
            candidate.versioned_id,
            destination,
        ),
    )


def ensure_offline_assets(record_dir: Path) -> Path:
    source = SKILL_ROOT / "viewer-assets" / "katex"
    if not source.is_dir():
        raise RuntimeError(f"Pinned KaTeX assets are missing: {source}")
    destination = record_dir / ".state" / "viewer-assets" / "katex"
    source_version = (source / "VERSION").read_text(encoding="utf-8").strip()
    destination_version = destination / "VERSION"
    installed_version = (
        destination_version.read_text(encoding="utf-8").strip()
        if destination_version.is_file()
        else ""
    )
    if installed_version != source_version:
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    return destination


def open_default_browser(
    url: str,
    *,
    opener: Callable[[str], bool] = webbrowser.open,
) -> None:
    if not opener(url):
        raise RuntimeError(f"Could not open a browser. Open this URL: {url}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open the private local arXiv Hub viewer."
    )
    parser.add_argument(
        "--date",
        help="Open a specific YYYY-MM-DD report instead of the newest report.",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=(
            Path.home()
            / "Library"
            / "Application Support"
            / "arXiv Hub"
            / "profile.toml"
        ),
        help="Path to the recommender profile.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Start the server without opening the default browser.",
    )
    parser.add_argument("--bridge-state", type=Path)
    parser.add_argument("--bridge-generation", type=int)
    parser.add_argument("--supervisor-handoff-url")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (args.bridge_state is None) != (args.bridge_generation is None):
        print(
            "VIEWER FAILED: bridge state and generation must be supplied together",
            file=sys.stderr,
        )
        return 2
    config = ProfileConfig.load(args.profile)
    try:
        report_date = select_report_date(config.record_dir, args.date)
        assets_dir = ensure_offline_assets(config.record_dir)
        generated = generate_all_html(config.record_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"VIEWER FAILED: {exc}", file=sys.stderr)
        return 2

    token = secrets.token_urlsafe(32)
    server = create_server(
        record_dir=config.record_dir,
        assets_dir=assets_dir,
        token=token,
        port=0,
        search_runner=make_daily_runner(
            record_dir=config.record_dir,
            profile_path=args.profile,
            python_path=Path(sys.executable),
        ),
        downloader=make_downloader(config),
        timezone_name=config.viewer_timezone,
        search_start_time=config.search_start_time,
        supervisor_handoff_url=args.supervisor_handoff_url,
    )
    url = viewer_url(server.server_port, token, report_date)
    if args.bridge_state is not None:
        try:
            atomic_write_destination(
                args.bridge_state,
                generation=args.bridge_generation,
                url=url,
            )
        except (OSError, ValueError) as exc:
            server.server_close()
            print(f"VIEWER FAILED: could not publish browser destination: {exc}", file=sys.stderr)
            return 2
    print(f"Daily arXiv Viewer: {url}")
    print(f"Refreshed {len(generated)} offline HTML report(s).")
    available_at = config.search_start_time.strftime("%-I:%M %p")
    print(
        f"Use Start searching after {available_at} "
        f"{config.viewer_timezone}."
    )
    print("Use Close Server in the browser or press Ctrl+C here to stop.")
    if not args.no_open:
        try:
            open_default_browser(url)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping viewer.")
    finally:
        server.server_close()
    return CONFIGURE_EXIT_CODE if server.action == "configure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
