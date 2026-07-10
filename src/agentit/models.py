from __future__ import annotations

from datetime import datetime
from enum import IntEnum

from pydantic import BaseModel, field_validator


class Severity(IntEnum):
    critical = 0
    high = 1
    medium = 2
    low = 3
    info = 4


class Finding(BaseModel):
    category: str
    severity: Severity
    description: str
    file_path: str | None = None
    recommendation: str


class DimensionScore(BaseModel):
    dimension: str
    score: int
    max_score: int
    findings: list[Finding]

    @field_validator("score")
    @classmethod
    def clamp_score(cls, v: int) -> int:
        return max(0, min(100, v))


class Language(BaseModel):
    name: str
    version: str | None = None
    file_count: int
    percentage: float


class Framework(BaseModel):
    name: str
    version: str | None = None
    language: str


class Database(BaseModel):
    name: str
    version: str | None = None
    connection_method: str | None = None


class Runtime(BaseModel):
    name: str
    version: str | None = None


class StackInfo(BaseModel):
    languages: list[Language]
    frameworks: list[Framework]
    databases: list[Database]
    runtimes: list[Runtime]
    package_managers: list[str]


class ArchitectureInfo(BaseModel):
    service_count: int
    architecture_style: str
    has_api: bool
    api_style: str | None = None
    external_dependencies: list[str]
    auth_mechanism: str | None = None


class RemediationItem(BaseModel):
    priority: int
    dimension: str
    description: str
    estimated_effort: str
    agent_responsible: str


class AssessmentReport(BaseModel):
    repo_url: str
    repo_name: str
    assessed_at: datetime
    stack: StackInfo
    architecture: ArchitectureInfo
    scores: list[DimensionScore]
    overall_score: float = 0.0
    criticality: str
    summary: str
    remediation_plan: list[RemediationItem]

    def model_post_init(self, _context: object) -> None:
        if self.scores:
            self.overall_score = sum(s.score for s in self.scores) / len(self.scores)
