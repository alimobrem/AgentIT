"""Learning agent — uses LLM to research CVEs/best-practices and generate new skills."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def research_cves(llm_client: object, limit: int = 5) -> list[dict]:
    """Ask the LLM about recent/notable CVEs relevant to Kubernetes workloads.

    Returns a list of dicts with keys: id, severity, description, affected_component, mitigation.
    """
    system = (
        "You are a security researcher. Return a JSON array of CVE entries relevant to "
        "Kubernetes, container, and cloud-native workloads. Each entry must have: "
        '"id" (CVE-YYYY-NNNNN), "severity" (critical/high/medium/low), '
        '"description" (one sentence), "affected_component" (e.g. kubelet, container runtime), '
        '"mitigation" (one sentence). JSON array only, no markdown fences.'
    )
    user = f"List {limit} notable CVEs for Kubernetes and container workloads. JSON array only."

    raw = llm_client._chat(system, user)
    if raw is None:
        logger.warning("LLM returned no CVE data")
        return []

    # Strip markdown fences if present
    cleaned = re.sub(r"^```(?:json)?\n?", "", raw.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, list):
            logger.warning("LLM CVE response is not a list")
            return []
        return parsed[:limit]
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Failed to parse CVE response: %s", exc)
        return []


def research_best_practices(llm_client: object, topic: str) -> list[dict]:
    """Ask the LLM about best practices for a given topic.

    Returns a list of dicts with keys: title, description, category, priority.
    """
    system = (
        "You are a Kubernetes platform engineer. Return a JSON array of best practice "
        "recommendations. Each entry must have: "
        '"title" (short name), "description" (2-3 sentences), '
        '"category" (security/observability/reliability/compliance/cicd), '
        '"priority" (critical/high/medium/low). JSON array only, no markdown fences.'
    )
    user = f"List best practices for: {topic}. JSON array only."

    raw = llm_client._chat(system, user)
    if raw is None:
        logger.warning("LLM returned no best-practices data")
        return []

    cleaned = re.sub(r"^```(?:json)?\n?", "", raw.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, list):
            logger.warning("LLM best-practices response is not a list")
            return []
        return parsed
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Failed to parse best-practices response: %s", exc)
        return []


def research_for_app(llm_client: object, report: object, limit: int = 5) -> list[dict]:
    """Targeted research based on an app's actual stack from its assessment.

    Uses the assessment report to ask specific questions about the app's
    languages, frameworks, databases, and architecture — not generic research.
    """
    stack_parts = []
    if hasattr(report, 'stack'):
        for lang in getattr(report.stack, 'languages', []):
            stack_parts.append(lang.name)
        for fw in getattr(report.stack, 'frameworks', []):
            stack_parts.append(fw.name)
        for db in getattr(report.stack, 'databases', []):
            stack_parts.append(db.name)
        if getattr(report.stack, 'runtime', None):
            stack_parts.append(report.stack.runtime.name)
    stack_str = ", ".join(stack_parts) if stack_parts else "unknown stack"

    finding_categories = []
    if hasattr(report, 'scores'):
        for s in report.scores:
            for f in s.findings:
                finding_categories.append(f"{s.dimension}: {f.description}")

    system = (
        "You are a platform security researcher. Given an application's specific "
        "technology stack and current findings, identify the most important "
        "improvements, risks, and best practices. Return a JSON array where each "
        "item has: title, description, category (security/observability/reliability/"
        "compliance/cicd/infrastructure), priority (critical/high/medium), "
        "and fix_approach (how to fix it on Kubernetes/OpenShift). "
        "Be SPECIFIC to the technologies listed — not generic advice. JSON only."
    )
    user = (
        f"Application: {getattr(report, 'repo_name', 'unknown')}\n"
        f"Stack: {stack_str}\n"
        f"Criticality: {getattr(report, 'criticality', 'medium')}\n"
        f"Current score: {getattr(report, 'overall_score', 0):.0f}/100\n"
        f"Current findings:\n" + "\n".join(f"  - {fc}" for fc in finding_categories[:10])
        + f"\n\nList {limit} specific, actionable improvements for THIS stack. "
        "Focus on what's missing that matters most for enterprise readiness. JSON only."
    )

    raw = llm_client._chat(system, user)
    if raw is None:
        return []

    cleaned = re.sub(r"^```(?:json)?\n?", "", raw.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, list):
            return []
        return parsed[:limit]
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse targeted research response")
        return []


def research_skill_improvement(llm_client: object, skill_name: str, domain: str, stats: dict) -> dict:
    """Ask the LLM to propose a specific fix/replacement for an underperforming skill.

    ``stats`` is one entry from ``AssessmentStore.get_low_effectiveness_skills()``
    (``{"skill", "approval_rate", "raw_approval_rate", "total"}`` -- the
    recency-weighted rate that got it flagged in the first place). Returns a
    dict shaped like ``research_cves()``/``research_best_practices()`` items
    (``title``/``description``/``category``/``priority``/``fix_approach``)
    so it can be fed straight into ``generate_skill_from_research()``, or
    ``{}`` if the LLM returned nothing usable.

    This is what closes the self-improvement loop: until this existed, the
    learning agent only ever researched CVEs/best-practices on its own
    schedule, blind to which of its *own* already-shipped skills humans keep
    rejecting.
    """
    system = (
        "You are a Kubernetes platform engineer improving an underperforming "
        "AgentIT skill. Return a single JSON object (not an array) with: "
        '"title" (short name for the improved replacement), '
        '"description" (2-3 sentences on what was likely wrong and how the '
        "replacement should behave differently), "
        '"category" (security/observability/reliability/compliance/cicd/infrastructure), '
        '"priority" (critical/high/medium), '
        '"fix_approach" (concrete guidance for generating the replacement). '
        "JSON object only, no markdown fences."
    )
    user = (
        f"Skill '{skill_name}' (domain: {domain}) has a low human approval rate: "
        f"{stats.get('approval_rate', 0):.0%} recency-weighted "
        f"({stats.get('raw_approval_rate', stats.get('approval_rate', 0)):.0%} all-time) "
        f"over {stats.get('total', 0)} recorded outcome(s). Humans keep rejecting or "
        "modifying what this skill generates. Propose a specific, improved replacement "
        "approach for this skill's property -- what likely causes the low approval, and "
        "how a better version should behave. Be concrete, not generic."
    )

    raw = llm_client._chat(system, user)
    if raw is None:
        logger.warning("LLM returned no skill-improvement data for %s", skill_name)
        return {}

    cleaned = re.sub(r"^```(?:json)?\n?", "", raw.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            logger.warning("LLM skill-improvement response for %s is not an object", skill_name)
            return {}
        return parsed
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Failed to parse skill-improvement response for %s: %s", skill_name, exc)
        return {}


def generate_skill_from_research(
    llm_client: object,
    research_item: dict,
    domain: str = "security",
) -> str:
    """Generate a complete skill Markdown file from a research item.

    Returns the full content of a .md skill file with YAML frontmatter and body.
    """
    system = (
        "You are a Kubernetes platform engineer generating AgentIT skill definitions. "
        "A skill is a Markdown file with YAML frontmatter and a body. "
        "The frontmatter must have: name, domain, version (int), triggers (list of keywords), "
        "outputs (list of Kubernetes resource kinds), property (one sentence), mode (llm or template), "
        "status (draft), source (learning-agent), created_at (ISO date). "
        "The body must have sections: Property, Key decisions for the LLM, Constraints, Verification. "
        "Output the complete Markdown file content. No extra fences around the whole output."
    )
    user = (
        f"Generate a skill definition for domain '{domain}' based on this research:\n"
        f"{json.dumps(research_item, indent=2)}\n\n"
        "Output the complete .md file content with YAML frontmatter (---) and body."
    )

    from agentit.llm import _SKILL_GENERATION_MAX_TOKENS
    raw = llm_client._chat(system, user, max_tokens=_SKILL_GENERATION_MAX_TOKENS)
    if raw is None:
        logger.warning("LLM returned no skill content")
        return ""
    return raw.strip()


def check_skill_exists(skills_dir: Path, name: str, domain: str) -> bool:
    """Check if a skill with a similar name already exists in the domain directory."""
    safe_name = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    target = skills_dir / domain / f"{safe_name}.md"
    if target.exists():
        return True
    # Also check for similar names
    domain_dir = skills_dir / domain
    if domain_dir.exists():
        for existing in domain_dir.glob("*.md"):
            existing_name = existing.stem.lower()
            # Fuzzy match: if >60% of words overlap
            new_words = set(safe_name.split("-"))
            old_words = set(existing_name.split("-"))
            if new_words and old_words:
                overlap = len(new_words & old_words) / max(len(new_words), len(old_words))
                if overlap > 0.6:
                    return True
    return False


def save_skill(content: str, skills_dir: Path, domain: str = "security", name: str = "") -> Path | None:
    """Write a generated skill to disk under skills_dir/domain/name.md.

    Extracts the skill name from frontmatter if not provided.
    Returns the path written, or None on failure.
    """
    if not content:
        logger.warning("Empty skill content, nothing to save")
        return None

    # Extract name from frontmatter
    if not name:
        match = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
        if match:
            name = match.group(1).strip().strip('"').strip("'")
        else:
            name = f"learned-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    # Sanitize name for filesystem
    safe_name = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    if not safe_name:
        safe_name = "unnamed-skill"

    target_dir = skills_dir / domain
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{safe_name}.md"

    try:
        target_path.write_text(content, encoding="utf-8")
        logger.info("Saved skill to %s", target_path)
        return target_path
    except OSError as exc:
        logger.warning("Failed to save skill to %s: %s", target_path, exc)
        return None


LEARNING_RUN_ACTION = "learning-run"


def describe_learning_run(
    trigger: str,
    mode: str | None,
    saved: list[str],
    skipped: list[str],
    error: str | None = None,
) -> tuple[str, str, dict]:
    """Build the ``(severity, summary, details)`` for a durable, queryable
    record of one research run -- a manual "Research CVEs & Generate Skills"
    button click, or one of the ``skill-learner`` watcher's 24h ticks.

    Before this helper existed, a run only left a trace in the ``events``
    table when it actually generated a skill (``skills-generated``) -- a
    run that found nothing to improve, skipped everything as already
    covered, or couldn't even reach the LLM vanished the moment its toast
    disappeared. Callers ``await store.log_event("learning-agent" or
    "skill-learner", LEARNING_RUN_ACTION, None, severity, summary,
    details=details)`` with the return value of this function so EVERY run
    -- success, no-op, or failure -- is queryable via
    ``list_events_by_action(LEARNING_RUN_ACTION)`` regardless of which of
    the two entry points triggered it.

    ``trigger`` is ``"manual"`` (portal button) or ``"watcher"`` (the
    skill-learner watcher's own tick). ``mode`` is ``"skill-improvement"``
    (a flagged low-effectiveness skill was targeted first, per
    ``get_low_effectiveness_skills()``), ``"cve-sweep"`` (the generic
    fallback), or ``None`` when the run never got far enough to pick a mode
    (e.g. the LLM was unavailable).
    """
    details: dict = {"trigger": trigger, "mode": mode, "saved": list(saved), "skipped": list(skipped)}
    if error:
        details["error"] = error
        return "error", f"Learning run failed: {error}", details
    if saved:
        kind = "improvement" if mode == "skill-improvement" else "new skill"
        summary = f"Generated {len(saved)} {kind}(s): {', '.join(saved)}"
        if skipped:
            summary += f" ({len(skipped)} skipped)"
        return "info", summary, details
    if skipped:
        summary = (
            f"No new skills — {len(skipped)} flagged low-effectiveness skill(s) couldn't be improved this time."
            if mode == "skill-improvement"
            else f"No new skills — {len(skipped)} researched CVE(s) already have matching skills."
        )
        return "warning", summary, details
    return "warning", "No new skills generated — research returned nothing usable this time.", details


def count_recent_improvement_failures(events: list[dict], cutoff: datetime) -> dict[str, int]:
    """Count how many times each skill name shows up in ``skipped`` across
    recent ``learning-run`` events logged with ``mode == "skill-improvement"``
    and a timestamp at or after ``cutoff``.

    This is the read side of the skill-improvement cooldown/backoff fix:
    without it, a flagged low-effectiveness skill that fails to improve
    gets re-researched every single tick forever with no new information
    (confirmed live: the same skill's improvement attempt kept failing
    "couldn't be improved this time" repeatedly). There's no dedicated
    attempts table -- every attempt already leaves a durable trace via
    ``describe_learning_run``'s ``details`` above, so replaying that
    history is enough to detect "we've already tried this one N times
    recently" without any new persisted state.

    ``events`` is the raw shape ``AssessmentStore.list_events_by_action()``
    returns (``timestamp`` an ISO-8601 string, ``details_json`` a JSON
    string) -- any event missing or malformed in either field is skipped,
    not raised on.
    """
    counts: dict[str, int] = {}
    for ev in events:
        ts = ev.get("timestamp")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff:
            continue
        try:
            run_details = json.loads(ev.get("details_json") or "{}")
        except (TypeError, ValueError):
            continue
        if run_details.get("mode") != "skill-improvement":
            continue
        for skill_name in run_details.get("skipped") or []:
            counts[skill_name] = counts.get(skill_name, 0) + 1
    return counts
