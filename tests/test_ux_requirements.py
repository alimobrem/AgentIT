"""Tests for the UX-requirements checklist fixes (docs/ux-design-requirements.md).

Covers, per item number in that doc's Part 3 checklist:
  #1  type-to-confirm for the highest-blast-radius actions only
  #2  Cancel default-focus on the shared confirm modal
  #3  Auto-Mode Allowlist was the primary control, global toggle a fallback --
      this checklist item is now doubly moot: the allowlist was removed along
      with Direct Apply/AutoMode's direct-apply branch, and AutoMode itself
      has since been removed entirely too (see test_portal.py's "AutoMode
      removed (Settings page)" section)
  #4/#5 Cmd+K command palette + its own discoverable shortcut hint
  #6/#8 real, step-by-step onboarding progress + SSE streaming
  #7  optimistic UI for the Suppress action
  #9  moments of joy tied to real milestones only
  #12 gate resolution redirects to the next actionable item
  #13 cause/responsibility/next-step error messages
  #10 specific empty-state copy
  #15 prefers-reduced-motion handling
  #16 accent color reserved for "needs attention" only

Route/template-level tests use TestClient (matching tests/test_portal.py's own
conventions); genuinely interaction-level behavior (focus, live typing,
keyboard shortcuts, optimistic hide/reconcile) is covered in
tests/test_browser.py instead, per this repo's own test_browser.py
docstring convention.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.models import AssessmentReport, DimensionScore, Finding, Language, Severity, StackInfo, ArchitectureInfo
from agentit.platform_context import PlatformContext
from agentit.portal.app import app
from conftest import make_store, prime_csrf

_NO_CLUSTER = PlatformContext()


def _make_report(repo_name: str = "ux-test-app", overall_score: float | None = None) -> AssessmentReport:
    score = int(overall_score) if overall_score is not None else 45
    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        stack=StackInfo(
            languages=[Language(name="python", version="3.12", file_count=10, percentage=100.0)],
            frameworks=[], databases=[], runtimes=[], package_managers=["pip"],
        ),
        architecture=ArchitectureInfo(
            service_count=1, architecture_style="monolith", has_api=True,
            api_style="REST", external_dependencies=[], auth_mechanism=None,
        ),
        scores=[
            DimensionScore(
                dimension="security", score=score, max_score=100,
                findings=[] if score >= 100 else [
                    Finding(category="secrets", severity=Severity.high,
                            description="No secret scanning configured",
                            recommendation="Add secret scanning"),
                ],
            ),
        ],
        criticality="medium",
        summary="test summary",
        remediation_plan=[],
    )


@pytest.fixture(autouse=True)
async def _override_store():
    test_store = await make_store()
    async_store = test_store
    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=async_store), \
         patch("agentit.portal.routes.health.get_store", return_value=async_store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=async_store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=async_store), \
         patch("agentit.portal.routes.settings.get_store", return_value=async_store), \
         patch("agentit.portal.routes.insights.get_store", return_value=async_store), \
         patch("agentit.portal.routes.slos.get_store", return_value=async_store), \
         patch("agentit.image_builder.build_app_image",
               return_value={"image_ref": "test/image:test", "run_name": "test-run", "status": "skipped-in-tests"}):
        yield test_store


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as c:
        await prime_csrf(c)
        yield c


@pytest.fixture
def _mock_kube():
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": True, "error": None}
        yield mock_kube


def _cicd_file() -> dict:
    return {
        "category": "skills",
        "path": "pipeline.yaml",
        "content": (
            "apiVersion: tekton.dev/v1\nkind: Pipeline\n"
            "metadata:\n  name: build\n  namespace: openshift-pipelines\n"
        ),
        "description": "tekton pipeline",
    }


# ── #2: Cancel default-focus on the shared confirm modal ────────────────


async def test_confirm_modal_focuses_cancel_on_show(client):
    """Every existing usage of the shared confirm component shares one
    Alpine component (confirmModal()) -- the fix lives once, in show()."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "this.$refs.cancelBtn.focus()" in resp.text
    assert 'x-ref="cancelBtn"' in resp.text


# ── #1: type-to-confirm reserved for highest-blast-radius actions only ──


