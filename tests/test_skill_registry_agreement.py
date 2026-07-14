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

from agentit.remediation.registry import FIX_REGISTRY
from agentit.skill_engine import SkillEngine
from conftest import make_report

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


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
