from __future__ import annotations

import re
from pathlib import Path

from agentit.analyzers.base import calculate_score, is_ignored, iter_text_files, iter_yaml_files
from agentit.models import DimensionScore, Finding, Severity

_CREATE_TABLE_IF = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS", re.IGNORECASE)
_ALTER_ADD_IF = re.compile(
    r"ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS",
    re.IGNORECASE,
)
_SKIP_SCHEMA_PARTS = frozenset({
    "tests", "test", "docs", "examples", "example", "vendor",
    "node_modules", ".venv", "venv", "__pycache__",
})


def _is_app_schema_path(repo_path: Path, file_path: Path) -> bool:
    """True for application source (not tests/docs/examples)."""
    try:
        rel = file_path.relative_to(repo_path)
    except ValueError:
        return False
    if is_ignored(file_path, repo_path):
        return False
    return not (_SKIP_SCHEMA_PARTS & {p.lower() for p in rel.parts})


def has_formal_migration_tooling(repo_path: Path) -> bool:
    """Alembic / Flyway / Liquibase / golang-migrate / goose layouts."""
    migration_dirs = ("migrations", "migrate", "alembic", "flyway", "liquibase", "db/migrate")
    for d in migration_dirs:
        if (repo_path / d).exists() or any(repo_path.glob(f"**/{d}")):
            return True
    for pattern in (
        "**/alembic.ini",
        "**/flyway.conf",
        "**/flyway.toml",
        "**/db/migrate/**",
        "**/liquibase*.xml",
        "**/goose/*.sql",
    ):
        if any(repo_path.glob(pattern)):
            return True
    return False


def has_hand_rolled_schema_evolution(repo_path: Path) -> bool:
    """Detect embedded / idempotent DDL used instead of Alembic-style tools.

    AgentIT (ADR 0002) and similar apps keep ``SCHEMA_SQL`` / ``CREATE TABLE
    IF NOT EXISTS`` (+ additive ``ALTER … IF NOT EXISTS``) in store modules
    rather than a separate migrator. That is a real schema-evolution path —
    Scan must not flag ``migration`` or open a stub Alembic PR.
    """
    for fp, content in iter_text_files(repo_path, {".py", ".sql", ".go", ".ts", ".js"}):
        if not _is_app_schema_path(repo_path, fp):
            continue
        creates = _CREATE_TABLE_IF.findall(content)
        if not creates:
            continue
        low = content.lower()
        if "schema_sql" in low or "no-migration-framework" in low:
            return True
        if len(creates) >= 2:
            return True
        if _ALTER_ADD_IF.search(content):
            return True
    return False


def has_migration_approach(repo_path: Path) -> bool:
    """Formal migrator **or** hand-rolled idempotent schema DDL."""
    return has_formal_migration_tooling(repo_path) or has_hand_rolled_schema_evolution(
        repo_path,
    )


class DataGovernanceAnalyzer:
    dimension = "data_governance"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        has_backup = False
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
        # tooling under a package, not the repo root. Also accept hand-rolled
        # Postgres DDL (AgentIT store / ADR 0002).
        has_migration = has_migration_approach(repo_path)

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
                recommendation=(
                    "Add database migration tooling (Alembic, Flyway, golang-migrate) "
                    "or embed idempotent schema DDL (CREATE TABLE IF NOT EXISTS / "
                    "additive ALTER) in the app store layer"
                ),
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
