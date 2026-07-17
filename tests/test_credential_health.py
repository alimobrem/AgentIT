"""Tests for the Health page's Credentials section: live status for the
GitHub token (`github_pr.py::check_github_token()`), the GitHub webhook
secret, and the LLM backend (Vertex AI vs. direct Anthropic API) --
`helpers.py::get_credential_states()` -- plus its rendering in
`health.html`.

Mirrors test_deploy_status.py's mocking conventions: patch
`agentit.portal.github_pr.requests.get` (never a real network call) and
use an `httpx.AsyncClient` (ASGI transport) + `prime_csrf` for route-level
tests, matching test_portal.py's/test_deploy_status.py's existing
`/health` coverage.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal import github_pr
from agentit.portal.app import app
from agentit.portal.helpers import get_credential_states

from conftest import prime_csrf


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as c:
        await prime_csrf(c)
        yield c


def _row(html: str, needle: str) -> str:
    """Isolate a single `<tr>...` row containing `needle`, matching the
    row-isolation pattern already used in test_portal.py (split on a
    landmark, take up to the next tag) so each credential's own styling
    can be asserted independently of the other rows on the page."""
    rows = html.split("<tr")
    return next(r for r in rows if needle in r)


# ── github_pr.py: check_github_token ────────────────────────────────────


def test_check_github_token_missing_when_env_unset(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = github_pr.check_github_token()
    assert result["status"] == "missing"


def test_check_github_token_missing_does_not_raise(monkeypatch):
    """`_get_token()` raises `RuntimeError` when `GITHUB_TOKEN` is unset --
    `check_github_token()` must catch that locally, never let it propagate
    into a 500 for the Health page."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    try:
        result = github_pr.check_github_token()
    except RuntimeError:
        pytest.fail("check_github_token() must catch _get_token()'s RuntimeError")
    assert result["status"] == "missing"


