from __future__ import annotations

import textwrap
from pathlib import Path

import yaml
from pydantic import BaseModel

from agentit.agents.base import GeneratedFile, _sanitize_name
from agentit.agents.hardening import HardeningAgent
from agentit.models import AssessmentReport


class CICDResult(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.files)
        self.summary = (
            f"Generated {count} CI/CD manifest{'s' if count != 1 else ''}."
        )


class CICDAgent:
    def __init__(self, report: AssessmentReport, output_dir: Path) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)

    def _image_ref(self) -> str:
        from agentit.image_builder import get_image_ref
        return get_image_ref(self.report.repo_name)

    def run(self) -> CICDResult:
        """Generate CI/CD and GitOps manifests based on assessment findings."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[GeneratedFile] = []

        generated.extend(self._generate_tekton_pipeline())
        generated.extend(self._generate_argocd_application())
        generated.extend(self._generate_argo_rollout())
        generated.extend(self._generate_containerfile())
        generated.extend(self._generate_quay_config())

        return CICDResult(files=generated)

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
        top = max(self.report.stack.languages, key=lambda lang: lang.percentage)
        return top.name.lower()

    def _write(self, filename: str, content: str) -> None:
        (self.output_dir / filename).write_text(content)

    # ------------------------------------------------------------------
    # generators
    # ------------------------------------------------------------------

    def _generate_tekton_pipeline(self) -> list[GeneratedFile]:
        hits = self._findings_for("pipeline", "ci/cd", "cicd")
        if not hits:
            return []

        name = self._name
        pipeline: dict = {
            "apiVersion": "tekton.dev/v1beta1",
            "kind": "Pipeline",
            "metadata": {
                "name": f"{name}-pipeline",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "params": [
                    {"name": "repo-url", "type": "string"},
                    {"name": "image-ref", "type": "string"},
                ],
                "workspaces": [
                    {"name": "shared-workspace"},
                ],
                "tasks": [
                    {
                        "name": "git-clone",
                        "taskRef": {"name": "git-clone", "kind": "ClusterTask"},
                        "params": [
                            {"name": "url", "value": "$(params.repo-url)"},
                        ],
                        "workspaces": [
                            {"name": "output", "workspace": "shared-workspace"},
                        ],
                    },
                    {
                        "name": "build",
                        "taskRef": {"name": "maven" if self._primary_language() == "java" else "npm", "kind": "ClusterTask"},
                        "runAfter": ["git-clone"],
                        "workspaces": [
                            {"name": "source", "workspace": "shared-workspace"},
                        ],
                    },
                    {
                        "name": "test",
                        "taskRef": {"name": "maven" if self._primary_language() == "java" else "npm", "kind": "ClusterTask"},
                        "runAfter": ["build"],
                        "workspaces": [
                            {"name": "source", "workspace": "shared-workspace"},
                        ],
                    },
                    {
                        "name": "image-build",
                        "taskRef": {"name": "buildah", "kind": "ClusterTask"},
                        "runAfter": ["test"],
                        "params": [
                            {"name": "IMAGE", "value": "$(params.image-ref)"},
                        ],
                        "workspaces": [
                            {"name": "source", "workspace": "shared-workspace"},
                        ],
                    },
                    {
                        "name": "image-push",
                        "taskRef": {"name": "buildah", "kind": "ClusterTask"},
                        "runAfter": ["image-build"],
                        "params": [
                            {"name": "IMAGE", "value": "$(params.image-ref)"},
                            {"name": "PUSH_EXTRA_ARGS", "value": "--digestfile /tmp/digest"},
                        ],
                        "workspaces": [
                            {"name": "source", "workspace": "shared-workspace"},
                        ],
                    },
                    {
                        "name": "image-scan",
                        "taskRef": {"name": f"{name}-image-scan", "kind": "Task"},
                        "runAfter": ["image-push"],
                        "params": [
                            {"name": "IMAGE", "value": "$(params.image-ref)"},
                        ],
                    },
                    {
                        "name": "sbom-generate",
                        "taskRef": {"name": f"{name}-sbom-generate", "kind": "Task"},
                        "runAfter": ["image-push"],
                        "params": [
                            {"name": "IMAGE", "value": "$(params.image-ref)"},
                        ],
                        "workspaces": [
                            {"name": "source", "workspace": "shared-workspace"},
                            {"name": "sbom-output", "workspace": "shared-workspace"},
                        ],
                    },
                    {
                        "name": "deploy",
                        "taskRef": {"name": "kubernetes-actions", "kind": "ClusterTask"},
                        "runAfter": ["image-scan", "sbom-generate"],
                        "params": [
                            {"name": "script", "value": f"kubectl rollout restart deployment/{name}"},
                        ],
                    },
                ],
            },
        }

        pipeline_run: dict = {
            "apiVersion": "tekton.dev/v1beta1",
            "kind": "PipelineRun",
            "metadata": {
                "generateName": f"{name}-pipeline-run-",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "pipelineRef": {"name": f"{name}-pipeline"},
                "params": [
                    {"name": "repo-url", "value": self.report.repo_url},
                    {"name": "image-ref", "value": self._image_ref()},
                ],
                "workspaces": [
                    {
                        "name": "shared-workspace",
                        "volumeClaimTemplate": {
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {"requests": {"storage": "1Gi"}},
                            },
                        },
                    },
                ],
            },
        }

        content = yaml.dump_all(
            [pipeline, pipeline_run], default_flow_style=False, sort_keys=False
        )
        self._write("tekton-pipeline.yaml", content)

        return [
            GeneratedFile(
                path="tekton-pipeline.yaml",
                content=content,
                description="Tekton Pipeline with git-clone, build, test, image-build, image-push, deploy tasks and PipelineRun template.",
                finding_addressed="; ".join(hits),
            ),
        ]

    def _generate_argocd_application(self) -> list[GeneratedFile]:
        hits = self._findings_for("gitops", "argo", "deployment")
        if not hits:
            return []

        name = self._name

        if self.report.infra_repo_url:
            return self._generate_applicationset(name, hits)

        doc: dict = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Application",
            "metadata": {
                "name": name,
                "namespace": "openshift-gitops",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "project": "default",
                "source": {
                    "repoURL": self.report.repo_url,
                    "targetRevision": "HEAD",
                    "path": "manifests",
                },
                "destination": {
                    "server": "https://kubernetes.default.svc",
                    "namespace": name,
                },
                "syncPolicy": {
                    "automated": {
                        "selfHeal": True,
                        "prune": True,
                    },
                    "syncOptions": [
                        "CreateNamespace=true",
                    ],
                },
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("argocd-application.yaml", content)

        return [
            GeneratedFile(
                path="argocd-application.yaml",
                content=content,
                description="Argo CD Application with auto-sync, self-heal, and prune enabled.",
                finding_addressed="; ".join(hits),
            ),
        ]

    def _generate_applicationset(self, name: str, hits: list[str]) -> list[GeneratedFile]:
        doc: dict = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "ApplicationSet",
            "metadata": {
                "name": "agentit-managed-apps",
                "namespace": "openshift-gitops",
            },
            "spec": {
                "generators": [
                    {
                        "git": {
                            "repoURL": self.report.infra_repo_url,
                            "revision": "HEAD",
                            "directories": [
                                {"path": "apps/*"},
                            ],
                        },
                    },
                ],
                "template": {
                    "metadata": {
                        "name": "{{path.basename}}",
                        "namespace": "openshift-gitops",
                    },
                    "spec": {
                        "project": "default",
                        "source": {
                            "repoURL": self.report.infra_repo_url,
                            "targetRevision": "HEAD",
                            "path": "{{path}}",
                        },
                        "destination": {
                            "server": "https://kubernetes.default.svc",
                            "namespace": "{{path.basename}}",
                        },
                        "syncPolicy": {
                            "automated": {
                                "selfHeal": True,
                                "prune": True,
                            },
                            "syncOptions": [
                                "CreateNamespace=true",
                            ],
                        },
                    },
                },
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("argocd-applicationset.yaml", content)

        return [
            GeneratedFile(
                path="argocd-applicationset.yaml",
                content=content,
                description=f"Argo CD ApplicationSet — auto-discovers apps in {self.report.infra_repo_url} under apps/*/.",
                finding_addressed="; ".join(hits),
            ),
        ]

    def _generate_argo_rollout(self) -> list[GeneratedFile]:
        name = self._name
        lang = self._primary_language()

        port = 8080
        if lang == "java":
            port = 8080
        elif lang in ("go", "python"):
            port = 8080
        elif lang in ("node", "typescript", "javascript"):
            port = 3000

        doc: dict = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Rollout",
            "metadata": {
                "name": name,
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "replicas": 2,
                "selector": {
                    "matchLabels": {"app": name},
                },
                "strategy": {
                    "canary": {
                        "steps": [
                            {"setWeight": 5},
                            {"pause": {"duration": "60s"}},
                            {"setWeight": 25},
                            {"pause": {"duration": "60s"}},
                            {"setWeight": 50},
                            {"pause": {"duration": "60s"}},
                            {"setWeight": 100},
                        ],
                        "canaryService": f"{name}-canary",
                        "stableService": name,
                    },
                },
                "template": {
                    "metadata": {
                        "labels": {"app": name},
                    },
                    "spec": {
                        "securityContext": {
                            "runAsNonRoot": True,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": name,
                                "image": self._image_ref(),
                                "ports": [{"containerPort": port}],
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"cpu": "500m", "memory": "512Mi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "livenessProbe": {
                                    "httpGet": {"path": "/", "port": port},
                                    "initialDelaySeconds": 10,
                                },
                                "readinessProbe": {
                                    "httpGet": {"path": "/", "port": port},
                                    "initialDelaySeconds": 5,
                                },
                            },
                        ],
                    },
                },
            },
        }

        canary_svc: dict = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": f"{name}-canary",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "type": "ClusterIP",
                "ports": [{"port": port, "targetPort": port, "protocol": "TCP"}],
                "selector": {"app": name},
            },
        }

        stable_svc: dict = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": name,
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "type": "ClusterIP",
                "ports": [{"port": port, "targetPort": port, "protocol": "TCP"}],
                "selector": {"app": name},
            },
        }

        content = yaml.dump_all([doc, stable_svc, canary_svc], default_flow_style=False, sort_keys=False)
        self._write("argo-rollout.yaml", content)

        return [
            GeneratedFile(
                path="argo-rollout.yaml",
                content=content,
                description="Argo Rollout with canary strategy (5% → 25% → 50% → 100%) plus stable and canary Services.",
                finding_addressed="Progressive delivery for safe production deployments.",
            ),
        ]

    def _generate_containerfile(self) -> list[GeneratedFile]:
        # Skip if hardening agent already generated one
        if (self.output_dir / "Containerfile").exists():
            return []

        hits = self._findings_for("container", "dockerfile")
        if not hits:
            return []

        lang = self._primary_language()
        content = HardeningAgent._containerfile_for(lang)
        self._write("Containerfile", content)

        return [
            GeneratedFile(
                path="Containerfile",
                content=content,
                description=f"Multi-stage Containerfile using UBI base for {lang}.",
                finding_addressed="; ".join(hits),
            ),
        ]

    def _generate_quay_config(self) -> list[GeneratedFile]:
        name = self._name
        doc: dict = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{name}-quay-config",
                "labels": {"app.kubernetes.io/name": name},
            },
            "data": {
                "repository": f"quay.io/org/{name}",
                "description": f"Container image for {name}",
                "visibility": "private",
                "image-scanning": "enabled",
                "vulnerability-notifications": "enabled",
                "tag-expiration": "4w",
                "robot-account": f"{name}-ci",
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("quay-config.yaml", content)

        return [
            GeneratedFile(
                path="quay-config.yaml",
                content=content,
                description="Quay repository configuration with image scanning and vulnerability notifications enabled.",
                finding_addressed="Registry configuration baseline.",
            ),
        ]