async def test_delete_app_uses_type_to_confirm(client, _override_store):
    store = _override_store
    await store.save(_make_report("delete-me-app"))
    resp = await client.get("/fleet")
    assert resp.status_code == 200
    assert "typeToConfirm:" in resp.text
    assert "I understand, delete this app" in resp.text


async def test_routine_fix_confirm_does_not_use_type_to_confirm(client, _override_store):
    """Routine per-finding actions must stay a plain confirm -- overusing
    type-to-confirm cheapens it (GitHub Primer, checklist #1's own
    warning)."""
    store = _override_store
    aid = await store.save(_make_report("fix-confirm-app"))
    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    # base.html's shared confirmModal() JS always references
    # `opts.typeToConfirm` (the property lookup) -- what must be absent is
    # any CALL SITE actually *setting* it (`typeToConfirm: ...`), which is
    # how the one real remaining usage (Delete App) passes it. The other
    # usage this checklist originally covered, cluster-admin-review's own
    # approval, was retired 2026-07-18 along with that gate type -- see
    # test_stale_cluster_admin_review_approval_no_longer_uses_type_to_confirm
    # below.
    assert "typeToConfirm:" not in resp.text


async def test_ordinary_recommendation_action_does_not_use_type_to_confirm(client, _override_store):
    """`cluster-admin-review` used to be one of only two actions reserved
    for the heaviest, type-to-confirm friction pattern (approving it
    performed a real, elevated-RBAC direct apply into a shared namespace).
    That gate type -- and the `gates` table/generic gate-resolution
    machinery entirely -- has since been removed (2026-07-19): the two
    remaining non-PR recommendation kinds (rollback-review, finding-
    unresolved-escalation) use the same generic, plain-confirm flow as any
    other action -- neither delivers via a direct apply anymore, so the
    extra friction is no longer warranted."""
    store = _override_store
    aid = await store.save(_make_report("ordinary-gate-app"))
    report = await store.get(aid)
    await store.log_event("slo-tracker", "rollback-recommended", report.repo_name, "warning", "Needs review")
    resp = await client.get(f"/assessments/{aid}?tab=ledger")
    assert resp.status_code == 200
    assert "typeToConfirm:" not in resp.text


# ── #3: Allowlist removed along with Direct Apply/AutoMode's direct-apply
# branch, and AutoMode itself has since been removed entirely too -- see
# test_portal.py's "AutoMode removed (Settings page)" section for the
# full coverage of what the Settings page looks like now.


# ── #4/#5: Cmd+K command palette + discoverable shortcut hint ───────────


async def test_command_palette_present_on_every_page(client, _override_store):
    for path in ("/", "/settings", "/insights", "/events"):
        resp = await client.get(path)
        assert resp.status_code == 200
        assert 'id="command-palette"' in resp.text
        assert "commandPalette()" in resp.text


async def test_command_palette_has_discoverable_shortcut_hint(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "cmdk-trigger" in resp.text
    # The literal kbd hint text (rendered as a Unicode command-glyph + "K").
    assert "\u2318K" in resp.text or "&#8984;K" in resp.text


async def test_command_palette_searches_real_fleet_data_not_mock(client, _override_store):
    """The palette's app search must hit the real /api/fleet endpoint --
    never mock/fabricated data."""
    resp = await client.get("/")
    assert "fetch('/api/fleet')" in resp.text


# ── #6/#8: real per-stage onboarding progress + SSE streaming ───────────


async def test_onboard_redirects_to_progress_page_not_straight_to_results(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report("onboard-progress-app"))
    with patch("agentit.platform_context.discover_platform", return_value=_NO_CLUSTER):
        resp = await client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    assert resp.status_code == 303
    assert f"/assessments/{aid}/onboard/progress/" in resp.headers["location"]


async def test_onboard_progress_page_shows_real_stepper_and_agent_steps(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report("onboard-progress-stepper-app"))
    with patch("agentit.platform_context.discover_platform", return_value=_NO_CLUSTER):
        resp = await client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    job_id = resp.headers["location"].rsplit("/", 1)[1]
    # By the time the background task (already run by TestClient) finished,
    # the job is "completed" -- the progress GET route redirects onward.
    progress_resp = await client.get(resp.headers["location"], follow_redirects=False)
    assert progress_resp.status_code == 303
    assert f"/assessments/{aid}/onboard-results" in progress_resp.headers["location"]
    assert job_id  # sanity: a real job id was actually minted


async def test_onboard_progress_page_renders_live_while_running(client, _override_store):
    """Directly exercise the progress template's own rendering (not just
    the redirect-once-done path) by hitting the route with a job pinned to
    'running' -- the real state a human watching a slow onboarding sees."""
    store = _override_store
    aid = await store.save(_make_report("onboard-progress-live-app"))
    job_id = await store.create_remediation_job(aid)
    await store.update_remediation_job(job_id, "running", "Running onboarding agents...")
    resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}")
    assert resp.status_code == 200
    assert "lifecycle-stepper" in resp.text
    assert "Running onboarding agents" in resp.text
    assert "sse-connect" in resp.text
    assert f"/assessments/{aid}/onboard/progress/{job_id}/stream" in resp.text


