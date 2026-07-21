"""Tests for the Health page's Webhook Deliveries section: real liveness
checks against GitHub's own delivery history for each managed app's
registered push webhook (`github_pr.py::check_webhook_delivery_health()`),
wired into `health.py::_get_cluster_health()` and rendered by health.html.

This closes the exact gap the 2026-07-18 "Awaiting verification"
investigation exposed: a webhook can be registered, active, and 100%
failing (oauth-proxy 302, TLS mismatch, wrong secret, ...) with nothing
short of a human manually running `gh api repos/.../hooks/{id}/deliveries`
to notice. This section runs that same check automatically.

Mirrors test_credential_health.py's mocking conventions (patch
`agentit.portal.github_pr.requests.get`, never a real network call) but
needs its own store-seeding client fixture: `conftest.py`'s `portal_client`
mocks out `_get_cluster_health` entirely (so other tests' Health-page
assertions don't depend on live kube/GitHub calls), which would hide the
exact code path this file needs to exercise.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal import github_pr
from agentit.portal.app import app
from agentit.portal.routes import health as health_routes

from conftest import make_report, make_store, prime_csrf


@pytest.fixture(autouse=True)
def _clear_webhook_health_cache():
    health_routes._webhook_health_cache.clear()
    yield
    health_routes._webhook_health_cache.clear()


@pytest.fixture
async def health_client(monkeypatch):
    """Like `portal_client`, but deliberately does NOT mock out
    `_get_cluster_health` -- these tests need it to actually run
    `get_fleet_data()` + the new webhook-delivery check, not a canned
    fixture dict.

    Sets ``AGENTIT_OFFLINE=1`` (kube.py's own documented hard-offline
    guarantee -- see ``get_client()``'s docstring) so every real
    Kubernetes API call `_get_cluster_health`/`_get_deploy_status` make
    (Argo apps, pods, pipeline runs, the Console CR lookup, ...) raises
    immediately and is caught by their existing try/except, instead of
    depending on `KUBECONFIG` being unset -- which kube.py's own docstring
    notes is *not* a reliable guarantee, since the Kubernetes client's
    default config-resolution chain still falls back to the ambient
    ``~/.kube/config``. Without this, a dev machine with a real, live
    kubeconfig would have those sections silently hit a real cluster,
    which previously bled into these tests as extra, unmocked
    ``requests.get`` calls (``github_pr.get_commit_info`` off a real Argo
    CD Application's commit) consuming this file's own `side_effect`
    lists meant only for `check_webhook_delivery_health`.
    """
    monkeypatch.setenv("AGENTIT_OFFLINE", "1")
    store = await make_store()
    with patch("agentit.portal.routes.health.get_store", return_value=store), \
         patch("agentit.portal.helpers.get_store", return_value=store), \
         patch("agentit.portal.helpers._store", store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            await prime_csrf(client)
            yield client, store


def _row(html: str, needle: str) -> str:
    rows = html.split("<tr")
    return next(r for r in rows if needle in r)


def _webhook_deliveries_section(html: str) -> str:
    """Isolate the Webhook Deliveries table specifically. Needed because
    `_get_cluster_health`'s Argo/pipeline/pod sections do their own
    function-local `from agentit import kube` import that a
    `patch("agentit.portal.routes.health.kube")` on the *module* attribute
    doesn't intercept -- on a dev machine with a real, live kubeconfig
    (unlike CI), those sections can render genuine live-cluster data (e.g.
    a real Argo CD Application actually named "agentit"), which could
    collide with a naive whole-page row search."""
    start = html.index("Webhook Deliveries")
    end = html.index("</table>", start)
    return html[start:end]


def _hooks_response(hook_id: int = 42, url: str = "https://agentit.example.com/api/webhook/github-push", active: bool = True):
    resp = type("Resp", (), {})()
    resp.status_code = 200
    resp.json = lambda: [{"id": hook_id, "active": active, "config": {"url": url}}]
    resp.raise_for_status = lambda: None
    return resp


def _deliveries_response(deliveries: list[dict]):
    resp = type("Resp", (), {})()
    resp.status_code = 200
    resp.json = lambda: deliveries
    resp.raise_for_status = lambda: None
    return resp


def _route_requests_get_by_url(*, deliveries: list[dict], hook_id: int = 42, hooks_url: str = "https://agentit.example.com/api/webhook/github-push"):
    """A `requests.get` `side_effect` that dispatches on the URL rather
    than call order. The Health-page route also runs
    `get_credential_states()` (which calls `check_github_token()`'s own
    `GET .../rate_limit`) in the same request, ahead of the webhook checks
    -- a fixed-position `side_effect=[...]` list would silently hand that
    unrelated call one of *this* mock's responses and throw off every
    later call by one position."""
    def _get(url, *args, **kwargs):
        if url.endswith("/rate_limit"):
            resp = type("Resp", (), {})()
            resp.status_code = 200
            return resp
        if url.endswith("/deliveries"):
            return _deliveries_response(deliveries)
        if url.endswith("/hooks"):
            return _hooks_response(hook_id=hook_id, url=hooks_url)
        raise AssertionError(f"unexpected requests.get call in test: {url}")
    return _get


# ── github_pr.py: check_webhook_delivery_health ─────────────────────────


def test_no_token_reports_no_token_status(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result == {
        "ok": False, "status": "no_token",
        "detail": "GITHUB_TOKEN is not set -- cannot check webhook delivery health",
    }


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_no_matching_hook_reports_not_registered():
    with patch("agentit.portal.github_pr.requests.get", return_value=_hooks_response(url="https://other.example.com/callback")):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is False
    assert result["status"] == "not_registered"


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_inactive_hook_reports_inactive():
    with patch("agentit.portal.github_pr.requests.get", return_value=_hooks_response(active=False)):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is False
    assert result["status"] == "inactive"
    assert "42" in result["detail"]


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_no_deliveries_yet_is_inconclusive_not_failing():
    """A brand-new hook GitHub hasn't called yet must not render as a
    failure -- `ok=None` is a distinct, deliberately non-red state."""
    hooks_resp = _hooks_response()
    deliveries_resp = _deliveries_response([])
    with patch("agentit.portal.github_pr.requests.get", side_effect=[hooks_resp, deliveries_resp]):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is None
    assert result["status"] == "no_deliveries"


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_latest_delivery_200_reports_delivering():
    hooks_resp = _hooks_response()
    deliveries_resp = _deliveries_response([
        {"status_code": 200, "status": "OK", "delivered_at": "2026-07-18T10:00:00Z"},
    ])
    with patch("agentit.portal.github_pr.requests.get", side_effect=[hooks_resp, deliveries_resp]):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is True
    assert result["status"] == "delivering"
    assert "200" in result["detail"]


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_latest_delivery_302_reports_failing():
    """Reproduces the exact live bug this check exists for: oauth-proxy
    302'd every github-push delivery to the OAuth login page instead of the
    app. GitHub's own delivery record for that failure mode is a 302."""
    hooks_resp = _hooks_response()
    deliveries_resp = _deliveries_response([
        {"status_code": 302, "status": "Found", "delivered_at": "2026-07-18T09:00:00Z"},
    ])
    with patch("agentit.portal.github_pr.requests.get", side_effect=[hooks_resp, deliveries_resp]):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is False
    assert result["status"] == "failing"
    assert "302" in result["detail"]
    assert "skip-auth-regex" in result["detail"]


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_only_the_most_recent_delivery_determines_status():
    """A hook that failed in the past but is delivering again right now
    must report healthy -- GitHub's deliveries list is newest-first."""
    hooks_resp = _hooks_response()
    deliveries_resp = _deliveries_response([
        {"status_code": 200, "status": "OK", "delivered_at": "2026-07-18T11:00:00Z"},
        {"status_code": 0, "status": "Failed to connect", "delivered_at": "2026-07-18T09:00:00Z"},
    ])
    with patch("agentit.portal.github_pr.requests.get", side_effect=[hooks_resp, deliveries_resp]):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is True


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_canary_503_with_recent_successes_is_transient_not_critical():
    """Live 2026-07-21 canary of 7347003: latest delivery was HTTP 503
    while 2+ of the prior deliveries in the same window were 200. Must not
    report ok=False (Self-Health Critical) with oauth-proxy advice."""
    hooks_resp = _hooks_response()
    deliveries_resp = _deliveries_response([
        {"status_code": 503, "status": "Invalid HTTP Response: 503", "delivered_at": "2026-07-21T19:57:53Z"},
        {"status_code": 504, "status": "timed out", "delivered_at": "2026-07-21T19:50:07Z"},
        {"status_code": 200, "status": "OK", "delivered_at": "2026-07-21T19:49:58Z"},
        {"status_code": 200, "status": "OK", "delivered_at": "2026-07-21T19:48:36Z"},
        {"status_code": 200, "status": "OK", "delivered_at": "2026-07-21T19:47:58Z"},
    ])
    with patch("agentit.portal.github_pr.requests.get", side_effect=[hooks_resp, deliveries_resp]):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is None
    assert result["status"] == "transient"
    assert "503" in result["detail"]
    assert "canary" in result["detail"].lower() or "rollout" in result["detail"].lower()
    assert "skip-auth-regex" not in result["detail"]


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_persistent_503_with_no_recent_success_still_failing():
    """A 503 with no successful deliveries in the recent window is still a
    real reachability failure -- not a canary blip."""
    hooks_resp = _hooks_response()
    deliveries_resp = _deliveries_response([
        {"status_code": 503, "status": "Invalid HTTP Response: 503", "delivered_at": "2026-07-21T19:57:53Z"},
        {"status_code": 503, "status": "Invalid HTTP Response: 503", "delivered_at": "2026-07-21T19:50:07Z"},
        {"status_code": 504, "status": "timed out", "delivered_at": "2026-07-21T19:49:58Z"},
    ])
    with patch("agentit.portal.github_pr.requests.get", side_effect=[hooks_resp, deliveries_resp]):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is False
    assert result["status"] == "failing"
    assert "503" in result["detail"]
    assert "pods Ready" in result["detail"] or "canary" in result["detail"].lower()


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_tls_failure_status_code_reports_failing():
    """The second, independently-broken hook from the live incident: a TLS
    verification failure against a self-signed ingress cert never gets a
    real HTTP status back from GitHub's perspective -- status_code is 0."""
    hooks_resp = _hooks_response()
    deliveries_resp = _deliveries_response([
        {"status_code": 0, "status": "certificate signed by unknown authority", "delivered_at": "2026-07-18T08:00:00Z"},
    ])
    with patch("agentit.portal.github_pr.requests.get", side_effect=[hooks_resp, deliveries_resp]):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is False
    assert result["status"] == "failing"
    assert "insecure_ssl" in result["detail"]


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_hooks_list_network_error_reports_error_not_raise():
    with patch("agentit.portal.github_pr.requests.get", side_effect=Exception("network error")):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is False
    assert result["status"] == "error"


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_deliveries_list_network_error_reports_error_not_raise():
    hooks_resp = _hooks_response()
    with patch("agentit.portal.github_pr.requests.get", side_effect=[hooks_resp, Exception("timeout")]):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is False
    assert result["status"] == "error"


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
def test_matches_hook_by_url_suffix_not_exact_url():
    """Doesn't need this app's own exact external base URL -- matches any
    hook whose config URL ends with the webhook path."""
    hooks_resp = _hooks_response(url="https://some-other-host.apps.cluster.example.com/api/webhook/github-push")
    deliveries_resp = _deliveries_response([{"status_code": 200, "status": "OK", "delivered_at": "now"}])
    with patch("agentit.portal.github_pr.requests.get", side_effect=[hooks_resp, deliveries_resp]):
        result = github_pr.check_webhook_delivery_health("https://github.com/t/r")
    assert result["ok"] is True


# ── health.py wiring / health.html rendering ─────────────────────────────


async def test_health_page_shows_webhook_deliveries_only_for_onboarded_apps(health_client):
    """An app with no onboarding never had `ensure_webhook()` called on it
    -- checking its webhook health would be a wasted (and misleading, since
    "not_registered" would look like a regression rather than "never
    applicable") GitHub API call."""
    client, store = health_client
    report = make_report(repo_name="never-onboarded", repo_url="https://github.com/t/never-onboarded")
    await store.save(report)

    resp = await client.get("/health")

    assert resp.status_code == 200
    assert "never-onboarded" not in resp.text


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
async def test_health_page_renders_green_row_for_healthy_webhook(health_client):
    client, store = health_client
    report = make_report(repo_name="webhook-health-test-healthy", repo_url="https://github.com/t/healthy-app")
    assessment_id = await store.save(report)
    await store.save_onboarding(assessment_id, [{"category": "security", "path": "x.yaml", "content": "x", "description": "d"}])

    mock_get = _route_requests_get_by_url(
        deliveries=[{"status_code": 200, "status": "OK", "delivered_at": "2026-07-18T12:00:00Z"}],
    )
    with patch("agentit.portal.github_pr.requests.get", side_effect=mock_get):
        resp = await client.get("/health")

    assert resp.status_code == 200
    section = _webhook_deliveries_section(resp.text)
    assert "webhook-health-test-healthy" in section
    row = _row(section, "webhook-health-test-healthy")
    assert "row-border-green" in row
    assert "badge-low" in row
    assert "Delivering" in row


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
async def test_health_page_renders_red_row_reproducing_the_live_incident(health_client):
    """The exact scenario from the 2026-07-18 incident: a registered,
    active hook whose most recent delivery got redirected to the OAuth
    login page (302) instead of reaching the app."""
    client, store = health_client
    report = make_report(repo_name="webhook-health-test-incident", repo_url="https://github.com/alimobrem/AgentIT")
    assessment_id = await store.save(report)
    await store.save_onboarding(assessment_id, [{"category": "security", "path": "x.yaml", "content": "x", "description": "d"}])

    mock_get = _route_requests_get_by_url(
        deliveries=[{"status_code": 302, "status": "Found", "delivered_at": "2026-07-18T06:00:00Z"}],
    )
    with patch("agentit.portal.github_pr.requests.get", side_effect=mock_get):
        resp = await client.get("/health")

    assert resp.status_code == 200
    section = _webhook_deliveries_section(resp.text)
    row = _row(section, "webhook-health-test-incident")
    assert "row-border-red" in row
    assert "badge-critical" in row
    assert "Failing" in row


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
async def test_health_page_survives_webhook_check_raising(health_client):
    """Same graceful-degradation guarantee as credentials -- an unexpected
    exception from the webhook check must not 500 the whole Health page."""
    client, store = health_client
    report = make_report(repo_name="webhook-health-test-boom", repo_url="https://github.com/t/boom-app")
    assessment_id = await store.save(report)
    await store.save_onboarding(assessment_id, [{"category": "security", "path": "x.yaml", "content": "x", "description": "d"}])

    with patch("agentit.portal.github_pr.check_webhook_delivery_health", side_effect=RuntimeError("boom")):
        resp = await client.get("/health")

    assert resp.status_code == 200


# ── Caching ───────────────────────────────────────────────────────────────


def test_cached_check_reuses_result_within_ttl():
    hooks_resp = _hooks_response()
    deliveries_resp = _deliveries_response([{"status_code": 200, "status": "OK", "delivered_at": "now"}])
    with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"}), \
         patch("agentit.portal.github_pr.requests.get", side_effect=[hooks_resp, deliveries_resp]) as mock_get:
        first = health_routes._check_webhook_delivery_health_cached("https://github.com/t/cache-me")
        second = health_routes._check_webhook_delivery_health_cached("https://github.com/t/cache-me")
    assert first == second
    # Only the first call should have hit GitHub's API at all (2 calls:
    # list hooks + list deliveries) -- the second call must be served
    # entirely from cache.
    assert mock_get.call_count == 2


def test_cache_expires_after_ttl(monkeypatch):
    import time as time_module

    real_monotonic = time_module.monotonic
    hooks_resp = _hooks_response()
    deliveries_resp = _deliveries_response([{"status_code": 200, "status": "OK", "delivered_at": "now"}])
    with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"}), \
         patch("agentit.portal.github_pr.requests.get", side_effect=[hooks_resp, deliveries_resp, hooks_resp, deliveries_resp]) as mock_get:
        health_routes._check_webhook_delivery_health_cached("https://github.com/t/expiring")
        # Captured `real_monotonic` before patching -- patching
        # `health_routes.time.monotonic` in place (not reassigning the
        # `time` module reference itself) means a self-referential lambda
        # would recurse into itself instead of advancing the clock.
        monkeypatch.setattr(
            health_routes.time, "monotonic",
            lambda: real_monotonic() + health_routes._WEBHOOK_HEALTH_CACHE_TTL + 1,
        )
        health_routes._check_webhook_delivery_health_cached("https://github.com/t/expiring")
    assert mock_get.call_count == 4
