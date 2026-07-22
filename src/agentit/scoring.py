"""Shared scoring bands, letter grades, score model v2, and fix impact.

Score model v2: per-dimension weighted pass ratio over applicable controls
(data-driven checks + analyzer findings as failed controls), then a
criticality-weighted overall. Legacy v1 (subtractive penalties + equal
mean) remains for historical reports with ``score_version != 2``.
"""
from __future__ import annotations

from typing import Any

from agentit.analyzers.base import DEFAULT_PENALTIES, calculate_score
from agentit.interfaces.score_aggregate import (
    DIMENSION_WEIGHTS,
    dimension_weights_for,
    weighted_overall_score,
)
from agentit.models import AssessmentReport, DimensionScore, Finding, Severity

# Single source of truth for "what's a good score" (templates + CSS classes).
SCORE_GOOD = 70
SCORE_OK = 40
SCORE_VERSION_V2 = 2

# Baseline applicable controls when a dimension has findings but no check rows
# (pure analyzer path) — keeps pass-ratio meaningful vs. floor-at-zero.
_ANALYZER_BASELINE_CONTROLS = 8


def score_band(score: float | int) -> str:
    """Return ``good``, ``ok``, or ``poor`` for threshold styling."""
    if score > SCORE_GOOD:
        return "good"
    if score >= SCORE_OK:
        return "ok"
    return "poor"


def score_text_class(score: float | int) -> str:
    return {
        "good": "text-success",
        "ok": "text-warning",
        "poor": "text-danger",
    }[score_band(score)]


def score_bar_class(score: float | int) -> str:
    return {
        "good": "score-green",
        "ok": "score-yellow",
        "poor": "score-red",
    }[score_band(score)]


def score_row_border_class(score: float | int) -> str:
    return {
        "good": "row-border-green",
        "ok": "row-border-yellow",
        "poor": "row-border-red",
    }[score_band(score)]


def letter_grade(score: float | int) -> str:
    """Published letter band for the hero."""
    s = float(score)
    if s >= 90:
        return "A"
    if s >= 80:
        return "B"
    if s >= SCORE_GOOD:
        return "C"
    if s >= SCORE_OK:
        return "D"
    return "F"


def calculate_dimension_score_v2(
    findings: list[Finding],
    check_rows: list[dict[str, Any]] | None = None,
) -> int:
    """Pass-ratio score: ``100 * passed / applicable`` over controls.

    Applicable controls = data-driven check rows for the dimension when
    present; otherwise a baseline of ``_ANALYZER_BASELINE_CONTROLS`` with
    each finding counting as one failed control (severity still used for
    impact estimates, not for the pass ratio floor).
    """
    rows = list(check_rows or [])
    if rows:
        applicable = len(rows)
        passed = sum(1 for r in rows if r.get("passed"))
        # Analyzer-only findings without a matching check still reduce the
        # score: treat each as an extra failed control.
        check_names = {str(r.get("check_name") or "") for r in rows}
        orphan_fails = sum(
            1 for f in findings
            if f.severity != Severity.info and (f.source or f.category) not in check_names
        )
        applicable += orphan_fails
        # passed unchanged; orphan_fails only increase denominator
    else:
        fails = sum(1 for f in findings if f.severity != Severity.info)
        applicable = max(_ANALYZER_BASELINE_CONTROLS, fails)
        passed = max(0, applicable - fails)
    if applicable <= 0:
        return 100
    return max(0, min(100, round(100 * passed / applicable)))



def apply_score_model_v2(
    scores: list[DimensionScore],
    check_statuses: list[dict[str, Any]] | None,
    criticality: str,
) -> list[DimensionScore]:
    """Rescore each dimension with v2 pass-ratio (mutates via new objects)."""
    by_dim: dict[str, list[dict[str, Any]]] = {}
    for row in check_statuses or []:
        by_dim.setdefault(str(row.get("dimension") or ""), []).append(row)

    out: list[DimensionScore] = []
    for sc in scores:
        new_score = calculate_dimension_score_v2(sc.findings, by_dim.get(sc.dimension))
        out.append(DimensionScore(
            dimension=sc.dimension,
            score=new_score,
            max_score=sc.max_score,
            findings=sc.findings,
        ))
    return out


def _overall_from_dimension_scores(dim_scores: list[int]) -> float:
    if not dim_scores:
        return 0.0
    return sum(dim_scores) / len(dim_scores)


def _findings_without(sc_findings: list[Finding], finding: Finding) -> list[Finding]:
    remaining = [f for f in sc_findings if f is not finding]
    if len(remaining) != len(sc_findings):
        return remaining
    remaining = [
        f for f in sc_findings
        if not (
            f.category == finding.category
            and f.severity == finding.severity
            and f.description == finding.description
            and f.source == finding.source
        )
    ]
    if len(remaining) != len(sc_findings):
        return remaining
    removed = False
    out: list[Finding] = []
    for f in sc_findings:
        if (
            not removed
            and f.category == finding.category
            and f.severity == finding.severity
        ):
            removed = True
            continue
        out.append(f)
    return out


def estimate_finding_overall_delta(
    report: AssessmentReport,
    finding: Finding,
    dimension: str,
) -> float:
    """Estimated overall-score gain if ``finding`` alone were cleared."""
    if finding.severity == Severity.info:
        return 0.0

    if report.score_version >= SCORE_VERSION_V2:
        new_dims: list[DimensionScore] = []
        for sc in report.scores:
            if sc.dimension != dimension:
                new_dims.append(sc)
                continue
            remaining = _findings_without(sc.findings, finding)
            new_dims.append(DimensionScore(
                dimension=sc.dimension,
                score=calculate_dimension_score_v2(remaining),
                max_score=sc.max_score,
                findings=remaining,
            ))
        new_overall = weighted_overall_score(new_dims, report.criticality)
        return round(new_overall - report.overall_score, 1)

    penalty = DEFAULT_PENALTIES.get(finding.severity, 0)
    if penalty <= 0:
        return 0.0

    dim_scores: list[int] = []
    for sc in report.scores:
        if sc.dimension != dimension:
            dim_scores.append(sc.score)
            continue
        remaining = _findings_without(sc.findings, finding)
        dim_scores.append(calculate_score(remaining))
    new_overall = _overall_from_dimension_scores(dim_scores)
    return round(new_overall - report.overall_score, 1)


def top_fix_impacts(
    report: AssessmentReport,
    *,
    remediable_categories: set[str] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Rank findings by estimated overall-score impact (desc).

    When ``remediable_categories`` is set, only those categories are
    included (typically auto_pr contracts).
    """
    items: list[dict[str, Any]] = []
    for sc in report.scores:
        for finding in sc.findings:
            if finding.severity == Severity.info:
                continue
            if remediable_categories is not None and finding.category not in remediable_categories:
                continue
            delta = estimate_finding_overall_delta(report, finding, sc.dimension)
            if delta <= 0:
                continue
            items.append({
                "category": finding.category,
                "dimension": sc.dimension,
                "severity": finding.severity.name,
                "description": finding.description,
                "estimated_delta": delta,
                "dimension_label": sc.dimension.replace("_", " ").title(),
            })
    items.sort(key=lambda x: (-x["estimated_delta"], x["severity"], x["category"]))
    return items[:limit]
