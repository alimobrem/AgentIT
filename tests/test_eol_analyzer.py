"""Tests for EOL (end-of-life) software detection: the deterministic
baseline scan in ``agentit.analyzers.eol``, the LLM-assisted path it
delegates to, and the ``InfrastructureAnalyzer``/``run_assessment`` wiring
that exercises both for real."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentit.analyzers import eol
from agentit.analyzers.infrastructure import InfrastructureAnalyzer
from agentit.models import Severity


# ---------------------------------------------------------------------------
# Deterministic baseline scan
# ---------------------------------------------------------------------------


class TestBaselineDockerfile:
    def test_detects_eol_python_base_image(self, create_mock_repo) -> None:
        repo = create_mock_repo({"Dockerfile": "FROM python:3.8-slim\nCMD [\"python\", \"app.py\"]\n"})
        findings = eol.baseline_findings(repo, today=date(2025, 1, 1))
        assert len(findings) == 1
        assert findings[0].category == "eol"
        assert findings[0].severity == Severity.high
        assert "python 3.8" in findings[0].description
        assert "2024-10-07" in findings[0].description
        assert findings[0].file_path == "Dockerfile"

    def test_detects_approaching_eol_within_window(self, create_mock_repo) -> None:
        """Node 20 EOL is 2026-04-30; 90 days earlier is inside the 180-day window."""
        repo = create_mock_repo({"Dockerfile": "FROM node:20-alpine\n"})
        findings = eol.baseline_findings(repo, today=date(2026, 1, 30))
        assert len(findings) == 1
        assert findings[0].severity == Severity.medium
        assert "approaching end-of-life" in findings[0].description

    def test_no_finding_outside_approaching_window(self, create_mock_repo) -> None:
        repo = create_mock_repo({"Dockerfile": "FROM node:20-alpine\n"})
        findings = eol.baseline_findings(repo, today=date(2025, 1, 1))
        assert findings == []

    def test_no_finding_for_version_not_in_table(self, create_mock_repo) -> None:
        """Never fabricate an EOL date for a version we don't have real data for."""
        repo = create_mock_repo({"Dockerfile": "FROM python:3.13-slim\n"})
        findings = eol.baseline_findings(repo, today=date(2025, 1, 1))
        assert findings == []

    def test_ignores_unknown_base_images(self, create_mock_repo) -> None:
        repo = create_mock_repo({"Dockerfile": "FROM registry.access.redhat.com/ubi9/ubi-minimal:9.4\n"})
        findings = eol.baseline_findings(repo, today=date(2025, 1, 1))
        assert findings == []

    def test_detects_ubuntu_centos_alpine(self, create_mock_repo) -> None:
        repo = create_mock_repo({
            "Dockerfile.ubuntu": "FROM ubuntu:18.04\n",
            "Dockerfile.centos": "FROM centos:7\n",
            "Dockerfile.alpine": "FROM alpine:3.15\n",
        })
        findings = eol.baseline_findings(repo, today=date(2025, 6, 1))
        components = {f.description.split()[0] for f in findings}
        assert components == {"ubuntu", "centos", "alpine"}
        assert all(f.severity == Severity.high for f in findings)

    def test_containerfile_is_also_scanned(self, create_mock_repo) -> None:
        repo = create_mock_repo({"Containerfile": "FROM centos:7\n"})
        findings = eol.baseline_findings(repo, today=date(2025, 1, 1))
        assert len(findings) == 1
        assert findings[0].file_path == "Containerfile"

    def test_no_dockerfile_no_findings(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.py": "print('hi')\n"})
        assert eol.baseline_findings(repo, today=date(2025, 1, 1)) == []


class TestBaselineLanguageFiles:
    def test_python_version_file(self, create_mock_repo) -> None:
        repo = create_mock_repo({".python-version": "3.8.10\n"})
        findings = eol.baseline_findings(repo, today=date(2025, 1, 1))
        assert len(findings) == 1
        assert findings[0].file_path == ".python-version"

    def test_runtime_txt(self, create_mock_repo) -> None:
        repo = create_mock_repo({"runtime.txt": "python-3.7.9\n"})
        findings = eol.baseline_findings(repo, today=date(2024, 1, 1))
        assert len(findings) == 1
        assert findings[0].file_path == "runtime.txt"

    def test_pyproject_toml_requires_python(self, create_mock_repo) -> None:
        repo = create_mock_repo({
            "pyproject.toml": "[project]\nname = \"app\"\nrequires-python = \">=3.8\"\n",
        })
        findings = eol.baseline_findings(repo, today=date(2025, 1, 1))
        assert len(findings) == 1
        assert findings[0].file_path == "pyproject.toml"

    def test_supported_python_version_no_finding(self, create_mock_repo) -> None:
        repo = create_mock_repo({".python-version": "3.12.4\n"})
        assert eol.baseline_findings(repo, today=date(2025, 1, 1)) == []

    def test_package_json_node_engine(self, create_mock_repo) -> None:
        repo = create_mock_repo({
            "package.json": '{"name": "app", "engines": {"node": ">=14.0.0"}}',
        })
        findings = eol.baseline_findings(repo, today=date(2024, 1, 1))
        assert len(findings) == 1
        assert findings[0].file_path == "package.json"
        assert "node 14" in findings[0].description

    def test_package_json_without_engines_no_finding(self, create_mock_repo) -> None:
        repo = create_mock_repo({"package.json": '{"name": "app"}'})
        assert eol.baseline_findings(repo, today=date(2025, 1, 1)) == []


# ---------------------------------------------------------------------------
# LLM-assisted path
# ---------------------------------------------------------------------------


class TestLlmFindings:
    def test_no_context_files_short_circuits_without_calling_llm(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.py": "print('hi')\n"})
        fake_llm = MagicMock()
        result = eol.llm_findings(repo, fake_llm, stack_info={})
        assert result == []
        fake_llm.detect_eol_risks.assert_not_called()

    def test_merges_high_confidence_risk(self, create_mock_repo) -> None:
        repo = create_mock_repo({"Dockerfile": "FROM python:3.12\n"})
        fake_llm = MagicMock()
        fake_llm.detect_eol_risks.return_value = [
            {
                "component": "Django",
                "version": "3.2",
                "status": "eol",
                "eol_date": "2024-04-01",
                "confidence": 0.9,
                "reason": "Django 3.2 LTS support ended per Django project's release roadmap",
            },
        ]
        findings = eol.llm_findings(repo, fake_llm, stack_info={"frameworks": [{"name": "django"}]})
        assert len(findings) == 1
        assert findings[0].source == "analyzer:infrastructure:eol-llm"
        assert findings[0].severity == Severity.high
        assert "Django 3.2" in findings[0].description

    def test_low_confidence_risk_filtered_out(self, create_mock_repo) -> None:
        repo = create_mock_repo({"Dockerfile": "FROM python:3.12\n"})
        fake_llm = MagicMock()
        fake_llm.detect_eol_risks.return_value = [
            {"component": "Django", "version": "3.2", "status": "eol",
             "eol_date": "2024-04-01", "confidence": 0.2, "reason": "not sure"},
        ]
        assert eol.llm_findings(repo, fake_llm, stack_info={}) == []

    def test_llm_returning_none_propagates_none(self, create_mock_repo) -> None:
        """None means 'unavailable/failed' -- caller must fall back to baseline only."""
        repo = create_mock_repo({"Dockerfile": "FROM python:3.12\n"})
        fake_llm = MagicMock()
        fake_llm.detect_eol_risks.return_value = None
        assert eol.llm_findings(repo, fake_llm, stack_info={}) is None

    def test_llm_returning_empty_list_is_a_real_no_risk_answer(self, create_mock_repo) -> None:
        repo = create_mock_repo({"Dockerfile": "FROM python:3.12\n"})
        fake_llm = MagicMock()
        fake_llm.detect_eol_risks.return_value = []
        assert eol.llm_findings(repo, fake_llm, stack_info={}) == []

    def test_unexpected_non_list_response_treated_as_none(self, create_mock_repo) -> None:
        """A plain MagicMock() (no return_value configured) is truthy but
        not a list -- this must degrade gracefully, not raise or misbehave,
        since that's exactly the shape an unconfigured test double or a
        misbehaving LLM client integration would produce."""
        repo = create_mock_repo({"Dockerfile": "FROM python:3.12\n"})
        fake_llm = MagicMock()  # .detect_eol_risks(...) returns a bare MagicMock
        assert eol.llm_findings(repo, fake_llm, stack_info={}) is None


# ---------------------------------------------------------------------------
# InfrastructureAnalyzer wiring
# ---------------------------------------------------------------------------


class TestInfrastructureAnalyzerEol:
    def test_baseline_only_without_llm_client(self, create_mock_repo) -> None:
        repo = create_mock_repo({
            "Dockerfile": "FROM python:3.6\n",  # long-EOL (2021-12-23); safe for any real 'today'
            "chart/Chart.yaml": "apiVersion: v2\nname: app\nversion: 1.0.0\n",
        })
        analyzer = InfrastructureAnalyzer()
        score = analyzer.analyze(repo)
        eol_findings = [f for f in score.findings if f.category == "eol"]
        assert len(eol_findings) == 1
        assert eol_findings[0].source == "analyzer:infrastructure:eol-baseline"

    def test_llm_extra_findings_merged_alongside_baseline(self, create_mock_repo) -> None:
        repo = create_mock_repo({
            "Dockerfile": "FROM python:3.6\n",
            "chart/Chart.yaml": "apiVersion: v2\nname: app\nversion: 1.0.0\n",
        })
        fake_llm = MagicMock()
        fake_llm.detect_eol_risks.return_value = [
            {"component": "Flask", "version": "1.0", "status": "eol",
             "eol_date": "2020-01-01", "confidence": 0.85, "reason": "superseded by 2.x"},
        ]
        analyzer = InfrastructureAnalyzer(llm_client=fake_llm)
        score = analyzer.analyze(repo)
        eol_findings = [f for f in score.findings if f.category == "eol"]
        sources = {f.source for f in eol_findings}
        assert "analyzer:infrastructure:eol-baseline" in sources
        assert "analyzer:infrastructure:eol-llm" in sources
        assert len(eol_findings) == 2
        fake_llm.detect_eol_risks.assert_called_once()

    def test_survives_unconfigured_llm_mock_without_crashing(self, create_mock_repo) -> None:
        """A bare MagicMock() llm_client (e.g. a caller that didn't set up
        .detect_eol_risks) must never crash analyze() -- mirrors the
        broken_llm scenario in tests/test_llm_graceful.py."""
        repo = create_mock_repo({"Dockerfile": "FROM python:3.6\n"})
        analyzer = InfrastructureAnalyzer(llm_client=MagicMock())
        score = analyzer.analyze(repo)  # must not raise
        eol_findings = [f for f in score.findings if f.category == "eol"]
        assert len(eol_findings) == 1  # baseline still fires; broken LLM path contributes nothing

    def test_no_llm_call_when_no_relevant_files_present(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.py": "print('hi')\n"})
        fake_llm = MagicMock()
        analyzer = InfrastructureAnalyzer(llm_client=fake_llm)
        analyzer.analyze(repo)
        fake_llm.detect_eol_risks.assert_not_called()


# ---------------------------------------------------------------------------
# run_assessment wiring (regression guard: an LLM client passed to
# run_assessment must actually reach the EOL detector, mirroring the exact
# bug this repo previously had with FleetOrchestrator never forwarding its
# LLM client to SkillEngine -- see tests/test_orchestrator.py
# TestSkillsFirstLLMPassthrough)
# ---------------------------------------------------------------------------


class TestRunAssessmentForwardsLlmClientToEol:
    def test_llm_client_reaches_infrastructure_eol_detection(self, create_mock_repo) -> None:
        repo = create_mock_repo({
            "Dockerfile": "FROM python:3.12\n",  # nothing in the baseline table fires
        })
        fake_llm = MagicMock()
        fake_llm.detect_eol_risks.return_value = [
            {"component": "some-framework", "version": "1.0", "status": "eol",
             "eol_date": "2023-01-01", "confidence": 0.95, "reason": "test reason"},
        ]
        fake_llm.classify_secret.return_value = None
        fake_llm.summarize_architecture.return_value = None

        from agentit.runner import run_assessment
        report = run_assessment(
            repo, repo_url="https://github.com/test/app", criticality="medium",
            llm_client=fake_llm,
        )
        infra_score = next(s for s in report.scores if s.dimension == "infrastructure")
        eol_llm_findings = [f for f in infra_score.findings if f.source == "analyzer:infrastructure:eol-llm"]
        assert len(eol_llm_findings) == 1
        fake_llm.detect_eol_risks.assert_called_once()

    def test_no_llm_client_still_runs_baseline(self, create_mock_repo) -> None:
        repo = create_mock_repo({"Dockerfile": "FROM centos:7\n"})
        from agentit.runner import run_assessment
        report = run_assessment(
            repo, repo_url="https://github.com/test/app", criticality="medium",
            llm_client=None,
        )
        infra_score = next(s for s in report.scores if s.dimension == "infrastructure")
        eol_findings = [f for f in infra_score.findings if f.category == "eol"]
        assert len(eol_findings) == 1
        assert eol_findings[0].source == "analyzer:infrastructure:eol-baseline"


# ---------------------------------------------------------------------------
# LLMClient.detect_eol_risks (mirrors tests/test_llm.py's mocking convention)
# ---------------------------------------------------------------------------


def _mock_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def _make_client(mock_create: MagicMock):
    from unittest.mock import patch

    from agentit.llm import LLMClient
    with patch("agentit.llm._create_client") as mock_factory:
        client_mock = MagicMock()
        client_mock.messages.create = mock_create
        mock_factory.return_value = client_mock
        return LLMClient(model="test-model")


class TestLLMClientDetectEolRisks:
    def test_parses_json_risks(self) -> None:
        import json
        response_json = json.dumps({
            "risks": [
                {"component": "Django", "version": "3.2", "status": "eol",
                 "eol_date": "2024-04-01", "confidence": 0.9, "reason": "LTS ended"},
            ],
        })
        client = _make_client(MagicMock(return_value=_mock_response(response_json)))
        result = client.detect_eol_risks({"languages": []}, {"Dockerfile": "FROM python:3.12"})
        assert result is not None
        assert len(result) == 1
        assert result[0]["component"] == "Django"
        assert result[0]["confidence"] == pytest.approx(0.9)

    def test_empty_risks_list_is_valid(self) -> None:
        import json
        client = _make_client(MagicMock(return_value=_mock_response(json.dumps({"risks": []}))))
        result = client.detect_eol_risks({"languages": []}, {})
        assert result == []

    def test_bad_json_returns_none(self) -> None:
        client = _make_client(MagicMock(return_value=_mock_response("not valid json at all")))
        result = client.detect_eol_risks({"languages": []}, {})
        assert result is None

    def test_llm_unavailable_returns_none(self) -> None:
        import anthropic
        client = _make_client(MagicMock(side_effect=anthropic.APIConnectionError(request=MagicMock())))
        result = client.detect_eol_risks({"languages": []}, {})
        assert result is None
