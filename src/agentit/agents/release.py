"""Release Coordinator Agent — orchestrates canary deployments with analysis-gated promotion."""

from __future__ import annotations

import textwrap
from pathlib import Path

import yaml
from pydantic import BaseModel

from agentit.agents.base import GeneratedFile, _sanitize_name
from agentit.models import AssessmentReport


class ReleaseResult(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.files)
        self.summary = (
            f"Generated {count} release coordination artifact{'s' if count != 1 else ''}."
        )


_PAUSE_DURATIONS = {
    5: "2m",
    25: "2m",
    50: "3m",
}

_DEFAULT_PORT = 8080
_NODE_PORT = 3000


class ReleaseCoordinatorAgent:
    def __init__(self, report: AssessmentReport, output_dir: Path) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)

    @property
    def _is_critical(self) -> bool:
        return self.report.criticality.lower() in ("critical", "high")

    def _primary_language(self) -> str:
        if not self.report.stack.languages:
            return "unknown"
        top = max(self.report.stack.languages, key=lambda l: l.percentage)
        return top.name.lower()

    def _app_port(self) -> int:
        lang = self._primary_language()
        if lang in ("node", "typescript", "javascript"):
            return _NODE_PORT
        return _DEFAULT_PORT

    def _image_ref(self) -> str:
        from agentit.image_builder import get_image_ref
        return get_image_ref(self.report.repo_name)

    def _write(self, filename: str, content: str) -> None:
        (self.output_dir / filename).write_text(content)

    def run(self) -> ReleaseResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[GeneratedFile] = []

        generated.append(self._generate_analysis_template())
        generated.append(self._generate_rollout_patch())
        generated.append(self._generate_rollback_policy())
        generated.append(self._generate_release_runbook())

        return ReleaseResult(files=generated)

    def _generate_analysis_template(self) -> GeneratedFile:
        name = self._name
        doc: dict = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "AnalysisTemplate",
            "metadata": {
                "name": f"{name}-success-rate",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "metrics": [
                    {
                        "name": "success-rate",
                        "interval": "30s",
                        "count": 5,
                        "failureLimit": 2,
                        "provider": {
                            "prometheus": {
                                "address": "http://prometheus-operated.openshift-monitoring.svc:9090",
                                "query": (
                                    f'sum(rate(http_requests_total{{app="{name}",status=~"2.."}}[5m])) '
                                    f'/ sum(rate(http_requests_total{{app="{name}"}}[5m]))'
                                ),
                            },
                        },
                        "successCondition": "result[0] >= 0.95",
                    },
                    {
                        "name": "error-rate",
                        "interval": "30s",
                        "count": 5,
                        "failureLimit": 2,
                        "provider": {
                            "prometheus": {
                                "address": "http://prometheus-operated.openshift-monitoring.svc:9090",
                                "query": (
                                    f'sum(rate(http_requests_total{{app="{name}",status=~"5.."}}[5m])) '
                                    f'/ sum(rate(http_requests_total{{app="{name}"}}[5m]))'
                                ),
                            },
                        },
                        "successCondition": "result[0] <= 0.05",
                    },
                ],
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("analysis-template.yaml", content)

        return GeneratedFile(
            path="analysis-template.yaml",
            content=content,
            description=f"Argo AnalysisTemplate for {name} canary analysis (success rate >= 95%, error rate <= 5%).",
            finding_addressed="Metric-gated canary promotion replacing time-only pauses.",
        )

    def _generate_rollout_patch(self) -> GeneratedFile:
        name = self._name
        port = self._app_port()
        auto_promote = not self._is_critical

        steps: list[dict] = []
        for weight, pause in _PAUSE_DURATIONS.items():
            steps.append({"setWeight": weight})
            steps.append({
                "pause": {"duration": pause},
            })
            steps.append({
                "analysis": {
                    "templates": [{"templateName": f"{name}-success-rate"}],
                },
            })
        steps.append({"setWeight": 100})

        doc: dict = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Rollout",
            "metadata": {
                "name": name,
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "replicas": 2,
                "revisionHistoryLimit": 5,
                "rollbackWindow": {"revisions": 2},
                "selector": {
                    "matchLabels": {"app": name},
                },
                "strategy": {
                    "canary": {
                        "steps": steps,
                        "canaryService": f"{name}-canary",
                        "stableService": name,
                        "autoPromotionEnabled": auto_promote,
                        "abortScaleDownDelaySeconds": 30,
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
                                "livenessProbe": {
                                    "httpGet": {"path": "/", "port": port},
                                    "initialDelaySeconds": 10,
                                },
                                "readinessProbe": {
                                    "httpGet": {"path": "/", "port": port},
                                    "initialDelaySeconds": 5,
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                            },
                        ],
                    },
                },
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("rollout-patch.yaml", content)

        mode = "manual promotion" if self._is_critical else "auto-promotion"
        return GeneratedFile(
            path="rollout-patch.yaml",
            content=content,
            description=f"Enhanced Argo Rollout for {name} with analysis-gated canary ({mode}).",
            finding_addressed="Metric-gated canary deployment replacing time-only pauses.",
        )

    def _generate_rollback_policy(self) -> GeneratedFile:
        name = self._name
        criticality = self.report.criticality

        error_budget = {
            "critical": "0.01%",
            "high": "0.1%",
            "medium": "0.5%",
            "low": "1.0%",
        }.get(criticality, "0.5%")

        doc: dict = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{name}-rollback-policy",
                "labels": {"app.kubernetes.io/name": name},
            },
            "data": {
                "auto-rollback": "true",
                "error-budget-threshold": error_budget,
                "rollback-window-revisions": "2",
                "abort-command": f"kubectl argo rollouts abort {name}",
                "undo-command": f"kubectl argo rollouts undo {name}",
                "retry-command": f"kubectl argo rollouts retry rollout {name}",
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("rollback-policy.yaml", content)

        return GeneratedFile(
            path="rollback-policy.yaml",
            content=content,
            description=f"Rollback policy ConfigMap for {name} (error budget: {error_budget}).",
            finding_addressed="Documented rollback procedures and auto-rollback configuration.",
        )

    def _generate_release_runbook(self) -> GeneratedFile:
        name = self._name
        criticality = self.report.criticality
        auto = "Yes" if not self._is_critical else "No — manual promotion required"

        content = textwrap.dedent(f"""\
            # Release Runbook: {self.report.repo_name}

            ## Pre-Deployment Checklist

            - [ ] All CI pipeline stages passed
            - [ ] Security scan shows no Critical/High CVEs
            - [ ] Compliance policies validated
            - [ ] Staging deployment verified
            - [ ] Database migrations applied (if applicable)
            - [ ] Monitoring dashboards reviewed

            ## Canary Deployment Steps

            | Step | Weight | Analysis Duration | Gate |
            |------|--------|-------------------|------|
            | 1 | 5% | 2 minutes | Automated (success rate >= 95%) |
            | 2 | 25% | 2 minutes | Automated (success rate >= 95%) |
            | 3 | 50% | 3 minutes | Automated (success rate >= 95%) |
            | 4 | 100% | — | Full rollout |

            **Auto-promotion:** {auto}
            **Criticality:** {criticality}

            ## Monitoring During Rollout

            ```bash
            # Watch rollout status
            kubectl argo rollouts get rollout {name} --watch

            # Check analysis runs
            kubectl get analysisrun -l rollouts-pod-template-hash
            ```

            ## Rollback Procedures

            ```bash
            # Abort current rollout (stops canary, routes all traffic to stable)
            kubectl argo rollouts abort {name}

            # Undo to previous revision
            kubectl argo rollouts undo {name}

            # Retry after fixing issues
            kubectl argo rollouts retry rollout {name}
            ```

            ## Escalation

            | Severity | Contact | Response Time |
            |----------|---------|---------------|
            | P1 — Full outage | On-call SRE | 15 minutes |
            | P2 — Degraded | Team lead | 30 minutes |
            | P3 — Minor | Ticket | Next business day |
        """)

        self._write("release-runbook.md", content)

        return GeneratedFile(
            path="release-runbook.md",
            content=content,
            description=f"Deployment runbook for {self.report.repo_name}.",
            finding_addressed="Documented release procedures and escalation paths.",
        )
