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
