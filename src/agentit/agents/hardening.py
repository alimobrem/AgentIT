from __future__ import annotations

import textwrap
from pathlib import Path

import yaml
from pydantic import BaseModel

from agentit.agents.base import GeneratedFile, _sanitize_name
from agentit.models import AssessmentReport, Severity


class HardeningResult(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.files)
        self.summary = (
            f"Generated {count} hardening manifest{'s' if count != 1 else ''}."
        )


class HardeningAgent:
    def __init__(self, report: AssessmentReport, output_dir: Path) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)

    def run(self) -> HardeningResult:
        """Generate all hardening manifests based on assessment findings."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[GeneratedFile] = []

        generated.extend(self._generate_network_policy())
        generated.extend(self._generate_containerfile())
        generated.extend(self._generate_rbac())
        generated.extend(self._generate_security_context())
        generated.extend(self._generate_resource_limits())

        return HardeningResult(files=generated)

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

    def _primary_language(self) -> str:
        if not self.report.stack.languages:
            return "unknown"
        top = max(self.report.stack.languages, key=lambda l: l.percentage)
        return top.name.lower()

    def _write(self, filename: str, content: str) -> None:
        (self.output_dir / filename).write_text(content)

    # ------------------------------------------------------------------
    # generators
    # ------------------------------------------------------------------

    def _generate_network_policy(self) -> list[GeneratedFile]:
        hits = self._findings_for("network")
        if not hits:
            return []

        name = self._name
        docs: list[dict] = [
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {
                    "name": f"{name}-deny-all",
                    "namespace": "default",
                    "labels": {"app.kubernetes.io/name": name},
                },
                "spec": {
                    "podSelector": {"matchLabels": {"app": name}},
                    "policyTypes": ["Ingress", "Egress"],
                },
            },
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {
                    "name": f"{name}-allow-common",
                    "namespace": "default",
                    "labels": {"app.kubernetes.io/name": name},
                },
                "spec": {
                    "podSelector": {"matchLabels": {"app": name}},
                    "policyTypes": ["Ingress"],
                    "ingress": [
                        {
                            "ports": [
                                {"protocol": "TCP", "port": 8080},
                                {"protocol": "TCP", "port": 5432},
                                {"protocol": "TCP", "port": 6379},
                            ],
                        },
                    ],
                },
            },
        ]

        content = yaml.dump_all(docs, default_flow_style=False, sort_keys=False)
        self._write("network-policy.yaml", content)

        return [
            GeneratedFile(
                path="network-policy.yaml",
                content=content,
                description="Deny-all default NetworkPolicy plus allow rules for 8080, 5432, 6379.",
                finding_addressed="; ".join(hits),
            ),
        ]

    def _generate_containerfile(self) -> list[GeneratedFile]:
        hits = self._findings_for("container", "dockerfile")
        if not hits:
            return []

        lang = self._primary_language()
        content = self._containerfile_for(lang)
        self._write("Containerfile", content)

        return [
            GeneratedFile(
                path="Containerfile",
                content=content,
                description=f"Multi-stage Containerfile using UBI base for {lang}.",
                finding_addressed="; ".join(hits),
            ),
        ]

    def _generate_rbac(self) -> list[GeneratedFile]:
        name = self._name
        docs: list[dict] = [
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": name,
                    "namespace": "default",
                    "labels": {"app.kubernetes.io/name": name},
                },
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {
                    "name": name,
                    "namespace": "default",
                    "labels": {"app.kubernetes.io/name": name},
                },
                "rules": [
                    {
                        "apiGroups": [""],
                        "resources": ["configmaps", "secrets"],
                        "verbs": ["get", "list", "watch"],
                    },
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {
                    "name": name,
                    "namespace": "default",
                    "labels": {"app.kubernetes.io/name": name},
                },
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": name,
                },
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": name,
                        "namespace": "default",
                    },
                ],
            },
        ]

        content = yaml.dump_all(docs, default_flow_style=False, sort_keys=False)
        self._write("rbac.yaml", content)

        return [
            GeneratedFile(
                path="rbac.yaml",
                content=content,
                description="ServiceAccount, Role (read configmaps/secrets), and RoleBinding.",
                finding_addressed="RBAC baseline for least-privilege access.",
            ),
        ]

    def _generate_security_context(self) -> list[GeneratedFile]:
        name = self._name
        doc = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"{name}-security-context-patch",
                "namespace": "default",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "securityContext": {
                    "runAsNonRoot": True,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": name,
                        "image": "PLACEHOLDER",
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    },
                ],
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("security-context.yaml", content)

        return [
            GeneratedFile(
                path="security-context.yaml",
                content=content,
                description="Pod security context: non-root, read-only rootfs, drop ALL capabilities.",
                finding_addressed="Enforce container hardening baseline.",
            ),
        ]

    def _generate_resource_limits(self) -> list[GeneratedFile]:
        hits = self._findings_for("resource")
        if not hits:
            return []

        name = self._name
        docs: list[dict] = [
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {
                    "name": f"{name}-quota",
                    "namespace": "default",
                    "labels": {"app.kubernetes.io/name": name},
                },
                "spec": {
                    "hard": {
                        "requests.cpu": "2",
                        "requests.memory": "4Gi",
                        "limits.cpu": "4",
                        "limits.memory": "8Gi",
                        "pods": "20",
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "LimitRange",
                "metadata": {
                    "name": f"{name}-limits",
                    "namespace": "default",
                    "labels": {"app.kubernetes.io/name": name},
                },
                "spec": {
                    "limits": [
                        {
                            "type": "Container",
                            "default": {"cpu": "500m", "memory": "512Mi"},
                            "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
                            "max": {"cpu": "2", "memory": "2Gi"},
                        },
                    ],
                },
            },
        ]

        content = yaml.dump_all(docs, default_flow_style=False, sort_keys=False)
        self._write("resource-limits.yaml", content)

        return [
            GeneratedFile(
                path="resource-limits.yaml",
                content=content,
                description="ResourceQuota and LimitRange for namespace resource governance.",
                finding_addressed="; ".join(hits),
            ),
        ]

    # ------------------------------------------------------------------
    # Containerfile templates
    # ------------------------------------------------------------------

    @staticmethod
    def _containerfile_for(lang: str) -> str:
        if lang == "go":
            return textwrap.dedent("""\
                FROM registry.access.redhat.com/ubi9/go-toolset:latest AS builder
                WORKDIR /opt/app-root/src
                COPY go.mod go.sum ./
                RUN go mod download
                COPY . .
                RUN go build -o /opt/app-root/bin/app .

                FROM registry.access.redhat.com/ubi9/ubi-minimal:latest
                COPY --from=builder /opt/app-root/bin/app /usr/local/bin/app
                USER 1001
                ENTRYPOINT ["/usr/local/bin/app"]
            """)

        if lang == "python":
            return textwrap.dedent("""\
                FROM registry.access.redhat.com/ubi9/python-312:latest
                WORKDIR /opt/app-root/src
                COPY requirements.txt ./
                RUN pip install --no-cache-dir -r requirements.txt
                COPY . .
                USER 1001
                ENTRYPOINT ["python", "-m", "app"]
            """)

        if lang == "java":
            return textwrap.dedent("""\
                FROM registry.access.redhat.com/ubi9/openjdk-21:latest
                WORKDIR /opt/app-root/src
                COPY . .
                RUN mvn -B package -DskipTests
                USER 1001
                ENTRYPOINT ["java", "-jar", "target/app.jar"]
            """)

        if lang in ("node", "typescript", "javascript"):
            return textwrap.dedent("""\
                FROM registry.access.redhat.com/ubi9/nodejs-20:latest
                WORKDIR /opt/app-root/src
                COPY package*.json ./
                RUN npm ci --production
                COPY . .
                USER 1001
                ENTRYPOINT ["node", "index.js"]
            """)

        # default
        return textwrap.dedent("""\
            FROM registry.access.redhat.com/ubi9/ubi-minimal:latest
            WORKDIR /opt/app-root/src
            COPY . .
            USER 1001
            ENTRYPOINT ["/bin/sh"]
        """)
