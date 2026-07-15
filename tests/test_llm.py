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


def test_propose_capability_improvement_realistic_payload_no_longer_truncates():
    """Regression test for the live capability-scout truncation bug: a real
    proposal has 7 fields, several of them full prose paragraphs
    (gap_description/evidence/change_summary/test_plan), which is realistically
    larger than the 512-token default that used to cut real JSON responses off
    mid-string. propose_capability_improvement must request the higher
    agentit.llm._CAPABILITY_PROPOSAL_MAX_TOKENS budget, and a payload of this
    size must parse cleanly end to end."""
    from agentit.llm import _CAPABILITY_PROPOSAL_MAX_TOKENS

    proposal_payload = {
        "has_proposal": True,
        "title": "Add SLO burn-rate alerting for check compliance regressions",
        "gap_description": (
            "docs/self-improvement-for-agentit.md (line 88) documents a known gap: "
            "check_compliance history is tracked per-check over time, but nothing "
            "automatically alerts when a check's pass rate degrades over a rolling "
            "window, so a regression can go unnoticed for days until a human "
            "happens to review the compliance dashboard manually."
        ),
        "evidence": (
            "get_check_compliance() returned 14 rows this cycle, three of which show "
            "a pass_rate below 0.5 over the trailing 7 days (network-policy: 0.42, "
            "rbac-least-privilege: 0.38, resource-limits: 0.47), and "
            "docs/self-improvement-for-agentit.md:88 explicitly states: 'Known gap: "
            "no automated alert exists today when a check's compliance trend "
            "degrades, only the /compliance dashboard shows it.' get_agent_stats() "
            "for the same window shows the security-agent and network-agent both "
            "regressed on the same three checks over the last two weeks, which "
            "corroborates the doc's gap admission with a second, independent "
            "real signal rather than resting on the doc quote alone."
        ),
        "target_files": ["src/agentit/slo_collector.py", "tests/test_slo_collector.py"],
        "change_summary": (
            "Add a new check_compliance_burn_rate gauge to slo_collector.py that "
            "computes each check's 7-day pass-rate delta versus its 30-day "
            "baseline and exposes it as a Prometheus gauge alongside the existing "
            "SLO metrics, following the same collect_and_export() pattern already "
            "used for error-budget burn rate. No new dependency or schema change "
            "is required -- the underlying check_results rows already exist, so this "
            "is purely a new aggregation over data the store already persists. The "
            "gauge should be labeled by check_id so Grafana/alertmanager can page on "
            "a per-check burn-rate threshold rather than only the aggregate compliance "
            "percentage the dashboard shows today."
        ),
        "risk": "low",
        "test_plan": (
            "tests/test_slo_collector.py gains a new test that seeds check_results "
            "rows with a known 7-day and 30-day pass rate, calls the new burn-rate "
            "function, and asserts the returned gauge value matches the expected "
            "delta within a small tolerance, plus a second test asserting a check "
            "with no rows in one of the two windows returns None rather than "
            "raising or dividing by zero, and a third test confirming the gauge is "
            "registered under a per-check_id label so two different checks never "
            "collide on the same Prometheus series."
        ),
    }
    response_text = json.dumps(proposal_payload)
    # Sanity check: at ~4 chars/token, this realistic 7-field proposal is
    # comfortably larger than the old 512-token default -- confirming this is
    # a genuine regression test for the truncation bug, not a payload that
    # would have fit either way.
    assert len(response_text) // 4 > 512

    mock_create = MagicMock(return_value=_mock_response(response_text))
    client = _make_client(mock_create)
    result = client.propose_capability_improvement({"doc_gaps": [{"file": "docs/self-improvement-for-agentit.md", "line_no": 88}]})

    assert result is not None
    assert result["has_proposal"] is True
    assert result["title"] == proposal_payload["title"]
    assert result["gap_description"] == proposal_payload["gap_description"]
    assert result["change_summary"] == proposal_payload["change_summary"]
    assert result["test_plan"] == proposal_payload["test_plan"]
    assert result["target_files"] == proposal_payload["target_files"]

    _, kwargs = mock_create.call_args
    assert kwargs["max_tokens"] == _CAPABILITY_PROPOSAL_MAX_TOKENS
    assert kwargs["max_tokens"] > 512


# ── max_tokens budget wiring: shorter-response callers stay on the default ──


def test_classify_action_uses_default_token_budget():
    """classify_action's simple safe/unsafe+confidence+reason response doesn't
    need a bigger budget -- it must still request agentit.llm._DEFAULT_MAX_TOKENS
    (512), proving the higher budgets given to detect_eol_risks/
    propose_capability_improvement didn't get blanket-applied to every caller."""
    from agentit.llm import _DEFAULT_MAX_TOKENS

    response = _mock_response(json.dumps({"is_destructive": False, "confidence": 0.9, "reason": "Adding a ConfigMap"}))
    mock_create = MagicMock(return_value=response)
    client = _make_client(mock_create)
    result = client.classify_action("apply", ["kind: ConfigMap"], "App: demo")

    assert result is not None
    _, kwargs = mock_create.call_args
    assert kwargs["max_tokens"] == _DEFAULT_MAX_TOKENS == 512


def test_classify_secret_uses_default_token_budget():
    """Same contract as classify_action above, pinned for classify_secret."""
    from agentit.llm import _DEFAULT_MAX_TOKENS

    response = _mock_response(json.dumps({"is_secret": False, "confidence": 0.9, "reason": "Env var lookup"}))
    mock_create = MagicMock(return_value=response)
    client = _make_client(mock_create)
    result = client.classify_secret("app.py", 'x = os.getenv("KEY")', [])

    assert result is not None
    _, kwargs = mock_create.call_args
    assert kwargs["max_tokens"] == _DEFAULT_MAX_TOKENS == 512


def test_review_fix_uses_default_token_budget():
    """Same contract as classify_action above, pinned for review_fix."""
    from agentit.llm import _DEFAULT_MAX_TOKENS

    response = _mock_response(json.dumps({"approved": True, "confidence": 0.9, "reason": "Correct and safe"}))
    mock_create = MagicMock(return_value=response)
    client = _make_client(mock_create)
    result = client.review_fix("Missing NetworkPolicy", "network", "kind: NetworkPolicy", "Go microservice")

    assert result is not None
    _, kwargs = mock_create.call_args
    assert kwargs["max_tokens"] == _DEFAULT_MAX_TOKENS == 512


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
