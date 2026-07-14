from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agentit.llm import LLMClient


def _mock_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def _make_client(mock_create: MagicMock) -> LLMClient:
    with patch("agentit.llm._create_client") as mock_factory:
        client_mock = MagicMock()
        client_mock.messages.create = mock_create
        mock_factory.return_value = client_mock
        return LLMClient(model="test-model")


def test_classify_secret_real_secret():
    response = _mock_response(json.dumps({"is_secret": True, "confidence": 0.95, "reason": "Hardcoded AWS key"}))
    client = _make_client(MagicMock(return_value=response))
    result = client.classify_secret("config.yaml", "aws_key = AKIAIOSFODNN7EXAMPLE", ["# config", "aws_key = AKIAIOSFODNN7EXAMPLE"])

    assert result is not None
    assert result["is_secret"] is True
    assert result["confidence"] == pytest.approx(0.95)


def test_classify_secret_false_positive():
    response = _mock_response(json.dumps({"is_secret": False, "confidence": 0.9, "reason": "Variable reference"}))
    client = _make_client(MagicMock(return_value=response))
    result = client.classify_secret("app.py", 'password = os.getenv("DB_PASSWORD")', ['import os', 'password = os.getenv("DB_PASSWORD")'])

    assert result is not None
    assert result["is_secret"] is False
    assert result["confidence"] == pytest.approx(0.9)


def test_llm_unavailable_returns_none():
    import anthropic
    client = _make_client(MagicMock(side_effect=anthropic.APIConnectionError(request=MagicMock())))
    result = client.classify_secret("f.py", "secret=abc123xyz789!!", [])

    assert result is None


def test_summarize_architecture():
    summary = "A Go microservice exposing a REST API backed by PostgreSQL."
    client = _make_client(MagicMock(return_value=_mock_response(summary)))
    result = client.summarize_architecture(
        {"languages": [{"name": "go"}], "databases": [{"name": "postgresql"}]},
        ["main.go", "handler.go", "db.go"],
    )

    assert result == summary


def test_classify_secret_bad_json_returns_none():
    client = _make_client(MagicMock(return_value=_mock_response("I'm not sure, maybe it's a secret?")))
    result = client.classify_secret("f.py", "key=abc", [])

    assert result is None


# ── propose_capability_improvement (capability-scout) ─────────────────────


def test_propose_capability_improvement_returns_structured_proposal():
    response = _mock_response(json.dumps({
        "has_proposal": True,
        "title": "Track stack signatures",
        "gap_description": "README documents an auto-trigger idea that was never built",
        "evidence": "README.md:42 — Documented future idea (not built)",
        "target_files": ["src/agentit/portal/store.py", "tests/test_store.py"],
        "change_summary": "Add a new counter and a threshold check",
        "risk": "low",
        "test_plan": "Assert the threshold logic in a new test",
    }))
    client = _make_client(MagicMock(return_value=response))
    result = client.propose_capability_improvement({"doc_gaps": [{"file": "README.md", "line_no": 42}]})

    assert result is not None
    assert result["has_proposal"] is True
    assert result["title"] == "Track stack signatures"
    assert result["target_files"] == ["src/agentit/portal/store.py", "tests/test_store.py"]
    assert result["risk"] == "low"


def test_propose_capability_improvement_no_proposal_is_valid():
    response = _mock_response(json.dumps({"has_proposal": False}))
    client = _make_client(MagicMock(return_value=response))
    result = client.propose_capability_improvement({"doc_gaps": []})

    assert result == {"has_proposal": False}


def test_propose_capability_improvement_llm_unavailable_returns_none():
    import anthropic
    client = _make_client(MagicMock(side_effect=anthropic.APIConnectionError(request=MagicMock())))
    result = client.propose_capability_improvement({"doc_gaps": []})

    assert result is None


def test_propose_capability_improvement_bad_json_returns_none():
    client = _make_client(MagicMock(return_value=_mock_response("not valid json at all")))
    result = client.propose_capability_improvement({"doc_gaps": []})

    assert result is None


def test_propose_capability_improvement_missing_required_field_returns_none():
    response = _mock_response(json.dumps({"has_proposal": True, "title": "Only a title"}))
    client = _make_client(MagicMock(return_value=response))
    result = client.propose_capability_improvement({"doc_gaps": []})

    assert result is None


