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


# ── markdown code-fence stripping (capability-scout "unparseable" bug) ────
#
# Live capability-scout cycles logged as "unparseable no-proposal" even after
# _chat()'s max_tokens fix stopped truncation, because the model wraps its
# JSON in a markdown code fence (```json ... ``` or bare ``` ... ```) despite
# every system prompt saying "Respond ONLY with valid JSON". _chat() now
# strips a recognized fence via the shared _strip_code_fence() helper before
# any caller's json.loads(), so every caller gets this for free from one
# place rather than needing its own duplicate strip logic.


def test_strip_code_fence_json_tagged():
    from agentit.llm import _strip_code_fence

    wrapped = '```json\n{"a": 1}\n```'
    assert _strip_code_fence(wrapped) == '{"a": 1}'


def test_strip_code_fence_bare_no_language_tag():
    from agentit.llm import _strip_code_fence

    wrapped = '```\n{"a": 1}\n```'
    assert _strip_code_fence(wrapped) == '{"a": 1}'


def test_strip_code_fence_extra_whitespace_and_newlines():
    from agentit.llm import _strip_code_fence

    wrapped = '  \n\n```json\n\n{"a": 1}\n\n```\n  \n'
    assert _strip_code_fence(wrapped) == '{"a": 1}'


def test_strip_code_fence_leaves_unwrapped_text_untouched():
    from agentit.llm import _strip_code_fence

    unwrapped = '{"a": 1}'
    assert _strip_code_fence(unwrapped) == unwrapped


def test_strip_code_fence_leaves_already_invalid_text_untouched():
    from agentit.llm import _strip_code_fence

    garbage = "this is not JSON at all, and never was"
    assert _strip_code_fence(garbage) == garbage


def test_strip_code_fence_unclosed_truncated_json_fence():
    """Live failure: model opens ```json then hits max_tokens mid-object."""
    from agentit.llm import _strip_code_fence

    truncated = '```json\n{"has_proposal": true, "title": "x", "evidence": "huge...'
    assert _strip_code_fence(truncated).startswith('{"has_proposal"')
    assert not _strip_code_fence(truncated).startswith("```")


def test_propose_capability_improvement_retries_after_unparseable():
    """First reply truncated/fenced junk; second compact JSON succeeds."""
    bad = '```json\n{"has_proposal": true, "title": "cut off...'
    good = {
        "has_proposal": True,
        "title": "Retry worked",
        "gap_description": "g",
        "evidence": "e",
        "target_files": ["tests/test_x.py"],
        "change_summary": "c",
        "risk": "low",
        "test_plan": "p",
    }
    client = _make_client(MagicMock(side_effect=[
        _mock_response(bad),
        _mock_response(json.dumps(good)),
    ]))
    result = client.propose_capability_improvement({"doc_gaps": [{"file": "x", "line_no": 1}]})
    assert result is not None
    assert result["title"] == "Retry worked"
    assert client._client.messages.create.call_count == 2


def test_propose_capability_improvement_strips_json_tagged_fence():
    """The exact live bug: model wraps the proposal in a ```json fence."""
    proposal_payload = {
        "has_proposal": True,
        "title": "Add SLO burn-rate alerting",
        "gap_description": "docs gap",
        "evidence": "evidence quote",
        "target_files": ["src/agentit/slo_collector.py"],
        "change_summary": "summary",
        "risk": "low",
        "test_plan": "plan",
    }
    fenced = "```json\n" + json.dumps(proposal_payload) + "\n```"
    client = _make_client(MagicMock(return_value=_mock_response(fenced)))
    result = client.propose_capability_improvement({"doc_gaps": [{"file": "x", "line_no": 1}]})

    assert result is not None
    assert result["has_proposal"] is True
    assert result["title"] == "Add SLO burn-rate alerting"
    assert result["target_files"] == ["src/agentit/slo_collector.py"]


def test_propose_capability_improvement_strips_bare_fence_no_language_tag():
    proposal_payload = {"has_proposal": False}
    fenced = "```\n" + json.dumps(proposal_payload) + "\n```"
    client = _make_client(MagicMock(return_value=_mock_response(fenced)))
    result = client.propose_capability_improvement({"doc_gaps": []})

    assert result == {"has_proposal": False}


def test_propose_capability_improvement_strips_fence_with_surrounding_whitespace():
    proposal_payload = {"has_proposal": True, "title": "T", "gap_description": "G", "evidence": "E",
                         "target_files": ["tests/test_x.py"], "change_summary": "C", "risk": "low", "test_plan": "P"}
    fenced = "  \n```json\n\n" + json.dumps(proposal_payload) + "\n\n```\n  "
    client = _make_client(MagicMock(return_value=_mock_response(fenced)))
    result = client.propose_capability_improvement({"doc_gaps": [{"file": "x", "line_no": 1}]})

    assert result is not None
    assert result["has_proposal"] is True
    assert result["title"] == "T"


