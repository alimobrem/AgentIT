"""Criticality-weighted overall score (no dependency on analyzers/portal).

Used by ``models.AssessmentReport`` so models does not import ``scoring``
(which imports models — a cycle). ``scoring`` re-exports the same weights.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class _ScoredDimension(Protocol):
    dimension: str
    score: int


DIMENSION_WEIGHTS: dict[str, dict[str, float]] = {
    "critical": {
        "security": 1.6, "compliance": 1.4, "ha_dr": 1.2, "infrastructure": 1.1,
        "cicd": 1.0, "observability": 1.0, "data_governance": 0.9,
    },
    "high": {
        "security": 1.4, "compliance": 1.2, "ha_dr": 1.15, "infrastructure": 1.05,
        "cicd": 1.0, "observability": 1.0, "data_governance": 0.95,
    },
    "medium": {
        "security": 1.15, "compliance": 1.05, "ha_dr": 1.0, "infrastructure": 1.0,
        "cicd": 1.0, "observability": 1.0, "data_governance": 1.0,
    },
    "low": {
        "security": 1.0, "compliance": 1.0, "ha_dr": 1.0, "infrastructure": 1.0,
        "cicd": 1.0, "observability": 1.0, "data_governance": 1.0,
    },
}


def dimension_weights_for(criticality: str) -> dict[str, float]:
    key = (criticality or "medium").lower()
    return dict(DIMENSION_WEIGHTS.get(key, DIMENSION_WEIGHTS["medium"]))


def weighted_overall_score(
    scores: list[_ScoredDimension],
    criticality: str,
) -> float:
    weights = dimension_weights_for(criticality)
    total_w = 0.0
    acc = 0.0
    for sc in scores:
        w = weights.get(sc.dimension, 1.0)
        acc += sc.score * w
        total_w += w
    if total_w <= 0:
        return 0.0
    return round(acc / total_w, 2)