async def test_onboard_progress_page_redirects_on_failure_to_assessment_with_error(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report("onboard-progress-fail-app"))
    job_id = await store.create_remediation_job(aid)
    await store.update_remediation_job(job_id, "failed", "boom", error="Onboarding failed: boom")
    resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/assessments/{aid}?error=")


async def test_onboard_progress_stream_terminates_on_completed_job(client, _override_store):
    """Real SSE framing (checklist #8) -- pre-seed the job as already
    completed so the generator's polling loop exits after exactly one
    tick, deterministically, with no sleep."""
    store = _override_store
    aid = await store.save(_make_report("onboard-sse-app"))
    job_id = await store.create_remediation_job(aid)
    await store.update_remediation_job(job_id, "completed", "Onboarding complete")

    async with client.stream("GET", f"/assessments/{aid}/onboard/progress/{job_id}/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b"".join([chunk async for chunk in resp.aiter_bytes()])
    text = body.decode()
    assert "event: progress" in text
    assert "data:" in text


async def test_onboard_agent_steps_sourced_from_real_events_not_fabricated(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report("onboard-agent-steps-app"))
    await store.log_event("cost", "completed", "onboard-agent-steps-app", "info",
                     "Generated 2 files", correlation_id=aid)
    job_id = await store.create_remediation_job(aid)
    await store.update_remediation_job(job_id, "running", "Running onboarding agents...")
    resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}")
    assert resp.status_code == 200
    assert "cost" in resp.text
    assert "Generated 2 files" in resp.text


async def test_onboard_progress_recovers_from_a_job_orphaned_by_pod_restart(client, _override_store):
    """Regression test for a stuck-forever onboarding progress page found
    on a live instance: the already-shipped client-side stall fallback
    (commit 2c7c461) correctly re-fetches this same progress URL once the
    SSE stream goes quiet, but that only helps if the job *has* a terminal
    status to redirect to. A job whose owning pod died mid-run (a routine
    rolling deploy killing the FastAPI ``BackgroundTasks`` coroutine that
    was tracking it, no persistent queue behind it) never gets one on its
    own -- ``_reap_orphaned_jobs`` (called at startup and every 5 min) is
    what actually unsticks it, by failing the job so the stall
    fallback's re-fetch has something real to redirect to."""
    from agentit.portal.app import _reap_orphaned_jobs

    store = _override_store
    aid = await store.save(_make_report("onboard-orphaned-app"))
    job_id = await store.create_remediation_job(aid)
    await store.update_remediation_job(job_id, "running", "Running onboarding agents...")
    await store._pool.execute(
        "UPDATE remediation_jobs SET created_at = $1 WHERE id = $2",
        datetime.now(timezone.utc) - timedelta(hours=1), job_id,
    )

    await _reap_orphaned_jobs()

    resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/assessments/{aid}?error=")
    events = await store.list_events_by_correlation_id(aid)
    assert any(e["action"] == "job-reaped" for e in events)


# ── #7: optimistic UI for the (reversible, low-stakes) Suppress action ──


