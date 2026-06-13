from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from arxiv_daily.models import Paper, RankedPaper
from arxiv_daily.pipeline import DailyPipeline, PipelineError
from arxiv_daily.report import ExistingReportError, render_report, write_report_atomic
from arxiv_daily.state import MetadataCache, RecommenderState


def sample_paper(arxiv_id: str = "2606.00001") -> Paper:
    return Paper(
        arxiv_id=arxiv_id,
        versioned_id=f"{arxiv_id}v1",
        title="Verified Neutron-Star Paper",
        abstract="This exact abstract came from the parsed arXiv Atom response.",
        authors=("Verified Author",),
        categories=("nucl-th", "astro-ph.HE"),
        primary_category="nucl-th",
        published=datetime(2026, 6, 8, 12, tzinfo=timezone.utc),
        updated=datetime(2026, 6, 8, 13, tzinfo=timezone.utc),
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v1",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v1",
    )


class ReportTests(unittest.TestCase):
    def test_human_readable_report_contains_auditable_fields(self) -> None:
        item = RankedPaper(
            paper=sample_paper(),
            score=0.84,
            tier="Core",
            matched_topics=("nuclear astrophysics", "neutron stars"),
            semantic_score=0.9,
            priority_score=0.8,
            lexical_score=0.7,
            recency_score=1.0,
            why="Strong semantic and explicit-topic match.",
        )

        text = render_report(
            run_date=date(2026, 6, 8),
            announcement_date=date(2026, 6, 8),
            query_start=date(2026, 6, 1),
            fetched_at=datetime(2026, 6, 8, 12, tzinfo=timezone.utc),
            candidate_count=42,
            model_label="allenai/specter2_base@revision + proximity@revision",
            selected=[item],
        )

        self.assertIn("## 1. [Verified Neutron-Star Paper]", text)
        self.assertIn("2606.00001v1", text)
        self.assertIn("**Interest:** unrated", text)
        self.assertIn("**Abstract (verbatim from arXiv):**", text)
        self.assertIn(sample_paper().abstract, text)
        self.assertIn("<!-- arxiv-record:", text)

    def test_visible_authors_are_limited_to_three_without_losing_metadata(self) -> None:
        paper = replace(
            sample_paper(),
            authors=("Author One", "Author Two", "Author Three", "Author Four"),
        )
        item = RankedPaper(
            paper=paper,
            score=0.84,
            tier="Core",
            matched_topics=("nuclear astrophysics",),
            semantic_score=0.9,
            priority_score=0.8,
            lexical_score=0.7,
            recency_score=1.0,
            why="Verified.",
        )

        text = render_report(
            run_date=date(2026, 6, 8),
            announcement_date=date(2026, 6, 8),
            query_start=date(2026, 6, 1),
            fetched_at=datetime(2026, 6, 8, 12, tzinfo=timezone.utc),
            candidate_count=1,
            model_label="verified-model",
            selected=[item],
        )

        self.assertIn(
            "**Authors:** Author One, Author Two, Author Three, et al.",
            text,
        )
        self.assertNotIn("**Authors:** Author One, Author Two, Author Three, Author Four", text)
        self.assertIn('"Author Four"', text)

    def test_visible_authors_ignore_punctuation_only_separators(self) -> None:
        paper = replace(
            sample_paper(),
            authors=(
                "Belle",
                "Belle II Collaborations",
                ":",
                "M. Abumusabh",
                "I. Adachi",
            ),
        )
        item = RankedPaper(
            paper=paper,
            score=0.84,
            tier="Core",
            matched_topics=("hep-ex",),
            semantic_score=0.9,
            priority_score=0.8,
            lexical_score=0.7,
            recency_score=1.0,
            why="Verified.",
        )

        text = render_report(
            run_date=date(2026, 6, 8),
            announcement_date=date(2026, 6, 8),
            query_start=date(2026, 6, 1),
            fetched_at=datetime(2026, 6, 8, 12, tzinfo=timezone.utc),
            candidate_count=1,
            model_label="verified-model",
            selected=[item],
        )

        self.assertIn(
            "**Authors:** Belle, Belle II Collaborations, M. Abumusabh, et al.",
            text,
        )
        self.assertIn('":"', text)

    def test_force_rerun_backs_up_and_preserves_rating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "2026-06-08.md"
            original.write_text(
                '<!-- arxiv-record:{"arxiv_id":"2606.00001"} -->\n'
                "**Interest:** strong\n",
                encoding="utf-8",
            )
            replacement = (
                '<!-- arxiv-record:{"arxiv_id":"2606.00001"} -->\n'
                "**Interest:** unrated\n"
            )

            with self.assertRaises(ExistingReportError):
                write_report_atomic(original, replacement, force=False)

            write_report_atomic(original, replacement, force=True)

            self.assertIn("**Interest:** strong", original.read_text(encoding="utf-8"))
            backups = list((root / ".state" / "backups").glob("2026-06-08.*.md"))
            self.assertEqual(len(backups), 1)

    def test_force_rerun_preserves_multiple_ratings_without_duplicating_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "2026-06-08.md"
            original.write_text(
                '<!-- arxiv-record:{"arxiv_id":"2606.00001"} -->\n'
                "**Interest:** strong\n"
                '<!-- arxiv-record:{"arxiv_id":"2606.00002"} -->\n'
                "**Interest:** skip\n",
                encoding="utf-8",
            )
            replacement = (
                '<!-- arxiv-record:{"arxiv_id":"2606.00001"} -->\n'
                "**Interest:** unrated\n"
                '<!-- arxiv-record:{"arxiv_id":"2606.00002"} -->\n'
                "**Interest:** unrated\n"
            )

            write_report_atomic(original, replacement, force=True)

            result = original.read_text(encoding="utf-8")
            self.assertEqual(result.count("<!-- arxiv-record:"), 2)
            self.assertEqual(result.count("**Interest:** strong"), 1)
            self.assertEqual(result.count("**Interest:** skip"), 1)

    def test_force_rerun_preserves_fine_grained_rating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "2026-06-08.md"
            original.write_text(
                '<!-- arxiv-record:{"arxiv_id":"2606.00001"} -->\n'
                "**Interest:** low\n",
                encoding="utf-8",
            )
            replacement = (
                '<!-- arxiv-record:{"arxiv_id":"2606.00001"} -->\n'
                "**Interest:** unrated\n"
            )

            write_report_atomic(original, replacement, force=True)

            self.assertIn("**Interest:** low", original.read_text(encoding="utf-8"))


