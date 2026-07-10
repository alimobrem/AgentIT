from __future__ import annotations

from pathlib import Path
from typing import Protocol

from agentit.models import DimensionScore


class Analyzer(Protocol):
    dimension: str

    def analyze(self, repo_path: Path) -> DimensionScore: ...
