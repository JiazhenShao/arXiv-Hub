from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from arxiv_daily.viewer import (
    SearchResult,
    create_server,
    generate_html_companion,
    list_report_dates,
    update_report_rating,
)
from arxiv_daily.downloader import DownloadStatus


REPORT = """# Daily arXiv Recommendations — 2026-06-08

<!-- arxiv-record:{"arxiv_id":"2606.00001","versioned_id":"2606.00001v1","title":"Stars <script>alert(1)</script> and $m^2$","abstract":"Inline $E = m c^2$ and display \\\\[x^2\\\\].","authors":["A & B"],"categories":["nucl-th"],"primary_category":"nucl-th","published":"2026-06-08T00:00:00+00:00","updated":"2026-06-08T00:00:00+00:00","abs_url":"https://arxiv.org/abs/2606.00001v1","pdf_url":"https://arxiv.org/pdf/2606.00001v1"} -->
- **arXiv:** [`2606.00001v1`](https://arxiv.org/abs/2606.00001v1)
- **Interest:** strong

**Abstract (verbatim from arXiv):**

> Inline $E = m c^2$ and display \\[x^2\\].
"""


class ViewerRenderingTests(unittest.TestCase):
    def test_lists_only_dated_markdown_reports_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-08.md").write_text(REPORT, encoding="utf-8")
            (root / "2026-06-09.md").write_text(REPORT, encoding="utf-8")
            (root / "notes.md").write_text("ignore", encoding="utf-8")

            self.assertEqual(
                list_report_dates(root),
                ["2026-06-09", "2026-06-08"],
            )

    def test_static_html_is_escaped_math_ready_and_read_only(self) -> None:
        html = generate_html_companion(
            REPORT,
            report_date="2026-06-08",
            interactive=False,
        )

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("$E = m c^2$", html)
        self.assertIn(r"\[x^2\]", html)
        self.assertIn("katex.min.css", html)
        self.assertIn("Open with arXiv Hub.command to change ratings", html)
        self.assertIn('data-rating="high"', html)
        self.assertIn('data-rating="medium"', html)
        self.assertIn('data-rating="low"', html)
        self.assertIn('data-rating="skip"', html)
        self.assertIn('data-rating="unrated"', html)
        self.assertIn('data-current-rating="high"', html)
        self.assertIn("disabled", html)

    def test_html_reconstructs_links_from_arxiv_id(self) -> None:
        report = REPORT.replace(
            '"abs_url":"https://arxiv.org/abs/2606.00001v1",'
            '"pdf_url":"https://arxiv.org/pdf/2606.00001v1"',
            '"abs_url":"javascript:alert(1)","pdf_url":"javascript:alert(2)"',
        )

        html = generate_html_companion(
            report,
            report_date="2026-06-08",
            interactive=False,
        )

        self.assertNotIn("javascript:", html)
        self.assertIn("https://arxiv.org/abs/2606.00001v1", html)
        self.assertIn("https://arxiv.org/pdf/2606.00001v1", html)

    def test_interactive_html_has_tokenized_controls_and_close_button(self) -> None:
        html = generate_html_companion(
            REPORT,
            report_date="2026-06-08",
            interactive=True,
            token="secret-token",
        )

        self.assertIn('"token": "secret-token"', html)
        self.assertIn('id="close-server"', html)
        self.assertIn('id="start-search"', html)
        self.assertIn("/api/search/status", html)
        self.assertIn("/api/search/start", html)
        self.assertIn('id="download-high"', html)
        self.assertIn("/api/download/status", html)
        self.assertIn("/api/download/start", html)
        self.assertIn("date: viewer.date", html)
        self.assertIn("date=${encodeURIComponent(viewer.date)}", html)
        self.assertIn('class="download-state"', html)
        self.assertIn(
            "closeButton.disabled = searchBusy || downloadBusy;",
            html,
        )
        self.assertIn(
            "Wait for the active job to finish before closing.",
            html,
        )
        self.assertNotIn("Open with arXiv Hub.command", html)

    def test_arxiv_id_links_to_abstract_page_separately_from_pdf(self) -> None:
        html = generate_html_companion(
            REPORT,
            report_date="2026-06-08",
            interactive=False,
        )

        self.assertIn(
            '<a href="https://arxiv.org/abs/2606.00001v1" '
            'target="_blank" rel="noopener noreferrer">2606.00001v1</a>',
            html,
        )
        self.assertIn(
            '<a href="https://arxiv.org/pdf/2606.00001v1" target="_blank"',
            html,
        )
        self.assertIn(
            "<h2>Stars &lt;script&gt;alert(1)&lt;/script&gt; and $m^2$</h2>",
            html,
        )
        self.assertNotIn("<h2><a ", html)
        self.assertEqual(
            html.count('href="https://arxiv.org/abs/2606.00001v1"'),
            1,
        )

    def test_html_limits_visible_authors_to_first_three(self) -> None:
        report = REPORT.replace(
            '"authors":["A & B"]',
            '"authors":["Author One","Author Two","Author Three",'
            '"Author Four","Author Five"]',
        )

        html = generate_html_companion(
            report,
            report_date="2026-06-08",
            interactive=False,
        )

        self.assertIn(
            '<p class="authors">Author One, Author Two, Author Three, et al.</p>',
            html,
        )
        self.assertNotIn("Author Four", html)
        self.assertNotIn("Author Five", html)