class FakeSource:
    def __init__(
        self,
        papers: list[Paper] | None = None,
        error: Exception | None = None,
        by_id: dict[str, Paper] | None = None,
    ):
        self.papers = papers or []
        self.error = error
        self.by_id = by_id or {}
        self.fetch_count = 0

    def fetch_candidates(self, start: date, end: date) -> list[Paper]:
        self.fetch_count += 1
        if self.error:
            raise self.error
        return self.papers

    def fetch_by_ids(self, arxiv_ids: list[str]) -> list[Paper]:
        if self.error:
            raise self.error
        return [
            self.by_id[arxiv_id]
            for arxiv_id in arxiv_ids
            if arxiv_id in self.by_id
        ]


class FakeEmbedder:
    label = "fake-model@verified"

    def __init__(self, error: Exception | None = None):
        self.error = error

    def embed(self, papers: list[Paper]) -> dict[str, list[float]]:
        if self.error:
            raise self.error
        return {paper.arxiv_id: [1.0, 0.0] for paper in papers}


class PipelineTests(unittest.TestCase):
    def test_verified_seed_folder_papers_join_interest_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.mkdir()
            (seed / "[!!!] 2501.00001v1.pdf").write_bytes(b"%PDF-test")
            seed_paper = sample_paper("2501.00001")
            source = FakeSource(by_id={seed_paper.arxiv_id: seed_paper})
            pipeline = DailyPipeline.minimal(
                record_dir=root,
                source=source,
                embedder=FakeEmbedder(),
            )
            pipeline.config = replace(
                pipeline.config,
                seed_library_dir=seed,
                seed_library_limit=100,
            )

            interests = pipeline._load_interests(
                date(2026, 6, 12),
                MetadataCache(root / ".state" / "metadata.json"),
            )

            self.assertEqual(
                [(item.paper.arxiv_id, item.weight) for item in interests],
                [("2501.00001", 4.0)],
            )

    def test_weekend_creates_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = DailyPipeline.minimal(
                record_dir=Path(tmp),
                source=FakeSource([sample_paper()]),
                embedder=FakeEmbedder(),
            )
            result = pipeline.run(date(2026, 6, 7))

            self.assertEqual(result.status, "skipped-weekend")
            self.assertEqual(list(Path(tmp).glob("*.md")), [])

    def test_stale_batch_creates_no_file(self) -> None:
        stale = sample_paper()
        stale = stale.with_dates(
            published=datetime(2026, 6, 5, tzinfo=timezone.utc),
            updated=datetime(2026, 6, 5, tzinfo=timezone.utc),
        )
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = DailyPipeline.minimal(
                record_dir=Path(tmp),
                source=FakeSource([stale]),
                embedder=FakeEmbedder(),
            )
            result = pipeline.run(date(2026, 6, 8))

            self.assertEqual(result.status, "skipped-no-announcement")
            self.assertEqual(list(Path(tmp).glob("*.md")), [])

    def test_source_or_model_failure_writes_no_daily_file(self) -> None:
        for source, embedder in [
            (FakeSource(error=RuntimeError("bad atom")), FakeEmbedder()),
            (FakeSource([sample_paper()]), FakeEmbedder(error=RuntimeError("model"))),
        ]:
            with self.subTest(source=source, embedder=embedder):
                with tempfile.TemporaryDirectory() as tmp:
                    pipeline = DailyPipeline.minimal(
                        record_dir=Path(tmp),
                        source=source,
                        embedder=embedder,
                    )
                    with self.assertRaises(PipelineError):
                        pipeline.run(date(2026, 6, 8))
                    self.assertEqual(list(Path(tmp).glob("*.md")), [])

    def test_new_batch_can_backfill_unseen_papers_from_seven_days(self) -> None:
        newest = sample_paper("2606.00001")
        older = sample_paper("2606.00002").with_dates(
            published=datetime(2026, 6, 3, tzinfo=timezone.utc),
            updated=datetime(2026, 6, 3, tzinfo=timezone.utc),
        )
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = DailyPipeline.minimal(
                record_dir=Path(tmp),
                source=FakeSource([newest, older]),
                embedder=FakeEmbedder(),
                threshold=0.0,
            )
            result = pipeline.run(date(2026, 6, 8), dry_run=True)

            self.assertEqual(result.status, "dry-run")
            self.assertIn("2606.00001v1", result.report_text)
            self.assertIn("2606.00002v1", result.report_text)
            self.assertEqual(list(Path(tmp).glob("*.md")), [])

    def test_existing_report_fails_before_network_without_force(self) -> None:
        source = FakeSource([sample_paper()])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-08.md").write_text("existing", encoding="utf-8")
            pipeline = DailyPipeline.minimal(
                record_dir=root,
                source=source,
                embedder=FakeEmbedder(),
            )

            with self.assertRaises(ExistingReportError):
                pipeline.run(date(2026, 6, 8))

            self.assertEqual(source.fetch_count, 0)

    def test_existing_reports_restore_permanent_dedup_when_state_is_missing(self) -> None:
        prior = sample_paper("2606.00001").with_dates(
            published=datetime(2026, 6, 5, tzinfo=timezone.utc),
            updated=datetime(2026, 6, 5, tzinfo=timezone.utc),
        )
        current = sample_paper("2606.00002")
        prior_item = RankedPaper(
            paper=prior,
            score=0.8,
            tier="Core",
            matched_topics=("neutron stars",),
            semantic_score=0.8,
            priority_score=0.8,
            lexical_score=0.8,
            recency_score=1.0,
            why="verified",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-05.md").write_text(
                render_report(
                    run_date=date(2026, 6, 5),
                    announcement_date=date(2026, 6, 5),
                    query_start=date(2026, 5, 29),
                    fetched_at=datetime(2026, 6, 5, tzinfo=timezone.utc),
                    candidate_count=1,
                    model_label="fake",
                    selected=[prior_item],
                ),
                encoding="utf-8",
            )
            pipeline = DailyPipeline.minimal(
                record_dir=root,
                source=FakeSource([current, prior]),
                embedder=FakeEmbedder(),
                threshold=0.0,
            )

            result = pipeline.run(date(2026, 6, 8), dry_run=True)

            self.assertIn("2606.00002v1", result.report_text)
            self.assertNotIn("2606.00001v1", result.report_text)

    def test_state_save_failure_leaves_no_daily_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pipeline = DailyPipeline.minimal(
                record_dir=root,
                source=FakeSource([sample_paper()]),
                embedder=FakeEmbedder(),
                threshold=0.0,
            )

            with patch.object(
                RecommenderState,
                "save",
                side_effect=OSError("disk failure"),
            ):
                with self.assertRaises(PipelineError):
                    pipeline.run(date(2026, 6, 8))

            self.assertFalse((root / "2026-06-08.md").exists())
            self.assertFalse((root / "2026-06-08.html").exists())

    def test_successful_run_writes_markdown_and_html_companion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pipeline = DailyPipeline.minimal(
                record_dir=root,
                source=FakeSource([sample_paper()]),
                embedder=FakeEmbedder(),
                threshold=0.0,
            )

            result = pipeline.run(date(2026, 6, 8))

            self.assertEqual(result.status, "written")
            self.assertTrue((root / "2026-06-08.md").is_file())
            html_path = root / "2026-06-08.html"
            self.assertTrue(html_path.is_file())
            html = html_path.read_text(encoding="utf-8")
            self.assertIn("Verified Neutron-Star Paper", html)
            self.assertIn("Open with arXiv Hub.command", html)

    def test_html_failure_rolls_back_markdown_state_and_companion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pipeline = DailyPipeline.minimal(
                record_dir=root,
                source=FakeSource([sample_paper()]),
                embedder=FakeEmbedder(),
                threshold=0.0,
            )

            with patch(
                "arxiv_daily.pipeline.write_html_companion",
                side_effect=OSError("html disk failure"),
            ):
                with self.assertRaises(PipelineError):
                    pipeline.run(date(2026, 6, 8))

            self.assertFalse((root / "2026-06-08.md").exists())
            self.assertFalse((root / "2026-06-08.html").exists())
            self.assertFalse((root / ".state" / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
