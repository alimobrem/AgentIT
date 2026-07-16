"""Tests for the ambient deploy-status indicator (nav badge + Health page
detail): `portal/metrics.py::get_build_info()`/`set_build_info()`,
`portal/github_pr.py::get_commit_info()`, and
`portal/routes/health.py::_get_deploy_status()` plus its two routes.

Mirrors the mocking convention already used for `/health` and friends in
test_portal.py: `patch("agentit.portal.routes.health.kube")` so no test
makes a real cluster round trip, and `prime_csrf`/an `httpx.AsyncClient`
(ASGI transport) for the route-level tests.
"""
from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal import github_pr
from agentit.portal.app import app
from agentit.portal.metrics import get_build_info, set_build_info
from agentit.portal.routes import health as health_routes
from agentit.portal.routes.health import _get_deploy_status

from conftest import prime_csrf


@pytest.fixture(autouse=True)
def _clear_deploy_status_cache():
    """Isolate last-good / TTL cache across tests."""
    health_routes._deploy_status_cache["data"] = None
    health_routes._deploy_status_cache["ts"] = 0.0
    yield
    health_routes._deploy_status_cache["data"] = None
    health_routes._deploy_status_cache["ts"] = 0.0


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as c:
        await prime_csrf(c)
        yield c


# ── portal/metrics.py: build info cache ────────────────────────────────


def test_set_and_get_build_info_round_trip():
    set_build_info("1.2.3", "abcdef0123456789", "abcdef0123456789")
    try:
        info = get_build_info()
        assert info == {
            "version": "1.2.3",
            "commit": "abcdef0123456789",
            "image_tag": "abcdef0123456789",
        }
    finally:
        set_build_info("unknown", "unknown", "unknown")


def test_get_build_info_defaults_to_unknown_before_startup_hook():
    """Regression guard: a fresh process (or a test that hasn't called
    set_build_info) must report "unknown", never raise or return None."""
    set_build_info("unknown", "unknown", "unknown")
    info = get_build_info()
    assert info["version"] == "unknown"
    assert info["commit"] == "unknown"
    assert info["image_tag"] == "unknown"


# ── portal/github_pr.py: get_commit_info ───────────────────────────────


def test_get_commit_info_success(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "sha": "abc123",
                "commit": {
                    "message": "fix: handle empty PipelineRun list\n\nLonger body here.",
                    "author": {"name": "Jane Doe"},
                },
                "html_url": "https://github.com/alimobrem/AgentIT/commit/abc123",
            }

    with patch("agentit.portal.github_pr.requests.get", return_value=_Resp()):
        info = github_pr.get_commit_info("https://github.com/alimobrem/AgentIT.git", "abc123")

    assert info["message"] == "fix: handle empty PipelineRun list"
    assert info["author"] == "Jane Doe"
    assert info["html_url"] == "https://github.com/alimobrem/AgentIT/commit/abc123"


