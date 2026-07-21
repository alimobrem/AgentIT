"""Single source of truth for agent capability descriptions.

Used by the orchestrator (for agent registration) and the portal
(for display on agent pages).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# security, observability, cicd, compliance, infrastructure, incident,
# release, retirement, and chaos are now skill-only domains -- their
# Python agents (agents/hardening.py, cicd.py, compliance.py,
# infrastructure.py, incident.py, release.py, retirement.py,
# observability.py, chaos.py) were removed once skills gained full
# template-fallback parity for every artifact they used to generate. See
# docs/agent-removal-readiness.md for the domain-by-domain readiness
# audit. `dependency` and `cost` keep their Python agents specifically for
# the narrative dependency-report.md/cost-report.md outputs, which depend
# on runtime-computed data (detected ecosystems/CVEs, computed cost tier)
# that a static skill template has no access to -- see that same doc's
# recommendation and this repo's "no mock data" rule. `codechange` is kept
# because it patches the application's own source repo, not a K8s
# manifest -- a fundamentally different capability skills don't model.
AGENT_CAPABILITIES: dict[str, str] = {
    "cost": "VPA, cost labels, cost report",
    "dependency": "Dependency report, Renovate/Dependabot config",
    "codechange": ".gitignore, OTel instrumentation, structured logging",
    # Long-lived watcher agents
    "vuln-watcher": "Monitors fleet for CVEs, raises an alert for every critical/high finding",
    "slo-tracker": "Checks SLO status, publishes breach alerts, recommends rollbacks",
    "drift-detector": "Queries Argo CD for OutOfSync apps, auto-syncs them back to Git",
    "skill-learner": "Researches CVEs via LLM, drafts new skills for human review",
    "capability-scout": "Proposes small, evidence-grounded changes to AgentIT itself as a draft PR",
    "reassess-scheduler": "Automatically re-Assesses apps on their configured cadence (daily/weekly/monthly)",
}

RESOURCE_TIERS: dict[str, dict[str, str]] = {
    "small": {"cpu_req": "50m", "cpu_lim": "250m", "mem_req": "128Mi", "mem_lim": "256Mi"},
    "standard": {"cpu_req": "100m", "cpu_lim": "500m", "mem_req": "256Mi", "mem_lim": "512Mi"},
    "large": {"cpu_req": "250m", "cpu_lim": "1000m", "mem_req": "512Mi", "mem_lim": "1Gi"},
}


def _agents_dir() -> Path:
    """Repo-root ``agents/`` directory holding ``mode: agent`` registration
    Markdown files -- the same default-resolution convention
    ``runner.py``'s ``_default_checks_dir()``/``_default_skills_dir()``
    already use for ``checks/``/``skills/``. This file lives at
    ``src/agentit/agents/capabilities.py``, one directory deeper than
    ``runner.py`` (``src/agentit/runner.py``), hence the extra ``.parent``.
    """
    return Path(__file__).resolve().parent.parent.parent.parent / "agents"


def _parse_agent_registration(path: Path) -> dict | None:
    """Parse one ``agents/*.md`` registration file's YAML frontmatter.

    Returns ``None`` (logging a warning) for a malformed file, mirroring
    ``check_engine._parse_check_file()``'s/``skill_engine.load_skill()``'s
    own graceful-skip behavior for a bad file rather than crashing the
    whole registry load.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read agent registration file %s: %s", path, exc)
        return None

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
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

    required = {"name", "category", "code_ref", "resource_tier", "description"}
    missing = required - set(meta.keys())
    if missing:
        logger.warning("Agent registration %s missing fields: %s", path, missing)
        return None

    code_ref = str(meta["code_ref"])
    if ":" not in code_ref:
        logger.warning(
            "Agent registration %s has malformed code_ref %r (expected 'module:ClassName')",
            path, code_ref,
        )
        return None

    return meta


def load_agent_classes(agents_dir: Path | None = None) -> dict[str, tuple[str, str, str, str]]:
    """Build the ``AGENT_CLASSES`` registry from ``agents/*.md`` Markdown
    registration files instead of a hardcoded dict literal -- Phase 2 of
    docs/extension-model-unification-plan-2026-07-18.md.

    Each file's ``code_ref`` (a ``module:ClassName`` string) is only
    *recorded* here, not imported -- ``get_agent_class()`` below still does
    the actual lazy import, at the exact same call site it always has, so
    nothing changes about *when* an agent's real Python class is loaded,
    only *where its registration metadata comes from*. A new agent is now
    a `git add agents/<name>.md` away instead of a Python dict-literal
    edit; retiring one is a `git rm`.
    """
    resolved = agents_dir if agents_dir is not None else _agents_dir()
    result: dict[str, tuple[str, str, str, str]] = {}
    if not resolved.is_dir():
        logger.warning(
            "Agent registration directory %s not found -- AGENT_CLASSES will be empty", resolved,
        )
        return result
    for path in sorted(resolved.glob("*.md")):
        meta = _parse_agent_registration(path)
        if meta is None:
            continue
        module_path, _, class_name = str(meta["code_ref"]).rpartition(":")
        result[str(meta["name"])] = (
            str(meta["category"]), module_path, class_name, str(meta["resource_tier"]),
        )
    return result


AGENT_CLASSES: dict[str, tuple[str, str, str, str]] = load_agent_classes()


AGENT_DISPLAY_NAMES: dict[str, str] = {
    "cost": "Cost Optimization",
    "dependency": "Dependency",
    "codechange": "Code Change",
}

WATCHER_AGENTS: list[dict[str, str]] = [
    {"name": "vuln-watcher", "mode": "Kafka consumer + polling", "interval": "6 hours", "description": "Monitors fleet for critical/high findings and raises an alert for each one"},
    {"name": "slo-tracker", "mode": "Polling", "interval": "5 minutes", "description": "Checks SLO status across all assessments, publishes breach alerts, recommends rollbacks"},
    {"name": "drift-detector", "mode": "Argo CD polling", "interval": "10 minutes", "description": "Queries Argo CD apps for OutOfSync state and auto-syncs them back to the Git-declared state"},
    {"name": "skill-learner", "mode": "LLM polling", "interval": "24 hours", "description": "Researches recent CVEs via LLM and drafts new skills (status: draft) for human review — requires an LLM connection"},
    {"name": "capability-scout", "mode": "LLM polling", "interval": "24 hours", "description": "Reads fleet usage/effectiveness data and doc-gap signals, proposes one small change to AgentIT itself as a draft PR for human review — requires an LLM connection and GITHUB_TOKEN"},
    {"name": "reassess-scheduler", "mode": "Polling", "interval": "1 hour", "description": "Checks every app's configured re-assessment cadence (daily/weekly/monthly, set on its Assessment Detail page) and automatically re-Assesses any app that's due, via the same route the manual Scan button uses"},
    {"name": "self-health-check", "mode": "Kube + GitHub API polling", "interval": "15 minutes", "description": "Verifies AgentIT's own critical infrastructure end to end -- GitHub webhook delivery health, CI pipeline stall detection, maintenance CronJob success, and cleanup-CronJob effectiveness -- publishing pass/fail events surfaced on the Health page's Self-Health panel and the sitewide Events badge"},
]


def get_onboarding_agents() -> list[dict[str, str]]:
    return [
        {"name": AGENT_DISPLAY_NAMES[cat], "generates": AGENT_CAPABILITIES[cat], "category": cat}
        for cat in AGENT_CLASSES
    ]


def get_agent_class(name: str):
    """Lazy-import and return the agent class for the given name."""
    import importlib
    if name not in AGENT_CLASSES:
        raise ValueError(f"Unknown agent: {name}")
    _cat, module_path, class_name, _tier = AGENT_CLASSES[name]
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)
