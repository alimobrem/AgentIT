from __future__ import annotations

from pathlib import Path

from agentit.models import DimensionScore, Finding, Severity

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "vendor", "dist", "build", "target"}


class DataGovernanceAnalyzer:
    dimension = "data_governance"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        has_backup = False
        has_migration = False
        has_pvc = False
        has_retention = False

        all_text = ""
        for fp in list(repo_path.rglob("*.yaml")) + list(repo_path.rglob("*.yml")):
            if any(d in fp.relative_to(repo_path).parts for d in IGNORED_DIRS):
                continue
            try:
                all_text += fp.read_text(errors="ignore") + "\n"
            except OSError:
                continue

        if "backup" in all_text.lower() or "CronJob" in all_text:
            has_backup = True
        if "PersistentVolumeClaim" in all_text:
            has_pvc = True
        if "retention" in all_text.lower():
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

        score = 100
        for f in findings:
            if f.severity == Severity.high:
                score -= 25
            elif f.severity == Severity.medium:
                score -= 15
        return DimensionScore(dimension="data_governance", score=max(0, score), max_score=100, findings=findings)
