"""Tests for the `mode: agent` registration metadata loader
(`agents/capabilities.py::load_agent_classes()`) -- Phase 2 of
docs/extension-model-unification-plan-2026-07-18.md.

Mirrors tests/test_all_checks.py's/tests/test_all_skills.py's role for
their own catalogs: schema validation for every real `agents/*.md` file
on disk, plus a parity proof that the file-derived `AGENT_CLASSES` dict is
byte-for-byte identical to the hardcoded dict literal it replaced (see the
now-superseded literal this file's `_PRE_PHASE2_AGENT_CLASSES` documents).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentit.agents.capabilities import (
    AGENT_CLASSES,
    get_agent_class,
    load_agent_classes,
)

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
REQUIRED_FIELDS = {"name", "category", "code_ref", "resource_tier", "description"}

# The exact hardcoded dict `AGENT_CLASSES` used to be (Phase 1 and earlier)
# -- see docs/extension-model-unification-plan-2026-07-18.md's Phase 2
# section. `load_agent_classes()` must reproduce this exactly from
# `agents/*.md` -- a pure format migration, zero behavior change, the same
# discipline Phase 1 proved for `checks/observability/health-check.yaml`.
_PRE_PHASE2_AGENT_CLASSES: dict[str, tuple[str, str, str, str]] = {
    "cost": ("cost", "agentit.agents.cost", "CostOptimizationAgent", "small"),
    "dependency": ("dependency", "agentit.agents.dependency", "DependencyAgent", "small"),
    "codechange": ("codechange", "agentit.agents.codechange", "CodeChangeAgent", "large"),
}


def _all_agent_files() -> list[Path]:
    if not AGENTS_DIR.is_dir():
        return []
    return sorted(AGENTS_DIR.glob("*.md"))


@pytest.fixture(params=_all_agent_files(), ids=lambda p: p.name)
def agent_file(request: pytest.FixtureRequest) -> Path:
    return request.param


def _parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    import re
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    assert m, f"{path.name}: no YAML frontmatter found"
    meta = yaml.safe_load(m.group(1))
    assert isinstance(meta, dict), f"{path.name}: frontmatter is not a mapping"
    return meta


class TestAllAgentRegistrations:
    """Schema validation for every `agents/*.md` file on disk."""

    def test_has_required_fields(self, agent_file: Path) -> None:
        meta = _parse_frontmatter(agent_file)
        missing = REQUIRED_FIELDS - set(meta.keys())
        assert not missing, f"{agent_file.name} missing fields: {missing}"

    def test_mode_is_agent(self, agent_file: Path) -> None:
        meta = _parse_frontmatter(agent_file)
        assert meta.get("mode") == "agent", f"{agent_file.name}: mode must be 'agent'"

    def test_code_ref_is_module_colon_classname(self, agent_file: Path) -> None:
        meta = _parse_frontmatter(agent_file)
        code_ref = str(meta["code_ref"])
        assert ":" in code_ref, f"{agent_file.name}: code_ref {code_ref!r} missing ':'"
        module_path, _, class_name = code_ref.rpartition(":")
        assert module_path and class_name, f"{agent_file.name}: malformed code_ref {code_ref!r}"

    def test_code_ref_class_is_actually_importable(self, agent_file: Path) -> None:
        """The class code_ref points at must really exist -- catches a typo
        that would otherwise only surface at agent-run time."""
        import importlib

        meta = _parse_frontmatter(agent_file)
        module_path, _, class_name = str(meta["code_ref"]).rpartition(":")
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name, None)
        assert cls is not None, f"{agent_file.name}: {class_name} not found in {module_path}"
        assert hasattr(cls, "run"), f"{agent_file.name}: {class_name} missing run()"

    def test_resource_tier_is_known(self, agent_file: Path) -> None:
        from agentit.agents.capabilities import RESOURCE_TIERS

        meta = _parse_frontmatter(agent_file)
        assert meta["resource_tier"] in RESOURCE_TIERS, (
            f"{agent_file.name}: unknown resource_tier {meta['resource_tier']!r}"
        )


class TestLoadAgentClasses:
    """`load_agent_classes()` must reproduce the pre-Phase-2 hardcoded dict
    exactly -- the parity proof for this migration."""

    def test_matches_pre_phase2_hardcoded_dict_exactly(self) -> None:
        assert load_agent_classes() == _PRE_PHASE2_AGENT_CLASSES

    def test_module_level_agent_classes_is_file_derived(self) -> None:
        """The real module-level `AGENT_CLASSES` (computed at import time)
        must equal a fresh call to the loader -- proves it's genuinely
        file-derived, not a stale copy captured before some file changed."""
        assert AGENT_CLASSES == load_agent_classes()

    def test_missing_directory_returns_empty_dict_not_a_crash(self, tmp_path: Path) -> None:
        assert load_agent_classes(tmp_path / "does-not-exist") == {}

    def test_skips_malformed_file_without_crashing(self, tmp_path: Path) -> None:
        (tmp_path / "good.md").write_text(
            "---\nmode: agent\nname: good\ncategory: good\n"
            "code_ref: agentit.agents.cost:CostOptimizationAgent\n"
            "resource_tier: small\ndescription: ok\n---\n\nbody\n",
        )
        (tmp_path / "bad.md").write_text("no frontmatter here at all\n")
        (tmp_path / "bad-code-ref.md").write_text(
            "---\nmode: agent\nname: bad-code-ref\ncategory: bad\n"
            "code_ref: not-a-module-colon-class\n"
            "resource_tier: small\ndescription: ok\n---\n\nbody\n",
        )
        result = load_agent_classes(tmp_path)
        assert result == {
            "good": ("good", "agentit.agents.cost", "CostOptimizationAgent", "small"),
        }

    def test_get_agent_class_resolves_every_loaded_agent(self) -> None:
        for name in AGENT_CLASSES:
            cls = get_agent_class(name)
            assert cls is not None
            assert hasattr(cls, "run")
