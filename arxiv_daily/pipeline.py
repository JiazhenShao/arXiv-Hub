from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Protocol

from .config import ProfileConfig, Topic
from .embedding import EmbeddingCache
from .history import annotation_marker, parse_report_history, report_arxiv_ids, scan_library
from .models import InterestPaper, Paper
from .preferences import PreferenceEvidence, PreferenceSignalStore
from .ranking import build_interest_clusters, rank_candidates, select_diverse
from .report import ExistingReportError, render_report, write_report_atomic
from .schedule import digest_cycle_for_date
from .seed_library import SeedScanError, scan_seed_library
from .state import MetadataCache, RecommenderState
from .viewer import write_html_companion


MAX_ADAPTIVE_BACKFILL_DAYS = 30


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
        preference_store: PreferenceSignalStore | None = None,
    ) -> list[InterestPaper]:
        signal_store = preference_store or PreferenceSignalStore(
            self.config.record_dir / ".state" / "preference-signals.json"
        )
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
                source="library",
                explicit=getattr(item, "explicit", item.weight != 1.0),
                fingerprint=(
                    getattr(item, "fingerprint", "")
                    or annotation_marker(Path(item.path).name)
                    or "unmarked"
                ),
                changed_at=(
                    getattr(item, "changed_at", None)
                    or datetime.fromtimestamp(
                        Path(item.path).stat().st_ctime,
                        tz=timezone.utc,
                    )
                ),
            )
            for item in library_items
            if item.arxiv_id in metadata_cache.papers
        ]

        reports_by_id: dict[str, InterestPaper] = {}
        libraries_by_id: dict[str, InterestPaper] = {}
        for item in report_items:
            reports_by_id.setdefault(item.paper.arxiv_id, item)
        for item in library_history:
            libraries_by_id.setdefault(item.paper.arxiv_id, item)

        combined: dict[str, InterestPaper] = {}
        for arxiv_id in reports_by_id.keys() | libraries_by_id.keys():
            report_item = reports_by_id.get(arxiv_id)
            library_item = libraries_by_id.get(arxiv_id)
            report_evidence = (
                PreferenceEvidence(
                    source="report",
                    weight=report_item.weight,
                    explicit=True,
                    fingerprint=report_item.fingerprint,
                    fallback_changed_at=(
                        report_item.changed_at
                        or datetime.combine(
                            report_item.observed_date or run_date,
                            datetime.min.time(),
                            tzinfo=timezone.utc,
                        )
                    ),
                )
                if report_item
                else None
            )
            library_evidence = (
                PreferenceEvidence(
                    source="library",
                    weight=library_item.weight,
                    explicit=library_item.explicit,
                    fingerprint=library_item.fingerprint,
                    fallback_changed_at=(
                        library_item.changed_at
                        or datetime.combine(
                            library_item.observed_date or run_date,
                            datetime.min.time(),
                            tzinfo=timezone.utc,
                        )
                    ),
                )
                if library_item
                else None
            )
            resolved = signal_store.resolve(
                arxiv_id,
                report=report_evidence,
                library=library_evidence,
            )
            source_item = report_item if resolved.source == "report" else library_item
            assert source_item is not None
            age = (
                max(0, (run_date - resolved.changed_at.date()).days)
            )
            decayed = resolved.weight * math.exp(
                -age / self.config.recency_decay_days
            )
            combined[arxiv_id] = InterestPaper(
                paper=source_item.paper,
                weight=decayed,
                observed_date=resolved.changed_at.date(),
                source=resolved.source,
                explicit=source_item.explicit,
                fingerprint=source_item.fingerprint,
                changed_at=resolved.changed_at,
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

    def _query_start(self, run_date: date, query_end: date) -> date:
        prior_report_dates = []
        for path in self.config.record_dir.glob("????-??-??.md"):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                report_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if report_date < run_date:
                prior_report_dates.append(report_date)
        missed_weekdays = 0
        if prior_report_dates:
            candidate = max(prior_report_dates) + timedelta(days=1)
            while candidate < run_date:
                if candidate.weekday() < 5:
                    missed_weekdays += 1
                candidate += timedelta(days=1)
        backfill_days = min(
            MAX_ADAPTIVE_BACKFILL_DAYS,
            self.config.backfill_days + missed_weekdays,
        )
        return query_end - timedelta(days=backfill_days)

    def run(
        self,
        run_date: date,
        *,
        dry_run: bool = False,
        force: bool = False,
    ) -> RunResult:
        if run_date.weekday() >= 5:
            return RunResult(status="skipped-weekend")
        cycle = digest_cycle_for_date(run_date)

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
        preference_store = PreferenceSignalStore(
            state_dir / "preference-signals.json"
        )
        query_start = self._query_start(run_date, cycle.submission_end)
        try:
            fetched = self.source.fetch_candidates(query_start, cycle.submission_end)
        except Exception as exc:
            raise PipelineError(f"Verified arXiv retrieval failed: {exc}") from exc
        if not fetched:
            return RunResult(status="skipped-no-announcement")
        latest_submission = max(paper.published.date() for paper in fetched)
        submission_age = (cycle.submission_end - latest_submission).days
        if (
            submission_age < 0
            or submission_age > self.config.announcement_max_age_days
            or (
                state.last_digest_date is not None
                and run_date <= state.last_digest_date
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
            interests = self._load_interests(
                run_date,
                metadata_cache,
                preference_store,
            )
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
            digest_date=run_date,
            announcement_at=cycle.announcement_at,
            query_start=query_start,
            query_end=cycle.submission_end,
            fetched_at=fetched_at,
            timezone_name=self.config.viewer_timezone,
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
        preference_path = state_dir / "preference-signals.json"
        html_path = report_path.with_suffix(".html")
        transaction_paths = [
            state_path,
            metadata_path,
            embedding_path,
            preference_path,
            report_path,
            html_path,
        ]
        snapshots = {
            path: path.read_bytes() if path.exists() else None
            for path in transaction_paths
        }
        state.seen_ids.update(item.paper.arxiv_id for item in selected)
        state.last_digest_date = run_date
        try:
            state.save(state_path)
            metadata_cache.save()
            embedding_cache.save()
            preference_store.save()
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
