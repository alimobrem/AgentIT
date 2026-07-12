"""Validate every check YAML file in checks/ directory."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CHECKS_DIR = Path(__file__).resolve().parent.parent / "checks"
VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}
VALID_TYPES = {"file_exists", "file_contains", "file_missing", "yaml_kind_exists", "yaml_kind_missing"}
REQUIRED_FIELDS = {"name", "dimension", "severity", "category", "type", "pattern", "description", "recommendation"}


def _all_check_files() -> list[Path]:
    """Collect every .yaml file under checks/."""
    if not CHECKS_DIR.is_dir():
        return []
    return sorted(CHECKS_DIR.rglob("*.yaml"))


@pytest.fixture(params=_all_check_files(), ids=lambda p: str(p.relative_to(CHECKS_DIR)))
def check_file(request: pytest.FixtureRequest) -> Path:
    return request.param


class TestAllChecks:
    """Validate structure of every check definition."""

    def test_loads_as_valid_yaml(self, check_file: Path) -> None:
        data = yaml.safe_load(check_file.read_text())
        assert isinstance(data, dict), f"{check_file.name} is not a YAML mapping"

    def test_has_required_fields(self, check_file: Path) -> None:
        data = yaml.safe_load(check_file.read_text())
        missing = REQUIRED_FIELDS - set(data.keys())
        assert not missing, f"{check_file.name} missing fields: {missing}"

    def test_severity_is_valid(self, check_file: Path) -> None:
        data = yaml.safe_load(check_file.read_text())
        sev = str(data["severity"]).lower()
        assert sev in VALID_SEVERITIES, f"{check_file.name} severity '{sev}' not in {VALID_SEVERITIES}"

    def test_type_is_valid(self, check_file: Path) -> None:
        data = yaml.safe_load(check_file.read_text())
        ctype = data["type"]
        assert ctype in VALID_TYPES, f"{check_file.name} type '{ctype}' not in {VALID_TYPES}"
