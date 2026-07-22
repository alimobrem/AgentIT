"""Typing Protocols for layer boundaries (models → analyzers → engine → portal)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AnalyzerProto(Protocol):
    """Minimal surface every analyzer exposes to the runner."""

    dimension: str

    def analyze(self, repo_path: Path) -> Any: ...


@runtime_checkable
class AssessmentStoreProto(Protocol):
    """Subset of store methods used outside ``portal.store``."""

    async def save(self, report: Any) -> str: ...

    async def get(self, assessment_id: str) -> Any: ...