class ViewerRatingTests(unittest.TestCase):
    def test_updates_only_known_paper_rating_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-06-08.md"
            path.write_text(REPORT, encoding="utf-8")

            update_report_rating(
                Path(tmp),
                report_date="2026-06-08",
                arxiv_id="2606.00001",
                rating="low",
            )

            result = path.read_text(encoding="utf-8")
            self.assertIn("**Interest:** low", result)
            self.assertIn("Inline $E = m c^2$", result)
            self.assertEqual(list(Path(tmp).glob(".*.tmp")), [])

    def test_rejects_invalid_rating_unknown_paper_and_invalid_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-08.md").write_text(REPORT, encoding="utf-8")
            for report_date, arxiv_id, rating in [
                ("2026-06-08", "2606.00001", "strong"),
                ("2026-06-08", "2606.99999", "high"),
                ("../profile", "2606.00001", "high"),
            ]:
                with self.subTest(
                    report_date=report_date,
                    arxiv_id=arxiv_id,
                    rating=rating,
                ):
                    with self.assertRaises(ValueError):
                        update_report_rating(
                            root,
                            report_date=report_date,
                            arxiv_id=arxiv_id,
                            rating=rating,
                        )

    def test_rejects_symlinked_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.md"
            outside.write_text(REPORT, encoding="utf-8")
            (root / "2026-06-08.md").symlink_to(outside)

            self.assertEqual(list_report_dates(root), [])
            with self.assertRaises(ValueError):
                update_report_rating(
                    root,
                    report_date="2026-06-08",
                    arxiv_id="2606.00001",
                    rating="high",
                )


class ViewerServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "2026-06-08.md").write_text(REPORT, encoding="utf-8")
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "katex.min.css").write_text(
            "@font-face{src:url(fonts/test.woff2)}",
            encoding="utf-8",
        )
        self.download_started = Event()
        self.download_release = Event()
        self.download_report_date = ""
        self.downloader = Mock()
        self.downloader.reconcile.return_value = {
            "2606.00001": DownloadStatus(
                "pending",
                "Ready to download.",
            )
        }

        def download_high(
            report_date: str,
            progress: object | None = None,
        ) -> dict[str, DownloadStatus]:
            self.download_report_date = report_date
            self.download_started.set()
            if callable(progress):
                progress(1, 1, "2606.00001")
            self.download_release.wait(timeout=2)
            return {
                "2606.00001": DownloadStatus(
                    "downloaded",
                    "PDF downloaded.",
                    "[high][nucl-th] Paper - 2606.00001v1.pdf",
                )
            }

        self.downloader.download_high.side_effect = download_high
        self.server = create_server(
            record_dir=self.root,
            assets_dir=assets,
            token="test-token",
            port=0,
            now_provider=lambda: datetime(
                2026, 6, 9, 20, 0, tzinfo=ZoneInfo("America/Chicago")
            ),
            search_runner=lambda run_date: SearchResult(
                "skipped",
                "No new digest was created.",
            ),
            downloader=self.downloader,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _post(
        self,
        path: str,
        payload: dict[str, str],
        *,
        token: str = "test-token",
        origin: str | None = None,
    ) -> urllib.response.addinfourl:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{path}?token={token}",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": origin or self.base,
            },
        )
        return urllib.request.urlopen(request, timeout=2)

    def _get_json(self, path: str) -> dict[str, object]:
        separator = "&" if "?" in path else "?"
        with urllib.request.urlopen(
            f"{self.base}{path}{separator}token=test-token",
            timeout=2,
        ) as response:
            return json.loads(response.read())

    def test_rating_endpoint_updates_markdown(self) -> None:
        with self._post(
            "/api/rating",
            {
                "date": "2026-06-08",
                "arxiv_id": "2606.00001",
                "rating": "medium",
            },
        ) as response:
            self.assertEqual(response.status, 200)

        self.assertIn(
            "**Interest:** medium",
            (self.root / "2026-06-08.md").read_text(encoding="utf-8"),
        )

    def test_mutations_require_token_and_same_origin(self) -> None:
        for token, origin in [
            ("wrong", self.base),
            ("test-token", "https://malicious.example"),
        ]:
            with self.subTest(token=token, origin=origin):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post(
                        "/api/rating",
                        {
                            "date": "2026-06-08",
                            "arxiv_id": "2606.00001",
                            "rating": "high",
                        },
                        token=token,
                        origin=origin,
                    )
                self.assertIn(caught.exception.code, {403, 404})

    def test_report_page_reflects_current_markdown_rating(self) -> None:
        update_report_rating(
            self.root,
            report_date="2026-06-08",
            arxiv_id="2606.00001",
            rating="low",
        )
        with urllib.request.urlopen(
            f"{self.base}/report/2026-06-08?token=test-token",
            timeout=2,
        ) as response:
            html = response.read().decode("utf-8")

        self.assertIn('data-current-rating="low"', html)

    def test_index_lists_newest_first_and_can_close_server(self) -> None:
        (self.root / "2026-06-09.md").write_text(REPORT, encoding="utf-8")
        with urllib.request.urlopen(
            f"{self.base}/?token=test-token",
            timeout=2,
        ) as response:
            html = response.read().decode("utf-8")

        self.assertLess(html.index("2026-06-09"), html.index("2026-06-08"))
        self.assertIn('id="close-server"', html)
        self.assertIn('id="start-search"', html)
        self.assertNotIn('id="download-high"', html)
        self.assertIn("Start searching", html)
        self.assertNotIn("/api/download/", html)

    def test_index_places_aligned_actions_above_report_dates(self) -> None:
        (self.root / "2026-06-09.md").write_text(REPORT, encoding="utf-8")
        with urllib.request.urlopen(
            f"{self.base}/?token=test-token",
            timeout=2,
        ) as response:
            html = response.read().decode("utf-8")

        self.assertLess(html.index('<div class="actions">'), html.index("<ol>"))
        self.assertIn(
            '<button id="start-search" class="primary-action" disabled>',
            html,
        )
        self.assertIn(
            '<button id="close-server" class="primary-action">',
            html,
        )
        self.assertIn(
            "grid-template-columns:repeat(3,minmax(0,1fr))",
            html,
        )
        self.assertIn(
            "grid-template-rows:auto minmax(1.25rem,auto)",
            html,
        )
        self.assertIn(
            "closeButton.disabled = searchBusy;",
            html,
        )
        self.assertIn(
            "Wait for the active job to finish before closing.",
            html,
        )
        self.assertLess(html.index("2026-06-09"), html.index("2026-06-08"))

    def test_download_status_reports_date_pending_count_and_card_state(self) -> None:
        status = self._get_json("/api/download/status?date=2026-06-08")

        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["pending"], 1)
        self.assertTrue(status["enabled"])
        self.assertEqual(
            status["papers"]["2606.00001"]["state"],
            "pending",
        )

    def test_download_runs_in_background_and_blocks_other_jobs_and_shutdown(self) -> None:
        with self._post(
            "/api/download/start",
            {"date": "2026-06-08"},
        ) as response:
            self.assertEqual(response.status, 202)
        self.assertTrue(self.download_started.wait(timeout=1))
        self.assertEqual(self.download_report_date, "2026-06-08")

        running = self._get_json("/api/download/status?date=2026-06-08")
        self.assertEqual(running["state"], "running")
        self.assertFalse(running["enabled"])
        self.assertIn("1/1", running["message"])
        with self.assertRaises(urllib.error.HTTPError) as search_error:
            self._post("/api/search/start", {})
        self.assertEqual(search_error.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError) as shutdown_error:
            self._post("/api/shutdown", {})
        self.assertEqual(shutdown_error.exception.code, 409)

        self.download_release.set()
        deadline = monotonic() + 2
        while monotonic() < deadline:
            completed = self._get_json(
                "/api/download/status?date=2026-06-08"
            )
            if completed["state"] == "complete":
                break
            sleep(0.02)
        self.assertEqual(completed["pending"], 0)
        self.assertFalse(completed["enabled"])
        self.assertEqual(
            completed["papers"]["2606.00001"]["state"],
            "downloaded",
        )

    def test_failed_download_remains_enabled_for_retry(self) -> None:
        self.download_release.set()
        self.downloader.download_high.side_effect = (
            lambda report_date, progress=None: {
                "2606.00001": DownloadStatus(
                    "failed",
                    "Temporary arXiv failure.",
                )
            }
        )

        with self._post(
            "/api/download/start",
            {"date": "2026-06-08"},
        ) as response:
            self.assertEqual(response.status, 202)

        deadline = monotonic() + 2
        while monotonic() < deadline:
            completed = self._get_json(
                "/api/download/status?date=2026-06-08"
            )
            if completed["state"] == "failed":
                break
            sleep(0.02)

        self.assertEqual(completed["pending"], 1)
        self.assertTrue(completed["enabled"])
        self.assertEqual(
            completed["papers"]["2606.00001"]["state"],
            "failed",
        )

    def test_download_mutation_requires_token_and_same_origin(self) -> None:
        for token, origin in [
            ("wrong", self.base),
            ("test-token", "https://malicious.example"),
        ]:
            with self.subTest(token=token, origin=origin):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post(
                        "/api/download/start",
                        {"date": "2026-06-08"},
                        token=token,
                        origin=origin,
                    )
                self.assertIn(caught.exception.code, {403, 404})

    def test_download_endpoints_require_an_existing_report_date(self) -> None:
        for report_date in ["", "not-a-date", "2026-06-09"]:
            with self.subTest(report_date=report_date):
                query = (
                    f"/api/download/status?date={report_date}"
                    if report_date
                    else "/api/download/status"
                )
                with self.assertRaises(urllib.error.HTTPError) as get_error:
                    self._get_json(query)
                self.assertEqual(get_error.exception.code, 400)
                with self.assertRaises(urllib.error.HTTPError) as post_error:
                    self._post(
                        "/api/download/start",
                        {"date": report_date},
                    )
                self.assertEqual(post_error.exception.code, 400)

    def test_download_status_is_cached_separately_for_each_report_date(self) -> None:
        (self.root / "2026-06-09.md").write_text(REPORT, encoding="utf-8")

        def reconcile(report_date: str) -> dict[str, DownloadStatus]:
            arxiv_id = (
                "2606.00001"
                if report_date == "2026-06-08"
                else "2606.00002"
            )
            return {
                arxiv_id: DownloadStatus(
                    "pending",
                    f"Ready for {report_date}.",
                )
            }

        self.downloader.reconcile.side_effect = reconcile

        older = self._get_json(
            "/api/download/status?date=2026-06-08"
        )
        newer = self._get_json(
            "/api/download/status?date=2026-06-09"
        )
        older_again = self._get_json(
            "/api/download/status?date=2026-06-08"
        )

        self.assertEqual(set(older["papers"]), {"2606.00001"})
        self.assertEqual(set(newer["papers"]), {"2606.00002"})
        self.assertEqual(older_again["papers"], older["papers"])
        self.downloader.reconcile.assert_any_call("2026-06-08")
        self.downloader.reconcile.assert_any_call("2026-06-09")

    def test_rating_is_saved_when_filename_reconciliation_fails(self) -> None:
        self.downloader.reconcile.side_effect = OSError("rename blocked")

        with self._post(
            "/api/rating",
            {
                "date": "2026-06-08",
                "arxiv_id": "2606.00001",
                "rating": "medium",
            },
        ) as response:
            payload = json.loads(response.read())

        self.assertTrue(payload["ok"])
        self.assertIn("filename update pending", payload["message"])
        self.assertIn(
            "**Interest:** medium",
            (self.root / "2026-06-08.md").read_text(encoding="utf-8"),
        )

    def test_read_only_assets_do_not_require_session_token(self) -> None:
        with urllib.request.urlopen(
            f"{self.base}/assets/katex.min.css",
            timeout=2,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers.get_content_type(),
                "text/css",
            )

    def test_shutdown_endpoint_stops_server(self) -> None:
        with self._post("/api/shutdown", {}) as response:
            self.assertEqual(response.status, 200)

        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def test_search_status_is_enabled_at_eight_pm(self) -> None:
        status = self._get_json("/api/search/status")

        self.assertEqual(status["state"], "idle")
        self.assertTrue(status["enabled"])
        self.assertEqual(status["date"], "2026-06-09")

    def test_existing_today_report_disables_search(self) -> None:
        (self.root / "2026-06-09.md").write_text(REPORT, encoding="utf-8")

        status = self._get_json("/api/search/status")

        self.assertEqual(status["state"], "ready")
        self.assertFalse(status["enabled"])

    def test_search_runs_in_background_and_exposes_completion(self) -> None:
        started = Event()
        release = Event()

        def search_runner(run_date: object) -> SearchResult:
            started.set()
            release.wait(timeout=2)
            (self.root / "2026-06-09.md").write_text(REPORT, encoding="utf-8")
            return SearchResult("written", "Today's report is ready.")

        self.server.search_runner = search_runner
        with self._post("/api/search/start", {}) as response:
            self.assertEqual(response.status, 202)
        self.assertTrue(started.wait(timeout=1))

        running = self._get_json("/api/search/status")
        self.assertEqual(running["state"], "running")
        self.assertFalse(running["enabled"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/search/start", {})
        self.assertEqual(caught.exception.code, 409)

        release.set()
        deadline = monotonic() + 2
        while monotonic() < deadline:
            completed = self._get_json("/api/search/status")
            if completed["state"] == "ready":
                break
            sleep(0.02)
        self.assertEqual(completed["state"], "ready")
        self.assertFalse(completed["enabled"])

    def test_search_failure_exposes_structured_message_as_json_text(self) -> None:
        self.server.search_runner = lambda run_date: SearchResult(
            "failed",
            'arXiv rate limit persisted after 90 seconds; <b>no report</b>.',
        )

        with self._post("/api/search/start", {}) as response:
            self.assertEqual(response.status, 202)

        deadline = monotonic() + 2
        while monotonic() < deadline:
            completed = self._get_json("/api/search/status")
            if completed["state"] == "failed":
                break
            sleep(0.02)

        self.assertEqual(completed["state"], "failed")
        self.assertEqual(
            completed["message"],
            'arXiv rate limit persisted after 90 seconds; <b>no report</b>.',
        )
        with urllib.request.urlopen(
            f"{self.base}/?token=test-token",
            timeout=2,
        ) as response:
            html = response.read().decode("utf-8")
        self.assertIn("searchStatus.textContent = status.message", html)
        self.assertNotIn("searchStatus.innerHTML", html)


class ViewerEarlySearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "2026-06-08.md").write_text(REPORT, encoding="utf-8")
        assets = self.root / "assets"
        assets.mkdir()
        self.runner = Mock(
            return_value=SearchResult(
                "skipped",
                "No new digest was created.",
            )
        )
        self.server = create_server(
            record_dir=self.root,
            assets_dir=assets,
            token="test-token",
            port=0,
            now_provider=lambda: datetime(
                2026, 6, 9, 19, 59, tzinfo=ZoneInfo("America/Chicago")
            ),
            search_runner=self.runner,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _post(self, path: str, payload: dict[str, str]) -> urllib.response.addinfourl:
        request = urllib.request.Request(
            f"{self.base}{path}?token=test-token",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": self.base,
            },
        )
        return urllib.request.urlopen(request, timeout=2)

    def test_search_is_disabled_and_rejected_before_eight_pm(self) -> None:
        with urllib.request.urlopen(
            f"{self.base}/api/search/status?token=test-token",
            timeout=2,
        ) as response:
            status = json.loads(response.read())

        self.assertEqual(status["state"], "too-early")
        self.assertFalse(status["enabled"])
        self.assertIn("8:00 PM", status["message"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/search/start", {})
        self.assertEqual(caught.exception.code, 403)
        self.runner.assert_not_called()

    def test_rating_edits_still_work_before_eight_pm(self) -> None:
        with self._post(
            "/api/rating",
            {
                "date": "2026-06-08",
                "arxiv_id": "2606.00001",
                "rating": "high",
            },
        ) as response:
            self.assertEqual(response.status, 200)

        self.assertIn(
            "**Interest:** high",
            (self.root / "2026-06-08.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
