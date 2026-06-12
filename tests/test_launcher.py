from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

from scripts.launch_viewer import (
    make_downloader,
    make_daily_runner,
    run_daily_if_missing,
    select_report_date,
    viewer_url,
)
from arxiv_daily.viewer import SearchResult


class LauncherTests(unittest.TestCase):
    def test_downloader_uses_configured_library_and_shared_arxiv_state(self) -> None:
        config = Mock(
            record_dir=Path("/tmp/records"),
            active_library_dir=Path("/tmp/library"),
            categories={"nucl-th": 1.0},
            user_agent="test-agent",
            api_min_interval_seconds=3.0,
            api_timeout_seconds=60.0,
            api_retry_backoffs=(10.0, 20.0, 40.0),
            api_retry_deadline_seconds=90.0,
        )

        downloader = make_downloader(config)

        self.assertEqual(downloader.record_dir, Path("/tmp/records"))
        self.assertEqual(downloader.library_dir, Path("/tmp/library"))
        self.assertEqual(downloader.state_dir, Path("/tmp/records/.state"))
        self.assertIsNotNone(downloader.fetcher)

    def test_default_launch_opens_report_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-08.md").touch()
            (root / "2026-06-09.md").touch()

            self.assertIsNone(select_report_date(root, None))

    def test_rejects_requested_date_without_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                select_report_date(Path(tmp), "2026-06-09")

    def test_builds_tokenized_report_and_index_urls(self) -> None:
        self.assertEqual(
            viewer_url(8123, "secret token", "2026-06-09"),
            "http://127.0.0.1:8123/report/2026-06-09?token=secret%20token",
        )
        self.assertEqual(
            viewer_url(8123, "secret token", None),
            "http://127.0.0.1:8123/?token=secret%20token",
        )

    def test_existing_today_report_skips_daily_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-09.md").touch()
            runner = Mock()

            status = run_daily_if_missing(
                record_dir=root,
                profile_path=Path("/tmp/profile.toml"),
                run_date=date(2026, 6, 9),
                python_path=Path("/tmp/python"),
                runner=runner,
            )

        self.assertEqual(status, SearchResult("already-exists", "Today's report is ready."))
        runner.assert_not_called()

    def test_missing_today_report_runs_pinned_daily_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))

            status = run_daily_if_missing(
                record_dir=root,
                profile_path=Path("/tmp/profile.toml"),
                run_date=date(2026, 6, 9),
                python_path=Path("/tmp/python"),
                runner=runner,
            )

        self.assertEqual(status, SearchResult("skipped", "No new digest was created."))
        runner.assert_called_once()
        command = runner.call_args.args[0]
        self.assertEqual(command[0], "/tmp/python")
        self.assertTrue(command[1].endswith("/scripts/run_daily.py"))
        self.assertEqual(
            command[2:],
            [
                "--date",
                "2026-06-09",
                "--profile",
                "/tmp/profile.toml",
            ],
        )
        self.assertFalse(runner.call_args.kwargs["check"])
        self.assertTrue(runner.call_args.kwargs["capture_output"])
        self.assertTrue(runner.call_args.kwargs["text"])

    def test_failed_daily_run_does_not_prevent_viewing_older_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-08.md").touch()
            runner = Mock(
                return_value=Mock(
                    returncode=2,
                    stdout="",
                    stderr=(
                        "FAILED: Verified arXiv retrieval failed: "
                        "arXiv rate limit persisted after 90 seconds (4 attempts)\n"
                    ),
                )
            )

            status = run_daily_if_missing(
                record_dir=root,
                profile_path=Path("/tmp/profile.toml"),
                run_date=date(2026, 6, 9),
                python_path=Path("/tmp/python"),
                runner=runner,
            )

            self.assertEqual(status.status, "failed")
            self.assertEqual(
                status.message,
                "arXiv rate limit persisted after 90 seconds; "
                "no report was written.",
            )
            self.assertIsNone(select_report_date(root, None))

    def test_daily_runner_is_lazy_until_called_by_viewer(self) -> None:
        runner = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
        daily_runner = make_daily_runner(
            record_dir=Path("/tmp/records"),
            profile_path=Path("/tmp/profile.toml"),
            python_path=Path("/tmp/python"),
            runner=runner,
        )

        runner.assert_not_called()
        status = daily_runner(date(2026, 6, 9))

        self.assertEqual(status, SearchResult("skipped", "No new digest was created."))
        runner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