def test_get_commit_info_missing_token_returns_empty(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    info = github_pr.get_commit_info("https://github.com/alimobrem/AgentIT.git", "abc123")
    assert info == {}


def test_get_commit_info_api_failure_returns_empty_not_fabricated(monkeypatch):
    """Never invent a commit message when the real GitHub call fails."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    with patch("agentit.portal.github_pr.requests.get", side_effect=Exception("network error")):
        info = github_pr.get_commit_info("https://github.com/alimobrem/AgentIT.git", "abc123")
    assert info == {}


# ── routes/health.py::_get_deploy_status ───────────────────────────────


_PR_NAME = "agentit-ci-abc12"


def _pipelinerun(reason: str, status: str, revision: str = "deadbeef" * 5, task_names=None) -> dict:
    """Build a PipelineRun dict matching the real Tekton v1 shape verified
    live: ``childReferences`` carry only name/kind/pipelineTaskName, never an
    embedded ``conditions`` array -- each task's real status must come from
    its own TaskRun object (see ``_taskrun_get_custom_resource`` below)."""
    return {
        "metadata": {"name": _PR_NAME, "labels": {"tekton.dev/pipeline": "agentit-ci"}},
        "spec": {"params": [{"name": "revision", "value": revision}]},
        "status": {
            "conditions": [{"reason": reason, "status": status}],
            "childReferences": [
                {"pipelineTaskName": name, "name": f"{_PR_NAME}-{name}"} for name in (task_names or [])
            ],
        },
    }


def _taskrun_get_custom_resource(task_statuses: dict[str, str]):
    """``kube.get_custom_resource`` side_effect resolving each TaskRun name
    (``<pipelinerun>-<task>``, matching ``_pipelinerun`` above) to a real
    TaskRun-shaped status dict, or ``None`` (404) for a task not in
    ``task_statuses`` (not started yet)."""
    def _get(group, version, plural, name, namespace="", **_kwargs):
        for task_name, task_status in task_statuses.items():
            if name == f"{_PR_NAME}-{task_name}":
                return {"status": {"conditions": [{"reason": task_status}]}}
        return None
    return _get


def _argo_app(sync: str, health: str, image_tag: str = "deadbeef" * 5, health_message: str = "") -> dict:
    return {
        "metadata": {"name": "agentit"},
        "spec": {
            "source": {
                "repoURL": "https://github.com/alimobrem/AgentIT.git",
                "helm": {"parameters": [{"name": "image.tag", "value": image_tag}]},
            },
        },
        "status": {
            "sync": {"status": sync},
            "health": {"status": health, "message": health_message},
        },
    }


def test_deploy_status_idle_when_nothing_running_or_pending():
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        status = _get_deploy_status()

    assert status["state"] == "idle"
    assert status["stage"] is None
    assert status["errors"] == []


def test_deploy_status_deploying_reports_current_pipeline_task():
    running_pr = _pipelinerun("Running", "Unknown", task_names=["git-clone", "run-tests"])
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [running_pr] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        mock_kube.get_custom_resource.side_effect = _taskrun_get_custom_resource(
            {"git-clone": "Succeeded", "run-tests": "Running"}
        )
        status = _get_deploy_status()

    assert status["state"] == "deploying"
    assert status["stage"] == "run-tests"
    assert status["pipeline"]["running"] is True
    assert status["pipeline"]["tasks"][1] == {"name": "run-tests", "status": "Running"}


def test_deploy_status_stage_pending_task_not_yet_started():
    """A task with no TaskRun object yet (hasn't started) reports 'Pending',
    not a fabricated 'Unknown'/'Succeeded'."""
    running_pr = _pipelinerun("Running", "Unknown", task_names=["git-clone", "run-tests", "build-image"])
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [running_pr] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        mock_kube.get_custom_resource.side_effect = _taskrun_get_custom_resource(
            {"git-clone": "Succeeded"}
        )
        status = _get_deploy_status()

    assert status["pipeline"]["tasks"][1] == {"name": "run-tests", "status": "Pending"}
    assert status["pipeline"]["tasks"][2] == {"name": "build-image", "status": "Pending"}
    assert status["stage"] == "run-tests"


def test_deploy_status_pipeline_failure_is_reported_not_hidden():
    """Regression guard: a real PipelineRun failure must surface as
    state=='failed' with a reason, never silently look like 'idle'."""
    failed_pr = _pipelinerun("Failed", "False")
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [failed_pr] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        status = _get_deploy_status()

    assert status["state"] == "failed"
    assert "Failed" in status["reason"]


def test_deploy_status_argo_out_of_sync_is_deploying_syncing():
    succeeded_pr = _pipelinerun("Succeeded", "True")
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [succeeded_pr] if "tekton" in group else [_argo_app("OutOfSync", "Healthy")]
        )
        status = _get_deploy_status()

    assert status["state"] == "deploying"
    assert status["stage"] == "syncing"


def test_deploy_status_argo_progressing_is_deploying_rolling_out():
    succeeded_pr = _pipelinerun("Succeeded", "True")
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [succeeded_pr] if "tekton" in group else [_argo_app("Synced", "Progressing")]
        )
        status = _get_deploy_status()

    assert status["state"] == "deploying"
    assert status["stage"] == "rolling out"


def test_deploy_status_argo_degraded_is_failed_with_message():
    """Regression guard for the 'never silently show nothing when something
    failed' rule: a Degraded Argo Application must surface, with its real
    health message when available."""
    succeeded_pr = _pipelinerun("Succeeded", "True")
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [succeeded_pr] if "tekton" in group
            else [_argo_app("Synced", "Degraded", health_message="Rollout has degraded")]
        )
        status = _get_deploy_status()

    assert status["state"] == "failed"
    assert status["reason"] == "Rollout has degraded"


def test_deploy_status_resolved_healthy_when_running_commit_matches_target():
    revision = "a" * 40
    succeeded_pr = _pipelinerun("Succeeded", "True", revision=revision)
    set_build_info("1.0.0", revision, revision)
    try:
        with patch("agentit.portal.routes.health.kube") as mock_kube:
            mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
                [succeeded_pr] if "tekton" in group else [_argo_app("Synced", "Healthy", image_tag=revision)]
            )
            status = _get_deploy_status()
    finally:
        set_build_info("unknown", "unknown", "unknown")

    assert status["state"] == "idle"
    assert status["resolved"]["outcome"] == "healthy"


def test_deploy_status_resolved_rolled_back_when_running_commit_differs():
    target_revision = "a" * 40
    running_commit = "b" * 40
    succeeded_pr = _pipelinerun("Succeeded", "True", revision=target_revision)
    set_build_info("1.0.0", running_commit, running_commit)
    try:
        with patch("agentit.portal.routes.health.kube") as mock_kube:
            mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
                [succeeded_pr] if "tekton" in group
                else [_argo_app("Synced", "Healthy", image_tag=target_revision)]
            )
            status = _get_deploy_status()
    finally:
        set_build_info("unknown", "unknown", "unknown")

    assert status["state"] == "idle"
    assert status["resolved"]["outcome"] == "rolled_back"
    assert running_commit[:12] in status["resolved"]["message"]
    assert target_revision[:12] in status["resolved"]["message"]


def test_deploy_status_reports_unreachable_apis_via_errors_not_silence():
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = Exception("cluster unreachable")
        status = _get_deploy_status()

    assert status["state"] == "idle"
    assert len(status["errors"]) == 2


def test_deploy_status_include_commit_info_calls_github_pr(monkeypatch):
    revision = "c" * 40
    running_pr = _pipelinerun("Running", "Unknown", revision=revision)
    with patch("agentit.portal.routes.health.kube") as mock_kube, \
         patch("agentit.portal.routes.health.github_pr.get_commit_info") as mock_commit_info:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [running_pr] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        mock_commit_info.return_value = {"message": "feat: add deploy status indicator"}
        status = _get_deploy_status(include_commit_info=True)

    mock_commit_info.assert_called_once_with("https://github.com/alimobrem/AgentIT.git", revision)
    assert status["commit_info"]["message"] == "feat: add deploy status indicator"


def test_deploy_status_excludes_commit_info_by_default():
    running_pr = _pipelinerun("Running", "Unknown")
    with patch("agentit.portal.routes.health.kube") as mock_kube, \
         patch("agentit.portal.routes.health.github_pr.get_commit_info") as mock_commit_info:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [running_pr] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        status = _get_deploy_status()

    mock_commit_info.assert_not_called()
    assert status["commit_info"] is None


# ── Routes: ambient badge + Health page detail ─────────────────────────


async def test_deploy_status_badge_route_idle(client):
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.return_value = []
        resp = await client.get("/api/deploy-status")

    assert resp.status_code == 200
    assert "deploy-status-badge" in resp.text
    assert 'role="status"' in resp.text


async def test_deploy_status_badge_route_deploying(client):
    running_pr = _pipelinerun("Running", "Unknown", task_names=["build-image"])
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [running_pr] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        mock_kube.get_custom_resource.side_effect = _taskrun_get_custom_resource({"build-image": "Running"})
        resp = await client.get("/api/deploy-status")

    assert resp.status_code == 200
    assert "Deploying" in resp.text
    assert "build-image" in resp.text
    assert "deploy-status-deploying" in resp.text


async def test_deploy_status_badge_never_calls_github(client):
    """The ambient/frequently-polled badge must stay cheap -- no GitHub API
    call on every 15s poll."""
    running_pr = _pipelinerun("Running", "Unknown")
    with patch("agentit.portal.routes.health.kube") as mock_kube, \
         patch("agentit.portal.routes.health.github_pr.get_commit_info") as mock_commit_info:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [running_pr] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        await client.get("/api/deploy-status")

    mock_commit_info.assert_not_called()


async def test_deploy_status_badge_timeout_returns_200_degraded(client, monkeypatch):
    """Regression for the outage: a wedged kube-apiserver must not hang the
    badge until oauth-proxy returns 502/503 -- respond 200 degraded/unknown."""
    monkeypatch.setattr(health_routes, "_DEPLOY_STATUS_DEADLINE", 0.05)
    released = threading.Event()

    def _hang(*_a, **_kw):
        released.wait(timeout=5)
        return {}

    try:
        with patch("agentit.portal.routes.health._get_deploy_status_bounded", side_effect=_hang):
            resp = await client.get("/api/deploy-status")
    finally:
        released.set()

    assert resp.status_code == 200
    assert "deploy-status-badge" in resp.text
    assert "deploy-status-degraded" in resp.text
    assert "Status unknown" in resp.text


async def test_deploy_status_badge_timeout_serves_last_good(client, monkeypatch):
    """When a prior poll succeeded, a timed-out poll serves last-good HTML."""
    monkeypatch.setattr(health_routes, "_DEPLOY_STATUS_DEADLINE", 0.05)
    good = {
        "running": {"version": "9.9.9", "commit": "abcdef0123456789", "image_tag": "abcdef0123456789"},
        "pipeline": None,
        "argo": {"sync": "Synced", "health": "Healthy"},
        "commit_info": None,
        "state": "idle",
        "stage": None,
        "reason": None,
        "resolved": None,
        "errors": [],
    }
    health_routes._deploy_status_cache["data"] = good
    health_routes._deploy_status_cache["ts"] = 0.0  # stale — force refresh path
    released = threading.Event()

    def _hang(*_a, **_kw):
        released.wait(timeout=5)
        return {}

    try:
        with patch("agentit.portal.routes.health._get_deploy_status_bounded", side_effect=_hang):
            resp = await client.get("/api/deploy-status")
    finally:
        released.set()

    assert resp.status_code == 200
    assert "deploy-status-degraded" in resp.text
    assert "v9.9.9" in resp.text
    assert "abcdef0" in resp.text


def test_deploy_status_bounded_uses_cache_within_ttl():
    """htmx polling must not hammer the apiserver every 15s when cache is warm."""
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        first = health_routes._get_deploy_status_bounded()
        second = health_routes._get_deploy_status_bounded()

    assert first["state"] == "idle"
    assert second is first
    assert mock_kube.list_custom_resources.call_count == 2  # tekton + argo, once


async def test_health_page_shows_deployment_status_section(client):
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.return_value = []
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert "Deployment Status" in resp.text


async def test_health_page_deployment_status_shows_pipeline_stepper(client):
    running_pr = _pipelinerun("Running", "Unknown", task_names=["git-clone", "run-tests"])
    with patch("agentit.portal.routes.health.kube") as mock_kube, \
         patch("agentit.portal.routes.health.github_pr.get_commit_info") as mock_commit_info:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [running_pr] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        mock_kube.get_custom_resource.side_effect = _taskrun_get_custom_resource(
            {"git-clone": "Succeeded", "run-tests": "Running"}
        )
        mock_commit_info.return_value = {"message": "feat: add ambient deploy status"}
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert "run-tests" in resp.text
    assert "feat: add ambient deploy status" in resp.text


async def test_health_page_deployment_status_reports_pipeline_failure():
    """The Health page's detail section must not silently show 'Idle' when
    the underlying PipelineRun actually failed."""
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True)
    await prime_csrf(c)
    failed_pr = _pipelinerun("Failed", "False")
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [failed_pr] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        resp = await c.get("/health")

    assert resp.status_code == 200
    assert "Failed" in resp.text


async def test_nav_badge_present_on_every_page(client):
    """The ambient indicator lives in base.html's nav, so it must render on
    any page, not just /health."""
    resp = await client.get("/")
    assert 'id="deploy-status"' in resp.text
    assert "/api/deploy-status" in resp.text


def test_deploy_status_cancelled_pipeline_is_not_failed():
    """Cancelled CI (capacity / concurrency) must not pin the Health badge to Failed."""
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [_pipelinerun("Cancelled", "False")] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        status = _get_deploy_status()
    assert status["state"] == "idle"
    assert status["pipeline"]["reason"] == "Cancelled"


def test_deploy_status_picks_newest_pipelinerun_by_creation_timestamp():
    older = _pipelinerun("Failed", "False", revision="oldrev" + "0" * 34)
    older["metadata"]["name"] = "agentit-ci-older"
    older["metadata"]["creationTimestamp"] = "2026-07-16T10:00:00Z"
    newer = _pipelinerun("Succeeded", "True", revision="newrev" + "0" * 34)
    newer["metadata"]["name"] = "agentit-ci-newer"
    newer["metadata"]["creationTimestamp"] = "2026-07-16T12:00:00Z"
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [newer, older] if "tekton" in group else [_argo_app("Synced", "Healthy")]
        )
        status = _get_deploy_status()
    assert status["pipeline"]["name"] == "agentit-ci-newer"
    assert status["state"] == "idle"


def test_deploy_status_argo_operation_running_is_deploying_not_failed():
    app = _argo_app("Synced", "Degraded", health_message="hook waiting")
    app["status"]["operationState"] = {"phase": "Running", "message": "waiting for hook"}
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [] if "tekton" in group else [app]
        )
        status = _get_deploy_status()
    assert status["state"] == "deploying"
    assert status["stage"] == "syncing"


def test_deploy_status_argo_suspended_is_deploying_canary_pause():
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = lambda group, *a, **kw: (
            [] if "tekton" in group else [_argo_app("Synced", "Suspended")]
        )
        status = _get_deploy_status()
    assert status["state"] == "deploying"
    assert status["stage"] == "canary pause"
