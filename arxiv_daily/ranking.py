from __future__ import annotations

import math
import os
import warnings
from datetime import date
from typing import Iterable

from .models import InterestCluster, InterestPaper, Paper, RankedPaper


def _dot(left: Iterable[float], right: Iterable[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _normalize(vector: Iterable[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in vector)
    norm = math.sqrt(_dot(values, values))
    if norm == 0:
        return values
    return tuple(value / norm for value in values)


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    return _dot(_normalize(left), _normalize(right))


def _weighted_centroid(
    vectors: list[list[float]],
    weights: list[float],
) -> tuple[float, ...]:
    total = sum(weights)
    centroid = [
        sum(vector[index] * weight for vector, weight in zip(vectors, weights))
        / total
        for index in range(len(vectors[0]))
    ]
    return _normalize(centroid)


def build_interest_clusters(
    interests: list[InterestPaper],
    embeddings: dict[str, list[float]],
    max_clusters: int = 8,
) -> list[InterestCluster]:
    usable = [
        item
        for item in interests
        if item.weight > 0 and item.paper.arxiv_id in embeddings
    ]
    if not usable:
        return []
    vectors = [embeddings[item.paper.arxiv_id] for item in usable]
    weights = [item.weight for item in usable]
    if len(usable) < 4:
        return [
            InterestCluster(
                centroid=_weighted_centroid(vectors, weights),
                weight=1.0,
                member_ids=tuple(item.paper.arxiv_id for item in usable),
            )
        ]

    try:
        os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score
    except ImportError as exc:
        raise RuntimeError(
            "Adaptive clustering requires the pinned numpy and scikit-learn dependencies"
        ) from exc

    matrix = np.asarray([_normalize(vector) for vector in vectors], dtype=float)
    sample_weights = np.asarray(weights, dtype=float)
    best_labels = None
    best_score = float("-inf")
    upper = min(max_clusters, len(usable) - 1)
    for cluster_count in range(2, upper + 1):
        model = KMeans(n_clusters=cluster_count, n_init=10, random_state=42)
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=UserWarning,
                module=r"joblib\.externals\.loky\.backend\.context",
            )
            labels = model.fit_predict(matrix, sample_weight=sample_weights)
        if len(set(int(label) for label in labels)) < 2:
            continue
        score = float(silhouette_score(matrix, labels, metric="cosine"))
        if score > best_score:
            best_score = score
            best_labels = labels

    if best_labels is None or best_score <= 0:
        return [
            InterestCluster(
                centroid=_weighted_centroid(vectors, weights),
                weight=1.0,
                member_ids=tuple(item.paper.arxiv_id for item in usable),
            )
        ]

    raw_clusters: list[tuple[tuple[float, ...], float, tuple[str, ...]]] = []
    for label in sorted(set(int(value) for value in best_labels)):
        indices = [
            index
            for index, value in enumerate(best_labels)
            if int(value) == label
        ]
        cluster_vectors = [vectors[index] for index in indices]
        cluster_weights = [weights[index] for index in indices]
        raw_clusters.append(
            (
                _weighted_centroid(cluster_vectors, cluster_weights),
                sum(cluster_weights),
                tuple(usable[index].paper.arxiv_id for index in indices),
            )
        )
    total_weight = sum(value[1] for value in raw_clusters)
    return [
        InterestCluster(
            centroid=centroid,
            weight=weight / total_weight,
            member_ids=member_ids,
        )
        for centroid, weight, member_ids in raw_clusters
    ]


def _matched_topics(
    paper: Paper,
    topic_weights: dict[str, float],
    topic_phrases: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    haystack = f"{paper.title} {paper.abstract}".lower()
    matched = [
        name
        for name in topic_weights
        if any(phrase in haystack for phrase in topic_phrases.get(name, ()))
    ]
    return tuple(
        sorted(matched, key=lambda name: topic_weights[name], reverse=True)
    )


def rank_candidates(
    *,
    candidates: list[Paper],
    candidate_embeddings: dict[str, list[float]],
    clusters: list[InterestCluster],
    negative_embeddings: list[list[float]],
    historical_embeddings: list[list[float]],
    topic_weights: dict[str, float],
    topic_phrases: dict[str, tuple[str, ...]],
    category_weights: dict[str, float],
    run_date: date,
) -> list[RankedPaper]:
    maximum_topic = max(topic_weights.values(), default=1.0)
    maximum_category = max(category_weights.values(), default=1.0)
    ranked: list[RankedPaper] = []
    for paper in candidates:
        embedding = candidate_embeddings[paper.arxiv_id]
        if clusters:
            semantic = max(
                ((cosine_similarity(embedding, cluster.centroid) + 1.0) / 2.0)
                * (0.85 + 0.15 * cluster.weight)
                for cluster in clusters
            )
        else:
            semantic = 0.5
        matched = _matched_topics(paper, topic_weights, topic_phrases)
        lexical = (
            max(topic_weights[name] for name in matched) / maximum_topic
            if matched
            else 0.0
        )
        category = max(
            (category_weights.get(value, 0.0) for value in paper.categories),
            default=0.0,
        ) / maximum_category
        priority = max(category, lexical)
        age_days = max(0, (run_date - paper.published.date()).days)
        recency = max(0.0, 1.0 - age_days / 7.0)
        negative_penalty = 0.0
        if negative_embeddings:
            negative_penalty = 0.15 * max(
                0.0,
                max(
                    (cosine_similarity(embedding, value) + 1.0) / 2.0
                    for value in negative_embeddings
                ),
            )
        duplicate_penalty = 0.0
        if historical_embeddings:
            nearest = max(
                cosine_similarity(embedding, value)
                for value in historical_embeddings
            )
            if nearest >= 0.97:
                duplicate_penalty = min(0.25, (nearest - 0.97) * 8.0 + 0.08)
        score = max(
            0.0,
            min(
                1.0,
                0.55 * semantic
                + 0.25 * priority
                + 0.10 * lexical
                + 0.10 * recency
                - negative_penalty
                - duplicate_penalty,
            ),
        )
        tier = "Core" if score >= 0.75 else "Adjacent" if score >= 0.65 else "Exploration"
        strongest = matched[:2] or tuple(
            sorted(
                paper.categories,
                key=lambda value: category_weights.get(value, 0.0),
                reverse=True,
            )[:2]
        )
        why = (
            f"Selected from verified arXiv metadata: semantic {semantic:.2f}, "
            f"priority {priority:.2f}, lexical {lexical:.2f}, recency {recency:.2f}."
        )
        ranked.append(
            RankedPaper(
                paper=paper,
                score=score,
                tier=tier,
                matched_topics=tuple(strongest),
                semantic_score=semantic,
                priority_score=priority,
                lexical_score=lexical,
                recency_score=recency,
                why=why,
            )
        )
    return sorted(ranked, key=lambda item: (-item.score, item.paper.arxiv_id))


def select_diverse(
    ranked: list[RankedPaper],
    embeddings: dict[str, list[float]],
    *,
    limit: int,
    threshold: float,
    mmr_lambda: float,
) -> list[RankedPaper]:
    remaining = [item for item in ranked if item.score >= threshold]
    selected: list[RankedPaper] = []
    tier_limits = {"Core": 12, "Adjacent": 5, "Exploration": 3}
    tier_counts = {key: 0 for key in tier_limits}
    while remaining and len(selected) < limit:
        eligible = [
            item
            for item in remaining
            if tier_counts[item.tier] < tier_limits[item.tier]
        ]
        if not eligible:
            break

        def mmr(item: RankedPaper) -> tuple[float, float, str]:
            if not selected:
                diversity_cost = 0.0
            else:
                diversity_cost = max(
                    cosine_similarity(
                        embeddings[item.paper.arxiv_id],
                        embeddings[chosen.paper.arxiv_id],
                    )
                    for chosen in selected
                )
            value = mmr_lambda * item.score - (1.0 - mmr_lambda) * diversity_cost
            if diversity_cost >= 0.97:
                value -= 0.15
            return value, item.score, item.paper.arxiv_id

        chosen = max(eligible, key=mmr)
        selected.append(chosen)
        tier_counts[chosen.tier] += 1
        remaining.remove(chosen)
    return selected
