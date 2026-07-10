from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from agentit.llm import LLMClient


def _mock_transport(handler):
    """Build an httpx.MockTransport from a handler function."""
    return httpx.MockTransport(handler)


def _make_chat_response(content: str) -> httpx.Response:
    body = {
        "choices": [{"message": {"content": content}}],
    }
    return httpx.Response(200, json=body)


def _make_client(transport: httpx.MockTransport) -> LLMClient:
    client = LLMClient(endpoint="http://fake-llm:8000", model="test-model")
    client._client = httpx.Client(transport=transport)
    return client


# ------------------------------------------------------------------
# classify_secret
# ------------------------------------------------------------------


def test_classify_secret_real_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        return _make_chat_response(
            json.dumps({"is_secret": True, "confidence": 0.95, "reason": "Hardcoded AWS key"})
        )

    client = _make_client(_mock_transport(handler))
    result = client.classify_secret("config.yaml", "aws_key = AKIAIOSFODNN7EXAMPLE", ["# config", "aws_key = AKIAIOSFODNN7EXAMPLE", "region = us-east-1"])

    assert result is not None
    assert result["is_secret"] is True
    assert result["confidence"] == pytest.approx(0.95)
    assert "AWS" in result["reason"] or "key" in result["reason"].lower()


def test_classify_secret_false_positive():
    def handler(request: httpx.Request) -> httpx.Response:
        return _make_chat_response(
            json.dumps({"is_secret": False, "confidence": 0.9, "reason": "Variable name reference, not a real value"})
        )

    client = _make_client(_mock_transport(handler))
    result = client.classify_secret("app.py", 'password = os.getenv("DB_PASSWORD")', ['import os', 'password = os.getenv("DB_PASSWORD")', 'connect(password)'])

    assert result is not None
    assert result["is_secret"] is False
    assert result["confidence"] == pytest.approx(0.9)


def test_llm_unavailable_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _make_client(_mock_transport(handler))
    result = client.classify_secret("f.py", "secret=abc123xyz789!!", [])

    assert result is None


def test_summarize_architecture():
    summary_text = "A Go microservice exposing a REST API backed by PostgreSQL."

    def handler(request: httpx.Request) -> httpx.Response:
        return _make_chat_response(summary_text)

    client = _make_client(_mock_transport(handler))
    result = client.summarize_architecture(
        {"languages": [{"name": "go"}], "frameworks": [], "databases": [{"name": "postgresql"}]},
        ["main.go", "handler.go", "db.go"],
    )

    assert result == summary_text



def test_classify_secret_bad_json_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return _make_chat_response("I'm not sure, maybe it's a secret?")

    client = _make_client(_mock_transport(handler))
    result = client.classify_secret("f.py", "key=abc", [])

    assert result is None
