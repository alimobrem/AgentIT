from __future__ import annotations

from pathlib import Path

from agentit.analyzers.base import calculate_score, iter_text_files, iter_yaml_files
from agentit.models import DimensionScore, Finding, Severity


class DataGovernanceAnalyzer:
    dimension = "data_governance"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        has_backup = False
        has_migration = False
        has_retention = False

        for _, content in iter_yaml_files(repo_path):
            content_lower = content.lower()
            if "backup" in content_lower or "kind: CronJob" in content:
                has_backup = True
            if "retention" in content_lower:
                has_retention = True

        for fp, content in iter_text_files(repo_path, {".py", ".go", ".js", ".ts"}):
            content_lower = content.lower()
            if "purge" in content_lower and ("retention" in content_lower or "days" in content_lower):
                has_retention = True
            if "backup" in content_lower and ("schedule" in content_lower or "cron" in content_lower):
                has_backup = True

        # Root-level and nested (e.g. apps/api/alembic) — monorepos keep
        # tooling under a package, not the repo root.
        migration_dirs = ("migrations", "migrate", "alembic", "flyway", "liquibase", "db/migrate")
        for d in migration_dirs:
            if (repo_path / d).exists() or any(repo_path.glob(f"**/{d}")):
                has_migration = True
                break
        if not has_migration:
            for pattern in (
                "**/alembic.ini",
                "**/flyway.conf",
                "**/flyway.toml",
                "**/db/migrate/**",
                "**/liquibase*.xml",
                "**/goose/*.sql",
            ):
                if any(repo_path.glob(pattern)):
                    has_migration = True
                    break

        if not has_backup:
            findings.append(Finding(
                category="backup",
                severity=Severity.high,
                description="No backup configuration detected",
                recommendation="Configure Crunchy PostgreSQL backup schedule or add backup CronJob",
                source="analyzer:data_governance",
            ))
        if not has_migration:
            findings.append(Finding(
                category="migration",
                severity=Severity.medium,
                description="No database migration tooling detected",
                recommendation="Add database migration tool (Alembic, Flyway, golang-migrate)",
                source="analyzer:data_governance",
            ))
        if not has_retention:
            findings.append(Finding(
                category="retention",
                severity=Severity.medium,
                description="No data retention policy detected",
                recommendation="Define data retention policies for compliance (GDPR, SOC 2)",
                source="analyzer:data_governance",
            ))

        return DimensionScore(
            dimension="data_governance",
            score=calculate_score(findings),
            max_score=100,
            findings=findings,
        )
