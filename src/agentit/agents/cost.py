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
    finding_addressed: str


class CostResult(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.files)
        self.summary = (
            f"Generated {count} cost optimization artifact{'s' if count != 1 else ''}."
        )


def _sanitize_name(name: str) -> str:
    """Turn a repo name into a k8s-safe DNS label."""
    sanitized = name.lower().replace("_", "-").replace(".", "-")[:63]
    return sanitized.strip("-") or "app"


# -- cost estimation tables ------------------------------------------------

_RESOURCE_PROFILES: dict[str, dict[str, str]] = {
    "small": {"cpu": "250m", "memory": "256Mi"},
    "medium": {"cpu": "500m", "memory": "512Mi"},
    "large": {"cpu": "1000m", "memory": "1Gi"},
}

_MONTHLY_COST: dict[str, str] = {
    "small": "$15-30",
    "medium": "$30-80",
    "large": "$80-200",
}

_REPLICA_DEFAULTS: dict[str, int] = {
    "small": 1,
    "medium": 2,
    "large": 3,
}


class CostOptimizationAgent:
    def __init__(self, report: AssessmentReport, output_dir: Path) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)

    @property
    def _is_critical(self) -> bool:
        return self.report.criticality.lower() in ("critical", "high")

    def _tier(self) -> str:
        """Estimate deployment tier from service count and stack complexity."""
        svc = self.report.architecture.service_count
        lang_count = len(self.report.stack.languages)
        if svc >= 5 or lang_count >= 3:
            return "large"
        if svc >= 2 or lang_count >= 2:
            return "medium"
        return "small"

    def _primary_language(self) -> str:
        if not self.report.stack.languages:
            return "unknown"
        top = max(self.report.stack.languages, key=lambda l: l.percentage)
        return top.name.lower()

    def _write(self, filename: str, content: str) -> None:
        (self.output_dir / filename).write_text(content)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def run(self) -> CostResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[GeneratedFile] = []

        generated.append(self._generate_cost_report())
        generated.append(self._generate_vpa())
        generated.append(self._generate_cost_labels())

        return CostResult(files=generated)

    # ------------------------------------------------------------------
    # generators
    # ------------------------------------------------------------------

    def _generate_cost_report(self) -> GeneratedFile:
        tier = self._tier()
        lang = self._primary_language()
        profile = _RESOURCE_PROFILES[tier]
        cost = _MONTHLY_COST[tier]
        replicas = _REPLICA_DEFAULTS[tier]

        databases = ", ".join(d.name for d in self.report.stack.databases) or "none detected"
        frameworks = ", ".join(f.name for f in self.report.stack.frameworks) or "none detected"

        content = textwrap.dedent(f"""\
            # Cost Optimization Report: {self.report.repo_name}

            ## Detected Stack

            - **Primary language:** {lang}
            - **Frameworks:** {frameworks}
            - **Databases:** {databases}
            - **Architecture:** {self.report.architecture.architecture_style}
            - **Service count:** {self.report.architecture.service_count}

            ## Resource Right-Sizing

            Estimated deployment tier: **{tier}**

            | Resource | Recommended |
            |----------|-------------|
            | CPU request | {profile["cpu"]} |
            | Memory request | {profile["memory"]} |
            | Replicas | {replicas} |

            ## Idle Resource Detection

            - Review workloads with CPU utilization below 10% over 7 days.
            - Check for PVCs not mounted to any running pod.
            - Identify Services with zero endpoint traffic.

            ## Reserved Capacity Suggestions

            - Criticality: **{self.report.criticality}**
            - For sustained workloads, commit to 1-year reserved instances to save 30-40%.
            - For variable workloads, use spot/preemptible nodes for non-critical tiers.

            ## Estimated Monthly Cost

            | Size | Replicas | Estimate |
            |------|----------|----------|
            | small | {_REPLICA_DEFAULTS["small"]} | {_MONTHLY_COST["small"]} |
            | medium | {_REPLICA_DEFAULTS["medium"]} | {_MONTHLY_COST["medium"]} |
            | large | {_REPLICA_DEFAULTS["large"]} | {_MONTHLY_COST["large"]} |

            **Selected tier ({tier}):** {cost}/month per replica x {replicas} replicas
        """)

        self._write("cost-report.md", content)
        return GeneratedFile(
            path="cost-report.md",
            content=content,
            description=f"Cost optimization report for {self.report.repo_name} ({tier} tier).",
            finding_addressed="Resource right-sizing, idle detection, reserved capacity guidance.",
        )

    def _generate_vpa(self) -> GeneratedFile:
        name = self._name
        tier = self._tier()
        profile = _RESOURCE_PROFILES[tier]
        update_mode = "Off" if self._is_critical else "Auto"

        doc: dict = {
            "apiVersion": "autoscaling.k8s.io/v1",
            "kind": "VerticalPodAutoscaler",
            "metadata": {
                "name": f"{name}-vpa",
                "namespace": "default",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "targetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": name,
                },
                "updatePolicy": {
                    "updateMode": update_mode,
                },
                "resourcePolicy": {
                    "containerPolicies": [
                        {
                            "containerName": name,
                            "minAllowed": {
                                "cpu": "50m",
                                "memory": "64Mi",
                            },
                            "maxAllowed": {
                                "cpu": profile["cpu"],
                                "memory": profile["memory"],
                            },
                        },
                    ],
                },
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("resource-recommendations.yaml", content)

        return GeneratedFile(
            path="resource-recommendations.yaml",
            content=content,
            description=f"VPA for {name} (updateMode: {update_mode}).",
            finding_addressed="Automated resource right-sizing via VerticalPodAutoscaler.",
        )

    def _generate_cost_labels(self) -> GeneratedFile:
        name = self._name
        env = "production" if self._is_critical else "development"

        doc: dict = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{name}-cost-labels",
                "namespace": "default",
                "labels": {"app.kubernetes.io/name": name},
            },
            "data": {
                "cost-center": "engineering",
                "team": name,
                "environment": env,
                "app-tier": self._tier(),
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("cost-labels.yaml", content)

        return GeneratedFile(
            path="cost-labels.yaml",
            content=content,
            description="Recommended cost-attribution labels as a ConfigMap.",
            finding_addressed="Cost attribution via standardized labels.",
        )