def test_check_github_token_valid_on_200_calls_rate_limit(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    class _Resp:
        status_code = 200

    with patch("agentit.portal.github_pr.requests.get", return_value=_Resp()) as mock_get:
        result = github_pr.check_github_token()

    assert result["status"] == "valid"
    # GET /rate_limit is the cheapest liveness check: no scope requirement
    # and (per GitHub's docs) it doesn't count against the caller's own
    # rate limit.
    called_url = mock_get.call_args.args[0] if mock_get.call_args.args else mock_get.call_args.kwargs.get("url")
    assert called_url.endswith("/rate_limit")


@pytest.mark.parametrize("status_code", [401, 403])
def test_check_github_token_invalid_on_401_403(monkeypatch, status_code):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    class _Resp:
        pass

    _Resp.status_code = status_code
    with patch("agentit.portal.github_pr.requests.get", return_value=_Resp()):
        result = github_pr.check_github_token()

    assert result["status"] == "invalid"
    assert str(status_code) in result["detail"]


def test_check_github_token_invalid_on_network_error(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    with patch("agentit.portal.github_pr.requests.get", side_effect=Exception("network error")):
        result = github_pr.check_github_token()
    assert result["status"] == "invalid"


# ── helpers.py: get_credential_states -- GitHub token ───────────────────


def test_credential_states_github_token_missing(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    states = get_credential_states()
    assert states["github-token"] == {
        "ok": False, "status": "missing", "detail": "GITHUB_TOKEN is not set",
    }


def test_credential_states_github_token_valid(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    with patch("agentit.portal.github_pr.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        states = get_credential_states()
    assert states["github-token"]["ok"] is True
    assert states["github-token"]["status"] == "valid"


def test_credential_states_github_token_invalid(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    with patch("agentit.portal.github_pr.requests.get") as mock_get:
        mock_get.return_value.status_code = 401
        states = get_credential_states()
    assert states["github-token"]["ok"] is False
    assert states["github-token"]["status"] == "invalid"


# ── helpers.py: get_credential_states -- GitHub webhook secret ──────────


def test_credential_states_webhook_secret_missing(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    states = get_credential_states()
    assert states["github-webhook-secret"]["ok"] is False
    assert states["github-webhook-secret"]["status"] == "missing"


def test_credential_states_webhook_secret_configured(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cr3t")
    states = get_credential_states()
    assert states["github-webhook-secret"]["ok"] is True
    assert states["github-webhook-secret"]["status"] == "configured"


# ── helpers.py: get_credential_states -- LLM backend ─────────────────────
#
# ANTHROPIC_API_KEY / ANTHROPIC_VERTEX_PROJECT_ID / CLOUD_ML_REGION /
# GOOGLE_APPLICATION_CREDENTIALS are stripped from every test's env by
# conftest.py's autouse `_hermetic_llm_env` fixture, so "neither configured"
# is each test's real starting state unless a test sets one back explicitly.


def test_credential_states_llm_backend_none_configured(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    states = get_credential_states()
    assert states["llm-backend"]["ok"] is False
    assert states["llm-backend"]["status"] == "missing"


def test_credential_states_llm_backend_direct_api_key(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    states = get_credential_states()
    assert states["llm-backend"]["ok"] is True
    assert states["llm-backend"]["status"] == "valid"
    assert "Direct" in states["llm-backend"]["detail"]


def test_credential_states_llm_backend_vertex_valid(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    creds_file = tmp_path / "sa.json"
    creds_file.write_text("{}")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-project")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(creds_file))
    states = get_credential_states()
    assert states["llm-backend"]["ok"] is True
    assert states["llm-backend"]["status"] == "valid"
    assert "Vertex" in states["llm-backend"]["detail"]


def test_credential_states_llm_backend_vertex_missing_credentials_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    missing_path = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-project")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(missing_path))
    states = get_credential_states()
    assert states["llm-backend"]["ok"] is False
    assert states["llm-backend"]["status"] == "invalid"


def test_credential_states_llm_backend_vertex_env_vars_but_no_credentials_path(monkeypatch):
    """Vertex project/region are set (so Vertex is the selected backend per
    `llm.py::_create_client()`), but GOOGLE_APPLICATION_CREDENTIALS itself
    is unset -- must report invalid, not silently treat it as valid."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-project")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    states = get_credential_states()
    assert states["llm-backend"]["ok"] is False
    assert states["llm-backend"]["status"] == "invalid"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can read any file regardless of its permission bits",
)
def test_credential_states_llm_backend_vertex_unreadable_file(monkeypatch, tmp_path):
    creds_file = tmp_path / "sa.json"
    creds_file.write_text("{}")
    creds_file.chmod(0o000)
    try:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-project")
        monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(creds_file))
        states = get_credential_states()
    finally:
        creds_file.chmod(0o644)
    assert states["llm-backend"]["ok"] is False
    assert states["llm-backend"]["status"] == "invalid"


# ── Health page: Credentials section rendering ───────────────────────────


async def test_health_page_shows_credentials_section(client, monkeypatch):
    """Graceful degradation: a missing GITHUB_TOKEN (the RuntimeError
    _get_token() raises today) must not crash the page -- it must render
    as a normal "missing" row."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.return_value = []
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert "Credentials" in resp.text
    assert "github-token" in resp.text
    assert "github-webhook-secret" in resp.text
    assert "llm-backend" in resp.text

    gh_row = _row(resp.text, "github-token")
    assert "row-border-red" in gh_row
    assert "badge-critical" in gh_row
    assert "Missing" in gh_row


async def test_health_page_credentials_render_green_when_all_configured(client, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cr3t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    with patch("agentit.portal.routes.health.kube") as mock_kube, \
         patch("agentit.portal.github_pr.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_kube.list_custom_resources.return_value = []
        resp = await client.get("/health")

    assert resp.status_code == 200

    gh_row = _row(resp.text, "github-token")
    assert "row-border-green" in gh_row
    assert "badge-low" in gh_row
    assert "Valid" in gh_row

    webhook_row = _row(resp.text, "github-webhook-secret")
    assert "row-border-green" in webhook_row
    assert "Configured" in webhook_row

    llm_row = _row(resp.text, "llm-backend")
    assert "row-border-green" in llm_row
    assert "Valid" in llm_row


async def test_health_page_credentials_render_red_when_token_invalid(client, monkeypatch):
    """A present-but-expired/invalid token (401/403) must render distinctly
    from "missing" -- both are red/failing, but the detail text differs."""
    monkeypatch.setenv("GITHUB_TOKEN", "stale-token")
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    with patch("agentit.portal.routes.health.kube") as mock_kube, \
         patch("agentit.portal.github_pr.requests.get") as mock_get:
        mock_get.return_value.status_code = 401
        mock_kube.list_custom_resources.return_value = []
        resp = await client.get("/health")

    assert resp.status_code == 200
    gh_row = _row(resp.text, "github-token")
    assert "row-border-red" in gh_row
    assert "Invalid" in gh_row
    assert "expired" in gh_row.lower() or "invalid" in gh_row.lower()


async def test_health_page_survives_github_token_check_raising(client, monkeypatch):
    """Even if the GitHub liveness check itself blows up unexpectedly (not
    just the documented missing-token RuntimeError), `_get_cluster_health`'s
    own try/except around `get_credential_states()` must keep the page at
    200 rather than a 500."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    with patch("agentit.portal.routes.health.kube") as mock_kube, \
         patch("agentit.portal.github_pr.check_github_token", side_effect=RuntimeError("boom")):
        mock_kube.list_custom_resources.return_value = []
        resp = await client.get("/health")

    assert resp.status_code == 200
