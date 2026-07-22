"""Validate every skill Markdown file in skills/ directory."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
REQUIRED_FIELDS = {"name", "domain", "triggers", "outputs"}
VALID_STATUSES = {"active", "draft", "deprecated"}
# retired skills should not be shipped; fail if found
FORBIDDEN_STATUSES = {"retired"}
# Outputs that are NOT K8s kinds (config files, runbooks, etc.)
NON_K8S_OUTPUTS = {"RenovateConfig", "DependabotConfig", "Runbook"}


def _all_skill_files() -> list[Path]:
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(SKILLS_DIR.rglob("*.md"))


def _parse_frontmatter(path: Path) -> tuple[dict, str]:
    """Return (frontmatter_dict, body) from a skill Markdown file."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
    assert m, f"{path.name}: no YAML frontmatter found"
    meta = yaml.safe_load(m.group(1))
    assert isinstance(meta, dict), f"{path.name}: frontmatter is not a mapping"
    return meta, m.group(2)


def _extract_yaml_block(body: str) -> str | None:
    """Pull first ```yaml ... ``` code block from body."""
    m = re.search(r"```ya?ml\s*\n(.*?)```", body, re.DOTALL)
    return m.group(1).strip() if m else None


@pytest.fixture(params=_all_skill_files(), ids=lambda p: str(p.relative_to(SKILLS_DIR)))
def skill_file(request: pytest.FixtureRequest) -> Path:
    return request.param


class TestAllSkills:
    """Validate every skill definition."""

    def test_loads_successfully(self, skill_file: Path) -> None:
        """Skill must parse without error (valid frontmatter)."""
        meta, _ = _parse_frontmatter(skill_file)
        assert meta is not None

    def test_has_required_fields(self, skill_file: Path) -> None:
        meta, _ = _parse_frontmatter(skill_file)
        missing = REQUIRED_FIELDS - set(meta.keys())
        assert not missing, f"missing: {missing}"

    def test_status_not_retired(self, skill_file: Path) -> None:
        """Status must be active or draft, not retired."""
        meta, _ = _parse_frontmatter(skill_file)
        status = meta.get("status", "active")
        assert status not in FORBIDDEN_STATUSES, f"retired skill still shipped: {skill_file.name}"
        assert status in VALID_STATUSES, f"unknown status '{status}'"

    def test_template_mode_has_yaml_block(self, skill_file: Path) -> None:
        """Template-mode skills that produce K8s kinds must contain a ```yaml code block."""
        meta, body = _parse_frontmatter(skill_file)
        if meta.get("mode") != "template":
            pytest.skip("not template mode")
        # Source-repo patches (Dockerfile, .node-version, audit.py, …) are
        # not K8s manifests — they use language-specific code fences or
        # programmatic generators, not ```yaml.
        if meta.get("delivery") == "source":
            pytest.skip("source-delivery skill (non-YAML patch)")
        outputs = set(meta.get("outputs", []))
        if outputs and outputs <= NON_K8S_OUTPUTS:
            pytest.skip("non-K8s output type")
        block = _extract_yaml_block(body)
        assert block is not None, "template skill has no yaml code block"

    def test_template_yaml_is_parseable(self, skill_file: Path) -> None:
        """The YAML block in a template skill must parse (ignoring {{}} placeholders)."""
        meta, body = _parse_frontmatter(skill_file)
        if meta.get("mode") != "template":
            pytest.skip("not template mode")
        block = _extract_yaml_block(body)
        if block is None:
            pytest.skip("no yaml block")
        # Replace template vars with placeholder strings so YAML parses
        sanitized = re.sub(r"\{\{(\w+)\}\}", r"placeholder-\1", block)
        docs = list(yaml.safe_load_all(sanitized))
        assert len(docs) >= 1, "yaml block produced no documents"

    def test_template_yaml_is_valid_k8s_manifest(self, skill_file: Path) -> None:
        """Each YAML doc in a template skill must have apiVersion, kind, metadata (K8s outputs only)."""
        meta, body = _parse_frontmatter(skill_file)
        if meta.get("mode") != "template":
            pytest.skip("not template mode")
        outputs = set(meta.get("outputs", []))
        if outputs and outputs <= NON_K8S_OUTPUTS:
            pytest.skip("non-K8s output type")
        block = _extract_yaml_block(body)
        if block is None:
            pytest.skip("no yaml block")
        sanitized = re.sub(r"\{\{(\w+)\}\}", r"placeholder-\1", block)
        for i, doc in enumerate(yaml.safe_load_all(sanitized)):
            if doc is None:
                continue
            assert "apiVersion" in doc, f"doc {i}: missing apiVersion"
            assert "kind" in doc, f"doc {i}: missing kind"
            assert "metadata" in doc, f"doc {i}: missing metadata"


