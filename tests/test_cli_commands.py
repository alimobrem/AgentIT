"""Tests for all CLI commands — help output and basic invocation."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from agentit.cli import main

_COMMANDS = [
    ("assess", "Assess enterprise readiness"),
    ("portal", "Launch"),
    ("watch", "Continuously"),
    ("orchestrate", "Fleet Orchestrator"),
    ("onboard", "onboarding"),
    ("vuln-watch", "vulnerability"),
    ("slo-track", "SLO"),
    ("drift-detect", "drift"),
    ("self-assess", "AgentIT itself"),
    ("--help", "Enterprise Readiness"),
]


@pytest.mark.parametrize("cmd,expected", _COMMANDS, ids=[c[0] for c in _COMMANDS])
def test_command_help(cmd, expected):
    runner = CliRunner()
    args = [cmd, "--help"] if not cmd.startswith("--") else [cmd]
    result = runner.invoke(main, args)
    assert result.exit_code == 0
    assert expected.lower() in result.output.lower()


_FLAG_CHECKS = [
    ("assess", ["--criticality", "--format", "--output"]),
    ("onboard", ["--output-dir", "--criticality"]),
    ("watch", ["--interval", "--webhook"]),
]


@pytest.mark.parametrize("cmd,flags", _FLAG_CHECKS, ids=[c[0] for c in _FLAG_CHECKS])
def test_command_flags(cmd, flags):
    runner = CliRunner()
    result = runner.invoke(main, [cmd, "--help"])
    assert result.exit_code == 0
    for flag in flags:
        assert flag in result.output, f"{flag} not in {cmd} --help"
