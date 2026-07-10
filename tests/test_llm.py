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
