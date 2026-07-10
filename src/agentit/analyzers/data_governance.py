from __future__ import annotations

from pathlib import Path

from agentit.analyzers.base import calculate_score, iter_yaml_files
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

        migration_dirs = ["migrations", "migrate", "alembic", "flyway", "liquibase", "db/migrate"]
        for d in migration_dirs:
            if (repo_path / d).exists():
                has_migration = True
                break

        if not has_backup:
            findings.append(Finding(
                category="backup",
                severity=Severity.high,
                description="No backup configuration detected",
                recommendation="Configure Crunchy PostgreSQL backup schedule or add backup CronJob",
            ))
        if not has_migration:
            findings.append(Finding(
                category="migration",
                severity=Severity.medium,
                description="No database migration tooling detected",
                recommendation="Add database migration tool (Alembic, Flyway, golang-migrate)",
            ))
        if not has_retention:
            findings.append(Finding(
                category="retention",
                severity=Severity.medium,
                description="No data retention policy detected",
                recommendation="Define data retention policies for compliance (GDPR, SOC 2)",
            ))

        return DimensionScore(
            dimension="data_governance",
            score=calculate_score(findings),
            max_score=100,
            findings=findings,
        )