def test_classify_secret_strips_json_fence():
    fenced = '```json\n{"is_secret": true, "confidence": 0.9, "reason": "Hardcoded key"}\n```'
    client = _make_client(MagicMock(return_value=_mock_response(fenced)))
    result = client.classify_secret("config.yaml", "aws_key = AKIA...", [])

    assert result is not None
    assert result["is_secret"] is True


def test_detect_eol_risks_strips_bare_fence():
    fenced = '```\n{"risks": [{"component": "python", "version": "3.8", "status": "eol", ' \
             '"eol_date": "2024-10-14", "confidence": 0.9, "reason": "Upstream EOL"}]}\n```'
    client = _make_client(MagicMock(return_value=_mock_response(fenced)))
    result = client.detect_eol_risks({"languages": [{"name": "python", "version": "3.8"}]}, {})

    assert result is not None
    assert len(result) == 1
    assert result[0]["component"] == "python"


def test_classify_secret_bad_json_still_returns_none_after_fence_strip():
    """Fence-stripping must never mask genuinely malformed JSON -- content
    that's still invalid after stripping a (possibly absent) fence must fail
    exactly as before, never fabricate a result."""
    fenced = "```json\nthis is not valid json even without the fence\n```"
    client = _make_client(MagicMock(return_value=_mock_response(fenced)))
    result = client.classify_secret("f.py", "key=abc", [])

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


# ── max_tokens budget wiring: skill-lifecycle callers outside this module ──
#
# Live bug: "Activation blocked — skill failed verification: skill matched
# the verification fixture but generated no output" (resourcequota-contextual,
# 2026-07-18). learning_agent.generate_skill_from_research() and
# skill_engine.SkillEngine._generate_with_llm() are both external callers
# that invoke llm_client._chat(system, user) directly (not a method on
# LLMClient itself, unlike propose_capability_improvement/detect_eol_risks
# above) -- so neither got a max_tokens override for free and both silently
# inherited the 512-token classifier default. Confirmed live: this truncated
# a learning-agent-drafted skill's own body (missing its Constraints/
# Verification sections) and, separately, truncated that skill's generated
# manifest until generation gave up and produced no output at all.


def test_generate_skill_from_research_uses_manifest_sized_token_budget():
    from agentit.learning_agent import generate_skill_from_research
    from agentit.llm import _SKILL_GENERATION_MAX_TOKENS

    skill_md = (
        "---\nname: x\ndomain: security\nversion: 1\ntriggers: [x]\noutputs: [Pod]\n"
        "property: x\nmode: template\nstatus: draft\nsource: learning-agent\n"
        'created_at: "2026-01-01"\n---\n\n## Property\n\nx\n\n## Constraints\n\nx\n\n'
        "## Verification\n\nx\n"
    )
    mock_create = MagicMock(return_value=_mock_response(skill_md))
    client = _make_client(mock_create)

    result = generate_skill_from_research(
        client, {"title": "t", "description": "d", "category": "security",
                 "priority": "high", "fix_approach": "f"},
    )

    assert result == skill_md.strip()
    _, kwargs = mock_create.call_args
    assert kwargs["max_tokens"] == _SKILL_GENERATION_MAX_TOKENS
    assert kwargs["max_tokens"] > 512


def test_skill_engine_generate_with_llm_uses_manifest_sized_token_budget():
    """End-to-end through the real LLMClient (only the Anthropic transport is
    mocked) -- proves SkillEngine._generate_with_llm() requests the same
    higher budget when driven by the actual production client, not just a
    test double's `_chat` stub."""
    from pathlib import Path

    from agentit.llm import _SKILL_GENERATION_MAX_TOKENS
    from agentit.skill_engine import Skill, SkillEngine
    from conftest import make_report

    manifest = (
        "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n"
        "  name: my-app-netpol\nspec:\n  podSelector: {}\n  policyTypes:\n    - Ingress\n"
    )
    mock_create = MagicMock(return_value=_mock_response(manifest))
    client = _make_client(mock_create)

    engine = SkillEngine(Path("skill-verification-fixture-nonexistent"), platform=None)
    skill = Skill(
        name="network-policy-llm", domain="security", version=1,
        triggers=["network"], outputs=["NetworkPolicy"],
        property_description="network-policy-llm property",
        body="# LLM-only skill, no template block",
        file_path="skills/security/network-policy-llm.md",
        mode="llm", status="active",
    )
    report = make_report(repo_name="my-app")

    files = engine.generate(skill, report, llm_client=client)

    assert len(files) == 1
    _, kwargs = mock_create.call_args
    assert kwargs["max_tokens"] == _SKILL_GENERATION_MAX_TOKENS
    assert kwargs["max_tokens"] > 512