# check_engine.VALID_TYPES, duplicated here deliberately (like
# tests/test_all_checks.py's own independent copy) so this schema test
# catches drift against the real engine instead of silently trusting it.
_DETECT_VALID_TYPES = {
    "file_exists", "file_contains", "file_missing", "yaml_kind_exists", "yaml_kind_missing",
}
_DETECT_VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}


class TestDetectModeSkills:
    """Schema validation for `mode: detect` skills -- the detection-shaped
    half of the unified extension model
    (docs/extension-model-unification-plan-2026-07-18.md). Mirrors
    tests/test_all_checks.py's role for legacy checks/*.yaml files: every
    detect-mode skill on disk must have a rule that will actually compile
    into a runnable check_engine.CheckDefinition, not just well-formed
    frontmatter."""

    def test_detect_mode_has_required_detection_fields(self, skill_file: Path) -> None:
        meta, _ = _parse_frontmatter(skill_file)
        if meta.get("mode") != "detect":
            pytest.skip("not detect mode")
        for field in ("severity", "category", "description", "recommendation", "rule"):
            assert field in meta, f"{skill_file.name}: mode=detect missing required field '{field}'"

    def test_detect_mode_rule_type_is_valid(self, skill_file: Path) -> None:
        meta, _ = _parse_frontmatter(skill_file)
        if meta.get("mode") != "detect":
            pytest.skip("not detect mode")
        rule = meta.get("rule", {})
        assert isinstance(rule, dict), f"{skill_file.name}: rule must be a mapping"
        assert rule.get("type") in _DETECT_VALID_TYPES, (
            f"{skill_file.name}: rule.type {rule.get('type')!r} is not one of {_DETECT_VALID_TYPES}"
        )

    def test_detect_mode_severity_is_valid(self, skill_file: Path) -> None:
        meta, _ = _parse_frontmatter(skill_file)
        if meta.get("mode") != "detect":
            pytest.skip("not detect mode")
        assert str(meta.get("severity", "")).lower() in _DETECT_VALID_SEVERITIES, (
            f"{skill_file.name}: severity {meta.get('severity')!r} is not one of {_DETECT_VALID_SEVERITIES}"
        )

    def test_detect_mode_triggers_and_outputs_are_empty(self, skill_file: Path) -> None:
        """Not a hard engine requirement (Skill.matches()/generate() both
        already no-op for mode=detect regardless of these values -- see
        skill_engine.py) but a real footgun if left non-empty: a
        detect-mode skill with real triggers looks, on the Capabilities
        page, like it also does something on match, which it never does."""
        meta, _ = _parse_frontmatter(skill_file)
        if meta.get("mode") != "detect":
            pytest.skip("not detect mode")
        assert meta.get("triggers") == [], f"{skill_file.name}: mode=detect should have empty triggers"
        assert meta.get("outputs") == [], f"{skill_file.name}: mode=detect should have empty outputs"

    def test_detect_mode_rule_compiles_via_skill_engine(self, skill_file: Path) -> None:
        """End-to-end schema check: load_skill() + _skill_to_check_definition()
        must actually produce a runnable CheckDefinition, not just pass the
        field-presence checks above in isolation."""
        from agentit.skill_engine import _skill_to_check_definition, load_skill

        meta, _ = _parse_frontmatter(skill_file)
        if meta.get("mode") != "detect":
            pytest.skip("not detect mode")
        skill = load_skill(skill_file)
        assert skill is not None, f"{skill_file.name}: failed to load as a Skill"
        defn = _skill_to_check_definition(skill)
        assert defn is not None, f"{skill_file.name}: rule did not compile to a CheckDefinition"
