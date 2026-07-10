from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from agentit.models import DimensionScore, Finding, Severity

IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "vendor", "dist", "build", "target",
    ".tox", ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
}

TEXT_EXTENSIONS = {
    ".py", ".go", ".java", ".js", ".ts", ".tsx", ".jsx", ".rb", ".rs",
    ".yaml", ".yml", ".toml", ".json", ".env", ".cfg", ".conf", ".ini",
    ".xml", ".properties", ".sh", ".gradle",
}

DEFAULT_PENALTIES: dict[Severity, int] = {
    Severity.critical: 25,
    Severity.high: 20,
    Severity.medium: 10,
    Severity.low: 3,
    Severity.info: 0,
}


class Analyzer(Protocol):
    dimension: str

    def analyze(self, repo_path: Path) -> DimensionScore: ...


def is_ignored(file_path: Path, repo_root: Path) -> bool:
    return bool(IGNORED_DIRS & set(file_path.relative_to(repo_root).parts))


def iter_text_files(
    repo_path: Path,
    extensions: set[str] | None = None,
) -> Iterator[tuple[Path, str]]:
    exts = extensions or TEXT_EXTENSIONS
    repo_resolved = repo_path.resolve()
    for fp in repo_path.rglob("*"):
        if not fp.is_file() or is_ignored(fp, repo_path):
            continue
        if not fp.resolve().is_relative_to(repo_resolved):
            continue
        if fp.suffix.lower() in exts:
            try:
                yield fp, fp.read_text(errors="ignore")
            except OSError:
                continue


def iter_yaml_files(repo_path: Path) -> Iterator[tuple[Path, str]]:
    yield from iter_text_files(repo_path, {".yaml", ".yml"})


def calculate_score(
    findings: list[Finding],
    penalties: dict[Severity, int] | None = None,
) -> int:
    weights = penalties or DEFAULT_PENALTIES
    score = 100
    for f in findings:
        score -= weights.get(f.severity, 0)
    return max(0, min(100, score))
