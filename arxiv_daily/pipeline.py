from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Protocol

from .config import ProfileConfig, Topic
from .embedding import EmbeddingCache
from .history import parse_report_history, report_arxiv_ids, scan_library
from .models import InterestPaper, Paper
from .ranking import build_interest_clusters, rank_candidates, select_diverse
from .report import ExistingReportError, render_report, write_report_atomic
from .seed_library import SeedScanError, scan_seed_library
from .state import MetadataCache, RecommenderState
from .viewer import write_html_companion


class PaperSource(Protocol):
    def fetch_candidates(self, start: date, end: date) -> list[Paper]: ...

    def fetch_by_ids(self, arxiv_ids: list[str]) -> list[Paper]: ...


class PaperEmbedder(Protocol):
    label: str

    def embed(self, papers: list[Paper]) -> dict[str, list[float]]: ...


class PipelineError(RuntimeError):
    """A run failed before a complete verified report could be written."""


@dataclass(frozen=True)
class RunResult:
    status: str
    report_path: Path | None = None
    report_text: str = ""
    selected_count: int = 0


class DailyPipeline:
    def __init__(
        self,
        *,
        config: ProfileConfig,
        source: PaperSource,
        embedder: PaperEmbedder,
    ) -> None:
        self.config = config
        self.source = source
        self.embedder = embedder

    @classmethod
    def minimal(
        cls,
        *,
        record_dir: Path,
        source: PaperSource,
        embedder: PaperEmbedder,
        threshold: float = 0.60,
    ) -> "DailyPipeline":
        return cls(
            config=ProfileConfig(
                record_dir=record_dir,
                active_library_dir=record_dir / "missing-active",
                archive_library_dir=record_dir / "missing-old",
                seed_library_dir=None,
                seed_library_limit=100,
                categories={"nucl-th": 1.0, "astro-ph.HE": 0.8},
                topics=(
                    Topic("nuclear astrophysics", 1.0, ("neutron", "dense matter")),
                ),
                report_limit=20,
                relevance_threshold=threshold,
                backfill_days=7,
                announcement_max_age_days=1,
                history_report_limit=100,
                history_library_limit=100,
                recency_decay_days=180.0,
                mmr_lambda=0.75,
                base_model="fake",
                base_revision="fake",
                adapter_model="fake",
                adapter_revision="fake",
                batch_size=8,
                user_agent="test",
            ),
            source=source,
            embedder=embedder,
        )

    def _load_interests(
        self,
        run_date: date,
        metadata_cache: MetadataCache,
    ) -> list[InterestPaper]:
        report_items = parse_report_history(
            self.config.record_dir,
            self.config.history_report_limit,
        )
        library_items = scan_library(
            [
                self.config.active_library_dir,
                self.config.archive_library_dir,
            ],
            self.config.history_library_limit,
        )
        if (
            self.config.seed_library_dir is not None
            and self.config.seed_library_dir.is_dir()
            and not self.config.seed_library_dir.is_symlink()
        ):
            try:
                seed_scan = scan_seed_library(
                    self.config.seed_library_dir,
                    cache_path=(
                        self.config.record_dir
                        / ".state"
                        / "seed-library-index.json"
                    ),
                    limit=self.config.seed_library_limit,
                )
                library_items.extend(seed_scan.candidates)
            except SeedScanError:
                pass
        missing_ids = [
            item.arxiv_id
            for item in library_items
            if item.arxiv_id not in metadata_cache.papers
        ]
        if missing_ids:
            for paper in self.source.fetch_by_ids(missing_ids):
                metadata_cache.papers[paper.arxiv_id] = paper
        library_history = [
            InterestPaper(
                paper=metadata_cache.papers[item.arxiv_id],
                weight=item.weight,
                observed_date=item.observed_date,
            )
            for item in library_items
            if item.arxiv_id in metadata_cache.papers
        ]

        combined: dict[str, InterestPaper] = {}
        for item in report_items + library_history:
            age = (
                max(0, (run_date - item.observed_date).days)
                if item.observed_date
                else 0
            )
            decayed = item.weight * math.exp(
                -age / self.config.recency_decay_days
            )
            current = combined.get(item.paper.arxiv_id)
            if current is None or abs(decayed) > abs(current.weight):
                combined[item.paper.arxiv_id] = InterestPaper(
                    paper=item.paper,
                    weight=decayed,
                    observed_date=item.observed_date,
                )
        return list(combined.values())

    def _embed_with_cache(
        self,
        papers: list[Paper],
        cache: EmbeddingCache,
    ) -> dict[str, list[float]]:
        vectors: dict[str, list[float]] = {}
        missing: list[Paper] = []
        for paper in papers:
            cached = cache.get(paper)
            if cached is None:
                missing.append(paper)
            else:
                vectors[paper.arxiv_id] = cached
        if missing:
            generated = self.embedder.embed(missing)
            missing_ids = {paper.arxiv_id for paper in missing}
            if set(generated) != missing_ids:
                raise PipelineError("Embedding model returned an incomplete result set")
            for paper in missing:
                vector = generated[paper.arxiv_id]
                vectors[paper.arxiv_id] = vector
                cache.put(paper, vector)
        return vectors

    def run(
        self,
        run_date: date,
        *,
        dry_run: bool = False,
        force: bool = False,
    ) -> RunResult:
        if run_date.weekday() >= 5:
            return RunResult(status="skipped-weekend")

        report_path = self.config.record_dir / f"{run_date.isoformat()}.md"
        if report_path.exists() and not force:
            raise ExistingReportError(f"Report already exists: {report_path}")

        state_dir = self.config.record_dir / ".state"
        state = RecommenderState.load(state_dir / "state.json")
        state.seen_ids.update(
            report_arxiv_ids(
                self.config.record_dir,
                exclude_date=run_date if force else None,
            )
        )
        metadata_cache = MetadataCache(state_dir / "metadata.json")
        embedding_cache = EmbeddingCache(
            state_dir / "embeddings.json",
            self.embedder.label,
        )
        query_start = run_date - timedelta(days=self.config.backfill_days)
        try:
            fetched = self.source.fetch_candidates(query_start, run_date)
        except Exception as exc:
            raise PipelineError(f"Verified arXiv retrieval failed: {exc}") from exc
        if not fetched:
            return RunResult(status="skipped-no-announcement")
        latest_announcement = max(paper.published.date() for paper in fetched)
        announcement_age = (run_date - latest_announcement).days
        if (
            announcement_age < 0
            or announcement_age > self.config.announcement_max_age_days
            or (
                state.last_announcement_date is not None
                and latest_announcement <= state.last_announcement_date
                and not force
            )
        ):
            return RunResult(status="skipped-no-announcement")

        for paper in fetched:
            metadata_cache.papers[paper.arxiv_id] = paper
        unseen = [paper for paper in fetched if paper.arxiv_id not in state.seen_ids]
        if not unseen:
            return RunResult(status="skipped-no-unseen")
        try:
            interests = self._load_interests(run_date, metadata_cache)
            all_for_embedding = [
                item.paper for item in interests
            ] + unseen
            all_vectors = self._embed_with_cache(all_for_embedding, embedding_cache)
            interest_vectors = {
                item.paper.arxiv_id: all_vectors[item.paper.arxiv_id]
                for item in interests
            }
            clusters = build_interest_clusters(interests, interest_vectors)
            negative_embeddings = [
                interest_vectors[item.paper.arxiv_id]
                for item in interests
                if item.weight < 0 and item.paper.arxiv_id in interest_vectors
            ]
            historical_embeddings = [
                interest_vectors[item.paper.arxiv_id]
                for item in interests
                if item.weight > 0 and item.paper.arxiv_id in interest_vectors
            ]
            candidate_vectors = {
                paper.arxiv_id: all_vectors[paper.arxiv_id] for paper in unseen
            }
            ranked = rank_candidates(
                candidates=unseen,
                candidate_embeddings=candidate_vectors,
                clusters=clusters,
                negative_embeddings=negative_embeddings,
                historical_embeddings=historical_embeddings,
                topic_weights=self.config.topic_weights,
                topic_phrases=self.config.topic_phrases,
                category_weights=self.config.categories,
                run_date=run_date,
            )
            selected = select_diverse(
                ranked,
                candidate_vectors,
                limit=self.config.report_limit,
                threshold=self.config.relevance_threshold,
                mmr_lambda=self.config.mmr_lambda,
            )
        except PipelineError:
            raise
        except Exception as exc:
            raise PipelineError(f"Ranking failed closed: {exc}") from exc

        if not selected:
            return RunResult(status="skipped-no-relevant")
        fetched_at = datetime.now(timezone.utc)
        report_text = render_report(
            run_date=run_date,
            announcement_date=latest_announcement,
            query_start=query_start,
            fetched_at=fetched_at,
            candidate_count=len(unseen),
            model_label=self.embedder.label,
            selected=selected,
        )
        if dry_run:
            return RunResult(
                status="dry-run",
                report_text=report_text,
                selected_count=len(selected),
            )

        state_path = state_dir / "state.json"
        metadata_path = state_dir / "metadata.json"
        embedding_path = state_dir / "embeddings.json"
        html_path = report_path.with_suffix(".html")
        transaction_paths = [
            state_path,
            metadata_path,
            embedding_path,
            report_path,
            html_path,
        ]
        snapshots = {
            path: path.read_bytes() if path.exists() else None
            for path in transaction_paths
        }
        state.seen_ids.update(item.paper.arxiv_id for item in selected)
        state.last_announcement_date = latest_announcement
        try:
            state.save(state_path)
            metadata_cache.save()
            embedding_cache.save()
            write_report_atomic(report_path, report_text, force=force)
            write_html_companion(
                report_path,
                asset_prefix=".state/viewer-assets/katex",
            )
        except Exception as exc:
            for path, content in snapshots.items():
                try:
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = path.with_name(f".{path.name}.rollback")
                        temporary.write_bytes(content)
                        os.replace(temporary, path)
                except OSError:
                    pass
            raise PipelineError(f"Transactional commit failed: {exc}") from exc
        return RunResult(
            status="written",
            report_path=report_path,
            selected_count=len(selected),
        )
