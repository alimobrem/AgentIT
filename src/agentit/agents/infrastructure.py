from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel

from agentit.agents.base import GeneratedFile, _sanitize_name, validate_manifest
from agentit.models import AssessmentReport

logger = logging.getLogger(__name__)

# ResourceQuota limits keyed by criticality
_QUOTA_LIMITS: dict[str, dict[str, str | int]] = {
    "low": {"cpu": "4", "memory": "8Gi", "pods": "10"},
    "medium": {"cpu": "8", "memory": "16Gi", "pods": "20"},
    "high": {"cpu": "16", "memory": "32Gi", "pods": "50"},
    "critical": {"cpu": "16", "memory": "32Gi", "pods": "50"},
}


class InfrastructureResult(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.files)
        self.summary = (
            f"Generated {count} infrastructure manifest{'s' if count != 1 else ''}."
        )


class InfrastructureAgent:
    """Generates infrastructure manifests (HPA, PDB, ResourceQuota, LimitRange, Namespace)."""

    def __init__(self, report: AssessmentReport, output_dir: Path) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)

    def run(self) -> InfrastructureResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[GeneratedFile] = []

        generated.extend(self._generate_hpa())
        generated.extend(self._generate_pdb())
        generated.extend(self._generate_resourcequota())
        generated.extend(self._generate_limitrange())
        generated.extend(self._generate_namespace())

        return InfrastructureResult(files=generated)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _findings_for(self, *categories: str) -> list[str]:
        hits: list[str] = []
        for score in self.report.scores:
            for f in score.findings:
                if any(kw in f.category.lower() for kw in categories):
                    hits.append(f.description)
        return hits

    def _write(self, filename: str, content: str) -> None:
        (self.output_dir / filename).write_text(content)

    def _validate_and_warn(self, filename: str, content: str) -> None:
        errors = validate_manifest(content)
        for e in errors:
            logger.warning("Manifest validation %s: %s", filename, e)

    # ------------------------------------------------------------------
    # generators
    # ------------------------------------------------------------------

    def _generate_hpa(self) -> list[GeneratedFile]:
        name = self._name
        doc: dict = {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {
                "name": f"{name}-hpa",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": name,
                },
                "minReplicas": 2,
                "maxReplicas": 10,
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "cpu",
                            "target": {
                                "type": "Utilization",
                                "averageUtilization": 80,
                            },
                        },
                    },
                ],
            },
        }
        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._validate_and_warn("hpa.yaml", content)
        self._write("hpa.yaml", content)

        return [
            GeneratedFile(
                path="hpa.yaml",
                content=content,
                description="HorizontalPodAutoscaler: min 2, max 10 replicas, CPU target 80%.",
                finding_addressed="Autoscaling baseline for production workloads.",
            ),
        ]

    def _generate_pdb(self) -> list[GeneratedFile]:
        name = self._name
        doc: dict = {
            "apiVersion": "policy/v1",
            "kind": "PodDisruptionBudget",
            "metadata": {
                "name": f"{name}-pdb",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "minAvailable": 1,
                "selector": {
                    "matchLabels": {"app.kubernetes.io/name": name},
                },
            },
        }
        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._validate_and_warn("pdb.yaml", content)
        self._write("pdb.yaml", content)

        return [
            GeneratedFile(
                path="pdb.yaml",
                content=content,
                description="PodDisruptionBudget: minAvailable 1 to survive voluntary disruptions.",
                finding_addressed="Availability baseline during node maintenance and rollouts.",
            ),
        ]

    def _generate_resourcequota(self) -> list[GeneratedFile]:
        name = self._name
        limits = _QUOTA_LIMITS.get(self.report.criticality, _QUOTA_LIMITS["medium"])

        doc: dict = {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {
                "name": f"{name}-quota",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "hard": {
                    "limits.cpu": limits["cpu"],
                    "limits.memory": limits["memory"],
                    "pods": str(limits["pods"]),
                },
            },
        }
        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._validate_and_warn("resourcequota.yaml", content)
        self._write("resourcequota.yaml", content)

        return [
            GeneratedFile(
                path="resourcequota.yaml",
                content=content,
                description=(
                    f"ResourceQuota for {self.report.criticality} criticality: "
                    f"{limits['cpu']} CPU, {limits['memory']} memory, {limits['pods']} pods."
                ),
                finding_addressed="Namespace resource governance based on app criticality.",
            ),
        ]

    def _generate_limitrange(self) -> list[GeneratedFile]:
        name = self._name
        doc: dict = {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {
                "name": f"{name}-limits",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "limits": [
                    {
                        "type": "Container",
                        "default": {"cpu": "200m", "memory": "256Mi"},
                        "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
                    },
                ],
            },
        }
        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._validate_and_warn("limitrange.yaml", content)
        self._write("limitrange.yaml", content)

        return [
            GeneratedFile(
                path="limitrange.yaml",
                content=content,
                description="LimitRange: default container 200m CPU, 256Mi memory.",
                finding_addressed="Default container resource constraints to prevent unbounded usage.",
            ),
        ]

    def _generate_namespace(self) -> list[GeneratedFile]:
        name = self._name
        doc: dict = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": name,
                "labels": {
                    "app.kubernetes.io/name": name,
                    "app.kubernetes.io/managed-by": "agentit",
                    "agentit/criticality": self.report.criticality,
                },
            },
        }
        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._validate_and_warn("namespace.yaml", content)
        self._write("namespace.yaml", content)

        return [
            GeneratedFile(
                path="namespace.yaml",
                content=content,
                description=f"Namespace '{name}' with standard labels and criticality annotation.",
                finding_addressed="Namespace isolation with standard labeling.",
            ),
        ]
