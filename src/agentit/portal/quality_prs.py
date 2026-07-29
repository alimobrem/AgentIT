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
    """Phase A: open a PR only when tied to remediable findings or a material score claim.

    detect_only / no_auto_pr / uncontracted categories do not count — Scan
    must not open companion PRs for findings that have no clearing skill.
    """
    try:
        from agentit.remediation.registry import remediable_findings

        remediable = remediable_findings(target_findings)
    except Exception:
        remediable = list(target_findings)
    if remediable:
        return True
    if score_delta_claimed is not None and abs(score_delta_claimed) >= min_score_delta:
        return True
    return False


def finding_gate_refuse_reason(target_findings: list[tuple[str, str]]) -> str:
    try:
        from agentit.remediation.registry import allows_auto_pr, remediable_findings

        if remediable_findings(target_findings):
            return ""
        if target_findings:
            blocked = sorted({c for c, _ in target_findings if c and not allows_auto_pr(c)})
            return (
                "Open findings are detect_only / no_auto_pr / uncontracted — "
                f"refusing PR ({', '.join(blocked) or 'none'}). "
                "Add a remediating SOLUTION_CONTRACT or leave as detect-only."
            )
    except Exception:
        if target_findings:
            return ""
    return (
        "No open findings / score delta — refusing to open a PR "
        "(catalog dumps are not helpful; see docs/plan-quality-helpful-prs.md Phase A)."
    )


def clear_evidence_simulation_ok(
    files: list[dict],
    target_findings: list[tuple[str, str]],
    *,
    live_workloads: list[dict[str, str]] | None = None,
    live_label_sets: list[dict[str, str]] | None = None,
    tree_paths: list[str] | None = None,
    self_managed: bool | None = None,
) -> tuple[bool, str]:
    """Pre-open gate: staged files must simulate clearing each remediable finding."""
    from agentit.remediation.clear_evidence import simulation_gate

    ok, reason, _results = simulation_gate(
        files, target_findings,
        live_workloads=live_workloads,
        live_label_sets=live_label_sets,
        tree_paths=tree_paths,
        self_managed=self_managed,
    )
    return ok, reason


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
    """Return the matched finding category (normalized key from the set), or None.

    Solution-contract findings (FIX_REGISTRY / SOLUTION_CONTRACTS) only accept
    the registered clearing skill — fuzzy path/description overlap must not
    attach Kyverno / LimitRange / apiserver audit-policy to source-only
    findings (pinky gitops #22/#23).
    """
    if not finding_categories:
        return None

    skill = normalize_category(str(file.get("skill_name") or ""))
    contracted: list[str] = []
    uncontracted: list[str] = []
    try:
        from agentit.remediation.registry import contract_for

        for cat in finding_categories:
            if contract_for(cat) is not None:
                contracted.append(cat)
            else:
                uncontracted.append(cat)

        # Exact skill_name == contract.skill_name wins.
        for cat in contracted:
            c = contract_for(cat)
            if c and skill == normalize_category(c.skill_name):
                return cat

        # Explicit refuse list (wrong-layer companions).
        if skill:
            for cat in contracted:
                c = contract_for(cat)
                if c and skill in {normalize_category(s) for s in c.refuse_companions}:
                    return None

        # Contracted findings: only the registered skill — no fuzzy attach.
        if contracted and not uncontracted:
            # Allow registry bridge when skill_name missing but path/signals
            # uniquely identify the contract skill (e.g. tests without metadata).
            if not skill:
                for cat in contracted:
                    c = contract_for(cat)
                    if c is None:
                        continue
                    needle = normalize_category(c.skill_name)
                    for signal in _file_signals(file):
                        if categories_overlap(signal, needle) or categories_overlap(
                            signal, cat,
                        ):
                            # Reject if signal also matches a refuse companion.
                            refuse = {normalize_category(s) for s in c.refuse_companions}
                            if any(categories_overlap(signal, r) for r in refuse):
                                continue
                            # Prefer exact skill token in path/description.
                            if needle in normalize_category(signal) or categories_overlap(
                                signal, needle,
                            ):
                                return cat
            return None
    except Exception:
        contracted, uncontracted = [], list(finding_categories)

    # Legacy fuzzy match only for findings without a solution contract.
    match_cats = set(uncontracted) if contracted else finding_categories
    if not match_cats:
        return None
    for signal in _file_signals(file):
        for cat in match_cats:
            if categories_overlap(signal, cat):
                return cat
    try:
        from agentit.remediation.registry import FIX_REGISTRY, lookup

        for finding_cat in match_cats:
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
                    for finding_cat in match_cats:
                        if categories_overlap(finding_cat, key) or categories_overlap(
                            finding_cat, domain,
                        ):
                            return finding_cat
    except Exception:
        pass
    return None


