"""Tests for the data-driven check engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentit.check_engine import (
    CheckDefinition,
    load_checks,
    run_checks,
    run_checks_by_dimension,
    _parse_check_file,
)
from agentit.models import Severity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_check(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(content)
    return p


VALID_CHECK = """\
name: test-check
dimension: security
severity: high
category: test
type: file_exists
pattern: "Dockerfile*"
description: No Dockerfile found
recommendation: Add a Dockerfile
"""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseCheckFile:
    def test_valid_check(self, tmp_path: Path) -> None:
        p = _write_check(tmp_path, "good", VALID_CHECK)
        defn = _parse_check_file(p)
        assert defn is not None
        assert defn.name == "test-check"
        assert defn.dimension == "security"
        assert defn.severity == Severity.high
        assert defn.check_type == "file_exists"
        assert defn.pattern == "Dockerfile*"

    def test_missing_key_returns_none(self, tmp_path: Path) -> None:
        bad = "name: x\nseverity: high\n"
        p = _write_check(tmp_path, "bad", bad)
        assert _parse_check_file(p) is None

    def test_invalid_type_returns_none(self, tmp_path: Path) -> None:
        content = VALID_CHECK.replace("file_exists", "nonexistent_type")
        p = _write_check(tmp_path, "badtype", content)
        assert _parse_check_file(p) is None

    def test_invalid_severity_returns_none(self, tmp_path: Path) -> None:
        content = VALID_CHECK.replace("high", "extreme")
        p = _write_check(tmp_path, "badsev", content)
        assert _parse_check_file(p) is None

    def test_non_yaml_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("{{{{not yaml")
        assert _parse_check_file(p) is None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestLoadChecks:
    def test_loads_from_directory(self, tmp_path: Path) -> None:
        sub = tmp_path / "security"
        sub.mkdir()
        (sub / "a.yaml").write_text(VALID_CHECK)
        checks = load_checks(tmp_path)
        assert len(checks) == 1
        assert checks[0].name == "test-check"

    def test_skips_invalid_files(self, tmp_path: Path) -> None:
        (tmp_path / "good.yaml").write_text(VALID_CHECK)
        (tmp_path / "bad.yaml").write_text("name: x\n")
        checks = load_checks(tmp_path)
        assert len(checks) == 1

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_checks(tmp_path / "nonexistent") == []

    def test_loads_real_checks_dir(self) -> None:
        checks_dir = Path(__file__).resolve().parent.parent / "checks"
        if checks_dir.is_dir():
            checks = load_checks(checks_dir)
            assert len(checks) >= 15  # we created ~20


# ---------------------------------------------------------------------------
# Check runners
# ---------------------------------------------------------------------------


class TestFileExists:
    def test_pass_when_file_present(self, create_mock_repo) -> None:
        repo = create_mock_repo({"Dockerfile": "FROM ubi9"})
        check = VALID_CHECK  # pattern: Dockerfile*
        checks = load_checks(_dir_with_check(repo.parent, check))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_fail_when_file_absent(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.py": "print('hi')"})
        checks = load_checks(_dir_with_check(repo.parent, VALID_CHECK))
        findings = run_checks(checks, repo)
        assert len(findings) == 1
        assert findings[0].category == "test"


class TestFileContains:
    def test_pass_when_content_present(self, create_mock_repo) -> None:
        repo = create_mock_repo({"ci.yml": "trivy scan"})
        content = VALID_CHECK.replace("file_exists", "file_contains").replace(
            '"Dockerfile*"', "trivy"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_fail_when_content_absent(self, create_mock_repo) -> None:
        repo = create_mock_repo({"ci.yml": "echo hello"})
        content = VALID_CHECK.replace("file_exists", "file_contains").replace(
            '"Dockerfile*"', "trivy"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 1


class TestFileMissing:
    def test_pass_when_file_absent(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.py": ""})
        content = VALID_CHECK.replace("file_exists", "file_missing").replace(
            '"Dockerfile*"', ".env"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_fail_when_file_present(self, create_mock_repo) -> None:
        repo = create_mock_repo({".env": "SECRET=bad"})
        content = VALID_CHECK.replace("file_exists", "file_missing").replace(
            '"Dockerfile*"', ".env"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 1


class TestYamlKindExists:
    def test_pass_when_kind_present(self, create_mock_repo) -> None:
        repo = create_mock_repo({"np.yaml": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"})
        content = VALID_CHECK.replace("file_exists", "yaml_kind_exists").replace(
            '"Dockerfile*"', "NetworkPolicy"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_fail_when_kind_absent(self, create_mock_repo) -> None:
        repo = create_mock_repo({"svc.yaml": "apiVersion: v1\nkind: Service\n"})
        content = VALID_CHECK.replace("file_exists", "yaml_kind_exists").replace(
            '"Dockerfile*"', "NetworkPolicy"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 1


class TestYamlKindMissing:
    def test_pass_when_kind_absent(self, create_mock_repo) -> None:
        repo = create_mock_repo({"svc.yaml": "kind: Service\n"})
        content = VALID_CHECK.replace("file_exists", "yaml_kind_missing").replace(
            '"Dockerfile*"', "DangerousKind"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_fail_when_kind_present(self, create_mock_repo) -> None:
        repo = create_mock_repo({"bad.yaml": "kind: DangerousKind\n"})
        content = VALID_CHECK.replace("file_exists", "yaml_kind_missing").replace(
            '"Dockerfile*"', "DangerousKind"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# Dimension grouping
# ---------------------------------------------------------------------------


class TestRunChecksByDimension:
    def test_groups_by_dimension(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.py": ""})
        sec_check = VALID_CHECK  # dimension: security, file_exists Dockerfile*
        comp_check = VALID_CHECK.replace("dimension: security", "dimension: compliance").replace(
            "name: test-check", "name: comp-check"
        ).replace('"Dockerfile*"', '"LICENSE*"')
        checks_dir = repo.parent / "checks"
        checks_dir.mkdir()
        (checks_dir / "sec.yaml").write_text(sec_check)
        (checks_dir / "comp.yaml").write_text(comp_check)
        checks = load_checks(checks_dir)
        grouped = run_checks_by_dimension(checks, repo)
        assert "security" in grouped
        assert "compliance" in grouped
        assert len(grouped["security"]) == 1
        assert len(grouped["compliance"]) == 1


# ---------------------------------------------------------------------------
# Integration: runner merges check findings
# ---------------------------------------------------------------------------


class TestRunnerIntegration:
    def test_check_findings_merged_into_scores(self, create_mock_repo, tmp_path: Path) -> None:
        repo = create_mock_repo({
            "main.py": "print('hello')",
            "Dockerfile": "FROM python:3.12\nCMD ['python', 'main.py']",
        })
        # Create a checks dir with a single check that should fire
        checks_dir = tmp_path / "test_checks"
        checks_dir.mkdir()
        (checks_dir / "extra.yaml").write_text(
            "name: extra-check\n"
            "dimension: security\n"
            "severity: medium\n"
            "category: extra_test\n"
            "type: file_exists\n"
            'pattern: "SECURITY.md"\n'
            "description: No SECURITY.md file\n"
            "recommendation: Add SECURITY.md\n"
        )

        from agentit.runner import run_assessment
        report = run_assessment(
            repo, repo_url="https://github.com/test/app",
            checks_dir=checks_dir,
        )
        sec_score = next(s for s in report.scores if s.dimension == "security")
        categories = [f.category for f in sec_score.findings]
        assert "extra_test" in categories

    def test_duplicate_findings_not_doubled(self, create_mock_repo, tmp_path: Path) -> None:
        repo = create_mock_repo({
            "main.py": "print('hello')",
        })
        # Create a check that duplicates what the analyzer already detects
        checks_dir = tmp_path / "dup_checks"
        checks_dir.mkdir()
        (checks_dir / "dup.yaml").write_text(
            "name: dup-network-policy\n"
            "dimension: security\n"
            "severity: high\n"
            "category: network\n"
            "type: yaml_kind_exists\n"
            "pattern: NetworkPolicy\n"
            "description: No NetworkPolicy manifests found\n"
            "recommendation: Add deny-all default NetworkPolicy with explicit allow rules\n"
        )

        from agentit.runner import run_assessment
        report = run_assessment(
            repo, repo_url="https://github.com/test/app",
            checks_dir=checks_dir,
        )
        sec_score = next(s for s in report.scores if s.dimension == "security")
        # The analyzer already finds "No NetworkPolicy manifests found".
        # The check has the same (category, description) so it should be deduped.
        net_findings = [f for f in sec_score.findings if f.category == "network"]
        assert len(net_findings) == 1

    def test_empty_checks_dir_no_change(self, create_mock_repo, tmp_path: Path) -> None:
        repo = create_mock_repo({
            "main.py": "print('hello')",
            "Dockerfile": "FROM python:3.12\nCMD ['python', 'main.py']",
        })
        checks_dir = tmp_path / "empty_checks"
        checks_dir.mkdir()

        from agentit.runner import run_assessment
        report = run_assessment(
            repo, repo_url="https://github.com/test/app",
            checks_dir=checks_dir,
        )
        assert len(report.scores) == 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dir_with_check(parent: Path, content: str) -> Path:
    """Create a temp checks dir with a single check file."""
    checks_dir = parent / "test_checks"
    checks_dir.mkdir(exist_ok=True)
    (checks_dir / "check.yaml").write_text(content)
    return checks_dir
