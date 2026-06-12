from __future__ import annotations

import unittest
from datetime import datetime, timezone

from arxiv_daily.models import InterestPaper, Paper
from arxiv_daily.ranking import (
    build_interest_clusters,
    rank_candidates,
    select_diverse,
)


def paper(arxiv_id: str, title: str, categories: tuple[str, ...]) -> Paper:
    return Paper(
        arxiv_id=arxiv_id,
        versioned_id=f"{arxiv_id}v1",
        title=title,
        abstract=f"Abstract for {title}",
        authors=("Author",),
        categories=categories,
        primary_category=categories[0],
        published=datetime(2026, 6, 8, tzinfo=timezone.utc),
        updated=datetime(2026, 6, 8, tzinfo=timezone.utc),
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v1",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v1",
    )


class RankingTests(unittest.TestCase):
    def test_adaptive_clusters_choose_separated_interest_groups(self) -> None:
        interests = [
            InterestPaper(paper("1", "neutron star one", ("nucl-th",)), 4.0, None),
            InterestPaper(paper("2", "neutron star two", ("nucl-th",)), 3.0, None),
            InterestPaper(paper("3", "group theory one", ("math.GR",)), 2.0, None),
            InterestPaper(paper("4", "group theory two", ("math.GR",)), 2.0, None),
        ]
        embeddings = {
            "1": [1.0, 0.0],
            "2": [0.98, 0.02],
            "3": [0.0, 1.0],
            "4": [0.02, 0.98],
        }

        clusters = build_interest_clusters(interests, embeddings, max_clusters=8)

        self.assertEqual(len(clusters), 2)
        self.assertAlmostEqual(sum(cluster.weight for cluster in clusters), 1.0)

    def test_explicit_priority_breaks_equal_semantic_scores(self) -> None:
        candidates = [
            paper("10", "Dense nuclear matter", ("nucl-th",)),
            paper("11", "Detector calibration", ("hep-ex",)),
        ]
        clusters = build_interest_clusters(
            [InterestPaper(paper("1", "seed", ("nucl-th",)), 4.0, None)],
            {"1": [1.0, 0.0]},
        )
        ranked = rank_candidates(
            candidates=candidates,
            candidate_embeddings={"10": [1.0, 0.0], "11": [1.0, 0.0]},
            clusters=clusters,
            negative_embeddings=[],
            historical_embeddings=[],
            topic_weights={"nuclear astrophysics": 1.0, "hep-ex": 0.3},
            topic_phrases={
                "nuclear astrophysics": ("dense nuclear matter",),
                "hep-ex": ("detector",),
            },
            category_weights={"nucl-th": 1.0, "hep-ex": 0.3},
            run_date=datetime(2026, 6, 8, tzinfo=timezone.utc).date(),
        )

        self.assertEqual(ranked[0].paper.arxiv_id, "10")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_mmr_prefers_diversity_and_does_not_pad_below_threshold(self) -> None:
        candidates = [
            paper("20", "NS A", ("nucl-th",)),
            paper("21", "NS B", ("nucl-th",)),
            paper("22", "RG", ("hep-ph",)),
        ]
        clusters = build_interest_clusters(
            [InterestPaper(paper("1", "seed", ("nucl-th",)), 4.0, None)],
            {"1": [1.0, 0.0]},
        )
        ranked = rank_candidates(
            candidates=candidates,
            candidate_embeddings={
                "20": [1.0, 0.0],
                "21": [0.999, 0.001],
                "22": [0.7, 0.7],
            },
            clusters=clusters,
            negative_embeddings=[],
            historical_embeddings=[],
            topic_weights={"neutron stars": 1.0, "field theory": 0.9},
            topic_phrases={
                "neutron stars": ("ns",),
                "field theory": ("rg",),
            },
            category_weights={"nucl-th": 1.0, "hep-ph": 0.9},
            run_date=datetime(2026, 6, 8, tzinfo=timezone.utc).date(),
        )
        selected = select_diverse(
            ranked,
            {"20": [1.0, 0.0], "21": [0.999, 0.001], "22": [0.7, 0.7]},
            limit=2,
            threshold=0.60,
            mmr_lambda=0.75,
        )

        self.assertEqual([item.paper.arxiv_id for item in selected], ["20", "22"])
        self.assertTrue(all(item.score >= 0.60 for item in selected))


if __name__ == "__main__":
    unittest.main()