# Categories a companion file is never eligible to ride along as, even when
# it's otherwise safe -- these are files this pipeline has already decided,
# by explicit design, are never a real deliverable (see delivery.py's
# ``preview_delivery_groups()``, which drops both from every mechanism):
# CATEGORY_NARRATIVE_REPORT is documentation-about-changes, not a change;
# CATEGORY_SECRET_BLOCKED is a permanent deny-rule, not a routing gap.
_COMPANION_INELIGIBLE_CATEGORIES = frozenset({"narrative_report", "secret_blocked"})


def _is_explicitly_refused_companion(file: dict, finding_cats: set[str]) -> bool:
    """True when this file's skill is on ANY of ``finding_cats``'
    SOLUTION_CONTRACT ``refuse_companions`` denylist.

    ``refuse_companions`` is a deliberate, per-contract safety rule (e.g.
    ``image_signing``'s contract explicitly refuses ``image-scan-task`` --
    a real scanning skill, not a wrong-layer stub, but a DIFFERENT
    security concern that must never be bundled into a signing-only PR
    as if it were incidental). ``file_matches_finding_categories()``
    already enforces this for Tier 1 matching, but collapses "explicitly
    refused" and "just doesn't match anything" into the same ``None`` --
    Tier 2 rescue must NOT treat an explicit refusal as "safe, just
    unrelated," or it would quietly reopen exactly the wrong-companion
    class of bug the denylist exists to close.
    """
    skill = normalize_category(str(file.get("skill_name") or ""))
    if not skill:
        return False
    try:
        from agentit.remediation.registry import contract_for
    except Exception:
        return False
    for cat in finding_cats:
        c = contract_for(cat)
        if c and skill in {normalize_category(s) for s in c.refuse_companions}:
            return True
    return False


def _companion_layer(file: dict, finding_cats: set[str]) -> str | None:
    """The delivery-layer category (``classify_file()``) a Tier 2
    "Also included" companion would need to share with an existing Tier 1
    cluster to ride along safely -- or ``None`` if this file is never
    companion-eligible (see ``_COMPANION_INELIGIBLE_CATEGORIES`` and
    ``_is_explicitly_refused_companion()``).

    Deliberately does NOT re-run any other content-safety check here
    (secrets, destructive rewrites, placeholders) -- those already ran
    upstream before a file reaches this filter, and every downstream
    per-cluster gate (SSA dry-run, resource collisions, placeholder
    detection) still applies uniformly to a companion once it's merged
    into a cluster's file list, exactly as it does for a Tier 1 fix file.
    """
    if _is_explicitly_refused_companion(file, finding_cats):
        return None
    from agentit.portal.delivery import classify_file

    category = classify_file(file)
    if category in _COMPANION_INELIGIBLE_CATEGORIES:
        return None
    return category


