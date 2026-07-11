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
        generated.extend(self._generate_sbom_script())
        generated.extend(self._generate_compliance_evidence())
        generated.extend(self._generate_audit_policy())
        generated.extend(self._generate_compliance_cronworkflow())

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

    def _generate_sbom_script(self) -> list[GeneratedFile]:
        hits = self._findings_for("sbom", "supply chain", "bom")
        if not hits:
            return []

        content = textwrap.dedent("""\
            #!/usr/bin/env bash
            set -euo pipefail

            # Generate SBOM in CycloneDX format using syft
            # Usage: ./generate-sbom.sh <image-or-path> [output-dir]

            TARGET="${1:-.}"
            OUTPUT_DIR="${2:-./sbom-output}"

            mkdir -p "${OUTPUT_DIR}"

            TIMESTAMP=$(date -u +%Y%m%d%H%M%S)
            OUTPUT_FILE="${OUTPUT_DIR}/sbom-${TIMESTAMP}.cdx.json"

            echo "Generating SBOM for: ${TARGET}"
            echo "Output: ${OUTPUT_FILE}"

            if ! command -v syft &>/dev/null; then
                echo "ERROR: syft is not installed. Install from https://github.com/anchore/syft" >&2
                exit 1
            fi

            syft "${TARGET}" -o cyclonedx-json="${OUTPUT_FILE}"

            echo "SBOM generated: ${OUTPUT_FILE}"
        """)
        self._write("generate-sbom.sh", content)

        return [
            GeneratedFile(
                path="generate-sbom.sh",
                content=content,
                description="Shell script to generate CycloneDX SBOM using syft.",
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

    def _generate_compliance_cronworkflow(self) -> list[GeneratedFile]:
        name = self._name
        doc: dict = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "CronWorkflow",
            "metadata": {
                "name": f"{name}-compliance-reassess",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "schedule": "0 3 1 * *",
                "timezone": "UTC",
                "concurrencyPolicy": "Forbid",
                "successfulJobsHistoryLimit": 3,
                "failedJobsHistoryLimit": 3,
                "workflowSpec": {
                    "entrypoint": "compliance-check",
                    "templates": [
                        {
                            "name": "compliance-check",
                            "container": {
                                "image": "REPLACE_WITH_AGENTIT_IMAGE",
                                "command": ["agentit"],
                                "args": ["assess", "--rescan"],
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "256Mi"},
                                    "limits": {"cpu": "500m", "memory": "512Mi"},
                                },
                            },
                        },
                    ],
                },
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("compliance-cronworkflow.yaml", content)

        return [
            GeneratedFile(
                path="compliance-cronworkflow.yaml",
                content=content,
                description="CronWorkflow: monthly compliance re-assessment (1st of month, 3am UTC).",
                finding_addressed="Continuous compliance posture — periodic re-evaluation.",
            ),
        ]
