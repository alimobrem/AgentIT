"""Tests for all CLI commands — help output and basic invocation."""

from __future__ import annotations

from click.testing import CliRunner

from agentit.cli import main


runner = CliRunner()


class TestAllCommandsExist:
    def test_assess_help(self):
        result = runner.invoke(main, ["assess", "--help"])
        assert result.exit_code == 0
        assert "Assess enterprise readiness" in result.output

    def test_harden_help(self):
        result = runner.invoke(main, ["harden", "--help"])
        assert result.exit_code == 0
        assert "hardening manifests" in result.output

    def test_portal_help(self):
        result = runner.invoke(main, ["portal", "--help"])
        assert result.exit_code == 0
        assert "Launch" in result.output

    def test_watch_help(self):
        result = runner.invoke(main, ["watch", "--help"])
        assert result.exit_code == 0
        assert "Continuously" in result.output

    def test_orchestrate_help(self):
        result = runner.invoke(main, ["orchestrate", "--help"])
        assert result.exit_code == 0
        assert "Fleet Orchestrator" in result.output

    def test_onboard_help(self):
        result = runner.invoke(main, ["onboard", "--help"])
        assert result.exit_code == 0
        assert "onboarding" in result.output.lower()

    def test_vuln_watch_help(self):
        result = runner.invoke(main, ["vuln-watch", "--help"])
        assert result.exit_code == 0
        assert "vulnerability" in result.output.lower()

    def test_slo_track_help(self):
        result = runner.invoke(main, ["slo-track", "--help"])
        assert result.exit_code == 0
        assert "SLO" in result.output

    def test_drift_detect_help(self):
        result = runner.invoke(main, ["drift-detect", "--help"])
        assert result.exit_code == 0
        assert "drift" in result.output.lower()

    def test_self_assess_help(self):
        result = runner.invoke(main, ["self-assess", "--help"])
        assert result.exit_code == 0
        assert "AgentIT itself" in result.output

    def test_main_help(self):
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Enterprise Readiness" in result.output
