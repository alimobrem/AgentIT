"""Tests for the data-driven check engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentit.check_engine import (
    CheckDefinition,
    load_checks,
    run_checks,
    run_checks_by_dimension,
    run_checks_by_dimension_with_status,
    run_checks_with_status,
    _parse_check_file,
)
from agentit.models import Severity

SAMPLE_APP = Path(__file__).resolve().parent / "fixtures" / "sample-app"
REAL_CHECKS_DIR = Path(__file__).resolve().parent.parent / "checks"


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
        """No lower-bound count assertion here on purpose: Phase 4 of
        docs/extension-model-unification-plan-2026-07-18.md is steadily
        porting checks/*.yaml files to mode: detect skills one at a time
        (see tests/test_phase4_check_migrations.py), so the real count
        only ever shrinks over time, down to zero once every check is
        migrated -- this test just proves load_checks() doesn't error out
        against whatever's actually on disk right now, and that every
        loaded entry is a real, well-formed CheckDefinition."""
        checks_dir = Path(__file__).resolve().parent.parent / "checks"
        if checks_dir.is_dir():
            checks = load_checks(checks_dir)
            assert all(isinstance(c, CheckDefinition) for c in checks)


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


class TestListPatternMatching:
    """Gap 1 (docs/extension-model-unification-plan-2026-07-18.md): pattern
    becomes str | list[str] with OR semantics, and an optional
    case_insensitive flag -- both additive, zero behavior change for any
    check file that doesn't use them. This is also the primitive
    `skill_engine.detect_check_definitions()` relies on to run a `mode:
    detect` Markdown skill's rule through this exact same engine."""

    def test_parse_check_file_accepts_list_pattern(self, tmp_path: Path) -> None:
        content = VALID_CHECK.replace('pattern: "Dockerfile*"', "pattern: [foo, bar]")
        p = _write_check(tmp_path, "listpat", content)
        defn = _parse_check_file(p)
        assert defn is not None
        assert defn.pattern == ["foo", "bar"]

    def test_parse_check_file_still_accepts_scalar_pattern(self, tmp_path: Path) -> None:
        """Regression guard: every existing check file (scalar pattern,
        no case_insensitive key) must parse identically to before."""
        p = _write_check(tmp_path, "scalarpat", VALID_CHECK)
        defn = _parse_check_file(p)
        assert defn is not None
        assert defn.pattern == "Dockerfile*"
        assert defn.case_insensitive is False

    def test_file_contains_matches_any_pattern_in_list(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.go": 'import "go.opentelemetry.io/otel"\n'})
        content = VALID_CHECK.replace("type: file_exists", "type: file_contains").replace(
            'pattern: "Dockerfile*"', "pattern: [opentelemetry, otel, jaeger]"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_file_contains_fails_when_no_pattern_in_list_matches(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.go": "package main\n"})
        content = VALID_CHECK.replace("type: file_exists", "type: file_contains").replace(
            'pattern: "Dockerfile*"', "pattern: [opentelemetry, otel, jaeger]"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 1

    def test_yaml_kind_exists_matches_any_kind_in_list(self, create_mock_repo) -> None:
        repo = create_mock_repo({"svc.yaml": "apiVersion: v1\nkind: PodMonitor\n"})
        content = VALID_CHECK.replace("type: file_exists", "type: yaml_kind_exists").replace(
            'pattern: "Dockerfile*"', "pattern: [ServiceMonitor, PodMonitor]"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_file_exists_matches_any_glob_in_list(self, create_mock_repo) -> None:
        repo = create_mock_repo({"Containerfile": "FROM ubi9"})
        content = VALID_CHECK.replace('pattern: "Dockerfile*"', "pattern: [Dockerfile*, Containerfile*]")
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_case_insensitive_file_contains(self, create_mock_repo) -> None:
        repo = create_mock_repo({"README.md": "# Structlog Setup Guide\n"})
        content = (
            VALID_CHECK.replace("type: file_exists", "type: file_contains")
            .replace('pattern: "Dockerfile*"', "pattern: structlog")
            + "case_insensitive: true\n"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_case_sensitive_by_default(self, create_mock_repo) -> None:
        """No case_insensitive key -> exact-case matching, unchanged from
        today. This is the zero-behavior-change guarantee for every
        pre-existing check file."""
        repo = create_mock_repo({"README.md": "# Structlog Setup Guide\n"})
        content = VALID_CHECK.replace("type: file_exists", "type: file_contains").replace(
            'pattern: "Dockerfile*"', "pattern: structlog"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 1  # "Structlog" (capital S) != "structlog"

    def test_case_insensitive_yaml_kind_exists(self, create_mock_repo) -> None:
        repo = create_mock_repo({"svc.yaml": "apiVersion: v1\nkind: servicemonitor\n"})
        content = (
            VALID_CHECK.replace("type: file_exists", "type: yaml_kind_exists")
            .replace('pattern: "Dockerfile*"', "pattern: ServiceMonitor")
            + "case_insensitive: true\n"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0


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
# Pass/fail status snapshots (for AssessmentStore.save_check_results)
# ---------------------------------------------------------------------------


class TestRunChecksWithStatus:
    def test_reports_one_row_per_check_regardless_of_outcome(self, create_mock_repo) -> None:
        """Unlike run_checks_by_dimension, this must report *passing* checks
        too, not just the ones that produced a Finding."""
        repo = create_mock_repo({"Dockerfile": "FROM python:3.12"})
        checks_dir = repo.parent / "checks"
        checks_dir.mkdir()
        (checks_dir / "sec.yaml").write_text(VALID_CHECK)  # passes: Dockerfile* exists
        checks = load_checks(checks_dir)

        statuses = run_checks_with_status(checks, repo)
        assert len(statuses) == 1
        assert statuses[0]["check_name"] == "test-check"
        assert statuses[0]["dimension"] == "security"
        assert statuses[0]["passed"] is True

    def test_reports_failed_check_as_not_passed(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.py": ""})  # no Dockerfile -> check fails
        checks_dir = repo.parent / "checks"
        checks_dir.mkdir()
        (checks_dir / "sec.yaml").write_text(VALID_CHECK)
        checks = load_checks(checks_dir)

        statuses = run_checks_with_status(checks, repo)
        assert statuses[0]["passed"] is False

    def test_by_dimension_with_status_runs_checks_only_once(self, create_mock_repo) -> None:
        """Both the grouped findings and the per-check statuses must come
        from a single pass over the checks, not two separate runs."""
        repo = create_mock_repo({"Dockerfile": "FROM python:3.12"})
        checks_dir = repo.parent / "checks"
        checks_dir.mkdir()
        (checks_dir / "sec.yaml").write_text(VALID_CHECK)
        checks = load_checks(checks_dir)

        grouped, statuses = run_checks_by_dimension_with_status(checks, repo)
        assert grouped == {}  # check passed -> no findings
        assert len(statuses) == 1
        assert statuses[0]["passed"] is True


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
        # Isolated from the real skills/ catalog on purpose -- without this,
        # run_assessment()'s skills_dir default picks up the real, live
        # skills/security/network-policy-exists.md (Phase 4 of
        # docs/extension-model-unification-plan-2026-07-18.md), which
        # produces the exact same (category, description) finding this test
        # is specifically trying to prove gets deduped against the analyzer
        # -- a real third source that isn't what this test is about.
        empty_skills_dir = tmp_path / "empty_skills_for_dedup_test"
        empty_skills_dir.mkdir()

        from agentit.runner import run_assessment
        report = run_assessment(
            repo, repo_url="https://github.com/test/app",
            checks_dir=checks_dir, skills_dir=empty_skills_dir,
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
# Sample-app fixture tests
# ---------------------------------------------------------------------------


class TestSampleAppFixture:
    """Run real checks against the sample-app fixture to verify end-to-end behavior.

    ``real_checks`` deliberately merges legacy ``checks/*.yaml`` files
    with ``mode: detect`` skills (``skills/*/*.md``), exactly like
    ``runner.run_assessment()`` does in production
    (docs/extension-model-unification-plan-2026-07-18.md, Phase 1) --
    Phase 4 is steadily porting individual checks from the former to the
    latter (see tests/test_phase4_check_migrations.py), so a fixture that
    only read the legacy YAML directory would silently lose coverage for
    a specific check (e.g. dockerfile/network-policy) the moment it's
    ported, even though the exact same rule keeps running in production
    via the skill. Reading both sources the way production does makes
    these tests robust to *where* a given rule currently lives.
    """

    REAL_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

    @pytest.fixture()
    def real_checks(self) -> list[CheckDefinition]:
        if not REAL_CHECKS_DIR.is_dir():
            pytest.skip("checks/ dir not found")
        from agentit.skill_engine import detect_check_definitions, load_all_skills

        checks = load_checks(REAL_CHECKS_DIR)
        checks += detect_check_definitions(load_all_skills(self.REAL_SKILLS_DIR))
        assert checks, "no legacy checks or detect-mode skills found on disk"
        return checks

    def test_dockerfile_check_passes(self, real_checks: list[CheckDefinition]) -> None:
        """sample-app has a Dockerfile, so file_exists Dockerfile* must pass."""
        docker_checks = [c for c in real_checks if "dockerfile" in c.name.lower() or "containerfile" in c.name.lower()]
        assert docker_checks, "no dockerfile/containerfile check found"
        findings = run_checks(docker_checks, SAMPLE_APP)
        # Dockerfile exists in the fixture -> at least the dockerfile check should pass
        docker_findings = [f for f in findings if "dockerfile" in f.description.lower()]
        assert len(docker_findings) == 0, "Dockerfile exists but check fired"

    def test_network_policy_check_fires(self, real_checks: list[CheckDefinition]) -> None:
        """sample-app has no NetworkPolicy YAML, so that check must fire."""
        np_checks = [c for c in real_checks if c.pattern == "NetworkPolicy"]
        assert np_checks, "no NetworkPolicy check found"
        findings = run_checks(np_checks, SAMPLE_APP)
        assert len(findings) >= 1

    def test_all_checks_produce_findings_list(self, real_checks: list[CheckDefinition]) -> None:
        """Running all checks must return a list (may be empty or not)."""
        findings = run_checks(real_checks, SAMPLE_APP)
        assert isinstance(findings, list)
        # sample-app is minimal so many checks should fire
        assert len(findings) >= 5

    def test_dimension_grouping_against_fixture(self, real_checks: list[CheckDefinition]) -> None:
        """Grouped findings must key by known dimensions."""
        grouped = run_checks_by_dimension(real_checks, SAMPLE_APP)
        assert isinstance(grouped, dict)
        for dim in grouped:
            assert isinstance(dim, str)
            assert len(grouped[dim]) >= 1

    def test_runner_integration_with_fixture(self) -> None:
        """run_assessment against sample-app with real checks dir produces a report."""
        if not REAL_CHECKS_DIR.is_dir():
            pytest.skip("checks/ dir not found")
        from agentit.runner import run_assessment
        report = run_assessment(
            SAMPLE_APP, repo_url="https://github.com/test/sample-app",
            checks_dir=REAL_CHECKS_DIR,
        )
        assert len(report.scores) == 7
        total_findings = sum(len(s.findings) for s in report.scores)
        assert total_findings >= 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dir_with_check(parent: Path, content: str) -> Path:
    """Create a temp checks dir with a single check file."""
    checks_dir = parent / "test_checks"
    checks_dir.mkdir(exist_ok=True)
    (checks_dir / "check.yaml").write_text(content)
    return checks_dir
