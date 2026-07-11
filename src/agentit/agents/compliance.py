from __future__ import annotations

import textwrap
from pathlib import Path

import yaml
from pydantic import BaseModel

from agentit.agents.base import GeneratedFile, _sanitize_name
from agentit.models import AssessmentReport, Severity


class ComplianceResult(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.files)
        self.summary = (
            f"Generated {count} compliance manifest{'s' if count != 1 else ''}."
        )


class ComplianceAgent:
    def __init__(self, report: AssessmentReport, output_dir: Path) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)

    def run(self) -> ComplianceResult:
        """Generate compliance and policy manifests based on assessment findings."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[GeneratedFile] = []

        generated.extend(self._generate_kyverno_policies())
        generated.extend(self._generate_sbom_task())
        generated.extend(self._generate_compliance_evidence())
        generated.extend(self._generate_audit_policy())
        generated.extend(self._generate_compliance_cronjob())

        return ComplianceResult(files=generated)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _findings_for(self, *categories: str) -> list[str]:
        """Return descriptions of findings whose category contains any keyword."""
        hits: list[str] = []
        for score in self.report.scores:
            for f in score.findings:
                if any(kw in f.category.lower() for kw in categories):
                    hits.append(f.description)
        return hits

    def _write(self, filename: str, content: str) -> None:
        (self.output_dir / filename).write_text(content)

    # ------------------------------------------------------------------
    # generators
    # ------------------------------------------------------------------

    def _generate_kyverno_policies(self) -> list[GeneratedFile]:
        hits = self._findings_for("policy", "compliance", "label", "image", "registry")
        if not hits:
            return []

        docs: list[dict] = [
            {
                "apiVersion": "kyverno.io/v1",
                "kind": "ClusterPolicy",
                "metadata": {"name": "require-labels"},
                "spec": {
                    "validationFailureAction": "Enforce",
                    "rules": [
                        {
                            "name": "check-required-labels",
                            "match": {"any": [{"resources": {"kinds": ["Pod"]}}]},
                            "validate": {
                                "message": "Labels 'app.kubernetes.io/name' and 'app.kubernetes.io/version' are required.",
                                "pattern": {
                                    "metadata": {
                                        "labels": {
                                            "app.kubernetes.io/name": "?*",
                                            "app.kubernetes.io/version": "?*",
                                        },
                                    },
                                },
                            },
                        },
                    ],
                },
            },
            {
                "apiVersion": "kyverno.io/v1",
                "kind": "ClusterPolicy",
                "metadata": {"name": "require-resource-limits"},
                "spec": {
                    "validationFailureAction": "Enforce",
                    "rules": [
                        {
                            "name": "check-resource-limits",
                            "match": {"any": [{"resources": {"kinds": ["Pod"]}}]},
                            "validate": {
                                "message": "All containers must have resource limits defined.",
                                "pattern": {
                                    "spec": {
                                        "containers": [
                                            {
                                                "resources": {
                                                    "limits": {
                                                        "cpu": "?*",
                                                        "memory": "?*",
                                                    },
                                                },
                                            },
                                        ],
                                    },
                                },
                            },
                        },
                    ],
                },
            },
            {
                "apiVersion": "kyverno.io/v1",
                "kind": "ClusterPolicy",
                "metadata": {"name": "restrict-image-registries"},
                "spec": {
                    "validationFailureAction": "Enforce",
                    "rules": [
                        {
                            "name": "validate-registries",
                            "match": {"any": [{"resources": {"kinds": ["Pod"]}}]},
                            "validate": {
                                "message": "Images must come from registry.access.redhat.com or quay.io.",
                                "pattern": {
                                    "spec": {
                                        "containers": [
                                            {
                                                "image": "registry.access.redhat.com/* | quay.io/*",
                                            },
                                        ],
                                    },
                                },
                            },
                        },
                    ],
                },
            },
            {
                "apiVersion": "kyverno.io/v1",
                "kind": "ClusterPolicy",
                "metadata": {"name": "disallow-latest-tag"},
                "spec": {
                    "validationFailureAction": "Enforce",
                    "rules": [
                        {
                            "name": "validate-image-tag",
                            "match": {"any": [{"resources": {"kinds": ["Pod"]}}]},
                            "validate": {
                                "message": "Using the 'latest' tag is not allowed.",
                                "pattern": {
                                    "spec": {
                                        "containers": [
                                            {"image": "!*:latest"},
                                        ],
                                    },
                                },
                            },
                        },
                    ],
                },
            },
        ]

        content = yaml.dump_all(docs, default_flow_style=False, sort_keys=False)
        self._write("kyverno-policies.yaml", content)

        return [
            GeneratedFile(
                path="kyverno-policies.yaml",
                content=content,
                description="Kyverno ClusterPolicies: require-labels, require-resource-limits, restrict-image-registries, disallow-latest-tag.",
                finding_addressed="; ".join(hits),
            ),
        ]

    def _generate_sbom_task(self) -> list[GeneratedFile]:
        hits = self._findings_for("sbom", "supply chain", "bom")
        if not hits:
            return []

        name = self._name
        task: dict = {
            "apiVersion": "tekton.dev/v1",
            "kind": "Task",
            "metadata": {
                "name": f"{name}-sbom-generate",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "params": [
                    {"name": "IMAGE", "type": "string", "description": "Image reference to scan"},
                ],
                "workspaces": [
                    {"name": "source"},
                    {"name": "sbom-output"},
                ],
                "steps": [
                    {
                        "name": "generate-sbom",
                        "image": "anchore/syft:latest",
                        "script": (
                            "#!/usr/bin/env sh\n"
                            "set -e\n"
                            'syft "$(params.IMAGE)" -o cyclonedx-json=/workspace/sbom-output/sbom.cdx.json\n'
                            'echo "SBOM generated for $(params.IMAGE)"\n'
                        ),
                    },
                ],
            },
        }

        content = yaml.dump(task, default_flow_style=False, sort_keys=False)
        self._write("sbom-generate-task.yaml", content)

        return [
            GeneratedFile(
                path="sbom-generate-task.yaml",
                content=content,
                description=f"Tekton Task to generate CycloneDX SBOM for {name} using syft.",
                finding_addressed="; ".join(hits),
            ),
        ]

    def _generate_compliance_evidence(self) -> list[GeneratedFile]:
        """Always generated — maps assessment findings to compliance controls."""
        sections = {
            "Security Controls": ["security", "container", "network", "vulnerability"],
            "Access Controls": ["rbac", "auth", "access", "iam"],
            "Audit Logging": ["audit", "log", "monitor"],
            "Data Protection": ["encrypt", "secret", "data", "tls", "certificate"],
        }

        lines: list[str] = [
            f"# Compliance Evidence Report",
            f"",
            f"**Repository:** {self.report.repo_name}",
            f"**Assessed:** {self.report.assessed_at.isoformat()}",
            f"**Overall Score:** {self.report.overall_score:.1f}",
            f"",
        ]

        for section_title, keywords in sections.items():
            lines.append(f"## {section_title}")
            lines.append("")

            findings_in_section = []
            for score in self.report.scores:
                for f in score.findings:
                    if any(kw in f.category.lower() for kw in keywords):
                        findings_in_section.append(f)

            if findings_in_section:
                for f in findings_in_section:
                    status = "fail" if f.severity <= Severity.high else "partial"
                    lines.append(f"### {f.category}")
                    lines.append(f"")
                    lines.append(f"- **Status:** {status}")
                    lines.append(f"- **Severity:** {f.severity.name}")
                    lines.append(f"- **Evidence:** {f.description}")
                    lines.append(f"- **Recommendation:** {f.recommendation}")
                    lines.append(f"")
            else:
                lines.append(f"### No findings")
                lines.append(f"")
                lines.append(f"- **Status:** pass")
                lines.append(f"- **Evidence:** No issues detected in this area.")
                lines.append(f"")

        content = "\n".join(lines)
        self._write("compliance-evidence.md", content)

        return [
            GeneratedFile(
                path="compliance-evidence.md",
                content=content,
                description="Compliance evidence document mapping findings to security controls.",
                finding_addressed="Compliance evidence baseline.",
            ),
        ]

    def _generate_audit_policy(self) -> list[GeneratedFile]:
        hits = self._findings_for("audit", "log")
        if not hits:
            return []

        doc = {
            "apiVersion": "audit.k8s.io/v1",
            "kind": "Policy",
            "rules": [
                {
                    "level": "RequestResponse",
                    "verbs": ["create", "update", "patch", "delete"],
                    "resources": [
                        {"group": "", "resources": ["pods", "services", "configmaps", "secrets"]},
                        {"group": "apps", "resources": ["deployments", "statefulsets", "daemonsets"]},
                        {"group": "rbac.authorization.k8s.io", "resources": ["roles", "rolebindings", "clusterroles", "clusterrolebindings"]},
                    ],
                },
                {
                    "level": "Metadata",
                    "verbs": ["get", "list", "watch"],
                    "resources": [
                        {"group": "", "resources": ["secrets"]},
                    ],
                },
                {
                    "level": "None",
                    "users": ["system:kube-proxy"],
                    "verbs": ["watch"],
                    "resources": [
                        {"group": "", "resources": ["endpoints", "services", "services/status"]},
                    ],
                },
            ],
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("audit-policy.yaml", content)

        return [
            GeneratedFile(
                path="audit-policy.yaml",
                content=content,
                description="Kubernetes audit policy logging write operations on critical resources.",
                finding_addressed="; ".join(hits),
            ),
        ]

    def _generate_compliance_cronjob(self) -> list[GeneratedFile]:
        from agentit.agents.base import make_cronjob

        name = self._name
        doc = make_cronjob(
            f"{name}-compliance-reassess",
            "0 3 1 * *",
            ["agentit", "assess", "--rescan"],
        )

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("compliance-cronjob.yaml", content)

        return [
            GeneratedFile(
                path="compliance-cronjob.yaml",
                content=content,
                description="CronJob: monthly compliance re-assessment (1st of month, 3am UTC).",
                finding_addressed="Continuous compliance posture — periodic re-evaluation.",
            ),
        ]
