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

# Cluster remediations (security, observability, cicd, compliance,
# infrastructure, cost, dependency, incident, release, retirement, chaos)
# are skill-only — their Python agents were removed once skills had full
# template-fallback parity. See docs/agent-removal-readiness.md.
# `codechange` is the sole remaining one-shot Python onboarding agent: it
# patches the application's own source repo (not K8s manifests) — optional
# source-patch path, not a peer "domain agent" to skills.
AGENT_CAPABILITIES: dict[str, str] = {
    "codechange": "Optional source patches: .gitignore, OTel, structured logging, Dockerfile/health",
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

    When the package is installed into site-packages (the container image),
    that relative climb lands under ``lib/python3.12/``, not the image
    WORKDIR. Fall back to ``Path("agents")`` (cwd = ``/opt/app-root/src``)
    the same way ``remediation/dispatcher._default_skills_dir()`` does for
    ``skills/`` -- Containerfile must ``COPY agents/ agents/``.
    """
    candidate = Path(__file__).resolve().parent.parent.parent.parent / "agents"
    return candidate if candidate.is_dir() else Path("agents")


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
    "codechange": "Code Change (source patches)",
}

def _watchers_dir() -> Path:
    """Repo-root ``watchers/`` directory holding registration Markdown
    files for the long-lived watcher agents -- the same
    default-resolution convention ``_agents_dir()`` above (and
    ``runner.py``'s ``_default_checks_dir()``/``_default_skills_dir()``)
    already use.

    Installed-package / container-image fallback matches ``_agents_dir()``
    (cwd-relative ``watchers/``); Containerfile must ``COPY watchers/``.
    Without that COPY, Schedules' Long-Lived Agents count stays 0 even
    when reassess-scheduler and peers are running.
    """
    candidate = Path(__file__).resolve().parent.parent.parent.parent / "watchers"
    return candidate if candidate.is_dir() else Path("watchers")


def _parse_watcher_registration(path: Path) -> dict | None:
    """Parse one ``watchers/*.md`` registration file's YAML frontmatter.

    Returns ``None`` (logging a warning) for a malformed file -- mirrors
    ``_parse_agent_registration()`` above. Unlike an agent registration,
    ``code_ref`` here is required to be well-formed but is otherwise
    unused by ``watchers/__init__.py``'s own registration/heartbeat
    wiring, which stays keyed on the watcher's ``name`` string, not a
    lazy import -- see ``load_watcher_agents()``'s docstring.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read watcher registration file %s: %s", path, exc)
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

    required = {"name", "mode", "interval", "description", "code_ref"}
    missing = required - set(meta.keys())
    if missing:
        logger.warning("Watcher registration %s missing fields: %s", path, missing)
        return None

    if ":" not in str(meta["code_ref"]):
        logger.warning(
            "Watcher registration %s has malformed code_ref %r (expected 'module:ClassName')",
            path, meta["code_ref"],
        )
        return None

    return meta


def load_watcher_agents(watchers_dir: Path | None = None) -> list[dict[str, str]]:
    """Build ``WATCHER_AGENTS`` from ``watchers/*.md`` registration files
    instead of a hardcoded list literal -- Phase 3 of
    docs/extension-model-unification-plan-2026-07-18.md.

    Returns the same ``[{"name", "mode", "interval", "description"}, ...]``
    shape the old list literal had (plus an additive ``code_ref`` key, for
    a human/agent that wants to find a watcher's implementing class from
    this registry alone) -- every existing consumer
    (``portal/routes/capabilities.py``, ``portal/routes/schedules.py``,
    ``agent_registry_cleanup.py``) only ever reads ``name``/``mode``/
    ``interval``/``description`` off each entry, so the extra key is
    invisible to them. ``watchers/__init__.py``'s own registration/
    heartbeat wiring (``record_tick``, ``sleep_with_heartbeat``) is
    unchanged by this -- it's keyed on each watcher's own ``name`` string
    at its call sites, never on this list -- only the *listing* of which
    watchers exist moves from Python to Markdown.
    """
    resolved = watchers_dir if watchers_dir is not None else _watchers_dir()
    result: list[dict[str, str]] = []
    if not resolved.is_dir():
        logger.warning(
            "Watcher registration directory %s not found -- WATCHER_AGENTS will be empty", resolved,
        )
        return result
    for path in sorted(resolved.glob("*.md")):
        meta = _parse_watcher_registration(path)
        if meta is None:
            continue
        result.append({
            "name": str(meta["name"]),
            "mode": str(meta["mode"]),
            "interval": str(meta["interval"]),
            "description": str(meta["description"]),
            "code_ref": str(meta["code_ref"]),
        })
    return result


WATCHER_AGENTS: list[dict[str, str]] = load_watcher_agents()


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
