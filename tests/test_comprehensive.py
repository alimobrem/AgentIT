"""Comprehensive test suite: edge cases, error paths, security, data models,
CLI, portal store, hardening agent, and integration."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentit.agents.hardening import HardeningAgent, _sanitize_name
from agentit.analyzers.base import iter_text_files
from agentit.analyzers.security import SecurityAnalyzer, _is_secret_scan_excluded
from agentit.cli import main
from agentit.cloner import CloneError, _validate_repo_url
from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    RemediationItem,
    Severity,
    StackInfo,
)
from agentit.portal.app import _safe_url
from agentit.portal.store import AssessmentStore
from agentit.reporter import render_json_report
from agentit.runner import generate_remediation_plan, run_assessment


# ── helpers ────────────────────────────────────────────────────────────


def _empty_stack() -> StackInfo:
    return StackInfo(
        languages=[], frameworks=[], databases=[], runtimes=[], package_managers=[]
    )


def _empty_arch() -> ArchitectureInfo:
    return ArchitectureInfo(
        service_count=1,
        architecture_style="monolith",
        has_api=False,
        api_style=None,
        external_dependencies=[],
        auth_mechanism=None,
    )


def _make_report(
    repo_name: str = "test-repo",
    scores: list[DimensionScore] | None = None,
    languages: list[Language] | None = None,
    remediation_plan: list[RemediationItem] | None = None,
) -> AssessmentReport:
    stack = StackInfo(
        languages=languages or [],
        frameworks=[],
        databases=[],
        runtimes=[],
        package_managers=[],
    )
    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc),
        stack=stack,
        architecture=_empty_arch(),
        scores=scores or [],
        criticality="medium",
        summary="test",
        remediation_plan=remediation_plan or [],
    )


def _init_git_repo(repo_dir: Path) -> None:
    """Turn *repo_dir* into a valid git repo with an initial commit."""
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "t@t.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "T"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "init"],
        check=True, capture_output=True,
    )


# ═══════════════════════════════════════════════════════════════════════
# Edge Cases (assessor)
# ═══════════════════════════════════════════════════════════════════════


class TestAssessorEdgeCases:
    def test_assess_empty_repo(self, create_mock_repo):
        """Repo with only a README should produce low scores across the board."""
        repo = create_mock_repo({"README.md": "# Hello"})
        report = run_assessment(repo, repo_url="https://example.com/empty.git")

        assert report.stack.languages == []
        # With no manifests / CI / policies every analyzer should penalize
        for s in report.scores:
            assert s.score <= 80, f"{s.dimension} score unexpectedly high: {s.score}"
        assert report.overall_score < 80

    def test_assess_binary_only_repo(self, create_mock_repo):
        """Repo with only binary files should detect no languages."""
        repo = create_mock_repo({
            "image.png": "\x89PNG\r\n\x1a\n" + "\x00" * 100,
            "data.bin": "\x00\xff" * 50,
        })
        report = run_assessment(repo, repo_url="https://example.com/binonly.git")
        assert report.stack.languages == []

    def test_assess_monorepo(self, create_mock_repo):
        """Repo with multiple languages should detect all of them."""
        repo = create_mock_repo({
            "backend/main.go": "package main\nfunc main() {}\n",
            "backend/go.mod": "module example.com/app\n\ngo 1.22\n",
            "frontend/index.ts": "console.log('hello');\n",
            "scripts/build.py": "print('build')\n",
        })
        report = run_assessment(repo, repo_url="https://example.com/mono.git")
        detected_names = {lang.name for lang in report.stack.languages}
        assert "go" in detected_names
        assert "typescript" in detected_names
        assert "python" in detected_names

    def test_assess_max_score(self, create_mock_repo):
        """Fully-equipped enterprise repo should score high."""
        repo = create_mock_repo({
            "main.go": 'package main\nimport "net/http"\nfunc handler(w http.ResponseWriter, r *http.Request) {}\nfunc main() { http.HandleFunc("/", handler) }\n',
            "go.mod": "module example.com/app\n\ngo 1.22\n",
            "Dockerfile": (
                "FROM registry.access.redhat.com/ubi9/go-toolset:latest AS builder\n"
                "WORKDIR /src\nCOPY . .\nRUN go build -o app .\n"
                "FROM registry.access.redhat.com/ubi9/ubi-minimal:9.3\n"
                "COPY --from=builder /src/app /app\n"
                "USER 1001\n"
                "HEALTHCHECK CMD curl -f http://localhost:8080/health\n"
                "ENTRYPOINT [\"/app\"]\n"
            ),
            "deploy/networkpolicy.yaml": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: deny-all\n",
            "deploy/rbac.yaml": "apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\n",
            ".github/workflows/ci.yml": (
                "name: CI\non: push\njobs:\n  scan:\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - uses: aquasecurity/trivy-action@master\n"
            ),
            "deploy/prometheus-rule.yaml": "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\n",
            "deploy/servicemonitor.yaml": "apiVersion: monitoring.coreos.com/v1\nkind: ServiceMonitor\n",
            "helm/Chart.yaml": "apiVersion: v2\nname: app\nversion: 1.0.0\n",
            "helm/values.yaml": "replicaCount: 2\n",
            "terraform/main.tf": 'provider "aws" { region = "us-east-1" }\n',
            "LICENSE": "Apache-2.0\n",
            "SECURITY.md": "# Security Policy\n",
        })
        report = run_assessment(repo, repo_url="https://example.com/enterprise.git")
        assert report.overall_score >= 50, f"Expected high score, got {report.overall_score}"


# ═══════════════════════════════════════════════════════════════════════
# Error Paths
# ═══════════════════════════════════════════════════════════════════════


class TestCloneErrorPaths:
    def test_clone_rejects_file_protocol(self):
        with pytest.raises(CloneError, match="Rejected URL scheme"):
            _validate_repo_url("file:///etc/passwd")

    def test_clone_rejects_ssh_protocol(self):
        with pytest.raises(CloneError, match="Rejected URL scheme"):
            _validate_repo_url("ssh://git@github.com/org/repo.git")

    def test_clone_rejects_ext_protocol(self):
        with pytest.raises(CloneError, match="dangerous pattern"):
            _validate_repo_url("ext::sh -c evil")

    def test_clone_rejects_dash_url(self):
        with pytest.raises(CloneError, match="dash"):
            _validate_repo_url("--upload-pack=evil")

    def test_clone_allows_https(self):
        # Should not raise
        _validate_repo_url("https://github.com/org/repo.git")


# ═══════════════════════════════════════════════════════════════════════
# Security
# ═══════════════════════════════════════════════════════════════════════


class TestSecurity:
    def test_symlink_traversal_blocked(self, create_mock_repo):
        """Symlink pointing outside repo root should be skipped by iter_text_files."""
        repo = create_mock_repo({"legit.py": "x = 1\n"})
        symlink_path = repo / "evil_link.py"
        try:
            symlink_path.symlink_to("/etc/passwd")
        except OSError:
            pytest.skip("Cannot create symlinks on this platform")

        collected = [str(fp) for fp, _ in iter_text_files(repo)]
        assert not any("passwd" in p for p in collected)
        assert not any("evil_link" in p for p in collected)

    def test_secret_scanner_skips_test_dirs(self, create_mock_repo):
        """Secrets inside tests/ directories should not be flagged."""
        repo = create_mock_repo({
            "tests/test_auth.py": 'password = "supersecretvalue123"\n',
        })
        analyzer = SecurityAnalyzer()
        score = analyzer.analyze(repo)
        secret_findings = [f for f in score.findings if f.category == "secrets"]
        assert len(secret_findings) == 0

    def test_secret_scanner_skips_helm_templates(self, create_mock_repo):
        """Helm template expressions like {{ .Values.password }} should not flag."""
        repo = create_mock_repo({
            "helm/templates/secret.yaml": (
                "apiVersion: v1\nkind: Secret\ndata:\n"
                '  password: {{ .Values.password | b64enc }}\n'
            ),
        })
        analyzer = SecurityAnalyzer()
        score = analyzer.analyze(repo)
        secret_findings = [f for f in score.findings if f.category == "secrets"]
        assert len(secret_findings) == 0

    def test_secret_scanner_detects_real_hardcoded_password(self, create_mock_repo):
        """A real hardcoded password literal should be flagged."""
        repo = create_mock_repo({
            "config.py": 'DB_PASSWORD = "realSecret123!"\n',
        })
        analyzer = SecurityAnalyzer()
        score = analyzer.analyze(repo)
        secret_findings = [f for f in score.findings if f.category == "secrets"]
        assert len(secret_findings) >= 1
        assert any(f.severity == Severity.critical for f in secret_findings)

    def test_safe_url_filter_blocks_javascript(self):
        assert _safe_url("javascript:alert(1)") == "#"

    def test_safe_url_filter_allows_https(self):
        url = "https://github.com/org/repo"
        assert _safe_url(url) == url


# ═══════════════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════════════


class TestDataModels:
    def test_assessment_report_json_roundtrip_with_findings(self):
        """Full report with findings should survive serialize/deserialize."""
        scores = [
            DimensionScore(
                dimension="security",
                score=45,
                max_score=100,
                findings=[
                    Finding(
                        category="secrets",
                        severity=Severity.critical,
                        description="Hardcoded API key",
                        file_path="config.py",
                        recommendation="Use vault",
                    ),
                    Finding(
                        category="container",
                        severity=Severity.high,
                        description="Running as root",
                        file_path="Dockerfile",
                        recommendation="Add USER 1001",
                    ),
                ],
            ),
            DimensionScore(
                dimension="observability",
                score=70,
                max_score=100,
                findings=[],
            ),
        ]
        report = _make_report(
            scores=scores,
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            remediation_plan=[
                RemediationItem(
                    priority=1,
                    dimension="security",
                    description="Fix secrets",
                    estimated_effort="2 agent-hours",
                    agent_responsible="Security Hardening Agent",
                ),
            ],
        )

        json_str = report.model_dump_json()
        restored = AssessmentReport.model_validate_json(json_str)

        assert restored.repo_name == report.repo_name
        assert restored.overall_score == report.overall_score
        assert len(restored.scores) == 2
        assert len(restored.scores[0].findings) == 2
        assert restored.scores[0].findings[0].severity == Severity.critical
        assert restored.remediation_plan[0].dimension == "security"

    def test_dimension_score_clamp_negative(self):
        score = DimensionScore(
            dimension="security", score=-50, max_score=100, findings=[]
        )
        assert score.score == 0

    def test_dimension_score_clamp_over_100(self):
        score = DimensionScore(
            dimension="security", score=150, max_score=100, findings=[]
        )
        assert score.score == 100

    def test_remediation_plan_sorted_by_severity(self):
        """generate_remediation_plan should order critical before high before medium."""
        scores = [
            DimensionScore(
                dimension="security",
                score=30,
                max_score=100,
                findings=[
                    Finding(
                        category="a",
                        severity=Severity.medium,
                        description="medium issue",
                        recommendation="fix",
                    ),
                    Finding(
                        category="b",
                        severity=Severity.critical,
                        description="critical issue",
                        recommendation="fix now",
                    ),
                    Finding(
                        category="c",
                        severity=Severity.high,
                        description="high issue",
                        recommendation="fix soon",
                    ),
                ],
            ),
        ]
        plan = generate_remediation_plan(scores)
        assert len(plan) == 3
        assert "critical" in plan[0].description
        assert "high" in plan[1].description
        assert "medium" in plan[2].description


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


class TestCLI:
    def test_cli_assess_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["assess", "--help"])
        assert result.exit_code == 0
        assert "--criticality" in result.output
        assert "--format" in result.output
        assert "--output" in result.output

    def test_cli_harden_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["harden", "--help"])
        assert result.exit_code == 0
        assert "--output-dir" in result.output

    def test_cli_onboard_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["onboard", "--help"])
        assert result.exit_code == 0
        assert "--output-dir" in result.output
        assert "--criticality" in result.output

    def test_cli_watch_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["watch", "--help"])
        assert result.exit_code == 0
        assert "--interval" in result.output
        assert "--webhook" in result.output


# ═══════════════════════════════════════════════════════════════════════
# Portal Store
# ═══════════════════════════════════════════════════════════════════════


class TestPortalStore:
    def _store(self) -> AssessmentStore:
        return AssessmentStore(db_path=":memory:")

    def test_store_save_and_get_roundtrip(self):
        store = self._store()
        report = _make_report(
            scores=[
                DimensionScore(
                    dimension="security",
                    score=42,
                    max_score=100,
                    findings=[
                        Finding(
                            category="test",
                            severity=Severity.high,
                            description="test finding",
                            recommendation="fix",
                        )
                    ],
                )
            ],
            languages=[Language(name="go", file_count=5, percentage=100.0)],
        )
        aid = store.save(report)
        assert aid

        restored = store.get(aid)
        assert restored is not None
        assert restored.repo_name == "test-repo"
        assert restored.scores[0].score == 42
        assert len(restored.scores[0].findings) == 1

    def test_store_list_all_ordering(self):
        store = self._store()
        r1 = _make_report(repo_name="first")
        r1.assessed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        r2 = _make_report(repo_name="second")
        r2.assessed_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
        store.save(r1)
        store.save(r2)

        items = store.list_all()
        assert len(items) == 2
        # newest first (DESC)
        assert items[0]["repo_name"] == "second"
        assert items[1]["repo_name"] == "first"

    def test_store_delete(self):
        store = self._store()
        aid = store.save(_make_report())
        assert store.get(aid) is not None

        ok = store.delete(aid)
        assert ok is True
        assert store.get(aid) is None

    def test_store_event_logging(self):
        store = self._store()
        store.log_event("agent-a", "scan", "app1", "info", "Scanned app1")
        store.log_event("agent-b", "deploy", "app2", "warning", "Deployed app2")
        store.log_event("agent-a", "remediate", "app1", "info", "Fixed secrets")

        events = store.list_events()
        assert len(events) >= 3

    def test_store_gate_lifecycle(self):
        store = self._store()
        aid = store.save(_make_report())
        gid = store.create_gate(aid, "deploy", "Approve deploy?")

        pending = store.list_gates(status="pending")
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"

        ok = store.resolve_gate(gid, "approved", "admin")
        assert ok is True

        assert store.list_gates(status="pending") == []
        resolved = store.list_gates(status="approved")
        assert len(resolved) == 1
        assert resolved[0]["resolved_by"] == "admin"


# ═══════════════════════════════════════════════════════════════════════
# Hardening Agent
# ═══════════════════════════════════════════════════════════════════════


class TestHardeningAgent:
    def test_sanitize_name_strips_hyphens(self):
        assert _sanitize_name("---") == "app"

    def test_sanitize_name_truncates(self):
        long_name = "a" * 100
        result = _sanitize_name(long_name)
        assert len(result) <= 63

    def test_sanitize_name_dots_and_underscores(self):
        assert _sanitize_name("my.app_name") == "my-app-name"

    def test_hardening_python_containerfile_uses_ubi(self, tmp_path: Path):
        report = _make_report(
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            scores=[
                DimensionScore(
                    dimension="security",
                    score=30,
                    max_score=100,
                    findings=[
                        Finding(
                            category="container",
                            severity=Severity.high,
                            description="No Dockerfile found",
                            recommendation="Add Containerfile",
                        )
                    ],
                )
            ],
        )
        result = HardeningAgent(report, tmp_path / "out").run()
        cf = [f for f in result.files if f.path == "Containerfile"]
        assert len(cf) == 1
        assert "registry.access.redhat.com" in cf[0].content


# ═══════════════════════════════════════════════════════════════════════
# Integration
# ═══════════════════════════════════════════════════════════════════════


class TestIntegration:
    def test_full_assess_to_onboard_flow(self, tmp_path: Path):
        """Create local repo -> assess -> verify report -> run agents -> check output."""
        repo_dir = tmp_path / "integration_repo"
        repo_dir.mkdir()

        # Create a realistic Go project
        (repo_dir / "go.mod").write_text(
            "module github.com/test/integration-app\n\ngo 1.22\n"
        )
        (repo_dir / "main.go").write_text(
            'package main\n\nimport "net/http"\n\n'
            "func handler(w http.ResponseWriter, r *http.Request) {}\n\n"
            "func main() {\n"
            '\thttp.HandleFunc("/", handler)\n'
            '\thttp.ListenAndServe(":8080", nil)\n'
            "}\n"
        )
        (repo_dir / "Dockerfile").write_text(
            "FROM golang:1.22\nWORKDIR /app\nCOPY . .\n"
            "RUN go build -o app .\nCMD [\"./app\"]\n"
        )

        # 1. Run assessment
        report = run_assessment(
            repo_dir,
            repo_url="https://github.com/test/integration-app.git",
            criticality="high",
        )

        assert report.repo_name == "integration-app"
        assert len(report.scores) == 7
        assert report.overall_score > 0

        # Languages detected
        lang_names = {l.name for l in report.stack.languages}
        assert "go" in lang_names

        # Should have findings (Dockerfile lacks USER, no trivy, etc.)
        total_findings = sum(len(s.findings) for s in report.scores)
        assert total_findings > 0

        # JSON roundtrip
        json_str = render_json_report(report)
        restored = AssessmentReport.model_validate_json(json_str)
        assert restored.repo_name == report.repo_name

        # 2. Run hardening agent
        from agentit.agents.hardening import HardeningAgent
        from agentit.agents.observability import ObservabilityAgent
        from agentit.agents.cicd import CICDAgent
        from agentit.agents.compliance import ComplianceAgent

        out = tmp_path / "onboard_output"
        agents = [
            ("security", HardeningAgent),
            ("observability", ObservabilityAgent),
            ("cicd", CICDAgent),
            ("compliance", ComplianceAgent),
        ]

        all_output_files: list[str] = []
        for subdir, agent_cls in agents:
            sub_path = out / subdir
            result = agent_cls(report=report, output_dir=sub_path).run()
            assert sub_path.exists(), f"{subdir} output dir not created"
            for gf in result.files:
                full_path = sub_path / gf.path
                assert full_path.exists(), f"Expected file {full_path} not found"
                all_output_files.append(gf.path)

        # Hardening should produce at least rbac.yaml and security-context.yaml
        assert "rbac.yaml" in all_output_files
        assert "security-context.yaml" in all_output_files

        # Should have generated a Containerfile (original Dockerfile lacks USER)
        assert "Containerfile" in all_output_files
