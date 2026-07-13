"""Skill engine — loads Markdown skill definitions, matches them to findings, renders templates."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agentit.agents.base import GeneratedFile
from agentit.models import AssessmentReport
from agentit.platform_context import PlatformContext

logger = logging.getLogger(__name__)

# Irregular plurals for K8s kinds used in skills/*/outputs that a naive
# "+s" suffix gets wrong (e.g. "networkpolicys", "ingresss"). Kubernetes
# API resource lists use the correct English plural, so has_api() lookups
# must match it. Anything not listed here falls back to naive "+s".
_IRREGULAR_KIND_PLURALS: dict[str, str] = {
    "policy": "policies",
    "networkpolicy": "networkpolicies",
    "ingress": "ingresses",
}


def _pluralize_kind(kind: str) -> str:
    """Return the correct lowercase plural API resource name for a K8s kind."""
    lower = kind.lower()
    return _IRREGULAR_KIND_PLURALS.get(lower, lower + "s")


@dataclass
class Skill:
    """A single skill definition loaded from a Markdown file with YAML frontmatter."""

    name: str
    domain: str
    version: int
    triggers: list[str]
    outputs: list[str]
    property_description: str
    body: str
    file_path: str
    mode: str = "template"
    # Lifecycle fields
    status: str = "active"  # active / deprecated / retired / draft
    superseded_by: str = ""
    deprecated_reason: str = ""
    conflicts_with: list[str] = field(default_factory=list)
    requires_crd: list[str] = field(default_factory=list)
    source: str = "manual"
    created_at: str = ""

    def matches(self, report: AssessmentReport) -> bool:
        """Return True if any trigger keyword appears in the report findings.

        Retired and draft skills never match. Deprecated skills match but log a warning.
        """
        if self.status in ("retired", "draft"):
            return False
        if self.status == "deprecated":
            msg = f"Skill '{self.name}' is deprecated"
            if self.deprecated_reason:
                msg += f": {self.deprecated_reason}"
            if self.superseded_by:
                msg += f" (use '{self.superseded_by}' instead)"
            logger.warning(msg)
        haystack = _report_text(report).lower()
        return any(t.lower() in haystack for t in self.triggers)


def _report_text(report: AssessmentReport) -> str:
    """Flatten a report into a single searchable string."""
    parts = [report.summary]
    for score in report.scores:
        parts.append(score.dimension)
        for f in score.findings:
            parts.extend([f.category, f.description, f.recommendation])
    return " ".join(parts)


def load_skill(path: Path) -> Skill | None:
    """Parse a Markdown file with YAML frontmatter into a Skill."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read skill file %s: %s", path, exc)
        return None

    # Split frontmatter from body
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
    if not match:
        logger.warning("No YAML frontmatter in %s", path)
        return None

    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logger.warning("Bad YAML frontmatter in %s: %s", path, exc)
        return None

    if not isinstance(meta, dict):
        return None

    required = {"name", "domain", "version", "triggers", "outputs"}
    missing = required - set(meta.keys())
    if missing:
        logger.warning("Skill %s missing fields: %s", path, missing)
        return None

    status = meta.get("status", "active")
    if status not in ("active", "deprecated", "retired", "draft"):
        logger.warning("Skill %s has invalid status '%s', defaulting to 'active'", path, status)
        status = "active"

    conflicts_raw = meta.get("conflicts_with", [])
    requires_crd_raw = meta.get("requires_crd", [])

    return Skill(
        name=meta["name"],
        domain=meta["domain"],
        version=int(meta["version"]),
        triggers=list(meta["triggers"]),
        outputs=list(meta["outputs"]),
        property_description=meta.get("property", ""),
        body=match.group(2),
        file_path=str(path),
        mode=meta.get("mode", "template"),
        status=status,
        superseded_by=meta.get("superseded_by", ""),
        deprecated_reason=meta.get("deprecated_reason", ""),
        conflicts_with=list(conflicts_raw) if isinstance(conflicts_raw, list) else [conflicts_raw],
        requires_crd=list(requires_crd_raw) if isinstance(requires_crd_raw, list) else [requires_crd_raw],
        source=meta.get("source", "manual"),
        created_at=meta.get("created_at", ""),
    )