# ── classify_action / review_fix safety-gate fail-closed regression ──────
#
# README.md previously mislabeled llm.py as "fail-open" in its repository-layout
# comment. It's actually fail-closed: both classify_action() and review_fix()
# return None on any _chat() failure or unparseable response, and every caller
# (AutoMode.should_auto_apply in automode.py, cli.py's self-fix Step 3) treats
# None as the destructive/rejected outcome, never a silent "safe" default.
# These tests pin that contract directly against LLMClient so a future change
# to _chat()/classify_action()/review_fix() that silently started defaulting
# to "safe" would fail loudly here.


def test_classify_action_timeout_returns_none():
    import anthropic
    import httpx

    timeout_exc = anthropic.APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))
    client = _make_client(MagicMock(side_effect=timeout_exc))
    result = client.classify_action("apply", ["kind: Deployment"], "App: demo, Criticality: high")

    assert result is None


def test_classify_action_connection_error_returns_none():
    import anthropic
    client = _make_client(MagicMock(side_effect=anthropic.APIConnectionError(request=MagicMock())))
    result = client.classify_action("apply", ["kind: Deployment"], "App: demo, Criticality: high")

    assert result is None


def test_classify_action_malformed_json_returns_none():
    client = _make_client(MagicMock(return_value=_mock_response("this is not JSON at all")))
    result = client.classify_action("apply", ["kind: Deployment"], "App: demo, Criticality: high")

    assert result is None


def test_classify_action_missing_field_returns_none():
    # Valid JSON, but missing the required "confidence" key -- must not be
    # coerced into a default "safe" classification.
    response = _mock_response(json.dumps({"is_destructive": False, "reason": "looks fine"}))
    client = _make_client(MagicMock(return_value=response))
    result = client.classify_action("apply", ["kind: Deployment"], "App: demo, Criticality: high")

    assert result is None


def test_classify_action_low_confidence_is_not_silently_upgraded_to_safe():
    # classify_action() itself doesn't threshold on confidence -- it faithfully
    # returns whatever the model reported. The fail-closed decision for a
    # low-confidence result is made by the caller (AutoMode.should_auto_apply,
    # see tests/test_automode.py::test_llm_low_confidence_returns_false), which
    # rejects auto-apply below _CONFIDENCE_THRESHOLD regardless of is_destructive.
    # This test pins that classify_action() never masks a low confidence value
    # or substitutes a higher one.
    response = _mock_response(json.dumps({"is_destructive": False, "confidence": 0.2, "reason": "Unclear"}))
    client = _make_client(MagicMock(return_value=response))
    result = client.classify_action("apply", ["kind: Deployment"], "App: demo, Criticality: high")

    assert result is not None
    assert result["confidence"] == pytest.approx(0.2)
    assert result["is_destructive"] is False


def test_review_fix_timeout_returns_none():
    import anthropic
    import httpx

    timeout_exc = anthropic.APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))
    client = _make_client(MagicMock(side_effect=timeout_exc))
    result = client.review_fix("Missing NetworkPolicy", "network", "kind: NetworkPolicy", "Go microservice")

    assert result is None


def test_review_fix_connection_error_returns_none():
    import anthropic
    client = _make_client(MagicMock(side_effect=anthropic.APIConnectionError(request=MagicMock())))
    result = client.review_fix("Missing NetworkPolicy", "network", "kind: NetworkPolicy", "Go microservice")

    assert result is None


def test_review_fix_malformed_json_returns_none():
    client = _make_client(MagicMock(return_value=_mock_response("not JSON, sorry")))
    result = client.review_fix("Missing NetworkPolicy", "network", "kind: NetworkPolicy", "Go microservice")

    assert result is None


def test_review_fix_missing_field_returns_none():
    response = _mock_response(json.dumps({"approved": True, "reason": "looks correct"}))
    client = _make_client(MagicMock(return_value=response))
    result = client.review_fix("Missing NetworkPolicy", "network", "kind: NetworkPolicy", "Go microservice")

    assert result is None


def test_review_fix_low_confidence_is_not_silently_upgraded_to_approved():
    # Same contract as classify_action above: review_fix() faithfully returns
    # the model's own confidence. cli.py's self-fix Step 3 gate
    # (`review["approved"] and review["confidence"] >= 0.7`) is what actually
    # rejects a low-confidence approval -- review_fix() itself must never
    # substitute a higher confidence or force approved=True.
    response = _mock_response(json.dumps({"approved": True, "confidence": 0.3, "reason": "Mostly right"}))
    client = _make_client(MagicMock(return_value=response))
    result = client.review_fix("Missing NetworkPolicy", "network", "kind: NetworkPolicy", "Go microservice")

    assert result is not None
    assert result["confidence"] == pytest.approx(0.3)
    assert result["approved"] is True
