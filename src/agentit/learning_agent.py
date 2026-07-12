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

    raw = llm_client._chat(system, user)
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
