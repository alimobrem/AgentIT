"""Analyze code changes and determine which agents need to re-run."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CHANGE_TO_AGENT: dict[str, list[str]] = {
    # File patterns -> agents that should re-run
    "Dockerfile": ["security"],
    "Containerfile": ["security"],
    "docker-compose": ["infrastructure"],
    "requirements.txt": ["security", "compliance"],
    "package.json": ["security", "compliance"],
    "go.mod": ["security", "compliance"],
    "pom.xml": ["security", "compliance"],
    "build.gradle": ["security", "compliance"],
    "Gemfile": ["security", "compliance"],
    ".env": ["security"],
    "values.yaml": ["infrastructure"],
    "Chart.yaml": ["infrastructure"],
    "deployment.yaml": ["infrastructure", "security"],
    "service.yaml": ["infrastructure", "observability"],
    "ingress": ["security", "infrastructure"],
    "networkpolicy": ["security"],
    "rbac": ["security"],
    "prometheus": ["observability"],
    "grafana": ["observability"],
    "alerting": ["observability"],
    "pipeline": ["cicd"],
    "tekton": ["cicd"],
    ".github/workflows": ["cicd"],
    "Makefile": ["cicd"],
    "test": ["cicd"],
    "migration": ["compliance"],
    "schema": ["compliance"],
    "openapi": ["compliance"],
    "swagger": ["compliance"],
    "license": ["compliance"],
}


@dataclass
class ChangeImpact:
    changed_files: list[str]
    agents_to_rerun: list[str] = field(default_factory=list)
    new_services: list[str] = field(default_factory=list)
    dependency_changes: bool = False
    config_changes: bool = False
    infra_changes: bool = False
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"{len(self.changed_files)} files changed"]
        if self.agents_to_rerun:
            parts.append(f"agents to re-run: {', '.join(self.agents_to_rerun)}")
        if self.new_services:
            parts.append(f"new services detected: {', '.join(self.new_services)}")
        return "; ".join(parts)


def analyze_changes(changed_files: list[str], added_files: list[str] | None = None) -> ChangeImpact:
    """Determine which agents need to re-run based on changed files."""
    agents: set[str] = set()
    reasons: dict[str, list[str]] = {}
    dependency_changes = False
    config_changes = False
    infra_changes = False
    new_services: list[str] = []

    dep_files = {"requirements.txt", "package.json", "go.mod", "pom.xml",
                 "build.gradle", "Gemfile", "Gemfile.lock", "package-lock.json",
                 "go.sum", "poetry.lock", "Pipfile.lock", "yarn.lock"}
    config_files = {".env", "config.yaml", "config.json", "application.properties",
                    "application.yml", "settings.py", "appsettings.json"}
    infra_files = {"Dockerfile", "Containerfile", "docker-compose.yml",
                   "docker-compose.yaml", "values.yaml", "Chart.yaml"}

    for f in changed_files:
        fname = f.rsplit("/", 1)[-1] if "/" in f else f
        flow = f.lower()

        if fname in dep_files:
            dependency_changes = True
        if fname in config_files:
            config_changes = True
        if fname in infra_files:
            infra_changes = True

        for pattern, agent_list in CHANGE_TO_AGENT.items():
            if pattern.lower() in flow:
                for agent in agent_list:
                    agents.add(agent)
                    reasons.setdefault(agent, []).append(f"{f} matches '{pattern}'")

    # Detect new services (new Dockerfile or docker-compose service)
    if added_files:
        for f in added_files:
            fname = f.rsplit("/", 1)[-1] if "/" in f else f
            if fname in ("Dockerfile", "Containerfile"):
                svc_dir = f.rsplit("/", 1)[0] if "/" in f else "root"
                new_services.append(svc_dir)
                agents.update(["security", "observability", "infrastructure", "cicd"])
                reasons.setdefault("security", []).append(f"new service in {svc_dir}")

    # If nothing matched but files changed, at least re-check security
    if not agents and changed_files:
        agents.add("security")

    return ChangeImpact(
        changed_files=changed_files,
        agents_to_rerun=sorted(agents),
        new_services=new_services,
        dependency_changes=dependency_changes,
        config_changes=config_changes,
        infra_changes=infra_changes,
        reasons=reasons,
    )