def load_all_skills(skills_dir: Path) -> list[Skill]:
    """Load every Markdown skill definition under *skills_dir* (recursively)."""
    if not skills_dir.is_dir():
        return []
    results: list[Skill] = []
    for md in sorted(skills_dir.rglob("*.md")):
        skill = load_skill(md)
        if skill is not None:
            results.append(skill)
    return results


class SkillEngine:
    """Loads skills from a directory and applies them to assessment reports."""

    def __init__(self, skills_dir: Path, *, platform: PlatformContext | None = None) -> None:
        self.skills_dir = skills_dir
        self.platform = platform
        self.skills: list[Skill] = []
        self._load_all()

    def _load_all(self) -> None:
        self.skills = load_all_skills(self.skills_dir)
        logger.info("Loaded %d skills from %s", len(self.skills), self.skills_dir)

    def match(self, report: AssessmentReport) -> list[Skill]:
        """Return all skills whose triggers match the report.

        Retired/draft skills are excluded by Skill.matches().
        Conflicts between active and deprecated skills are resolved (active wins).
        """
        matched = [s for s in self.skills if s.matches(report)]
        return self._resolve_conflicts(matched)

    @staticmethod
    def _resolve_conflicts(skills: list[Skill]) -> list[Skill]:
        """When two matched skills conflict, prefer active over deprecated."""
        by_name: dict[str, Skill] = {s.name: s for s in skills}
        to_remove: set[str] = set()
        for skill in skills:
            for conflict_name in skill.conflicts_with:
                if conflict_name not in by_name:
                    continue
                other = by_name[conflict_name]
                # active beats deprecated
                if skill.status == "active" and other.status == "deprecated":
                    to_remove.add(other.name)
                elif skill.status == "deprecated" and other.status == "active":
                    to_remove.add(skill.name)
                # same status: keep the higher-versioned one
                elif skill.version >= other.version:
                    to_remove.add(other.name)
                else:
                    to_remove.add(skill.name)
        return [s for s in skills if s.name not in to_remove]

    def generate(self, skill: Skill, report: AssessmentReport,
                 llm_client: object | None = None) -> list[GeneratedFile]:
        """Render a skill against the report. Uses LLM if available, falls back to template."""
        app_name = report.repo_name.lower().replace("_", "-").replace(".", "-")[:63].strip("-") or "app"

        # Check if the output kind is available on the platform
        if self.platform:
            for output_kind in skill.outputs:
                if not self.platform.has_api(_pluralize_kind(output_kind)) and not self.platform.has_api(output_kind.lower()):
                    logger.debug("Skipping skill %s: %s not available", skill.name, output_kind)
                    return []

        # Try LLM generation first (tailored to the specific app)
        if llm_client and hasattr(llm_client, '_chat'):
            llm_result = self._generate_with_llm(skill, report, app_name, llm_client)
            if llm_result:
                return llm_result
            logger.debug("LLM generation failed for %s — falling back to template", skill.name)

        # Fall back to template rendering
        if skill.mode == "template" or skill.mode == "llm":
            template_text = _extract_template(skill.body)
            if template_text:
                rendered = template_text.replace("{{app_name}}", app_name)
                return [GeneratedFile(
                    path=f"{app_name}-{skill.name}.yaml",
                    content=rendered,
                    description=f"Generated by skill {skill.name}",
                    finding_addressed=skill.property_description,
                )]

        return []

    def _generate_with_llm(self, skill: Skill, report: AssessmentReport,
                           app_name: str, llm_client: object) -> list[GeneratedFile]:
        """Use LLM to generate a tailored fix based on the skill knowledge + app context."""
        import yaml
        from agentit.agents.base import validate_manifest

        stack = ", ".join(l.name for l in report.stack.languages) if report.stack.languages else "unknown"
        platform_ctx = self.platform.to_prompt_context() if self.platform else "Unknown platform"

        system = (
            "You are a Kubernetes platform engineer generating manifests. "
            "Output ONLY valid YAML. No markdown fences, no explanations. "
            "Multiple documents separated by '---'. "
            f"Use app name '{app_name}' in all metadata."
        )
        user = (
            f"Application: {app_name}\n"
            f"Stack: {stack}\n"
            f"Criticality: {report.criticality}\n"
            f"Score: {report.overall_score:.0f}/100\n\n"
            f"Platform:\n{platform_ctx}\n\n"
            f"Skill instructions:\n{skill.body}\n\n"
            f"Generate the appropriate {', '.join(skill.outputs)} for this application."
        )

        for attempt in range(2):
            raw = llm_client._chat(system, user)
            if raw is None:
                return []

            content = re.sub(r"^```(?:yaml)?\n?", "", raw.strip())
            content = re.sub(r"\n?```$", "", content)

            errors = validate_manifest(content)
            if errors:
                logger.debug("LLM output validation failed (attempt %d): %s", attempt + 1, errors)
                user += f"\n\nYour previous output had errors: {errors}. Fix them."
                continue

            return [GeneratedFile(
                path=f"{app_name}-{skill.name}.yaml",
                content=content,
                description=f"Generated by skill {skill.name} (LLM-tailored for {stack})",
                finding_addressed=skill.property_description,
            )]

        return []

    def run_all(
        self,
        report: AssessmentReport,
        *,
        store: object | None = None,
        llm_client: object | None = None,
    ) -> list[GeneratedFile]:
        """Match skills to the report and generate all files.

        Returns every GeneratedFile produced, tagged with source='skill' metadata
        via the description field.  The caller can inspect each file's
        ``finding_addressed`` to determine coverage.
        """
        matched = self.match(report)
        all_files: list[GeneratedFile] = []
        for skill in matched:
            files = self.generate(skill, report, llm_client=llm_client)
            all_files.extend(files)
        return all_files

    def covered_domains(self, files: list[GeneratedFile]) -> set[str]:
        """Return the set of skill domains that produced output.

        Works by matching ``finding_addressed`` back to loaded skills.
        """
        addressed = {f.finding_addressed for f in files if f.finding_addressed}
        domains: set[str] = set()
        for skill in self.skills:
            if skill.property_description in addressed:
                domains.add(skill.domain)
        return domains

    def generate_for_finding(self, finding_category: str, finding_description: str,
                             report: AssessmentReport,
                             llm_client: object | None = None) -> list[GeneratedFile]:
        """Generate a fix for a specific finding using the best matching skill."""
        category_lower = finding_category.lower()
        for skill in self.skills:
            if any(t.lower() in category_lower or category_lower in t.lower() for t in skill.triggers):
                return self.generate(skill, report, llm_client=llm_client)
        return []

    def find_uncovered_findings(
        self,
        report: AssessmentReport,
        generated_files: list[GeneratedFile],
    ) -> list[str]:
        """Return finding descriptions not addressed by any generated file."""
        addressed = {f.finding_addressed.lower() for f in generated_files if f.finding_addressed}
        uncovered: list[str] = []
        for score in report.scores:
            for finding in score.findings:
                desc = finding.description.lower()
                if desc not in addressed:
                    uncovered.append(finding.description)
        return uncovered


def _extract_template(body: str) -> str | None:
    """Pull a YAML code block from a skill's Markdown body."""
    match = re.search(r"```ya?ml\s*\n(.*?)```", body, re.DOTALL)
    return match.group(1).strip() if match else None