async def test_suppress_form_is_optimistic_htmx_with_reconciliation(client, _override_store):
    store = _override_store
    report = _make_report("suppress-app")
    report.scores[0].findings[0].source = "trivy"
    aid = await store.save(report)
    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert 'hx-post="/api/suppress"' in resp.text
    assert 'hx-swap="none"' in resp.text
    assert '@submit="suppressed = true; findingsCount--"' in resp.text
    assert "response-error.camel" in resp.text


async def test_suppress_via_htmx_returns_json_not_redirect(client, _override_store):
    """An htmx-originated call gets a small JSON ack (the client already
    optimistically reflects the outcome) -- never a full-page redirect,
    which would defeat the point of not round-tripping the whole page."""
    store = _override_store
    aid = await store.save(_make_report("suppress-json-app"))
    resp = await client.post(
        "/api/suppress",
        data={"app_name": "suppress-json-app", "check_source": "trivy", "assessment_id": aid},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "suppressed"
    assert await store.get_suppressions("suppress-json-app")


async def test_suppress_via_plain_form_still_redirects(client, _override_store):
    """Any non-htmx caller keeps the original behavior."""
    store = _override_store
    aid = await store.save(_make_report("suppress-plain-app"))
    resp = await client.post(
        "/api/suppress",
        data={"app_name": "suppress-plain-app", "check_source": "trivy", "assessment_id": aid},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/assessments/{aid}"


# ── #9: moments of joy tied to real milestones only ─────────────────────


async def test_first_perfect_score_celebrates(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report("perfect-score-app", overall_score=100))
    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert "celebrate" in resp.text
    assert "First perfect score" in resp.text


async def test_repeat_perfect_score_does_not_re_celebrate(client, _override_store):
    """A routine re-assessment that's ALREADY been at 100 before must not
    re-trigger the celebration -- checklist #9's explicit "never on
    routine actions" guard."""
    store = _override_store
    await store.save(_make_report("repeat-perfect-app", overall_score=100))
    aid2 = await store.save(_make_report("repeat-perfect-app", overall_score=100))
    resp = await client.get(f"/assessments/{aid2}")
    assert resp.status_code == 200
    assert "First perfect score" not in resp.text


async def test_non_perfect_score_never_celebrates(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report("imperfect-app", overall_score=72))
    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    # base.html's shared `.celebrate`/`@keyframes celebrate-pulse` CSS is
    # always present (it's a reusable component, not page-specific) -- what
    # must be absent is the class actually being APPLIED to this score, and
    # the celebratory copy itself.
    assert 'class="score-hero celebrate' not in resp.text
    assert "First perfect score" not in resp.text


async def test_clean_multi_manifest_delivery_celebrates(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report("multi-deliver-app"))
    await store.save_onboarding(aid, [
        {"category": "skills", "path": f"f{i}.yaml", "content": "kind: ConfigMap", "description": "x"}
        for i in range(3)
    ])
    await store.save_apply_results(
        aid, {"applied": ["f0.yaml", "f1.yaml", "f2.yaml"], "skipped": [], "errors": []},
        namespace="multi-deliver-app", dry_run=False,
    )
    resp = await client.get(f"/assessments/{aid}/onboard-results")
    assert resp.status_code == 200
    assert "celebrate" in resp.text
    assert "all 3 manifests applied clean" in resp.text


async def test_single_manifest_delivery_does_not_celebrate(client, _override_store):
    """A routine single-fix Deliver must never be gamified (checklist #9's
    explicit warning against celebrating routine actions)."""
    store = _override_store
    aid = await store.save(_make_report("single-deliver-app"))
    await store.save_onboarding(aid, [
        {"category": "skills", "path": "f0.yaml", "content": "kind: ConfigMap", "description": "x"},
    ])
    await store.save_apply_results(
        aid, {"applied": ["f0.yaml"], "skipped": [], "errors": []},
        namespace="single-deliver-app", dry_run=False,
    )
    resp = await client.get(f"/assessments/{aid}/onboard-results")
    assert resp.status_code == 200
    assert 'class="step-guide celebrate' not in resp.text
    assert "manifests applied clean" not in resp.text


# ── #12: gate resolution redirects to the next actionable item ──────────


# ── #13: cause/responsibility/next-step error messages ──────────────────


async def test_deliver_failure_states_cause_and_next_step(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report("deliver-fail-app"))
    await store.save_onboarding(aid, [
        {"category": "skills", "path": "f0.yaml", "content": "kind: ConfigMap", "description": "x"},
    ])
    with patch("agentit.portal.delivery.route_and_deliver",
               side_effect=RuntimeError("cluster unreachable: connection refused")):
        resp = await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)
    assert resp.status_code == 303
    from urllib.parse import unquote
    location = unquote(resp.headers["location"])
    assert "cluster unreachable" in location  # the real cause, not a bare "check server logs"
    assert "retry Deliver" in location  # a concrete next step


async def test_onboarding_failure_states_cause_and_next_step(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report("onboard-fail-app"))
    with patch("agentit.portal.routes.assessments._run_onboarding",
               side_effect=RuntimeError("repository not reachable")):
        resp = await client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    progress_url = resp.headers["location"]
    progress_resp = await client.get(progress_url, follow_redirects=False)
    assert progress_resp.status_code == 303
    from urllib.parse import unquote
    location = unquote(progress_resp.headers["location"])
    assert location.startswith(f"/assessments/{aid}?error=")
    assert "repository not reachable" in location
    assert "retry Onboard" in location


# ── #10: specific, real empty-state copy ─────────────────────────────────


async def test_ledger_tab_empty_state_shows_real_recent_resolution_count(client, _override_store):
    """The "recently resolved" empty-state count reflects real, recently
    merged/closed PRs -- the `gates` table/generic gate-resolution
    machinery it used to also count has been removed entirely
    (2026-07-19)."""
    store = _override_store
    aid = await store.save(_make_report("empty-state-actions-app"))
    report = await store.get(aid)
    pr_url = "https://github.com/org/empty-state-actions-app/pull/1"
    await store.create_delivery(
        aid, report.repo_name, {"cluster_config": 1}, mechanism="cluster_config:infra-repo-commit",
        status="delivered", details={"outcomes": {"cluster_config": {"pr_url": pr_url}}},
    )
    with patch("agentit.portal.github_pr.merge_pr", return_value={"merged": True, "sha": "abc123"}):
        await client.post("/prs/merge", data={"pr_url": pr_url, "assessment_id": aid})

    from datetime import datetime, timezone
    with patch(
        "agentit.portal.github_pr.get_pr_status",
        return_value={
            "state": "merged", "html_url": pr_url, "title": "fix",
            "merged_at": datetime.now(timezone.utc).isoformat(),
        },
    ):
        resp = await client.get(f"/assessments/{aid}?tab=ledger")
    assert resp.status_code == 200
    assert "No pending actions" in resp.text
    assert "resolved in the last 24 hours" in resp.text


# ── #15: prefers-reduced-motion handling ─────────────────────────────────


async def test_prefers_reduced_motion_globally_handled(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "prefers-reduced-motion: reduce" in resp.text
    assert "transition-duration: 0.001ms !important" in resp.text


async def test_toast_motion_classes_are_actually_defined(client):
    """Regression: toast-enter/toast-leave were referenced by x-transition
    but never defined anywhere in the stylesheet -- a real (if silent) bug
    this pass also fixes while adding reduced-motion support."""
    resp = await client.get("/")
    assert ".toast-enter {" in resp.text
    assert ".toast-leave {" in resp.text


# ── #16: accent color reserved for "needs attention" only ───────────────


async def test_accent_color_not_used_for_plain_links_or_headings(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "a { color: var(--color-link); text-decoration: none; }" in resp.text
    assert "h1 { color: var(--color-heading);" in resp.text
    assert "h2 { font-family: var(--font-display); font-size: 1.15rem; color: var(--color-heading);" in resp.text


async def test_accent_color_still_reserved_for_attention_signals(client, _override_store):
    """nav-badge (pending-count bubble) and the confirm modal's danger
    styling are legitimate "needs attention" uses -- still accent."""
    resp = await client.get("/")
    assert ".nav-badge { background: var(--color-accent);" in resp.text
