from __future__ import annotations

import textwrap
from pathlib import Path

import yaml
from pydantic import BaseModel

from agentit.models import AssessmentReport


class GeneratedFile(BaseModel):
    path: str
    content: str
    description: str


class IncidentResult(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.files)
        self.summary = (
            f"Generated {count} incident response artifact{'s' if count != 1 else ''}."
        )


def _sanitize_name(name: str) -> str:
    """Turn a repo name into a k8s-safe DNS label."""
    sanitized = name.lower().replace("_", "-").replace(".", "-")[:63]
    return sanitized.strip("-") or "app"


_URGENCY_MAP = {
    "critical": "high",
    "high": "high",
    "medium": "low",
    "low": "low",
}


class IncidentAgent:
    def __init__(self, report: AssessmentReport, output_dir: Path) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)

    def run(self) -> IncidentResult:
        """Generate all incident response artifacts based on assessment."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[GeneratedFile] = []

        generated.append(self._generate_runbook())
        generated.append(self._generate_pagerduty_config())
        generated.append(self._generate_alertmanager_config())

        return IncidentResult(files=generated)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _detected_databases(self) -> list[str]:
        return [db.name.lower() for db in self.report.stack.databases]

    def _stack_names(self) -> list[str]:
        names: list[str] = []
        for lang in self.report.stack.languages:
            names.append(lang.name.lower())
        for fw in self.report.stack.frameworks:
            names.append(fw.name.lower())
        for db in self.report.stack.databases:
            names.append(db.name.lower())
        for rt in self.report.stack.runtimes:
            names.append(rt.name.lower())
        return names

    def _write(self, filename: str, content: str) -> None:
        (self.output_dir / filename).write_text(content)

    # ------------------------------------------------------------------
    # generators
    # ------------------------------------------------------------------

    def _generate_runbook(self) -> GeneratedFile:
        name = self._name
        stack = self._stack_names()
        databases = self._detected_databases()

        sections: list[str] = []
        sections.append(f"# Incident Response Runbook — {name}\n")

        # Triage
        sections.append("## Triage Steps\n")
        sections.append("1. Confirm the alert source and affected service.")
        sections.append(f"2. Verify pod status: `kubectl get pods -l app={name}`")
        sections.append(f"3. Check recent logs: `kubectl logs -l app={name} --tail=100`")

        for db in databases:
            if "postgres" in db:
                sections.append(
                    f"4. Check PostgreSQL connections: "
                    f"`kubectl exec -it <pod> -- pg_isready`"
                )
            if "redis" in db:
                sections.append(
                    f"4. Check Redis connectivity: "
                    f"`kubectl exec -it <pod> -- redis-cli ping`"
                )
            if "mongo" in db:
                sections.append(
                    f"4. Check MongoDB status: "
                    f"`kubectl exec -it <pod> -- mongosh --eval 'db.runCommand({{ping:1}})'`"
                )

        # Common failure modes
        sections.append("\n## Common Failure Modes\n")
        sections.append("| Mode | Symptom | Likely Cause |")
        sections.append("|------|---------|--------------|")
        sections.append("| OOMKilled | Pod restarts, exit code 137 | Memory limit too low or leak |")
        sections.append("| CrashLoopBackOff | Repeated restarts | Missing config, bad image, startup error |")
        sections.append("| ImagePullBackOff | Pod stuck Pending | Bad image ref or missing pull secret |")

        for db in databases:
            if "postgres" in db:
                sections.append(
                    "| DB connection refused | App errors on startup | "
                    "PostgreSQL down or connection limit reached |"
                )
            if "redis" in db:
                sections.append(
                    "| Cache miss spike | Elevated latency | "
                    "Redis pod evicted or memory exhausted |"
                )

        # Escalation
        sections.append("\n## Escalation Contacts\n")
        sections.append("| Role | Contact |")
        sections.append("|------|---------|")
        sections.append("| On-call engineer | `TODO` |")
        sections.append("| Service owner | `TODO` |")
        sections.append("| Platform team | `TODO` |")

        # Recovery
        sections.append("\n## Recovery Procedures\n")
        sections.append(f"1. Rolling restart: `kubectl rollout restart deployment/{name}`")
        sections.append(f"2. Scale up: `kubectl scale deployment/{name} --replicas=3`")
        sections.append(f"3. Rollback: `kubectl rollout undo deployment/{name}`")

        for db in databases:
            if "postgres" in db:
                sections.append(
                    "4. Reset PostgreSQL connections: restart the database pod "
                    "or increase `max_connections`."
                )

        content = "\n".join(sections) + "\n"
        self._write("runbook.md", content)

        return GeneratedFile(
            path="runbook.md",
            content=content,
            description=f"Incident response runbook for {name}.",
        )

    def _generate_pagerduty_config(self) -> GeneratedFile:
        name = self._name
        criticality = self.report.criticality.lower()
        urgency = _URGENCY_MAP.get(criticality, "low")

        escalation_timeout = 300 if urgency == "high" else 900

        doc = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{name}-pagerduty",
                "namespace": "default",
                "labels": {"app.kubernetes.io/name": name},
            },
            "data": {
                "service-name": name,
                "urgency": urgency,
                "escalation-timeout-seconds": str(escalation_timeout),
                "integration-key": "PLACEHOLDER",
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("pagerduty-service.yaml", content)

        return GeneratedFile(
            path="pagerduty-service.yaml",
            content=content,
            description=f"PagerDuty service ConfigMap for {name} (urgency={urgency}).",
        )

    def _generate_alertmanager_config(self) -> GeneratedFile:
        name = self._name

        doc = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{name}-alertmanager-routes",
                "namespace": "default",
                "labels": {"app.kubernetes.io/name": name},
            },
            "data": {
                "routes.yaml": yaml.dump(
                    {
                        "route": {
                            "receiver": "default",
                            "group_by": ["alertname", "namespace"],
                            "group_wait": "30s",
                            "group_interval": "5m",
                            "repeat_interval": "4h",
                            "routes": [
                                {
                                    "match": {"app": name, "severity": "critical"},
                                    "receiver": "pagerduty",
                                },
                                {
                                    "match": {"app": name, "severity": "high"},
                                    "receiver": "slack",
                                },
                                {
                                    "match": {"app": name, "severity": "medium"},
                                    "receiver": "email",
                                },
                            ],
                        },
                    },
                    default_flow_style=False,
                    sort_keys=False,
                ),
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("alertmanager-config.yaml", content)

        return GeneratedFile(
            path="alertmanager-config.yaml",
            content=content,
            description=f"AlertManager route ConfigMap for {name}.",
        )
