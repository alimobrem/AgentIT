"""Regression guard for the skill-routing unification (see skill_engine.py's
``SkillEngine.skill_for_category()`` docstring).

Before this, ``remediation/registry.py``'s ``FIX_REGISTRY`` (used by
``RemediationDispatcher`` -- the portal/webhook "fix a finding" path) and
``SkillEngine.generate_for_finding()`` (used by the CLI's ``self-fix``
path) each independently matched a finding category to a skill: the
registry via a static dict, the engine via ad hoc keyword-trigger
substring matching. For a real category ("policy"), those two algorithms
picked *different* skills (``kyverno-require-labels`` vs.
``image-registry-policy``) with no test ever catching the drift.

Both call sites now route through the single ``SkillEngine.skill_for_category()``
function. This test asserts, for every category ``FIX_REGISTRY`` knows
about, that the skill it resolves to actually matches a loaded skill by
that name -- so any future change that reintroduces a second, disagreeing
routing path (e.g. someone adding trigger-matching back into one call site
but not the other) fails loudly here instead of silently drifting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentit.remediation.registry import (
    FIX_REGISTRY,
    SOLUTION_CONTRACTS,
    clears_via_source,
    contract_for,
    lookup,
)
from agentit.skill_engine import SkillEngine
from conftest import make_report

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


class TestSolutionContracts:
    def test_fix_registry_derived_from_contracts(self) -> None:
        assert set(FIX_REGISTRY) == set(SOLUTION_CONTRACTS)
        for key, (domain, skill) in FIX_REGISTRY.items():
            c = SOLUTION_CONTRACTS[key]
            assert (c.domain, c.skill_name) == (domain, skill)

    def test_source_findings_clear_via_source(self) -> None:
        for cat in ("container", "dockerfile", "audit", "eol", "migration", "iac", "manifests"):
            assert clears_via_source(cat), cat
            assert contract_for(cat).delivery == "source"

    def test_audit_refuses_apiserver_policy_companion(self) -> None:
        c = contract_for("audit")
        assert c is not None
        assert c.skill_name == "app-audit-logging"
        assert "audit-policy" in c.refuse_companions

    def test_container_refuses_kyverno_companions(self) -> None:
        c = contract_for("container")
        assert c is not None
        assert "image-registry-policy" in c.refuse_companions
        assert "limitrange" in c.refuse_companions

    def test_lookup_agrees_with_contract(self) -> None:
        assert lookup("scaling") == ("infrastructure", "hpa")
        assert contract_for("scaling").skill_name == "hpa"


@pytest.fixture(scope="module")
def engine() -> SkillEngine:
    return SkillEngine(_SKILLS_DIR, platform=None)


class TestRegistryAndSkillEngineAgree:
    @pytest.mark.parametrize("category", sorted(FIX_REGISTRY.keys()))
    def test_skill_for_category_matches_fix_registry(self, engine: SkillEngine, category: str) -> None:
        expected_domain, expected_skill_name = FIX_REGISTRY[category]

        skill = engine.skill_for_category(category)

        if expected_skill_name == "patch_base_image":
            # Not a skill-shaped fix (RemediationDispatcher special-cases it
            # directly from the registry) -- skill_for_category() must not
            # silently fall back to an unrelated skill via trigger matching.
            assert skill is None, (
                f"category '{category}' maps to the non-skill sentinel "
                f"'patch_base_image', but skill_for_category() resolved a "
                f"real skill ({skill.name if skill else None}) instead"
            )
            return

        assert skill is not None, (
            f"FIX_REGISTRY maps category '{category}' to skill "
            f"'{expected_skill_name}', but skill_for_category() found no "
            f"matching loaded skill"
        )
        assert skill.name == expected_skill_name, (
            f"Routing disagreement for category '{category}': FIX_REGISTRY "
            f"says '{expected_skill_name}', but skill_for_category() chose "
            f"'{skill.name}' instead"
        )
        assert skill.domain == expected_domain

    def test_policy_category_resolves_to_kyverno_not_image_registry_policy(
        self, engine: SkillEngine,
    ) -> None:
        """The concrete, named example of the bug: both `kyverno-require-labels`
        and `image-registry-policy` list "policy" as a trigger, and
        `image-registry-policy.md` used to win by alphabetical file-load
        order under the old keyword-matching fallback -- silently
        disagreeing with FIX_REGISTRY's authoritative "policy" -> "kyverno-
        require-labels" mapping."""
        skill = engine.skill_for_category("policy")
        assert skill is not None
        assert skill.name == "kyverno-require-labels"

    def test_availability_category_resolves_to_pdb_not_pod_delete(
        self, engine: SkillEngine,
    ) -> None:
        """The same class of bug as "policy" above, for ha_dr's "No
        PodDisruptionBudget defined" finding (category "availability"):
        both skills/infrastructure/pdb.md (the real remediation) and
        skills/chaos/pod-delete.md (a resiliency-test generator, not a fix)
        declare trigger "availability" -- pod-delete silently won by
        alphabetical file-load order ("skills/chaos/" < "skills/
        infrastructure/") before "availability" had a FIX_REGISTRY row."""
        skill = engine.skill_for_category("availability")
        assert skill is not None
        assert skill.name == "pdb"
        assert skill.domain == "infrastructure"


