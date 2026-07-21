"""Quality bar for Scan-opened PRs (docs/plan-quality-helpful-prs.md).

Phases A–F attach here as pure helpers; ``auto_delivery`` / ``delivery`` /
``github_pr`` call them. Product contract: assess detects, Scan generates,
humans merge on GitHub, Argo deploys — a good PR clears a real finding (or
claimed score delta), is cluster-scoped, validated before open, explained
in the body, and never auto-merges or approves skills on PR open.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Founder-tunable cap: keep self-managed / fleet Scan PRs reviewable.
MAX_FILES_PER_CLUSTER_PR = 5

_ARGO_MERGE_NOTE = (
    "Argo deploys after merge; AgentIT does **not** auto-merge. "
    "Humans merge on GitHub — that is the only deploy path (no Direct Apply)."
)


def normalize_category(category: str) -> str:
    return (category or "").lower().replace(" ", "_").replace("-", "_")


def categories_overlap(a: str, b: str) -> bool:
    na, nb = normalize_category(a), normalize_category(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def resolve_target_findings(
    report: object | None,
    target_findings: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Canonical open-finding keys for a Scan delivery.

    Prefer an explicit caller list (onboard_pipeline already passes
    ``sorted(current_finding_keys(report))``). When omitted, derive from the
    assessment so every Scan path still records ``target_findings``.
    """
    if target_findings is not None:
        return [tuple(k) for k in target_findings]
    if report is None:
        return []
    from agentit.assessment_diff import current_finding_keys

    return sorted(current_finding_keys(report))


def finding_gate_allows_pr(
    target_findings: list[tuple[str, str]],
    *,
    score_delta_claimed: float | None = None,
    min_score_delta: float = 5.0,
) -> bool:
    """Phase A: open a PR only when tied to open findings or a material score claim."""
    if target_findings:
        return True
    if score_delta_claimed is not None and abs(score_delta_claimed) >= min_score_delta:
        return True
    return False


def finding_gate_refuse_reason(target_findings: list[tuple[str, str]]) -> str:
    if target_findings:
        return ""
    return (
        "No open findings / score delta — refusing to open a PR "
        "(catalog dumps are not helpful; see docs/plan-quality-helpful-prs.md Phase A)."
    )


def _file_signals(file: dict) -> list[str]:
    signals: list[str] = []
    addressed = file.get("finding_addressed")
    if isinstance(addressed, str) and addressed.strip():
        signals.append(addressed)
    elif isinstance(addressed, (list, tuple)) and addressed:
        signals.append(str(addressed[0]))
    for key in ("skill_name", "category", "domain"):
        val = file.get(key)
        if isinstance(val, str) and val.strip():
            signals.append(val)
    path = str(file.get("path") or file.get("target_path") or "")
    if path:
        signals.append(path)
        for part in re.split(r"[/_.-]+", path):
            if len(part) >= 3:
                signals.append(part)
    desc = file.get("description")
    if isinstance(desc, str) and desc.strip():
        signals.append(desc)
    return signals


def file_matches_finding_categories(file: dict, finding_categories: set[str]) -> str | None:
    """Return the matched finding category (normalized key from the set), or None."""
    if not finding_categories:
        return None
    for signal in _file_signals(file):
        for cat in finding_categories:
            if categories_overlap(signal, cat):
                return cat
    # Skill / FIX_REGISTRY bridge: skill "network-policy" ↔ finding "network".
    try:
        from agentit.remediation.registry import FIX_REGISTRY, lookup

        skill = normalize_category(str(file.get("skill_name") or ""))
        for finding_cat in finding_categories:
            mapped = lookup(finding_cat)
            if mapped and (
                categories_overlap(mapped[1], skill)
                or categories_overlap(mapped[1], str(file.get("path") or ""))
                or categories_overlap(mapped[0], str(file.get("category") or ""))
            ):
                return finding_cat
        if skill:
            for key, (domain, skill_name) in FIX_REGISTRY.items():
                if categories_overlap(skill, skill_name) or categories_overlap(skill, key):
                    for finding_cat in finding_categories:
                        if categories_overlap(finding_cat, key) or categories_overlap(finding_cat, domain):
                            return finding_cat
    except Exception:
        pass
    return None


def filter_files_to_open_findings(
    files: list[dict],
    target_findings: list[tuple[str, str]],
) -> tuple[list[dict], list[str]]:
    """Phase A: drop generated files whose category/skill is not in the open finding set.

    Returns ``(kept, drop_reasons)``.
    """
    finding_cats = {normalize_category(c) for c, _ in target_findings if c}
    display_cats: dict[str, str] = {}
    for c, _ in target_findings:
        nc = normalize_category(c)
        display_cats.setdefault(nc, c)

    kept: list[dict] = []
    drop_reasons: list[str] = []
    for f in files:
        matched = file_matches_finding_categories(f, finding_cats)
        if matched is None:
            path = f.get("path") or f.get("target_path") or "?"
            drop_reasons.append(
                f"{path}: not tied to an open finding "
                f"({', '.join(sorted(display_cats.values())) or 'none'})"
            )
            continue
        enriched = {**f, "_finding_cluster": display_cats.get(matched, matched)}
        kept.append(enriched)
    return kept, drop_reasons


