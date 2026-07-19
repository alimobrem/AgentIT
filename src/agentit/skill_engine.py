"""Skill engine — loads Markdown skill definitions, matches them to findings, renders templates."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agentit.agents.base import GeneratedFile, _sanitize_name
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


# Matches only AgentIT's own bare-identifier placeholders (e.g. {{app_name}},
# {{namespace}}, {{scanner_image}}) -- deliberately the same shape as
# test_all_skills.py's `re.sub(r"\{\{(\w+)\}\}", ...)` sanitization regex, so
# both agree on what counts as "an AgentIT placeholder". This must NOT match
# Go-template/Alertmanager notification syntax skills legitimately ship
# verbatim for the receiving system to evaluate at runtime (e.g.
# `{{ .GroupLabels.alertname }}`, `{{ range .Alerts }}...{{ end }}` in
# alertmanager-config.md/pagerduty-config.md) -- those always have a leading
# space/dot or pipe/keyword, which `\w+` alone never matches.
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


class UnresolvedPlaceholderError(Exception):
    """Raised when a rendered skill template still contains literal
    ``{{...}}`` placeholder text after substituting every value this
    method actually has real data for.

    This is the safety net for template-fallback generation: it guarantees
    a manifest with unsubstituted placeholders (e.g. ``image: "{{image}}"``)
    never reaches a ``GeneratedFile`` a user might apply to a real cluster.
    """

    def __init__(self, placeholders: list[str]) -> None:
        self.placeholders = placeholders
        super().__init__(f"unresolved placeholder(s): {', '.join(placeholders)}")


def _template_variables(app_name: str, report: AssessmentReport) -> dict[str, str]:
    """Real, already-available substitution values for skill templates.

    Deliberately small: every value here has a genuine, non-fabricated
    source of truth, so a skill that needs something not listed (a
    scanner image, cost center, team, environment, ...) gets no
    substitution for it and ``_render_template`` hard-fails rather than
    inventing a value (per the "no mock data" rule -- see AGENTS docs).

    - ``namespace`` intentionally mirrors ``routes/assessments.py``'s
      delivery route (``namespace = report.repo_name.lower()...``), i.e.
      the real convention this codebase already uses for "the onboarded
      app's own namespace" -- NOT ``self.platform.namespace``, which is
      AgentIT's *own* operating namespace (see ``PlatformContext``), a
      different thing entirely.
    - ``repo_url``/``git_url`` are the same real value
      (``report.repo_url``) under the two different placeholder names
      different skills happen to use (argocd-application.md uses
      ``{{git_url}}``, tekton-pipeline.md uses ``{{repo_url}}``).
    - ``image_ref`` is ``image_builder.get_image_ref(app_name)`` -- the
      exact internal-registry path ``build_app_image()`` already pushes
      to from its two real production call sites (``webhooks.py``,
      ``assessments.py``, both calling it with no explicit namespace, i.e.
      its "agentit" default) -- a true statement regardless of build
      history (this is where the Pipeline this skill defines *will* push
      to), unlike a *deployed* container's ``{{image}}`` (rollout-patch.md,
      argo-rollout.md), which would be an unverifiable claim that a build
      already happened. That distinction is why only ``image_ref`` is
      listed here.
    """
    from agentit.image_builder import get_image_ref

    return {
        "app_name": app_name,
        "namespace": app_name,
        "repo_url": report.repo_url,
        "git_url": report.repo_url,
        "image_ref": get_image_ref(app_name),
    }


def _render_template(template_text: str, variables: dict[str, str]) -> str:
    """Substitute every known placeholder in *variables*, then hard-fail
    (raise ``UnresolvedPlaceholderError``) if any AgentIT-style ``{{...}}``
    placeholder remains -- rather than returning content with literal
    unsubstituted text a caller might ship as a real manifest.
    """
    rendered = template_text
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)

    unresolved = sorted(set(_PLACEHOLDER_RE.findall(rendered)))
    if unresolved:
        raise UnresolvedPlaceholderError(unresolved)
    return rendered


@dataclass
class Skill:
    """A single skill definition loaded from a Markdown file with YAML frontmatter.

    ``mode`` distinguishes two fundamentally different jobs this same file
    format now covers (see docs/extension-model-unification-plan-2026-07-18.md):
    ``"template"``/``"llm"`` (the original, remediation-shaped skills --
    matched by keyword ``triggers`` against a report's *finding text*,
    producing a ``GeneratedFile``) and ``"detect"`` (the new,
    detection-shaped kind -- runs a declarative ``rule`` against the
    *repo's file content* directly, producing a ``Finding``, exactly like a
    legacy ``checks/*.yaml`` file but expressed in this same Markdown+
    frontmatter format so it gets the same lifecycle/activation machinery
    below for free). A ``detect``-mode skill's ``triggers``/``outputs`` are
    always empty (meaningless for detection) -- see
    ``detect_check_definitions()``/``_skill_to_check_definition()`` for how
    its ``rule`` is actually run.
    """

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
    # `mode: detect` fields -- unused (left at their defaults) by every
    # template/llm-mode skill.
    rule: dict = field(default_factory=dict)
    severity: str = ""
    category: str = ""
    description: str = ""
    recommendation: str = ""

    def matches(self, report: AssessmentReport) -> bool:
        """Return True if any trigger keyword appears in the report findings.

        Retired and draft skills never match. Deprecated skills match but
        log a warning. A ``detect``-mode skill never matches here -- it has
        no remediation output to generate; its findings come from
        ``detect_check_definitions()`` running its ``rule`` against a repo
        directly, not from keyword-matching a report's finding text.
        """
        if self.mode == "detect":
            return False
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

    rule_raw = meta.get("rule", {})

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
        rule=rule_raw if isinstance(rule_raw, dict) else {},
        severity=str(meta.get("severity", "")),
        category=str(meta.get("category", "")),
        description=str(meta.get("description", "")),
        recommendation=str(meta.get("recommendation", "")),
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


def _skill_to_check_definition(skill: Skill):
    """Build a ``check_engine.CheckDefinition`` from a ``mode: detect``
    skill's ``rule`` frontmatter, reusing check_engine's own rule runners
    (``_RUNNERS``, via ``run_checks*``) rather than duplicating
    pattern-matching logic in a second place. Returns ``None`` (logging a
    warning) if the skill's rule/severity don't resolve to something
    check_engine can actually run -- the caller treats that exactly like a
    malformed legacy YAML check file (``check_engine._parse_check_file``
    returning ``None``): skipped, not fatal to the rest of the load.
    """
    from agentit.check_engine import CheckDefinition, SEVERITY_MAP, VALID_TYPES

    check_type = skill.rule.get("type")
    if check_type not in VALID_TYPES:
        logger.warning(
            "Skill %s (mode=detect) has missing/invalid rule.type %r", skill.name, check_type,
        )
        return None

    sev = SEVERITY_MAP.get(skill.severity.lower())
    if sev is None:
        logger.warning(
            "Skill %s (mode=detect) has missing/invalid severity %r", skill.name, skill.severity,
        )
        return None

    raw_pattern = skill.rule.get("pattern")
    pattern: str | list[str] = (
        [str(p) for p in raw_pattern] if isinstance(raw_pattern, list) else str(raw_pattern)
    )

    return CheckDefinition(
        name=skill.name,
        dimension=skill.domain,
        severity=sev,
        category=skill.category or skill.domain,
        check_type=check_type,
        pattern=pattern,
        description=skill.description,
        recommendation=skill.recommendation,
        source_path=skill.file_path,
        case_insensitive=bool(skill.rule.get("case_insensitive", False)),
    )


def detect_check_definitions(skills: list[Skill]) -> list:
    """Return ``check_engine.CheckDefinition``s for every non-retired,
    non-draft ``mode: detect`` skill -- the bridge that lets a
    Markdown-defined detection rule run through the exact same engine
    (``check_engine.run_checks_by_dimension_with_status``) as a legacy
    ``checks/*.yaml`` file, so ``runner.run_assessment()`` can merge findings
    from both formats identically during the transition
    (docs/extension-model-unification-plan-2026-07-18.md, Phase 1). A draft
    skill isn't reviewed/verified yet (mirrors ``Skill.matches()``'s own
    draft exclusion for template-mode skills); a retired one is
    intentionally decommissioned. A *deprecated* detect skill still runs
    (mirrors ``Skill.matches()``'s "deprecated matches but warns" behavior)
    since deprecating a detection rule as a signal to fix it later is a
    different lifecycle decision than never running it again.
    """
    result = []
    for skill in skills:
        if skill.mode != "detect":
            continue
        if skill.status in ("draft", "retired"):
            continue
        if skill.status == "deprecated":
            logger.warning(
                "Detect-mode skill '%s' is deprecated but still contributing findings"
                " (deprecated_reason=%r)", skill.name, skill.deprecated_reason,
            )
        defn = _skill_to_check_definition(skill)
        if defn is not None:
            result.append(defn)
    return result


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
                 llm_client: object | None = None,
                 human_override: str | None = None) -> list[GeneratedFile]:
        """Render a skill against the report. Uses LLM if available, falls back to template."""
        if skill.mode == "detect":
            # Defense in depth alongside Skill.matches()'s own `mode ==
            # "detect"` guard above -- a detect-mode skill has no
            # remediation output to generate at all (see its own
            # docstring); it only ever contributes Findings, via
            # detect_check_definitions(), not GeneratedFiles.
            return []

        app_name = _sanitize_name(report.repo_name)

        # Check if the output kind is available on the platform
        if self.platform:
            for output_kind in skill.outputs:
                if not self.platform.has_api(_pluralize_kind(output_kind)) and not self.platform.has_api(output_kind.lower()):
                    logger.debug("Skipping skill %s: %s not available", skill.name, output_kind)
                    return []

        # Try LLM generation first (tailored to the specific app)
        if llm_client and hasattr(llm_client, '_chat'):
            llm_result = self._generate_with_llm(skill, report, app_name, llm_client, human_override=human_override)
            if llm_result:
                return llm_result
            logger.debug("LLM generation failed for %s — falling back to template", skill.name)

        # Fall back to template rendering
        if skill.mode == "template" or skill.mode == "llm":
            template_text = _extract_template(skill.body)
            if template_text:
                try:
                    rendered = _render_template(template_text, _template_variables(app_name, report))
                except UnresolvedPlaceholderError as exc:
                    logger.error(
                        "Skill %s template-fallback generation for %s rejected: %s "
                        "-- refusing to ship a manifest with literal placeholder text",
                        skill.name, app_name, exc,
                    )
                    return []
                return [GeneratedFile(
                    path=f"{app_name}-{skill.name}.yaml",
                    content=rendered,
                    description=f"Generated by skill {skill.name}",
                    finding_addressed=skill.property_description,
                    skill_name=skill.name,
                )]

        return []

    def _generate_with_llm(self, skill: Skill, report: AssessmentReport,
                           app_name: str, llm_client: object,
                           human_override: str | None = None) -> list[GeneratedFile]:
        """Use LLM to generate a tailored fix based on the skill knowledge + app context.

        ``human_override``, when given, is the most recent human-corrected
        value recorded for this app+domain (``AssessmentStore.get_human_override``)
        -- included as extra prompt guidance so a skill that's already been
        manually corrected once tends toward the pattern a human preferred,
        instead of regenerating the same thing that got edited last time.
        """
        import yaml
        from agentit.agents.base import validate_manifest
        from agentit.llm import _SKILL_GENERATION_MAX_TOKENS

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
        if human_override:
            user += (
                f"\n\nA human previously corrected a similar generated fix for this "
                f"application's '{skill.domain}' domain to the following value -- "
                f"prefer patterns consistent with it where applicable:\n{human_override}"
            )

        for attempt in range(2):
            raw = llm_client._chat(system, user, max_tokens=_SKILL_GENERATION_MAX_TOKENS)
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
                skill_name=skill.name,
            )]

        return []

    def run_all(
        self,
        report: AssessmentReport,
        *,
        store: object | None = None,
        llm_client: object | None = None,
        loop: object | None = None,
    ) -> list[GeneratedFile]:
        """Match skills to the report and generate all files.

        Returns every GeneratedFile produced, tagged with source='skill' metadata
        via the description field.  The caller can inspect each file's
        ``finding_addressed`` to determine coverage.

        ``store``, when given, gates and informs generation per matched skill
        via the same feedback data ``webhooks.py`` already uses to skip
        auto-fix-after-3-rejections:

        - ``get_rejection_count(app_name, skill.domain)`` -- a domain that's
          been rejected 3+ times for this app is skipped entirely (mirroring
          the ``webhooks.py`` threshold) rather than regenerating the same
          kind of fix a human keeps turning down.
        - ``get_human_override(app_name, skill.domain)`` -- if a human
          previously corrected a fix for this app+domain, that value is
          passed to LLM generation as extra guidance (see
          ``_generate_with_llm``'s ``human_override`` param).

        This method itself stays synchronous (it's invoked via
        ``asyncio.to_thread`` from the one async production call site,
        ``FleetOrchestrator.run()``) -- ``store``'s coroutine methods are
        bridged back onto ``loop`` (the event loop that constructed the
        store, captured by the caller before dispatching to a worker
        thread) via ``asyncio.run_coroutine_threadsafe``, the same pattern
        ``EventConsumer._persist_dead_letter`` uses for the identical
        constraint.
        """
        def _bridge(result):
            if not asyncio.iscoroutine(result):
                return result
            return asyncio.run_coroutine_threadsafe(result, loop).result(timeout=30)

        matched = self.match(report)
        all_files: list[GeneratedFile] = []
        for skill in matched:
            if store is not None:
                try:
                    if _bridge(store.get_rejection_count(report.repo_name, skill.domain)) >= 3:
                        logger.info(
                            "Skipping skill %s -- domain '%s' rejected 3+ times for %s",
                            skill.name, skill.domain, report.repo_name,
                        )
                        _bridge(store.log_event(
                            "skill-engine", "skipped-rejected", report.repo_name, "info",
                            f"Skipping skill {skill.name} -- domain '{skill.domain}' rejected 3+ times",
                        ))
                        continue
                except Exception:
                    logger.warning("get_rejection_count lookup failed for %s/%s",
                                   report.repo_name, skill.domain, exc_info=True)

            human_override = None
            if store is not None:
                try:
                    human_override = _bridge(store.get_human_override(report.repo_name, skill.domain))
                except Exception:
                    logger.warning("get_human_override lookup failed for %s/%s",
                                   report.repo_name, skill.domain, exc_info=True)

            files = self.generate(skill, report, llm_client=llm_client, human_override=human_override)
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

    def skill_for_category(self, category: str) -> Skill | None:
        """Resolve a finding category to the one skill responsible for fixing it.

        This is the single function both ``generate_for_finding`` (the CLI's
        ``self-fix`` path) and ``RemediationDispatcher`` (the portal/webhook
        fix-a-finding path -- see ``remediation/dispatcher.py``) route
        through, so a category like ``"policy"`` can never resolve to two
        different skills depending on which caller asked. Before this, each
        call site re-implemented its own category -> skill matching
        independently: ``remediation/registry.py``'s ``FIX_REGISTRY`` mapped
        ``"policy"`` to ``kyverno-require-labels``, while this method's old
        keyword-trigger matching picked ``image-registry-policy`` instead
        (both skills list "policy" as a trigger; ``image-registry-policy.md``
        merely sorts first alphabetically) -- a real, silent disagreement.

        ``FIX_REGISTRY`` is now authoritative: a category it maps to a real,
        loaded skill always resolves to that skill. A category it doesn't
        cover (or maps to a non-skill sentinel like ``"patch_base_image"``,
        which ``RemediationDispatcher`` special-cases itself) falls back to
        keyword-trigger matching, preserving this method's pre-unification
        behavior for anything the static registry doesn't yet know about.
        """
        from agentit.remediation.registry import lookup

        match = lookup(category)
        if match is not None:
            _domain, skill_name = match
            skill = next((s for s in self.skills if s.name == skill_name), None)
            if skill is not None:
                return skill
            logger.warning(
                "FIX_REGISTRY maps category '%s' to skill '%s', but no such skill "
                "is loaded (or it's a non-skill sentinel handled elsewhere)",
                category, skill_name,
            )
            return None

        category_lower = category.lower()
        for skill in self.skills:
            if any(t.lower() in category_lower or category_lower in t.lower() for t in skill.triggers):
                return skill
        return None

    def generate_for_finding(self, finding_category: str, finding_description: str,
                             report: AssessmentReport,
                             llm_client: object | None = None) -> list[GeneratedFile]:
        """Generate a fix for a specific finding using the best matching skill."""
        skill = self.skill_for_category(finding_category)
        if skill is None:
            return []
        return self.generate(skill, report, llm_client=llm_client)

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


def _verification_fixture_report(skill: Skill) -> AssessmentReport:
    """Build a minimal, synthetic ``AssessmentReport`` engineered to trigger
    *skill* -- a self-test fixture for the skill's own generation logic
    (mirrors what ``agentit test-skill --repo`` does against a real repo,
    just without requiring one). Nothing derived from this fixture is ever
    shown to a user as if it described a real application; it exists only
    so ``verify_skill`` can smoke-test that the skill actually produces
    valid output before a human is allowed to activate it.
    """
    from datetime import datetime, timezone

    from agentit.models import ArchitectureInfo, DimensionScore, Finding, Language, Severity, StackInfo

    trigger = skill.triggers[0] if skill.triggers else skill.name
    return AssessmentReport(
        repo_url="https://example.invalid/skill-verification-fixture",
        repo_name="skill-verify",
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(
            languages=[Language(name="python", file_count=1, percentage=100.0)],
            frameworks=[], databases=[], runtimes=[], package_managers=[],
        ),
        architecture=ArchitectureInfo(
            service_count=1, architecture_style="monolith", has_api=True,
            api_style="REST", external_dependencies=[],
        ),
        scores=[DimensionScore(
            dimension=skill.domain, score=50, max_score=100,
            findings=[Finding(
                category=trigger, severity=Severity.medium,
                description=f"Missing {trigger}", recommendation=f"Add {trigger}",
            )],
        )],
        criticality="medium",
        summary="Synthetic fixture for skill verification",
        remediation_plan=[],
    )


def _verify_detect_skill(skill: Skill) -> tuple[bool, list[str], list[str]]:
    """Functional verification for ``mode: detect`` skills -- the
    detection-shaped branch of the unified extension model (see
    docs/extension-model-unification-plan-2026-07-18.md). Mirrors
    ``verify_skill()``'s ``(passed, issues, warnings)`` contract, but checks
    the fields a detection rule actually needs (``rule``/``severity``/
    ``description``/``recommendation``) instead of the
    remediation-shaped ``triggers``/``outputs``/body-template checks
    ``verify_skill()`` runs for template/llm-mode skills, which are
    meaningless here (a detect-mode skill never generates a
    ``GeneratedFile``, so there's nothing to functionally smoke-test by
    generating one).
    """
    issues: list[str] = []
    warnings: list[str] = []

    if not skill.rule:
        issues.append("mode: detect skill has no rule defined")
    if not skill.severity:
        issues.append("mode: detect skill has no severity defined")
    if not skill.description:
        issues.append("mode: detect skill has no description defined")
    if not skill.recommendation:
        issues.append("mode: detect skill has no recommendation defined")
    if skill.status not in ("active", "deprecated", "retired", "draft"):
        issues.append(f"invalid status: {skill.status}")
    if issues:
        return False, issues, warnings

    defn = _skill_to_check_definition(skill)
    if defn is None:
        issues.append(
            "rule failed to compile to a runnable check -- invalid rule.type or severity"
        )
        return False, issues, warnings

    return True, issues, warnings


def verify_skill(skill: Skill, *, llm_client: object | None = None) -> tuple[bool, list[str], list[str]]:
    """Functionally verify a skill before promoting it from draft to active.

    Mirrors ``agentit test-skill``'s static checks (frontmatter
    completeness, expected body sections) plus an actual generation smoke
    test against a synthetic fixture engineered to trigger this skill --
    catching what the portal's old activation flow missed entirely: a
    skill whose frontmatter is well-formed but whose body has no usable
    template produces nothing (or invalid YAML) once matched in production.

    Returns ``(passed, issues, warnings)``:

    - ``issues``: problems serious enough to block activation.
    - ``warnings``: non-blocking notes (e.g. "couldn't functionally verify
      an LLM-only skill without an LLM client configured") -- activation
      may still proceed, but the caller should surface these to the human.

    ``mode: detect`` skills are verified by ``_verify_detect_skill()``
    instead of the checks below: they never generate a ``GeneratedFile``
    (so "no triggers"/"no outputs"/"matches a synthetic fixture" are
    meaningless for them), but they do need a real, runnable ``rule`` --
    the check-shaped half of this function's contract.
    """
    if skill.mode == "detect":
        return _verify_detect_skill(skill)

    issues: list[str] = []
    warnings: list[str] = []

    if not skill.triggers:
        issues.append("no triggers defined")
    if not skill.outputs:
        issues.append("no outputs defined")
    if not skill.body.strip():
        issues.append("empty body")
    if skill.status not in ("active", "deprecated", "retired", "draft"):
        issues.append(f"invalid status: {skill.status}")

    # Missing doc sections are a completeness lint, not a functional
    # blocker (unlike `agentit test-skill`'s standalone diagnostic report,
    # activation shouldn't block on documentation style alone) -- surfaced
    # as a warning so the human reviewer still sees it.
    body_lower = skill.body.lower()
    for section in ("property", "constraint", "verification"):
        if section not in body_lower:
            warnings.append(f"missing '{section}' section in body")

    if issues:
        return False, issues, warnings

    # Skill.matches() hardcodes "draft skills never match" (by design, so
    # the engine never picks them up in real assessments) -- but that's
    # exactly the status this skill has *before* activation, which would
    # make every verification call fail regardless of the skill's actual
    # triggers. Verify against a temporary "active" copy so this checks
    # what the skill *would* do once promoted, without ever mutating (or
    # writing to disk) the real draft.
    import dataclasses
    verify_copy = dataclasses.replace(skill, status="active")

    fixture = _verification_fixture_report(verify_copy)
    if not verify_copy.matches(fixture):
        issues.append(
            "skill's own triggers don't match a fixture finding built from its "
            "first trigger -- triggers may be malformed"
        )
        return False, issues, warnings

    # A throwaway engine with no on-disk skill catalog and no platform
    # gating -- generate() only needs self.platform, and skipping the
    # platform check here means verification never depends on cluster
    # reachability (a skill's output-kind availability is a deployment-time
    # concern, not a "does this skill work" one).
    engine = SkillEngine(Path("skill-verification-fixture-nonexistent"), platform=None)
    files = engine.generate(verify_copy, fixture, llm_client=llm_client)

    if not files:
        if skill.mode == "llm" and llm_client is None:
            warnings.append(
                "skill is LLM-only (mode: llm) with no template fallback -- could not "
                "functionally verify generation without an LLM client configured "
                "(ANTHROPIC_API_KEY/ANTHROPIC_VERTEX_PROJECT_ID)"
            )
            return True, issues, warnings
        issues.append("skill matched the verification fixture but generated no output")
        return False, issues, warnings

    from agentit.agents.base import validate_manifest
    for f in files:
        if not f.path.endswith((".yaml", ".yml")):
            continue
        errors = validate_manifest(f.content)
        if errors:
            issues.append(f"generated {f.path} failed manifest validation: {'; '.join(errors)}")

    return not issues, issues, warnings


def skill_name_from_path(path: str, app_name: str) -> str | None:
    """Recover a skill's name from a file ``SkillEngine.generate()`` produced.

    ``generate()`` names every file it writes ``{app_name}-{skill.name}.yaml``
    (see both branches above). Once that file has passed through
    ``AgentResult``/``onboarding_results.files_json`` (which only carry a
    plain path string, not the richer ``GeneratedFile.skill_name`` field),
    this is the only way left to recover which skill produced it. Returns
    ``None`` if *path* doesn't start with the expected ``{app_name}-``
    prefix -- e.g. a Python-agent-generated file.
    """
    stem = path
    for ext in (".yaml", ".yml"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    prefix = f"{app_name}-"
    if stem.startswith(prefix) and len(stem) > len(prefix):
        return stem[len(prefix):]
    return None


async def record_skill_outcomes(
    store: object | None,
    app_name: str,
    files: list[dict],
    applied_paths: set[str] | None,
    outcome: str,
    reason: str = "",
) -> None:
    """Record a ``skill_effectiveness`` outcome for skill-generated files.

    Used by the real production paths that decide whether a skill's output
    was good (onboarding apply, gate resolve, auto-mode) -- until this was
    wired in, ``record_skill_outcome()`` only ever fired from the CLI
    ``self-fix`` path, so ``skill_effectiveness`` had almost no production
    data. ``outcome`` must be one of the strings already used by that path
    (``"approved"``/``"rejected"`` -- see ``get_skill_effectiveness()``,
    which only tallies those two into its approval-rate math).

    ``files`` is the ``list[dict]`` shape persisted by
    ``save_onboarding()``/``get_onboarding()`` (each with at least
    ``category``/``path`` keys). Only entries with ``category == "skills"``
    are considered -- those are the only files a Skill actually produced;
    Python agents' outputs carry no skill attribution and are silently
    skipped. ``applied_paths``, if given, further restricts recording to
    files at those exact paths (e.g. only the ones a real cluster apply
    actually succeeded on); pass ``None`` to record for every
    skill-generated file regardless (e.g. a gate rejection, where nothing
    was ever applied).
    """
    if store is None:
        return
    sanitized = _sanitize_name(app_name)
    for f in files:
        if f.get("category") != "skills":
            continue
        path = f.get("path", "")
        if applied_paths is not None and path not in applied_paths:
            continue
        skill_name = skill_name_from_path(path, sanitized)
        if not skill_name:
            continue
        try:
            await store.record_skill_outcome(skill_name, app_name, outcome, reason)
        except Exception:
            logger.warning("Failed to record skill outcome for %s/%s", skill_name, app_name, exc_info=True)