class TestCategoriesWithNoRealRemediationYetResolveHonestly:
    """`license` (missing LICENSE file) and `backup` (no recurring backup
    config) have no FIX_REGISTRY row and no real remediation skill in the
    catalog -- only their own `mode: detect` detection skills
    (license-file-exists.md, backup-config-exists.md). Before this,
    keyword-trigger fallback matching silently mispaired them with an
    unrelated skill that happened to share a trigger word
    (sbom-task.md's "license" trigger, meant for SBOM/license-compliance
    context; data-archive-job.md's "backup" trigger, meant for
    pre-decommission data export, not recurring backups) -- generating the
    *wrong* content instead of honestly reporting "no fix yet," matching
    eol/migration's pre-#145 status quo."""

    @pytest.fixture(scope="class")
    def engine(self) -> SkillEngine:
        return SkillEngine(_SKILLS_DIR, platform=None)

    def test_license_resolves_to_none_not_sbom_task(self, engine: SkillEngine) -> None:
        assert engine.skill_for_category("license") is None

    def test_backup_resolves_to_none_not_data_archive_job(self, engine: SkillEngine) -> None:
        assert engine.skill_for_category("backup") is None

    def test_sbom_trigger_removal_does_not_break_real_sbom_matching(
        self, engine: SkillEngine,
    ) -> None:
        """Removing "license" from sbom-task.md's triggers must not lose
        its real SBOM-related matching -- "sbom"/"bom"/"software"/"bill"
        are untouched."""
        skill = engine.skill_for_category("sbom")
        assert skill is not None
        assert skill.name == "sbom-task"


class TestNewSkillTriggersDoNotCollide:
    """iac/manifests/health are now registered in FIX_REGISTRY (exact-match
    lookup, authoritative), but ``Skill.matches()``'s own trigger-keyword
    fallback (used for greenfield reports with no open findings, see
    ``SkillEngine.match()``) still runs across every skill's ``triggers``
    list. The `policy`/`availability`/`license`/`backup` bugs this file
    already guards against were all *exact* trigger-string collisions
    between two skills that both listed the same word -- verify
    helm-chart's and health-probes-policy's new trigger words don't repeat
    any word an existing, unrelated skill already uses."""

    @pytest.fixture(scope="class")
    def all_skills(self) -> list:
        from agentit.skill_engine import load_all_skills

        return load_all_skills(_SKILLS_DIR)

    @pytest.mark.parametrize("skill_name,triggers", [
        ("helm-chart", ["helm", "chart", "iac", "kustomize", "terraform", "k8s manifest", "kubernetes manifest"]),
        ("health-probes-policy", ["health", "probe", "liveness", "readiness"]),
    ])
    def test_new_skill_triggers_are_unique(self, all_skills: list, skill_name: str, triggers: list[str]) -> None:
        others = [s for s in all_skills if s.name != skill_name]
        for trigger in triggers:
            colliding = [s.name for s in others if trigger in [t.lower() for t in s.triggers]]
            assert not colliding, (
                f"trigger {trigger!r} on skill '{skill_name}' also declared by "
                f"{colliding} -- exact-match trigger collision (see policy/"
                f"availability/license/backup precedent above)"
            )


class TestDispatcherRoutesThroughSameFunction:
    """`RemediationDispatcher._dispatch_generate` must resolve the skill via
    the exact same `skill_for_category()` used by `generate_for_finding()`,
    not a separate by-name lookup that could drift from it."""

    def test_dispatch_generate_and_skill_for_category_agree_for_every_registry_category(
        self, engine: SkillEngine,
    ) -> None:
        from agentit.remediation.dispatcher import RemediationDispatcher

        dispatcher = RemediationDispatcher(store=None)
        report = make_report()
        for category, (_domain, skill_name) in FIX_REGISTRY.items():
            if skill_name == "patch_base_image":
                continue
            resolved = engine.skill_for_category(category)
            assert resolved is not None
            # `_dispatch_generate` is a plain sync method (no store I/O) --
            # safe to call directly without an event loop.
            result = dispatcher._dispatch_generate(_domain, category, report)
            assert result["method"] == resolved.name == skill_name
