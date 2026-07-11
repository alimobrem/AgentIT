from __future__ import annotations

import textwrap
from pathlib import Path

import yaml
from pydantic import BaseModel

from agentit.agents.base import GeneratedFile, _sanitize_name
from agentit.models import AssessmentReport


class RetirementResult(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.files)
        self.summary = (
            f"Generated {count} retirement artifact{'s' if count != 1 else ''}."
        )


class RetirementAgent:
    def __init__(self, report: AssessmentReport, output_dir: Path) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)
        self._namespace = "default"

    def run(self) -> RetirementResult:
        """Generate all retirement/decommission artifacts."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[GeneratedFile] = []

        generated.append(self._generate_decommission_plan())
        generated.append(self._generate_cleanup_task())
        archive = self._generate_data_archive_job()
        if archive is not None:
            generated.append(archive)

        return RetirementResult(files=generated)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _write(self, filename: str, content: str) -> None:
        (self.output_dir / filename).write_text(content)

    def _detected_databases(self) -> list[str]:
        return [db.name for db in self.report.stack.databases]

    def _has_postgres(self) -> bool:
        return any(
            "postgres" in db.lower() for db in self._detected_databases()
        )

    # ------------------------------------------------------------------
    # generators
    # ------------------------------------------------------------------

    def _generate_decommission_plan(self) -> GeneratedFile:
        name = self._name
        ns = self._namespace
        databases = self._detected_databases()

        if databases:
            db_list = "\n".join(f"   - Back up **{db}** data" for db in databases)
            data_section = textwrap.dedent(f"""\
                ## 1. Data Archival

                Detected databases: {', '.join(databases)}

                {db_list}
                - Verify backup integrity
                - Store backups in long-term storage (S3 / PVC)
                - Document backup location and retention policy
            """)
        else:
            data_section = textwrap.dedent("""\
                ## 1. Data Archival

                No databases detected. Verify no persistent data needs archival.
            """)

        content = textwrap.dedent(f"""\
            # Decommission Plan: {name}

            **Namespace:** {ns}
            **Date:** YYYY-MM-DD
            **Owner:** TBD

            {data_section}
            ## 2. DNS / Route Cleanup

            - [ ] Remove DNS entries pointing to this service
            - [ ] Delete OpenShift Routes / Ingress resources
            - [ ] Update external load balancer configuration
            - [ ] Remove service from service mesh (if applicable)

            ## 3. Dependency Notification Checklist

            - [ ] Notify upstream consumers of retirement timeline
            - [ ] Notify downstream dependencies
            - [ ] Update service registry / catalog
            - [ ] Notify on-call / SRE team
            - [ ] Send announcement to relevant Slack channels

            ## 4. Resource Reclamation

            - [ ] Delete Deployments / StatefulSets
            - [ ] Delete Services, ConfigMaps, Secrets
            - [ ] Delete PersistentVolumeClaims (after data archival)
            - [ ] Delete ServiceAccounts and RBAC bindings
            - [ ] Delete NetworkPolicies
            - [ ] Remove CI/CD pipelines
            - [ ] Archive source repository

            ## 5. Timeline (30-Day Sunset)

            | Day | Action |
            |-----|--------|
            | 0   | Announce retirement intent |
            | 7   | Disable new traffic / mark deprecated |
            | 14  | Complete data archival |
            | 21  | Remove from monitoring / alerting |
            | 30  | Execute cleanup script, delete namespace |
        """)

        self._write("decommission-plan.md", content)
        return GeneratedFile(
            path="decommission-plan.md",
            content=content,
            description="Step-by-step decommission plan with 30-day sunset timeline.",
        )

    def _generate_cleanup_task(self) -> GeneratedFile:
        name = self._name
        ns = self._namespace

        task: dict = {
            "apiVersion": "tekton.dev/v1",
            "kind": "Task",
            "metadata": {
                "name": f"{name}-cleanup",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "params": [
                    {"name": "APP_NAME", "type": "string", "default": name},
                    {"name": "NAMESPACE", "type": "string", "default": ns},
                    {"name": "DELETE_PVCS", "type": "string", "default": "false"},
                ],
                "steps": [
                    {
                        "name": "delete-workloads",
                        "image": "registry.redhat.io/openshift4/ose-cli:latest",
                        "script": (
                            "#!/usr/bin/env sh\n"
                            "set -e\n"
                            'NS="$(params.NAMESPACE)"\n'
                            'APP="$(params.APP_NAME)"\n'
                            'oc delete deployment,service,route,configmap,secret -l app="$APP" -n "$NS" --ignore-not-found\n'
                            'echo "[OK] Workload resources deleted for $APP in $NS"\n'
                        ),
                    },
                    {
                        "name": "delete-pvcs",
                        "image": "registry.redhat.io/openshift4/ose-cli:latest",
                        "script": (
                            "#!/usr/bin/env sh\n"
                            'if [ "$(params.DELETE_PVCS)" = "true" ]; then\n'
                            '  oc delete pvc -l app="$(params.APP_NAME)" -n "$(params.NAMESPACE)" --ignore-not-found\n'
                            '  echo "[OK] PVCs deleted"\n'
                            "else\n"
                            '  echo "[SKIP] PVC deletion not requested"\n'
                            "fi\n"
                        ),
                    },
                ],
            },
        }

        content = yaml.dump(task, default_flow_style=False, sort_keys=False)
        self._write("cleanup-task.yaml", content)

        return GeneratedFile(
            path="cleanup-task.yaml",
            content=content,
            description=f"Tekton Task for {name} resource cleanup via oc CLI.",
        )

    def _generate_data_archive_job(self) -> GeneratedFile | None:
        if not self._has_postgres():
            return None

        name = self._name
        ns = self._namespace

        doc = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": f"{name}-data-archive",
                "namespace": ns,
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "backoffLimit": 2,
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "pg-dump",
                                "image": "registry.access.redhat.com/rhel9/postgresql-15:latest",
                                "command": [
                                    "/bin/bash",
                                    "-c",
                                    (
                                        "pg_dump"
                                        " -h $PGHOST"
                                        " -U $PGUSER"
                                        " -d $PGDATABASE"
                                        " -Fc"
                                        " -f /archive/dump.pg"
                                    ),
                                ],
                                "env": [
                                    {
                                        "name": "PGHOST",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": f"{name}-db",
                                                "key": "host",
                                            },
                                        },
                                    },
                                    {
                                        "name": "PGUSER",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": f"{name}-db",
                                                "key": "username",
                                            },
                                        },
                                    },
                                    {
                                        "name": "PGPASSWORD",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": f"{name}-db",
                                                "key": "password",
                                            },
                                        },
                                    },
                                    {
                                        "name": "PGDATABASE",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": f"{name}-db",
                                                "key": "database",
                                            },
                                        },
                                    },
                                ],
                                "volumeMounts": [
                                    {
                                        "name": "archive-volume",
                                        "mountPath": "/archive",
                                    },
                                ],
                            },
                        ],
                        "volumes": [
                            {
                                "name": "archive-volume",
                                "persistentVolumeClaim": {
                                    "claimName": f"{name}-archive",
                                },
                            },
                        ],
                    },
                },
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("data-archive-job.yaml", content)

        return GeneratedFile(
            path="data-archive-job.yaml",
            content=content,
            description="Kubernetes Job for PostgreSQL data backup before deletion.",
        )
