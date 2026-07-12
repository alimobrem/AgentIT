"""Compare two assessment reports and produce a structured diff."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from agentit.models import AssessmentReport, DimensionScore

logger = logging.getLogger(__name__)


@dataclass
class FindingDelta:
    category: str
    description: str
    severity: str
    status: str  # "new", "resolved", "unchanged"


@dataclass
class DimensionDelta:
    dimension: str
    old_score: float
    new_score: float
    delta: float
    new_findings: list[FindingDelta] = field(default_factory=list)
    resolved_findings: list[FindingDelta] = field(default_factory=list)

    @property
    def improved(self) -> bool:
        return self.delta > 0

    @property
    def degraded(self) -> bool:
        return self.delta < 0


@dataclass
class AssessmentDiff:
    repo_name: str
    old_overall: float
    new_overall: float
    overall_delta: float
    dimension_deltas: list[DimensionDelta] = field(default_factory=list)
    new_findings: list[FindingDelta] = field(default_factory=list)
    resolved_findings: list[FindingDelta] = field(default_factory=list)
    auto_fixable: list[FindingDelta] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.overall_delta < -5

    @property
    def improved(self) -> bool:
        return self.overall_delta > 5

    def summary(self) -> str:
        direction = "improved" if self.improved else "degraded" if self.degraded else "stable"
        parts = [f"{self.repo_name}: {self.old_overall:.0f} -> {self.new_overall:.0f} ({direction})"]
        if self.new_findings:
            parts.append(f"  {len(self.new_findings)} new finding(s)")
        if self.resolved_findings:
            parts.append(f"  {len(self.resolved_findings)} resolved finding(s)")
        if self.auto_fixable:
            parts.append(f"  {len(self.auto_fixable)} auto-fixable")
        return "\n".join(parts)


def diff_assessments(old: AssessmentReport, new: AssessmentReport) -> AssessmentDiff:
    """Compare two assessment reports for the same repo."""
    old_scores = {s.dimension: s for s in old.scores}
    new_scores = {s.dimension: s for s in new.scores}

    all_dims = sorted(set(list(old_scores.keys()) + list(new_scores.keys())))

    dimension_deltas = []
    all_new_findings: list[FindingDelta] = []
    all_resolved_findings: list[FindingDelta] = []

    for dim in all_dims:
        old_dim = old_scores.get(dim)
        new_dim = new_scores.get(dim)
        old_score = old_dim.score if old_dim else 0
        new_score = new_dim.score if new_dim else 0

        old_finding_keys: set[tuple[str, str]] = set()
        new_finding_keys: set[tuple[str, str]] = set()

        if old_dim:
            for f in old_dim.findings:
                old_finding_keys.add((f.category, f.description.lower()[:80]))
        if new_dim:
            for f in new_dim.findings:
                new_finding_keys.add((f.category, f.description.lower()[:80]))

        new_in_dim: list[FindingDelta] = []
        resolved_in_dim: list[FindingDelta] = []

        if new_dim:
            for f in new_dim.findings:
                key = (f.category, f.description.lower()[:80])
                if key not in old_finding_keys:
                    fd = FindingDelta(
                        category=f.category,
                        description=f.description,
                        severity=f.severity.name if hasattr(f.severity, "name") else str(f.severity),
                        status="new",
                    )
                    new_in_dim.append(fd)
                    all_new_findings.append(fd)

        if old_dim:
            for f in old_dim.findings:
                key = (f.category, f.description.lower()[:80])
                if key not in new_finding_keys:
                    fd = FindingDelta(
                        category=f.category,
                        description=f.description,
                        severity=f.severity.name if hasattr(f.severity, "name") else str(f.severity),
                        status="resolved",
                    )
                    resolved_in_dim.append(fd)
                    all_resolved_findings.append(fd)

        dimension_deltas.append(DimensionDelta(
            dimension=dim,
            old_score=old_score,
            new_score=new_score,
            delta=new_score - old_score,
            new_findings=new_in_dim,
            resolved_findings=resolved_in_dim,
        ))

    # Determine auto-fixable findings (categories in the remediation dispatcher)
    from agentit.remediation.registry import FIX_REGISTRY
    auto_fixable = [f for f in all_new_findings if any(k in f.category.lower() for k in FIX_REGISTRY)]

    return AssessmentDiff(
        repo_name=new.repo_name,
        old_overall=old.overall_score,
        new_overall=new.overall_score,
        overall_delta=new.overall_score - old.overall_score,
        dimension_deltas=dimension_deltas,
        new_findings=all_new_findings,
        resolved_findings=all_resolved_findings,
        auto_fixable=auto_fixable,
    )
