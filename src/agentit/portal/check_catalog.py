"""Checks & resolutions catalog — single source for Capabilities / Insights.

Merges analyzer finding categories, ``mode: detect`` skills, and
``SOLUTION_CONTRACTS`` into rows the portal can render without drifting from
Scan behavior.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agentit.remediation.registry import SOLUTION_CONTRACTS, contract_for

# Analyzer category → dimension (matches runner.run_assessment domains).
# Keep in sync with analyzers/* finding.category emissions.
ANALYZER_CATEGORIES: dict[str, str] = {
    "secrets": "security",
    "container": "security",
    "network": "security",
    "scanning": "security",
    "instrumentation": "observability",
    "metrics": "observability",
    "logging": "observability",
    "tracing": "observability",
    "dashboards": "observability",
    "alerting": "observability",
    "pipeline": "cicd",
    "gitops": "cicd",
    "eol": "infrastructure",
    "iac": "infrastructure",
    "manifests": "infrastructure",
    "resources": "infrastructure",
    "quota": "infrastructure",
    "license": "compliance",
    "sbom": "compliance",
    "audit": "compliance",
    "policy": "compliance",
    "backup": "data_governance",
    "migration": "data_governance",
    "retention": "data_governance",
    "availability": "ha_dr",
    "scaling": "ha_dr",
    "health": "ha_dr",
}


@dataclass(frozen=True)
class CatalogRow:
    category: str
    dimension: str
    badge: str  # remediable | detect_only | uncontracted
    detect_skill: str | None
    detect_description: str
    skill_name: str | None
    delivery: str | None
    clear_evidence: str
    refuse_companions: tuple[str, ...]
    fleet_path: str
    self_managed_path: str
    auto_pr: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _detect_by_category(skills: list) -> dict[str, Any]:
    """Map normalized category → first mode:detect skill covering it."""
    out: dict[str, Any] = {}
    for skill in skills:
        if getattr(skill, "mode", None) != "detect":
            continue
        cat = (skill.category or "").lower().replace("-", "_").replace(" ", "_")
        if not cat or cat in out:
            continue
        out[cat] = skill
    return out


def build_check_catalog(skills: list | None = None) -> list[CatalogRow]:
    """Build the full checks & resolutions matrix.

    Categories = analyzer set ∪ SOLUTION_CONTRACTS keys ∪ detect skill categories.
    """
    if skills is None:
        from agentit.skill_engine import load_all_skills

        skills = load_all_skills(Path("skills"))

    detect_map = _detect_by_category(skills)
    categories = sorted(
        set(ANALYZER_CATEGORIES)
        | set(SOLUTION_CONTRACTS)
        | set(detect_map)
    )

    rows: list[CatalogRow] = []
    for cat in categories:
        contract = contract_for(cat)
        detect = detect_map.get(cat)
        dimension = (
            (contract.domain if contract else None)
            or ANALYZER_CATEGORIES.get(cat)
            or (detect.domain if detect else "other")
        )
        if contract is None:
            badge = "uncontracted"
            rows.append(
                CatalogRow(
                    category=cat,
                    dimension=dimension,
                    badge=badge,
                    detect_skill=detect.name if detect else None,
                    detect_description=(detect.description if detect else ""),
                    skill_name=None,
                    delivery=None,
                    clear_evidence="No solution contract — Scan will not open a PR",
                    refuse_companions=(),
                    fleet_path="",
                    self_managed_path="",
                    auto_pr=False,
                )
            )
            continue

        if contract.auto_pr:
            badge = "remediable"
        else:
            badge = "detect_only"

        rows.append(
            CatalogRow(
                category=cat,
                dimension=dimension,
                badge=badge,
                detect_skill=detect.name if detect else None,
                detect_description=(detect.description if detect else ""),
                skill_name=contract.skill_name if contract.auto_pr else None,
                delivery=contract.delivery,
                clear_evidence=contract.clear_evidence,
                refuse_companions=tuple(sorted(contract.refuse_companions)),
                fleet_path=contract.fleet_path,
                self_managed_path=contract.self_managed_path,
                auto_pr=contract.auto_pr,
            )
        )
    return rows


def catalog_summary(rows: list[CatalogRow] | None = None) -> dict[str, int]:
    rows = rows if rows is not None else build_check_catalog()
    return {
        "total": len(rows),
        "remediable": sum(1 for r in rows if r.badge == "remediable"),
        "detect_only": sum(1 for r in rows if r.badge == "detect_only"),
        "uncontracted": sum(1 for r in rows if r.badge == "uncontracted"),
    }


def catalog_by_dimension(
    rows: list[CatalogRow] | None = None,
) -> dict[str, list[CatalogRow]]:
    rows = rows if rows is not None else build_check_catalog()
    out: dict[str, list[CatalogRow]] = {}
    for row in rows:
        out.setdefault(row.dimension, []).append(row)
    return dict(sorted(out.items()))


def badge_for_category(category: str) -> str:
    """remediable | detect_only | uncontracted for a finding category."""
    contract = contract_for(category)
    if contract is None:
        return "uncontracted"
    return "remediable" if contract.auto_pr else "detect_only"


def annotate_compliance_rows(compliance: list[dict]) -> list[dict]:
    """Add contract badge / delivery to Insights check-compliance rows."""
    catalog = build_check_catalog()
    by_detect = {
        (r.detect_skill or "").lower().replace("-", "_"): r
        for r in catalog
        if r.detect_skill
    }
    by_cat = {r.category: r for r in catalog}

    annotated: list[dict] = []
    for row in compliance:
        name = (row.get("check_name") or "").lower().replace("-", "_").replace(" ", "_")
        catalog_row = by_detect.get(name) or by_cat.get(name)
        if catalog_row is None:
            for suffix in ("_exists", "_detected", "_check"):
                if name.endswith(suffix):
                    trial = name[: -len(suffix)]
                    catalog_row = by_cat.get(trial)
                    if catalog_row:
                        break
        if catalog_row is not None:
            badge = catalog_row.badge
            cat = catalog_row.category
            if catalog_row.badge == "remediable":
                resolution = (
                    f"Scan PR via {catalog_row.skill_name} ({catalog_row.delivery})"
                )
            elif catalog_row.badge == "detect_only":
                resolution = "Detect-only — no Scan PR"
            else:
                resolution = "See Capabilities catalog"
        else:
            badge = "uncontracted"
            cat = name
            resolution = "See Capabilities catalog"
        enriched = dict(row)
        enriched["contract_badge"] = badge
        enriched["contract_category"] = cat
        enriched["resolution"] = resolution
        annotated.append(enriched)
    return annotated