@dataclass(frozen=True)
class FindingCluster:
    """One finding-category cluster ready for a single Scan PR."""

    key: str
    files: list[dict]
    target_findings: list[tuple[str, str]] = field(default_factory=list)
    branch_suffix: str = ""


def partition_by_finding_cluster(
    files: list[dict],
    target_findings: list[tuple[str, str]],
    *,
    max_files: int = MAX_FILES_PER_CLUSTER_PR,
) -> list[FindingCluster]:
    """Phase B: one PR per finding cluster (prefer separate PRs over grab-bags)."""
    by_key: dict[str, list[dict]] = {}
    for f in files:
        key = str(f.get("_finding_cluster") or f.get("category") or "misc")
        by_key.setdefault(key, []).append(f)

    findings_by_cat: dict[str, list[tuple[str, str]]] = {}
    for cat, desc in target_findings:
        findings_by_cat.setdefault(normalize_category(cat), []).append((cat, desc))

    clusters: list[FindingCluster] = []
    for key, cluster_files in sorted(by_key.items(), key=lambda kv: kv[0]):
        cluster_findings = findings_by_cat.get(normalize_category(key), [])
        if not cluster_findings:
            cluster_findings = [
                (c, d) for c, d in target_findings if categories_overlap(c, key)
            ]
        if not cluster_findings:
            cluster_findings = list(target_findings)

        safe = re.sub(r"[^a-z0-9-]+", "-", key.lower()).strip("-") or "cluster"
        for i in range(0, len(cluster_files), max(1, max_files)):
            chunk = cluster_files[i : i + max_files]
            suffix = safe if i == 0 else f"{safe}-{i // max_files + 1}"
            clusters.append(
                FindingCluster(
                    key=key if i == 0 else f"{key}#{i // max_files + 1}",
                    files=chunk,
                    target_findings=cluster_findings,
                    branch_suffix=suffix,
                )
            )
    return clusters


def cluster_validation_ok(
    *,
    dry_run_errors: list[str],
    failed_properties: list[str],
) -> tuple[bool, str]:
    """Phase C: per-cluster bar — SSA/dry-run errors or targeted property fails block the PR."""
    if dry_run_errors:
        return False, "SSA/dry-run failed: " + "; ".join(dry_run_errors[:5])
    if failed_properties:
        return False, "Property check failed for targeted findings: " + ", ".join(failed_properties)
    return True, ""


def build_helpful_pr_body(
    *,
    title_line: str,
    target_findings: list[tuple[str, str]],
    files: list[dict],
    validation_summary: str = "SSA dry-run / property checks / delivery gate passed for this cluster",
    expected_effect: str = "",
    drop_reasons: list[str] | None = None,
    score_dimensions: list[str] | None = None,
) -> str:
    """Phase D: finding → change → expected outcome (reviewable from GitHub alone)."""
    finding_lines = []
    for cat, desc in target_findings:
        finding_lines.append(f"- `{cat}` — {desc}")
    if not finding_lines:
        finding_lines.append("- _(score-delta claim; no discrete finding keys)_")

    if not expected_effect:
        dims = ", ".join(score_dimensions or []) or "targeted dimension(s)"
        expected_effect = (
            f"Clear the finding(s) above on next re-Assess / raise score in {dims}."
        )

    file_lines = []
    for f in files:
        path = f.get("target_path") or f.get("path") or "?"
        why = f.get("description") or f.get("finding_addressed") or "addresses open finding"
        file_lines.append(f"- `{path}` — {why}")

    parts = [
        f"## {title_line}",
        "",
        "### Targeted findings",
        *finding_lines,
        "",
        "### Expected effect",
        expected_effect,
        "",
        "### Validation",
        validation_summary,
        "",
        "### Files",
        *(file_lines or ["- _(none)_"]),
        "",
    ]
    if drop_reasons:
        parts.extend([
            "### Not included (filtered)",
            *[f"- {r}" for r in drop_reasons[:20]],
            "",
        ])
    parts.extend([
        "### Deploy path",
        _ARGO_MERGE_NOTE,
        "",
        "> Generated by [AgentIT](https://github.com/alimobrem/AgentIT) Scan — "
        "skills are not marked approved until merge + evidence the finding cleared.",
    ])
    return "\n".join(parts)


def branch_name_for_cluster(app_name: str, branch_suffix: str, *, source_patch: bool = False) -> str:
    """Distinct branch per cluster so Phase B can open parallel PRs safely."""
    safe_app = re.sub(r"[^a-z0-9-]+", "-", (app_name or "app").lower()).strip("-")
    safe_suf = re.sub(r"[^a-z0-9-]+", "-", (branch_suffix or "cluster").lower()).strip("-")
    return f"agentit/{safe_app}-{safe_suf}"
