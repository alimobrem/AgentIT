"""Tests that LLM failures never crash assessments or the portal."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_llm_client_init_failure_returns_none():
    """Portal _get_llm_client returns None when LLM init throws."""
    with patch.dict("os.environ", {"ANTHROPIC_VERTEX_PROJECT_ID": "test-project", "CLOUD_ML_REGION": "us-east5"}):
        with patch("agentit.llm._create_client", side_effect=Exception("DefaultCredentialsError: no creds")):
            from agentit.portal.app import _get_llm_client
            result = _get_llm_client()
            assert result is None


def test_llm_chat_failure_returns_none():
    """LLMClient._chat returns None on any exception, not just APIError."""
    with patch("agentit.llm._create_client") as mock_factory:
        client_mock = MagicMock()
        client_mock.messages.create.side_effect = Exception("DefaultCredentialsError: no creds")
        mock_factory.return_value = client_mock

        from agentit.llm import LLMClient
        client = LLMClient(model="test")
        result = client.classify_secret("file.py", "password=secret123", ["line1", "password=secret123"])
        assert result is None


def test_llm_chat_failure_does_not_crash_summarize():
    """summarize_architecture returns None on LLM failure."""
    with patch("agentit.llm._create_client") as mock_factory:
        client_mock = MagicMock()
        client_mock.messages.create.side_effect = Exception("connection refused")
        mock_factory.return_value = client_mock

        from agentit.llm import LLMClient
        client = LLMClient(model="test")
        result = client.summarize_architecture({"languages": []}, ["file.py"])
        assert result is None


def test_assessment_succeeds_without_llm(create_mock_repo):
    """Full assessment works when LLM is None."""
    repo = create_mock_repo({
        "main.go": "package main\nfunc main() {}\n",
        "go.mod": "module test\n\ngo 1.22\n",
    })
    from agentit.runner import run_assessment
    report = run_assessment(repo, repo_url="https://github.com/test/app", criticality="medium", llm_client=None)
    assert report.overall_score > 0
    assert len(report.scores) == 7


def test_assessment_succeeds_with_broken_llm(create_mock_repo):
    """Full assessment works when LLM throws on every call."""
    repo = create_mock_repo({
        "main.go": "package main\nfunc main() {}\n",
        "go.mod": "module test\n\ngo 1.22\n",
    })
    broken_llm = MagicMock()
    broken_llm.classify_secret.return_value = None
    broken_llm.summarize_architecture.return_value = None

    from agentit.runner import run_assessment
    report = run_assessment(repo, repo_url="https://github.com/test/app", criticality="medium", llm_client=broken_llm)
    assert report.overall_score > 0
    assert len(report.scores) == 7


def test_portal_assess_works_without_gcp_creds():
    """Portal assessment doesn't crash when GCP creds are missing."""
    with patch.dict("os.environ", {"ANTHROPIC_VERTEX_PROJECT_ID": "test", "CLOUD_ML_REGION": "global"}, clear=False):
        with patch("agentit.llm._create_client", side_effect=Exception("no credentials")):
            from agentit.portal.app import _get_llm_client
            client = _get_llm_client()
            assert client is None


def test_safe_url_filter_blocks_javascript():
    """XSS prevention: javascript: URIs are blocked."""
    from agentit.portal.app import _safe_url
    assert _safe_url("javascript:alert(1)") == "#"
    assert _safe_url("https://github.com/org/repo") == "https://github.com/org/repo"
    assert _safe_url("http://example.com") == "http://example.com"
    assert _safe_url("data:text/html,<h1>hi</h1>") == "#"