def filter_files_to_open_findings(
    files: list[dict],
    target_findings: list[tuple[str, str]],
) -> tuple[list[dict], list[str]]:
    """Phase A + coverage: split generated files into two tiers instead of a
    binary keep/drop.

    - **Tier 1 "fix"** (unchanged from the original Phase A behavior):
      files whose category/skill exactly matches an open finding. These
      are what makes a PR's "clears finding X" claim trustworthy, and
      still go through the full existing gate stack.
    - **Tier 2 "companion"** (new): files that don't map to any open
      finding, but are otherwise safe to ship alongside a Tier 1 fix --
      marked ``_tier="companion"`` and ``_companion_layer=<category>`` so
      ``partition_by_finding_cluster()`` can attach them to a same-layer
      cluster, and ``build_helpful_pr_body()`` can render them under a
      distinct "Also included" section rather than claiming they clear
      anything. A companion with no eligible layer, or one that can't be
      attached to any Tier 1 cluster later, is dropped exactly like today
      -- this never weakens the "no catalog-dump PRs" guarantee, it only
      stops throwing away real, safe content that happened to ride along
      with a real fix.

    Returns ``(kept, drop_reasons)`` -- ``kept`` may contain both tiers;
    check ``f.get("_tier")`` to distinguish. Confirmed live (2026-07-24)
    that a meaningful fraction of real skill/CodeChangeAgent output
    (extra Dockerfiles, otel_setup.py, …) was silently dropped here on
    every fleet app even though it was perfectly safe to ship.
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
        if matched is not None:
            enriched = {**f, "_finding_cluster": display_cats.get(matched, matched)}
            kept.append(enriched)
            continue

        layer = _companion_layer(f, finding_cats)
        path = f.get("path") or f.get("target_path") or "?"
        if layer is None:
            drop_reasons.append(
                f"{path}: not tied to an open finding "
                f"({', '.join(sorted(display_cats.values())) or 'none'})"
            )
            continue
        kept.append({**f, "_tier": "companion", "_companion_layer": layer})
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
    """Phase B: one PR per finding cluster (prefer separate PRs over grab-bags).

    Tier 1 "fix" files (see ``filter_files_to_open_findings()``) are
    grouped/chunked exactly as before. Tier 2 "companion" files
    (``_tier == "companion"``) have no ``_finding_cluster`` of their own
    -- each gets attached to an existing same-layer Tier 1 cluster
    instead (see ``_attach_companions_to_clusters()``). A companion whose
    layer matches no Tier 1 cluster anywhere in this batch is dropped --
    this never opens a companion-only PR, which would violate Phase A's
    "must clear a finding or claim a real score delta" gate.
    """
    tier1_files = [f for f in files if f.get("_tier") != "companion"]
    companions = [f for f in files if f.get("_tier") == "companion"]

    by_key: dict[str, list[dict]] = {}
    for f in tier1_files:
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

    if companions and clusters:
        _attach_companions_to_clusters(clusters, companions, max_files=max_files)

    return clusters


def _attach_companions_to_clusters(
    clusters: list[FindingCluster],
    companions: list[dict],
    *,
    max_files: int,
) -> None:
    """Mutates ``clusters`` in place: appends each Tier 2 companion to a
    same-layer Tier 1 cluster with room (below ``max_files``), spilling
    into a new same-family chunk if every existing same-layer chunk is
    already full. Never invents a new finding cluster from scratch --
    only ever extends one that already exists, reusing its
    ``target_findings`` (a companion never gets its own).
    """
    from agentit.portal.delivery import classify_file

    # Each cluster is homogeneous by construction (grouped by
    # _finding_cluster, and strip_wrong_layer_companions already keeps a
    # cluster single-layer) -- one classify_file() call per cluster is enough.
    cluster_layer = {
        id(c): classify_file(c.files[0]) for c in clusters if c.files
    }

    by_layer: dict[str, list[dict]] = {}
    for comp in companions:
        by_layer.setdefault(comp.get("_companion_layer") or "", []).append(comp)

    for layer, comps in by_layer.items():
        if not layer:
            continue
        same_layer = [c for c in clusters if cluster_layer.get(id(c)) == layer]
        if not same_layer:
            continue
        overflow_idx = 0
        for comp in comps:
            target = next((c for c in same_layer if len(c.files) < max_files), None)
            if target is not None:
                target.files.append(comp)
                continue
            overflow_idx += 1
            base = same_layer[0]
            overflow = FindingCluster(
                key=f"{base.key}-also-included-{overflow_idx}",
                files=[comp],
                target_findings=base.target_findings,
                branch_suffix=f"{base.branch_suffix}-more-{overflow_idx}",
            )
            clusters.append(overflow)
            same_layer.append(overflow)
            cluster_layer[id(overflow)] = layer


def _iter_resource_docs(files: list[dict]):
    """Yield ``(file_path, parsed_doc)`` for every real K8s resource
    document across a batch of generated files (handles multi-doc
    ``---`` YAML). Non-YAML / unparsable content is skipped, not an error
    here — other gates (SSA dry-run, property checks) already own that."""
    import yaml

    for f in files:
        path = f.get("target_path") or f.get("path") or "?"
        content = f.get("content") or ""
        try:
            docs = list(yaml.safe_load_all(content))
        except yaml.YAMLError:
            continue
        for doc in docs:
            if isinstance(doc, dict) and doc.get("kind") and isinstance(doc.get("metadata"), dict):
                yield path, doc


def find_resource_collisions(
    files: list[dict],
    *,
    existing_files: list[dict] | None = None,
) -> list[str]:
    """Refuse a batch that would create two resources with the same
    (apiVersion, kind, namespace, name) -- the exact class of bug that
    blocked ``managed-pinky``'s real Argo sync after merge (duplicate
    GVK/name across generated files, only ever discovered live). Nothing
    else in this pipeline checks this: SSA dry-run validates each
    file/resource independently, and Argo's own "who owns this" conflict
    only ever surfaces after merge, not before a PR opens.

    ``existing_files`` (optional, same ``{"path"/"target_path", "content"}``
    shape as ``files``): manifests already committed on the target repo's
    default branch -- the caller fetches these (GitHub REST tree_paths +
    read_file, exactly the pattern the source-patch pre-enrich step
    already uses) and passes them in so a *new* file colliding with
    something already merged under ``apps/{app}/**`` (or ``chart/**`` for
    self-managed) is caught too, closing this function's own previously-
    documented follow-up. Omit it (default) to keep the original,
    within-batch-only behavior -- every existing caller/test is
    unaffected. Read-only: never validated or re-checked itself, only
    used as a collision baseline.
    """
    seen: dict[tuple[str, str, str, str], str] = {}
    for path, doc in _iter_resource_docs(existing_files or []):
        kind = str(doc.get("kind") or "")
        meta = doc.get("metadata") or {}
        name = str(meta.get("name") or "")
        if not kind or not name:
            continue
        namespace = str(meta.get("namespace") or "")
        api_version = str(doc.get("apiVersion") or "")
        seen[(api_version, kind, namespace, name)] = f"already committed at {path}"

    collisions: list[str] = []
    for path, doc in _iter_resource_docs(files):
        kind = str(doc.get("kind") or "")
        meta = doc.get("metadata") or {}
        name = str(meta.get("name") or "")
        if not kind or not name:
            continue
        namespace = str(meta.get("namespace") or "")
        api_version = str(doc.get("apiVersion") or "")
        key = (api_version, kind, namespace, name)
        if key in seen:
            where = seen[key] if seen[key] == path else f"{seen[key]} and {path}"
            collisions.append(
                f"duplicate {kind}/{name}"
                + (f" in namespace {namespace}" if namespace else "")
                + f" ({where}) — would collide on Argo sync, not caught by "
                "per-file SSA dry-run alone"
            )
        else:
            seen[key] = path
    return collisions


def cluster_validation_ok(
    *,
    dry_run_errors: list[str],
    failed_properties: list[str],
    collisions: list[str] | None = None,
) -> tuple[bool, str]:
    """Phase C: per-cluster bar — hard SSA/dry-run errors, targeted property
    fails, or a same-(kind,name,namespace) resource collision block the PR.

    Soft dry-run warnings (Forbidden / missing optional CRD) are *not* passed
    in ``dry_run_errors`` — callers keep those in a separate warnings list
    for PR body notes without blocking open.
    """
    if collisions:
        return False, "Resource collision: " + "; ".join(collisions[:5])
    if dry_run_errors:
        return False, "SSA/dry-run failed: " + "; ".join(dry_run_errors[:5])
    if failed_properties:
        return False, "Property check failed for targeted findings: " + ", ".join(failed_properties)
    return True, ""


def strip_wrong_layer_companions(
    files: list[dict],
    target_findings: list[tuple[str, str]],
) -> tuple[list[dict], list[str]]:
    """Drop cluster YAML from source-only finding clusters (and vice versa).

    Pinky gitops #22/#23 opened infra PRs that listed Dockerfile/audit.py
    alongside Kyverno/audit-policy — wrong layer; merging never clears the
    finding. When every targeted finding clears via ``delivery: source``,
    keep only source-patch files; refuse pure-cluster leftovers.
    """
    if not target_findings or not files:
        return list(files), []

    try:
        from agentit.remediation.registry import clears_via_source, contract_for
    except Exception:
        return list(files), []

    cats = [c for c, _ in target_findings if c]
    if not cats or not all(clears_via_source(c) for c in cats):
        return list(files), []

    from agentit.portal.delivery import (
        CATEGORY_CICD_SHARED_NAMESPACE,
        CATEGORY_CLUSTER_CONFIG,
        classify_file,
    )

    kept: list[dict] = []
    drops: list[str] = []
    allowed_skills = {
        (contract_for(c).skill_name if contract_for(c) else "")
        for c in cats
    }
    allowed_skills.discard("")

    for f in files:
        skill = str(f.get("skill_name") or "")
        cat = classify_file(f)
        if cat in (CATEGORY_CLUSTER_CONFIG, CATEGORY_CICD_SHARED_NAMESPACE):
            path = f.get("path") or f.get("target_path") or "?"
            drops.append(
                f"{path}: wrong layer for source-only finding "
                f"({', '.join(cats)}) — clears via app-repo patch, not gitops"
            )
            continue
        # Tier 2 companions (quality_prs.filter_files_to_open_findings) were
        # already vetted for being safe to ride along without clearing any
        # specific finding -- the "must be the exact clearing skill" bar
        # below is a Tier 1 correctness property (this file's presence
        # implies it clears cat), which a companion never claims. Skipping
        # it here would otherwise re-drop every rescued companion the
        # moment every target finding happens to clear via source.
        is_companion = f.get("_tier") == "companion"
        if not is_companion and skill and allowed_skills and skill not in allowed_skills:
            path = f.get("path") or f.get("target_path") or "?"
            drops.append(
                f"{path}: skill '{skill}' is not the clearing skill "
                f"({', '.join(sorted(allowed_skills))})"
            )
            continue
        kept.append(f)
    return kept, drops


def build_helpful_pr_body(
    *,
    title_line: str,
    target_findings: list[tuple[str, str]],
    files: list[dict],
    validation_summary: str = "SSA dry-run / property checks / delivery gate passed for this cluster",
    expected_effect: str = "",
    drop_reasons: list[str] | None = None,
    score_dimensions: list[str] | None = None,
    shared_ns_note: str = "",
    llm_review: dict | None = None,
) -> str:
    """Phase D: finding → change → expected outcome (reviewable from GitHub alone).

    ``files`` may include Tier 2 "companion" entries (``_tier ==
    "companion"``, see ``filter_files_to_open_findings()``) alongside the
    Tier 1 files that actually clear ``target_findings`` -- rendered
    under a separate "Also included" section so the PR never overstates
    what a companion is claimed to do.
    """
    tier1_files = [f for f in files if f.get("_tier") != "companion"]
    companion_files = [f for f in files if f.get("_tier") == "companion"]

    finding_lines = []
    for cat, desc in target_findings:
        finding_lines.append(f"- `{cat}` — {desc}")
    if not finding_lines:
        finding_lines.append("- _(score-delta claim; no discrete finding keys)_")

    if not expected_effect:
        try:
            from agentit.remediation.registry import expected_clear_lines

            clear_lines = expected_clear_lines(target_findings)
        except Exception:
            clear_lines = []
        if clear_lines:
            expected_effect = "\n".join(clear_lines)
            dims = ", ".join(score_dimensions or [])
            if dims:
                expected_effect += f"\n\nScore lift expected in: {dims}."
        else:
            dims = ", ".join(score_dimensions or []) or "targeted dimension(s)"
            expected_effect = (
                f"Clear the finding(s) above on next re-Assess / raise score in {dims}."
            )

    file_lines = []
    for f in tier1_files:
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
        "### Finding-clear proof (post-merge)",
        "After merge + Argo sync, re-Assess this app. AgentIT correlates "
        "`target_findings` on the delivery row — skills stay unapproved until "
        "those keys are gone (`correlate_delivery_finding` → `resolved`). "
        "If they remain, Ledger shows still-present and skills are rejected.",
        "",
        "### Validation",
        validation_summary,
        "",
        "### Files",
        *(file_lines or ["- _(none)_"]),
        "",
    ]
    if companion_files:
        companion_lines = []
        for f in companion_files:
            path = f.get("target_path") or f.get("path") or "?"
            why = f.get("description") or f.get("finding_addressed") or "generated alongside this fix"
            companion_lines.append(f"- `{path}` — {why}")
        parts.extend([
            "### Also included",
            "Generated alongside the fix above but not required to clear any "
            "of the targeted findings -- included for completeness, not "
            "claimed to resolve anything on its own.",
            *companion_lines,
            "",
        ])
    if shared_ns_note:
        parts.extend([
            "### Shared-namespace blast radius",
            shared_ns_note,
            "",
        ])
    if drop_reasons:
        parts.extend([
            "### Not included (filtered)",
            *[f"- {r}" for r in drop_reasons[:20]],
            "",
        ])
    if llm_review is not None and not llm_review.get("approved", True):
        reason = str(llm_review.get("reason") or "").strip()
        concerns = llm_review.get("concerns") or []
        if not isinstance(concerns, list):
            concerns = []
        concern_lines = [f"- {c}" for c in concerns[:10] if str(c).strip()]
        parts.extend([
            "### LLM review concerns",
            (
                "Final LLM review flagged concerns (non-blocking — human gate "
                "remains merge). " + (reason or "See concerns below.")
            ),
            *(concern_lines or []),
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
