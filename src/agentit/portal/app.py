from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import time as _time
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

from markupsafe import Markup

from agentit.logging_config import configure_logging

configure_logging()

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse
from fastapi.templating import Jinja2Templates

from agentit.audit import audit_log
from agentit.cloner import clone_repo
from agentit.models import AssessmentReport, Severity
from agentit.portal.cluster_apply import apply_manifests_to_cluster, install_operator
from agentit.portal.github_pr import create_onboarding_pr
from agentit.portal.helpers import (
    get_store,
    get_retention_days,
    get_current_user,
    publish_event as _publish_event,
    safe_url as _safe_url,
    format_dimension as _format_dimension,
    get_llm_client as _get_llm_client,
)
from agentit.portal.csrf import (
    CSRF_COOKIE_NAME,
    STATE_CHANGING_METHODS,
    generate_csrf_token,
    is_csrf_exempt,
    verify_csrf,
)
from agentit.agents.capabilities import AGENT_CAPABILITIES, WATCHER_AGENTS as _WATCHER_AGENTS
from agentit.runner import run_assessment
from agentit.skill_inventory import diff_and_log_inventory_changes

log = logging.getLogger(__name__)

_skills_cache: dict = {"data": None, "ts": 0}
_checks_cache: dict = {"data": None, "ts": 0}
_CACHE_TTL = 60  # seconds


def _cached_skills():
    if _skills_cache["data"] is None or _time.monotonic() - _skills_cache["ts"] > _CACHE_TTL:
        from agentit.skill_engine import load_all_skills
        _skills_cache["data"] = load_all_skills(Path("skills"))
        _skills_cache["ts"] = _time.monotonic()
    return _skills_cache["data"]


def _cached_checks():
    if _checks_cache["data"] is None or _time.monotonic() - _checks_cache["ts"] > _CACHE_TTL:
        from agentit.check_engine import load_checks
        _checks_cache["data"] = load_checks(Path("checks"))
        _checks_cache["ts"] = _time.monotonic()
    return _checks_cache["data"]


OPERATION_TIMEOUT = 300  # 5 minutes max for any blocking operation


async def _with_timeout(coro, timeout: int = OPERATION_TIMEOUT):
    """Wrap an async operation with a timeout to prevent stuck requests."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"Operation timed out after {timeout}s")


def _get_trusted_base_url(request: Request) -> str:
    """This app's own externally-reachable base URL, for building outbound URLs
    (e.g. the GitHub webhook registration below) that we hand to third parties.

    Deliberately does NOT use `request.base_url` as the primary source: that's
    derived from the client-supplied Host header, so a forged Host would make
    us register a webhook pointing at an attacker-controlled server. Prefer an
    explicit trusted override, then our own OpenShift Route (a cluster-side,
    not client-side, source of truth). Only falls back to the request's Host
    header if neither is available (e.g. local dev with no Route).
    """
    override = os.environ.get("AGENTIT_EXTERNAL_URL")
    if override:
        return override.rstrip("/")
    # Only attempt the Route lookup when actually running in-cluster (the
    # standard KUBERNETES_SERVICE_HOST env var Kubernetes injects into every
    # pod) -- otherwise this would fall through to a real, possibly slow or
    # unreachable, kubeconfig-based cluster on the developer's machine (e.g.
    # in local dev/tests) for every request, instead of a fast, correct
    # no-op that lands on the same request.base_url fallback anyway.
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        try:
            from agentit import kube
            namespace = os.environ.get("AGENTIT_NAMESPACE", "agentit")
            routes = kube.list_custom_resources("route.openshift.io", "v1", "routes", namespace)
            for route in routes:
                if route.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") == "agentit":
                    host = route.get("spec", {}).get("host")
                    if host:
                        return f"https://{host}"
        except Exception:
            log.warning("Could not resolve own Route for trusted base URL; "
                        "falling back to request Host header", exc_info=True)
    return str(request.base_url).rstrip("/")


TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="AgentIT Portal")

from agentit.portal.metrics import instrument_app
instrument_app(app)


def _csrf_context_processor(request: Request) -> dict:
    """Exposes `{{ csrf_token }}` to every template, sourced from the token
    the csrf_middleware below already resolved for this request -- so any
    template can render `<input type="hidden" name="csrf_token" value="{{
    csrf_token }}">` if it isn't relying on the htmx:configRequest header
    injection in base.html (e.g. a non-boosted form)."""
    return {"csrf_token": getattr(request.state, "csrf_token", "")}


templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[_csrf_context_processor])


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    """Double-submit-cookie CSRF protection -- see csrf.py for the full
    rationale. Runs on every request (not just POSTs) so GET requests always
    have a fresh token available to hand out via the cookie + template
    global above, ready for whatever POST/etc. the page subsequently makes."""
    token = request.cookies.get(CSRF_COOKIE_NAME) or generate_csrf_token()
    request.state.csrf_token = token

    if request.method in STATE_CHANGING_METHODS and not is_csrf_exempt(request.url.path):
        if not await verify_csrf(request):
            return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)

    response = await call_next(request)

    if request.cookies.get(CSRF_COOKIE_NAME) != token:
        # Secure only when the client actually arrived over HTTPS (checking
        # X-Forwarded-Proto since TLS terminates at the Route/oauth-proxy,
        # not this app) -- an unconditional Secure flag would make the
        # cookie silently never get sent back in plain-HTTP dev/test setups.
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        response.set_cookie(
            CSRF_COOKIE_NAME, token,
            httponly=False, samesite="lax", secure=(proto == "https"), path="/",
        )
    return response


_maintenance_task = None


async def _background_maintenance() -> None:
    """Every 5 min: refresh DB/event-buffer size metrics. Hourly: expire stale
    gates, diff the skill/check inventory. Daily: purge old data."""
    tick = 0
    while True:
        await asyncio.sleep(300)
        tick += 1

        try:
            from agentit.portal.metrics import refresh_db_metrics
            s = await get_store()
            refresh_db_metrics(s.raw if hasattr(s, "raw") else s)
        except Exception:
            log.debug("Background DB metrics refresh failed", exc_info=True)

        if tick % 12 != 0:
            continue  # everything below this line only runs hourly (12 * 5min)

        try:
            s = await get_store()
            expired = await s.expire_stale_gates(hours=24)
            if expired:
                log.info("Background: expired %d stale gates", expired)
            if (tick // 12) % 24 == 0:
                retention = get_retention_days()
                counts = await s.purge_old_data(retention_days=retention)
                total = sum(counts.values())
                if total:
                    log.info("Background: purged %d old rows (retention=%dd)", total, retention)
        except Exception:
            log.debug("Background maintenance failed", exc_info=True)

        try:
            s = await get_store()
            diff_and_log_inventory_changes(s.raw if hasattr(s, "raw") else s)
        except Exception:
            log.debug("Background skill inventory diff failed", exc_info=True)


def _set_build_info() -> None:
    """Populate the `agentit_build` Info metric once at startup.

    Best-effort: `AGENTIT_IMAGE_TAG`/`AGENTIT_GIT_COMMIT` are set by the CI
    pipeline in the container's env; falls back to "unknown" locally rather
    than failing the whole startup sequence.
    """
    from agentit.portal.metrics import build_info
    try:
        import importlib.metadata
        version = importlib.metadata.version("agentit")
    except Exception:
        version = "unknown"
    build_info.info({
        "version": version,
        "commit": os.environ.get("AGENTIT_GIT_COMMIT", "unknown"),
        "image_tag": os.environ.get("AGENTIT_IMAGE_TAG", "unknown"),
    })


@app.on_event("startup")
async def _start_background_tasks() -> None:
    global _maintenance_task
    _set_build_info()
    _maintenance_task = asyncio.create_task(_background_maintenance())


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _maintenance_task:
        _maintenance_task.cancel()
    try:
        from agentit.events import get_publisher
        get_publisher().close()
    except Exception:
        log.debug("Publisher close failed", exc_info=True)
    try:
        from agentit.portal.helpers import get_store
        s = await get_store()
        if hasattr(s, "close"):
            await s.close()
        else:
            (s.raw if hasattr(s, "raw") else s)._conn.close()
    except Exception:
        log.debug("Store close failed", exc_info=True)


def _tojson_filter(value: object) -> "Markup":
    """Safely embed a Python value as a JS literal inside an HTML attribute.

    FastAPI's Jinja2Templates doesn't register Flask's `tojson` filter, which
    templates need for values interpolated into inline Alpine expressions
    (e.g. @click="... 'Delete ' + {{ name | tojson }} ..."). Without this,
    raw string interpolation like {{ r.repo_name }} inside a JS string
    literal lets a value containing a quote break out of the string and
    inject arbitrary JS. json.dumps + escaping <, >, &, ' mirrors Flask's
    implementation and makes the result safe to drop into an HTML attribute.
    """
    raw = json.dumps(value)
    raw = (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("'", "\\u0027")
    )
    return Markup(raw)


templates.env.filters["safe_url"] = _safe_url
templates.env.filters["dimension_label"] = _format_dimension
templates.env.filters["tojson"] = _tojson_filter


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html",
        {"status_code": 404, "detail": "Page not found"},
        status_code=404,
    )


@app.exception_handler(500)
async def server_error_handler(request: Request, exc: Exception) -> HTMLResponse:
    log.exception("Internal server error on %s", request.url.path)
    return templates.TemplateResponse(
        request, "error.html",
        {"status_code": 500, "detail": "Internal server error"},
        status_code=500,
    )


# ── Register route modules ───────────────────────────────────────────

from agentit.portal.routes.webhooks import router as webhooks_router  # noqa: E402
from agentit.portal.routes.health import router as health_router  # noqa: E402
from agentit.portal.routes.schedules import router as schedules_router  # noqa: E402

app.include_router(webhooks_router)
app.include_router(health_router)
app.include_router(schedules_router)


# ── Fleet enrichment ─────────────────────────────────────────────────


_argo_cache: dict = {"data": {}, "ts": 0}
_ARGO_CACHE_TTL = 60  # seconds


def _enrich_fleet_with_cluster_status(fleet: list[dict], _store=None) -> list[dict]:
    """Check cluster for each app's deployment status. Caches Argo CD data for 60s."""
    import time as _t
    from agentit import kube

    now = _t.monotonic()
    if _argo_cache["data"] and (now - _argo_cache["ts"]) < _ARGO_CACHE_TTL:
        argo_status = _argo_cache["data"]
    else:
        argo_status = {}
        try:
            items = kube.list_custom_resources("argoproj.io", "v1alpha1", "applications", namespace="openshift-gitops")
            for a in items:
                name = a.get("metadata", {}).get("name", "")
                dest = a.get("spec", {}).get("destination", {})
                cluster = dest.get("server", "unknown")
                namespace = dest.get("namespace", "default")
                argo_status[name] = {
                    "sync": a.get("status", {}).get("sync", {}).get("status", "Unknown"),
                    "health": a.get("status", {}).get("health", {}).get("status", "Unknown"),
                    "cluster": cluster,
                    "namespace": namespace,
                }
        except Exception:
            log.debug("Failed to fetch Argo CD apps for fleet enrichment", exc_info=True)
        _argo_cache["data"] = argo_status
        _argo_cache["ts"] = now

    for app_item in fleet:
        app_name = app_item["repo_name"].lower().replace("_", "-").replace(".", "-")
        argo = argo_status.get(app_name)
        apply_results = None
        try:
            apply_results = _store.get_apply_results(app_item["id"]) if _store else None
        except Exception:
            log.debug("Failed to get apply results for %s", app_item["id"], exc_info=True)

        if argo:
            app_item["deploy_status"] = "synced" if argo["sync"] == "Synced" else "out-of-sync"
            app_item["deploy_health"] = argo["health"].lower()
            app_item["deploy_cluster"] = argo["cluster"]
            app_item["deploy_namespace"] = argo["namespace"]
        elif apply_results and apply_results.get("applied"):
            app_item["deploy_status"] = "applied"
            app_item["deploy_health"] = "unknown"
            app_item["deploy_cluster"] = "local"
            app_item["deploy_namespace"] = apply_results.get("namespace", "default")
        else:
            app_item["deploy_status"] = "not deployed"
            app_item["deploy_health"] = "—"
            app_item["deploy_cluster"] = "—"
            app_item["deploy_namespace"] = "—"

    return fleet


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    s = await get_store()
    fleet = await s.get_fleet_data()
    fleet = await asyncio.to_thread(_enrich_fleet_with_cluster_status, fleet, s.raw if hasattr(s, "raw") else s)
    total_apps = len(fleet)
    if total_apps == 0:
        return templates.TemplateResponse(request, "dashboard.html", {
            "assessments": [], "total_apps": 0, "avg_score": 0, "critical_total": 0, "trends": {},
        })
    avg_score = sum(r["latest_score"] for r in fleet) / total_apps
    critical_total = sum(r["critical_count"] for r in fleet)

    from agentit.portal.metrics import fleet_size as _fs, fleet_avg_score as _fas
    _fs.set(total_apps)
    _fas.set(avg_score)

    return templates.TemplateResponse(
        request,
        "fleet.html",
        {
            "fleet": fleet,
            "total_apps": total_apps,
            "avg_score": avg_score,
            "critical_total": critical_total,
        },
    )


@app.get("/fleet", response_class=HTMLResponse)
async def fleet_redirect() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=301)


@app.get("/fleet/slos", response_class=HTMLResponse)
async def fleet_slos(request: Request) -> HTMLResponse:
    """Fleet-wide SLO view — all SLOs across all apps."""
    s = await get_store()
    fleet = await s.get_fleet_data()
    all_slos = []
    for app_data in fleet:
        slos = await s.list_slos(app_data["id"])
        for slo in slos:
            slo["app_name"] = app_data["repo_name"]
            slo["app_id"] = app_data["id"]
            all_slos.append(slo)
    breached = [sl for sl in all_slos if sl.get("status") == "breached"]
    return templates.TemplateResponse(request, "fleet_slos.html", {
        "slos": all_slos, "breached_count": len(breached),
        "total_count": len(all_slos),
    })


@app.get("/fleet/remediations", response_class=HTMLResponse)
async def fleet_remediations(request: Request) -> HTMLResponse:
    """Fleet-wide remediation view — all remediations across all apps."""
    s = await get_store()
    fleet = await s.get_fleet_data()
    all_remediations = []
    for app_data in fleet:
        remeds = await s.list_remediations(app_data["id"])
        for r in remeds:
            r["app_name"] = app_data["repo_name"]
            r["app_id"] = app_data["id"]
            all_remediations.append(r)
    pending = [r for r in all_remediations if r.get("status") != "completed"]
    return templates.TemplateResponse(request, "fleet_remediations.html", {
        "remediations": all_remediations, "pending_count": len(pending),
        "total_count": len(all_remediations),
    })


@app.get("/api/fleet")
async def api_fleet() -> JSONResponse:
    s = await get_store()
    return JSONResponse(await s.get_fleet_data())


@app.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request) -> HTMLResponse:
    s = await get_store()
    fleet_insights = await s.get_fleet_insights()
    agent_stats = await s.get_agent_stats()
    feedback = await s.get_all_feedback(limit=10) if hasattr(s, 'get_all_feedback') else []
    low_skills = await s.get_low_effectiveness_skills() if hasattr(s, 'get_low_effectiveness_skills') else []
    check_compliance = await s.get_check_compliance() if hasattr(s, 'get_check_compliance') else []
    return templates.TemplateResponse(request, "insights.html", {
        "insights": fleet_insights,
        "agent_stats": agent_stats,
        "recent_feedback": feedback,
        "low_skills": low_skills,
        "check_compliance": check_compliance,
    })


@app.get("/decisions", response_class=HTMLResponse)
async def decisions_page(request: Request, decision_type: str = "", attribution: str = "") -> HTMLResponse:
    """Audit every real LLM decision point, attributed by agent/skill.

    Answers "how is agent/skill X actually performing" — how often the LLM
    approves, rejects, or gates its output, and why — by merging the
    fix-review (skill_effectiveness) and auto-mode classify (events) decision
    records into one view. See agentit/llm_decisions.py for what's covered
    and what isn't (classify_secret has no persisted decision to show yet).
    """
    from agentit.llm_decisions import list_llm_decisions, summarize_by_attribution

    s = await get_store()
    all_decisions = await asyncio.to_thread(list_llm_decisions, s.raw if hasattr(s, "raw") else s, 500)
    decision_types = sorted({d["decision_type"] for d in all_decisions})
    attributions = sorted({d["attribution"] for d in all_decisions})

    decisions = all_decisions
    if decision_type:
        decisions = [d for d in decisions if d["decision_type"] == decision_type]
    if attribution:
        decisions = [d for d in decisions if d["attribution"] == attribution]
    summary = summarize_by_attribution(decisions)

    return templates.TemplateResponse(request, "decisions.html", {
        "decisions": decisions[:100],
        "summary": summary,
        "decision_type_filter": decision_type,
        "attribution_filter": attribution,
        "decision_types": decision_types,
        "attributions": attributions,
        "total_decisions": len(all_decisions),
    })


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, page: int = 1, per_page: int = 25,
                      q: str = "", severity: str = "", correlation_id: str = "") -> HTMLResponse:
    s = await get_store()
    if correlation_id and hasattr(s, "list_events_by_correlation_id"):
        all_events = await s.list_events_by_correlation_id(correlation_id, limit=2000)
    else:
        all_events = await s.list_events(limit=2000)
    if q:
        ql = q.lower()
        all_events = [e for e in all_events
                      if ql in e.get("agent_id", "").lower()
                      or ql in e.get("action", "").lower()
                      or ql in (e.get("target_app") or "").lower()
                      or ql in e.get("summary", "").lower()
                      or ql in (e.get("correlation_id") or "").lower()]
    if severity:
        all_events = [e for e in all_events if e.get("severity") == severity]
    total = len(all_events)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    events = all_events[start:start + per_page]
    return templates.TemplateResponse(request, "events.html", {
        "events": events, "page": page, "total_pages": total_pages,
        "per_page": per_page, "q": q, "severity_filter": severity,
        "correlation_id_filter": correlation_id,
    })


@app.get("/events/dlq", response_class=HTMLResponse)
async def dlq_page(request: Request) -> HTMLResponse:
    """Show dead-lettered messages from the events store."""
    s = await get_store()
    dlq_messages = await s.list_dlq_messages()
    return templates.TemplateResponse(request, "dlq.html", {"dlq_messages": dlq_messages})


@app.post("/events/dlq/{event_id}/retry")
async def dlq_retry(event_id: str):
    s = await get_store()
    if not await s.retry_dlq_message(event_id):
        return RedirectResponse(url="/events/dlq?error=Message+not+found+or+already+processed", status_code=303)
    return RedirectResponse(url="/events/dlq?success=Message+queued+for+retry", status_code=303)


@app.post("/events/dlq/{event_id}/dismiss")
async def dlq_dismiss(event_id: str):
    s = await get_store()
    if not await s.dismiss_dlq_message(event_id):
        return RedirectResponse(url="/events/dlq?error=Message+not+found+or+already+processed", status_code=303)
    return RedirectResponse(url="/events/dlq?success=Message+dismissed", status_code=303)


@app.post("/events/dlq/dismiss-all")
async def dlq_dismiss_all():
    s = await get_store()
    count = await s.dismiss_all_dlq()
    return RedirectResponse(url=f"/events/dlq?success=Dismissed+{count}+messages", status_code=303)


@app.get("/api/events")
async def api_events(limit: int = 50, target_app: str | None = None):
    s = await get_store()
    return JSONResponse(await s.list_events(limit=limit, target_app=target_app))


@app.get("/assess")
async def assess_form():
    """Redirect to fleet with modal open — single entry point for assessment."""
    return RedirectResponse(url="/?assess=1", status_code=303)


def _clone_assess_cleanup(
    repo_url: str,
    criticality: str,
    infra_repo_url: str | None = None,
    check_results_out: list[dict] | None = None,
):
    repo_path = clone_repo(repo_url)
    try:
        return run_assessment(
            repo_path, repo_url, criticality,
            llm_client=_get_llm_client(), infra_repo_url=infra_repo_url,
            check_results_out=check_results_out,
        )
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


def _assess_sync(
    repo_url: str,
    criticality: str,
    infra_repo_url: str | None = None,
    check_results_out: list[dict] | None = None,
):
    """Run assessment synchronously. Used by webhooks and background threads."""
    infra = infra_repo_url
    if not infra:
        infra = _auto_create_infra_repo(repo_url)
    return _clone_assess_cleanup(repo_url, criticality, infra, check_results_out=check_results_out)


@app.post("/assess", response_model=None)
async def assess_submit(
    request: Request,
    repo_url: str = Form(...),
    criticality: str = Form("medium"),
    infra_repo_url: str = Form(""),
):
    infra = infra_repo_url.strip() or None
    s = await get_store()
    job_id = await s.create_assessment_job(repo_url)
    # The work below runs in a background thread (long clone+assess pipeline),
    # so it needs a synchronous store handle -- see helpers.run_onboarding's
    # docstring / docs/postgres-migration-plan.md for why this is the
    # established pattern for background-thread store access rather than
    # awaiting the async facade from inside a non-async thread.
    raw = s.raw if hasattr(s, "raw") else None
    if raw is None:
        raise HTTPException(500, "Background assessment jobs require the sqlite backend's synchronous handle "
                                  "(store_pg.AssessmentStore has none yet) -- see docs/postgres-migration-plan.md")

    import threading

    def _run():
        try:
            raw.update_assessment_job(job_id, "cloning", "Cloning repository...")
            raw.update_assessment_job(job_id, "assessing", "Analyzing repository...")
            check_results: list[dict] = []
            report = _assess_sync(repo_url, criticality, infra, check_results_out=check_results)
            raw.update_assessment_job(job_id, "saving", "Saving results...")
            from agentit.portal.metrics import assessments_total as _at
            _at.labels(criticality=criticality, status="success").inc()
            assessment_id = raw.save(report)
            raw.save_check_results(assessment_id, check_results)
            # Publish event on first assessment for this repo
            history = raw.list_history(report.repo_url)
            if len(history) <= 1:
                _publish_event(
                    'first-assessment', report.repo_name,
                    f'First assessment — consider running: agentit learn-for {report.repo_url}',
                    {'assessment_id': assessment_id, 'score': report.overall_score},
                    correlation_id=assessment_id,
                )
            raw.update_assessment_job(job_id, "completed", "Assessment complete", assessment_id=assessment_id)
        except Exception as exc:
            log.exception("Assessment failed for %s", repo_url)
            from agentit.portal.metrics import assessments_total as _at
            _at.labels(criticality=criticality, status="error").inc()
            msg = str(exc)
            if "clone" in msg.lower() or "git" in msg.lower():
                msg = f"Could not clone repository. Check the URL and permissions. ({msg[:100]})"
            elif "GITHUB_TOKEN" in msg:
                msg = "GitHub integration is not configured. Contact your administrator."
            raw.update_assessment_job(job_id, "failed", msg[:200])

    threading.Thread(target=_run, daemon=True).start()
    return RedirectResponse(url=f"/assess/progress/{job_id}", status_code=303)


@app.get("/assess/progress/{job_id}", response_class=HTMLResponse)
async def assess_progress(request: Request, job_id: str):
    s = await get_store()
    job = await s.get_remediation_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    if job["status"] == "completed" and job.get("assessment_id"):
        return RedirectResponse(url=f"/assessments/{job['assessment_id']}", status_code=303)

    return templates.TemplateResponse(request, "assess_progress.html", {
        "job": job, "job_id": job_id,
    })


def _auto_create_infra_repo(repo_url: str) -> str | None:
    """Auto-create a GitOps infra repo based on the app repo owner."""
    try:
        from agentit.portal.github_pr import _parse_owner_repo, ensure_infra_repo
        owner, _ = _parse_owner_repo(repo_url)
        result = ensure_infra_repo(owner)
        if "repo_url" in result:
            log.info("Infra repo: %s (created=%s)", result["repo_url"], result.get("created", False))
            return result["repo_url"]
        log.warning("Failed to create infra repo: %s", result.get("error"))
    except Exception as exc:
        log.warning("Auto-create infra repo failed: %s", exc)
    return None


@app.post("/self-assess", response_model=None)
async def self_assess_route(request: Request):
    """One-click self-assessment -- AgentIT assesses its own repo."""
    repo_url = "https://github.com/alimobrem/AgentIT"
    infra = await asyncio.to_thread(_auto_create_infra_repo, repo_url)
    check_results: list[dict] = []
    try:
        report = await _with_timeout(
            asyncio.to_thread(
                _clone_assess_cleanup, repo_url, "high", infra, check_results_out=check_results,
            )
        )
    except Exception as exc:
        log.exception("Self-assessment failed")
        return RedirectResponse(url=f"/?error={quote(str(exc)[:200])}", status_code=303)
    s = await get_store()
    assessment_id = await s.save(report)
    await s.save_check_results(assessment_id, check_results)
    await s.log_event("self-assess", "assessment-complete", "agentit", "info",
                       f"Self-assessment complete: {report.overall_score:.0f}/100")
    from agentit.events import TOPIC_ASSESSMENTS as _TOPIC_ASSESS
    _publish_event("assessment-complete", "agentit",
                   f"Self-assessment: {report.overall_score:.0f}/100",
                   {"assessment_id": assessment_id, "score": report.overall_score},
                   correlation_id=assessment_id,
                   extra_topic=_TOPIC_ASSESS)
    return RedirectResponse(url=f"/assessments/{assessment_id}", status_code=303)


@app.get("/assessments/{assessment_id}", response_class=HTMLResponse)
async def assessment_detail(request: Request, assessment_id: str) -> HTMLResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    scores_sorted = sorted(report.scores, key=lambda sc: sc.score)
    urgent_findings = [
        f
        for sc in report.scores
        for f in sc.findings
        if f.severity in (Severity.critical, Severity.high)
    ]

    remediations = await s.list_remediations(assessment_id)
    slos = await s.list_slos(assessment_id)
    onboardings = await s.list_onboardings(assessment_id)

    from agentit.remediation.registry import lookup
    fixable_categories = {f.category for f in urgent_findings if lookup(f.category) is not None}

    timeline = await s.get_assessment_timeline(assessment_id) if hasattr(s, 'get_assessment_timeline') else []
    trend = await s.get_trend(report.repo_url) if hasattr(s, 'get_trend') else {}
    score_history = await s.get_score_history(report.repo_url) if hasattr(s, 'get_score_history') else []
    for i, h in enumerate(score_history):
        h["delta"] = round(h["overall_score"] - score_history[i - 1]["overall_score"], 2) if i > 0 else None
    apply_results = await s.get_apply_results(assessment_id)

    app_name = report.repo_name
    schedules_exist = await s.has_schedules_for_app(app_name) if hasattr(s, 'has_schedules_for_app') else False
    if apply_results and apply_results.get("applied") and (slos or schedules_exist):
        lifecycle_stage = "monitored"
    elif apply_results and apply_results.get("applied"):
        lifecycle_stage = "applied"
    elif onboardings:
        lifecycle_stage = "onboarded"
    else:
        lifecycle_stage = "assessed"

    suppressions = await s.get_suppressions(report.repo_name)

    return templates.TemplateResponse(
        request,
        "assessment_detail.html",
        {
            "report": report,
            "scores_sorted": scores_sorted,
            "urgent_findings": urgent_findings,
            "assessment_id": assessment_id,
            "remediation_count": len(remediations),
            "slo_count": len(slos),
            "onboarding_count": len(onboardings),
            "fixable_categories": fixable_categories,
            "timeline": timeline,
            "trend": trend,
            "score_history": score_history,
            "lifecycle_stage": lifecycle_stage,
            "suppressions": suppressions,
        },
    )


@app.post("/assessments/{assessment_id}/fix")
async def fix_finding(request: Request, assessment_id: str):
    """Dispatch a single finding fix via the generic remediation dispatcher."""
    form = await request.form()
    category = str(form.get("category", ""))
    description = str(form.get("description", ""))

    if not category:
        raise HTTPException(400, "category required")

    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(404, "Assessment not found")

    # RemediationDispatcher is deliberately still fully synchronous (see
    # docs/postgres-migration-plan.md's Phase 3 progress notes), so it needs
    # the raw sync store handle, run off the event loop via to_thread.
    raw = s.raw if hasattr(s, "raw") else None
    if raw is None:
        raise HTTPException(500, "Fix dispatch requires the sqlite backend's synchronous handle "
                                  "(store_pg.AssessmentStore has none yet) -- see docs/postgres-migration-plan.md")

    from agentit.remediation.dispatcher import RemediationDispatcher
    dispatcher = RemediationDispatcher(raw)
    result = await asyncio.to_thread(dispatcher.dispatch, assessment_id, category, report.repo_name)

    from agentit.portal.metrics import remediations_total as _rt
    _status = "success" if result["files"] else ("error" if result.get("error") else "empty")
    _rt.labels(agent=result.get("agent", "unknown"), status=_status).inc()

    if result.get("error") and not result["files"]:
        return RedirectResponse(
            url=f"/assessments/{assessment_id}?error={quote(result['error'])}",
            status_code=303,
        )

    if result["files"]:
        from agentit.portal.cluster_apply import apply_manifests_to_cluster
        namespace = report.repo_name.lower().replace("_", "-").replace(".", "-")
        apply_result = await asyncio.to_thread(
            apply_manifests_to_cluster, result["files"], namespace, dry_run=True,
        )
        await s.save_apply_results(assessment_id, apply_result, namespace, dry_run=True)

        for f in result["files"]:
            await s.save_remediation(
                assessment_id, result["agent"], f["description"],
                status="generated", manifest_path=f["path"],
            )
        await s.log_event(
            "dispatcher", "fix-generated", report.repo_name, "info",
            f"Generated {len(result['files'])} fix(es) for '{category}' via {result['agent']}",
        )
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?fix_generated={len(result['files'])}&agent={result['agent']}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/assessments/{assessment_id}?error={quote('No fix generated for this finding')}",
        status_code=303,
    )


@app.get("/api/assessments")
async def api_list() -> JSONResponse:
    s = await get_store()
    return JSONResponse(await s.list_all())


@app.get("/api/assessments/{assessment_id}")
async def api_detail(assessment_id: str) -> JSONResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return JSONResponse(report.model_dump(mode="json"))


@app.post("/assessments/{assessment_id}/delete", response_model=None)
async def delete_assessment(assessment_id: str):
    s = await get_store()
    if not await s.delete(assessment_id):
        raise HTTPException(404, "Assessment not found")
    await s.log_event("portal", "assessment-deleted", None, "info", f"Deleted assessment {assessment_id}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/assessments/{assessment_id}/slos/{slo_id}/delete", response_model=None)
async def delete_slo(assessment_id: str, slo_id: str):
    s = await get_store()
    await s.delete_slo(slo_id, assessment_id)
    return RedirectResponse(url=f"/assessments/{assessment_id}/slos", status_code=303)


@app.post("/assessments/{assessment_id}/remediations/{rem_id}/delete", response_model=None)
async def delete_remediation(assessment_id: str, rem_id: str):
    s = await get_store()
    await s.delete_remediation(rem_id, assessment_id)
    return RedirectResponse(url=f"/assessments/{assessment_id}/remediations", status_code=303)


@app.post("/gates/{gate_id}/cancel", response_model=None)
async def cancel_gate(request: Request, gate_id: str):
    s = await get_store()
    await s.resolve_gate(gate_id, "cancelled", get_current_user(request))
    return RedirectResponse(url="/gates?success=Gate+dismissed", status_code=303)


def _run_onboarding(
    report: AssessmentReport, assessment_id: str | None = None, raw_store: object | None = None,
) -> tuple[list[dict], dict]:
    """Run orchestrated onboarding. Returns (files, orchestration_summary).

    Delegates to the shared implementation in helpers.py so this route and
    the webhook-triggered path (routes/webhooks.py) can never drift apart on
    which summary fields get stored (e.g. auto_approve/gates).

    Runs inside a worker thread via ``asyncio.to_thread`` (see the caller
    below) -- ``raw_store`` must therefore already be the *synchronous*
    store handle (``FleetOrchestrator`` is deliberately still fully
    synchronous; see docs/postgres-migration-plan.md's Phase 3 notes), not
    the async facade `get_store()` now returns.
    """
    from agentit.portal.helpers import run_onboarding as _shared_run_onboarding
    return _shared_run_onboarding(report, assessment_id, store=raw_store)


@app.get("/assessments/{assessment_id}/onboarding-history", response_class=HTMLResponse)
async def onboarding_history(request: Request, assessment_id: str) -> HTMLResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    onboardings = await s.list_onboardings(assessment_id)
    pr_urls = [ob["pr_url"] for ob in onboardings if ob.get("pr_url")]
    if pr_urls:
        from agentit.portal.github_pr import get_pr_status
        statuses = await asyncio.gather(
            *(asyncio.to_thread(get_pr_status, url) for url in pr_urls)
        )
        status_map = dict(zip(pr_urls, statuses))
        for ob in onboardings:
            if ob.get("pr_url"):
                ob["pr_status"] = status_map.get(ob["pr_url"], {})
    return templates.TemplateResponse(request, "onboarding_history.html", {
        "report": report,
        "onboardings": onboardings,
        "assessment_id": assessment_id,
    })


@app.post("/assessments/{assessment_id}/onboard", response_model=None)
async def onboard_submit(request: Request, assessment_id: str):
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    raw = s.raw if hasattr(s, "raw") else None
    if raw is None:
        raise HTTPException(500, "Onboarding requires the sqlite backend's synchronous handle "
                                  "(store_pg.AssessmentStore has none yet) -- see docs/postgres-migration-plan.md")
    try:
        files, orch_summary = await _with_timeout(asyncio.to_thread(_run_onboarding, report, assessment_id, raw))
    except HTTPException:
        raise
    except Exception:
        log.exception("Onboarding failed for %s", assessment_id)
        from agentit.portal.metrics import onboardings_total as _ot
        _ot.labels(status="error").inc()
        return RedirectResponse(
            url=f"/assessments/{assessment_id}?error={quote('Onboarding failed — check server logs')}",
            status_code=303,
        )
    from agentit.portal.metrics import onboardings_total as _ot
    _ot.labels(status="success").inc()
    await s.save_onboarding(assessment_id, files, orchestration=orch_summary)

    _publish_event("onboarding-complete", report.repo_name,
                   f"Generated {len(files)} manifests",
                   {"assessment_id": assessment_id, "file_count": len(files)},
                   correlation_id=assessment_id, agent_id="onboarding")

    # Trigger image build only if a Containerfile was generated
    warnings = []
    has_containerfile = any(
        f["path"].lower() in ("containerfile", "dockerfile") for f in files
    )
    if has_containerfile:
        from agentit.image_builder import build_app_image
        build_result = await asyncio.to_thread(build_app_image, report.repo_url, report.repo_name)
        if "error" in build_result:
            log.warning("Image build trigger failed for %s: %s", report.repo_name, build_result["error"])
            await s.log_event("image-builder", "build-failed", report.repo_name, "warning",
                               f"Image build failed: {build_result['error'][:200]}")
            warnings.append(f"Image build failed: {build_result['error'][:100]}")
        else:
            log.info("Image build triggered: %s → %s", report.repo_name, build_result.get("image_ref"))
            await s.log_event("image-builder", "build-triggered", report.repo_name, "info",
                               f"Building image: {build_result.get('image_ref')}")

    from agentit.portal.github_pr import ensure_webhook
    webhook_url = _get_trusted_base_url(request) + "/api/webhook/github-push"
    hook_result = await asyncio.to_thread(ensure_webhook, report.repo_url, webhook_url)
    if "error" in hook_result:
        log.warning("Webhook registration failed for %s: %s", report.repo_name, hook_result["error"])
        warnings.append(f"Auto-reassessment webhook not registered: {hook_result['error'][:100]}")
    elif hook_result.get("created"):
        await s.log_event("portal", "webhook-registered", report.repo_name,
                           "info", "GitHub push webhook registered for auto-reassessment")

    redirect_url = f"/assessments/{assessment_id}/onboard-results"
    if warnings:
        redirect_url += f"?warning={quote('|'.join(warnings))}"
    return RedirectResponse(url=redirect_url, status_code=303)


@app.get("/assessments/{assessment_id}/onboard-results", response_class=HTMLResponse)
async def onboard_results(request: Request, assessment_id: str) -> HTMLResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    grouped: dict[str, list[dict]] = {}
    for f in files:
        grouped.setdefault(f["category"], []).append(f)

    orchestration = await s.get_orchestration(assessment_id) or {}
    apply_results = await s.get_apply_results(assessment_id)

    missing_operators = {}
    if apply_results:
        from agentit.portal.cluster_apply import _CRD_TO_OPERATOR
        for skip_reason in apply_results.get("skipped", []):
            if "CRD not installed" in skip_reason:
                for kind, op in _CRD_TO_OPERATOR.items():
                    if kind in skip_reason:
                        missing_operators[kind] = op
        for err in apply_results.get("errors", []):
            if "resource mapping not found" in err.lower():
                for kind, op in _CRD_TO_OPERATOR.items():
                    if kind.lower() in err.lower():
                        missing_operators[kind] = op

    pr_status = None
    onboardings = await s.list_onboardings(assessment_id)
    pr_url = onboardings[0]["pr_url"] if onboardings and onboardings[0]["pr_url"] else ""
    if pr_url:
        from agentit.portal.github_pr import get_pr_status
        pr_status = await asyncio.to_thread(get_pr_status, pr_url)

    return templates.TemplateResponse(
        request,
        "onboard_results.html",
        {
            "report": report,
            "grouped": grouped,
            "assessment_id": assessment_id,
            "orchestration": orchestration,
            "apply_results": apply_results,
            "missing_operators": missing_operators,
            "pr_status": pr_status,
        },
    )


@app.get("/api/assessments/{assessment_id}/manifests")
async def api_manifests(assessment_id: str) -> JSONResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")
    return JSONResponse(files)


@app.get("/api/assessments/{assessment_id}/manifests/download")
async def download_manifests(assessment_id: str):
    """Download all onboarding manifests as a zip file."""
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            arcname = f"{f['category']}/{f['path']}"
            zf.writestr(arcname, f["content"])
    buf.seek(0)

    filename = f"{report.repo_name}-onboarding.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/assessments/{assessment_id}/apply", response_model=None)
async def apply_to_cluster(request: Request, assessment_id: str):
    """Apply onboarding manifests to the current cluster."""
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    form = await request.form()
    namespace = str(form.get("namespace", "default"))
    dry_run = form.get("dry_run") == "true"

    try:
        results = await asyncio.to_thread(
            apply_manifests_to_cluster, files, namespace, dry_run,
        )
    except Exception:
        log.exception("Cluster apply failed for assessment %s", assessment_id)
        audit_log(actor="portal-user", action="apply-to-cluster", resource=f"assessment:{assessment_id}",
                  outcome="error", details={"namespace": namespace, "dry_run": dry_run})
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote('Cluster apply failed — check server logs')}",
            status_code=303,
        )

    await s.save_apply_results(assessment_id, results, namespace, dry_run)

    applied = len(results["applied"])
    skipped = len(results["skipped"])
    errs = len(results["errors"])
    audit_log(actor="portal-user", action="apply-to-cluster", resource=f"assessment:{assessment_id}",
              outcome="success" if not results["errors"] else "partial",
              details={"namespace": namespace, "dry_run": dry_run, "applied": applied, "errors": errs})
    return RedirectResponse(
        url=(
            f"/assessments/{assessment_id}/onboard-results"
            f"?applied={applied}&skipped={skipped}&errors={errs}"
            f"&dry_run={'true' if dry_run else 'false'}"
        ),
        status_code=303,
    )


@app.post("/api/install-operator", response_model=None)
async def install_operator_endpoint(request: Request):
    """Install an OLM operator. Called from the missing prerequisites UI."""
    form = await request.form()
    package = str(form.get("package", ""))
    channel = str(form.get("channel", "stable"))
    source = str(form.get("source", "redhat-operators"))
    assessment_id = str(form.get("assessment_id", ""))

    if not package:
        raise HTTPException(400, "package required")

    result = await asyncio.to_thread(install_operator, package, channel, source)

    if assessment_id:
        error_param = f"&install_error={quote(result['error'][:400])}" if result.get("error") else ""
        return RedirectResponse(
            url=(
                f"/assessments/{assessment_id}/onboard-results"
                f"?operator_installed={package}&install_status={result['status']}{error_param}"
            ),
            status_code=303,
        )
    return JSONResponse(result)


@app.get("/gates", response_class=HTMLResponse)
async def gates_page(request: Request):
    """Show pending approval gates. Auto-expires gates older than 24h."""
    s = await get_store()
    expired_count = await s.expire_stale_gates(hours=24)
    if expired_count:
        await s.log_event("portal", "gates-expired", None, "info",
                           f"Auto-expired {expired_count} stale gate(s)")

    all_gates = await s.list_all_gates()
    pending = [g for g in all_gates if g["status"] == "pending"]
    stale = await s.get_stale_gates(hours=4)
    stale_ids = {g["id"] for g in stale}
    for g in pending:
        g["stale"] = g["id"] in stale_ids
    resolved = [g for g in all_gates if g["status"] in ("approved", "rejected", "expired")]
    resolved.sort(key=lambda g: g.get("resolved_at") or g.get("created_at", ""), reverse=True)
    return templates.TemplateResponse(request, "gates.html", {
        "pending": pending, "resolved": resolved[:20],
        "stale_count": len(stale), "expired_count": expired_count,
    })


@app.post("/gates/{gate_id}/resolve", response_model=None)
async def resolve_gate(request: Request, gate_id: str):
    form = await request.form()
    status = form.get("status")
    if status not in ("approved", "rejected", "dismissed"):
        raise HTTPException(400, "Invalid status: must be approved, rejected, or dismissed")
    resolved_by = form.get("resolved_by") or get_current_user(request)
    s = await get_store()

    gates = await s.list_gates(status="pending")
    gate = next((g for g in gates if g["id"] == gate_id), None)
    if gate is None:
        raise HTTPException(404, "Gate not found")

    audit_log(actor=str(resolved_by), action=f"gate-{status}", resource=f"gate:{gate_id}",
              details={"gate_type": gate.get("gate_type"), "assessment_id": gate.get("assessment_id")})

    if status == "approved" and gate.get("assessment_id"):
        assessment_id = gate["assessment_id"]

        if gate.get("gate_type") == "rollback-review":
            await s.resolve_gate(gate_id, status, resolved_by)
            await s.log_event(
                "gate-resolver", "rollback-approved",
                gate.get("target_app"), "warning",
                f"Rollback approved for assessment {assessment_id} — manual intervention required",
            )
            return RedirectResponse(
                url=f"/assessments/{assessment_id}?success=Rollback+approved.+Review+the+deployment+and+roll+back+manually+or+via+Argo+Rollouts.",
                status_code=303,
            )

        files = await s.get_onboarding(assessment_id)
        report = await s.get(assessment_id)
        if files and report:
            namespace = report.repo_name.lower().replace("_", "-").replace(".", "-")
            try:
                results = await asyncio.to_thread(
                    apply_manifests_to_cluster, files, namespace, False,
                )
            except Exception:
                log.exception("Manifest apply failed for gate %s (assessment %s)", gate_id, assessment_id)
                return RedirectResponse(
                    url=f"/assessments/{assessment_id}/onboard-results?error={quote('Manifest apply failed — gate remains pending')}",
                    status_code=303,
                )
            await s.resolve_gate(gate_id, status, resolved_by)
            await s.save_apply_results(assessment_id, results, namespace, False)
            applied = len(results["applied"])
            return RedirectResponse(
                url=f"/assessments/{assessment_id}/onboard-results?applied={applied}&gate_approved=true",
                status_code=303,
            )

    await s.resolve_gate(gate_id, status, resolved_by)

    if status == "rejected":
        await s.record_feedback(
            app_name=gate.get("target_app", ""),
            agent_name=gate.get("agent_name", "gate"),
            finding_category=gate.get("gate_type", ""),
            action="rejected",
            human_reason=str(form.get("reason", "")),
        )

    return RedirectResponse(url="/gates", status_code=303)


@app.post("/api/feedback")
async def record_feedback_endpoint(request: Request):
    """Record human feedback on agent recommendations."""
    form = await request.form()
    s = await get_store()
    fid = await s.record_feedback(
        app_name=str(form.get("app_name", "")),
        agent_name=str(form.get("agent_name", "")),
        finding_category=str(form.get("finding_category", "")),
        action=str(form.get("action", "")),
        human_reason=str(form.get("reason", "")),
        original_value=str(form.get("original_value", "")),
        human_value=str(form.get("human_value", "")),
    )
    return {"status": "recorded", "feedback_id": fid}


@app.post("/api/suppress")
async def suppress_check_endpoint(request: Request):
    """Suppress a check for a specific app — it won't fire on future assessments."""
    form = await request.form()
    app_name = str(form.get("app_name", ""))
    check_source = str(form.get("check_source", ""))
    reason = str(form.get("reason", ""))
    assessment_id = str(form.get("assessment_id", ""))
    if not app_name or not check_source:
        raise HTTPException(status_code=400, detail="app_name and check_source required")
    s = await get_store()
    await s.suppress_check(app_name, check_source, reason)
    if assessment_id:
        return RedirectResponse(f"/assessments/{assessment_id}", status_code=303)
    return {"status": "suppressed", "app_name": app_name, "check_source": check_source}


@app.post("/api/unsuppress")
async def unsuppress_check_endpoint(request: Request):
    """Remove a suppression for a check on a specific app."""
    form = await request.form()
    app_name = str(form.get("app_name", ""))
    check_source = str(form.get("check_source", ""))
    assessment_id = str(form.get("assessment_id", ""))
    if not app_name or not check_source:
        raise HTTPException(status_code=400, detail="app_name and check_source required")
    s = await get_store()
    await s.unsuppress_check(app_name, check_source)
    if assessment_id:
        return RedirectResponse(f"/assessments/{assessment_id}", status_code=303)
    return {"status": "unsuppressed", "app_name": app_name, "check_source": check_source}


@app.get("/api/gates")
async def api_gates(status: str = "pending"):
    s = await get_store()
    return JSONResponse(await s.list_gates(status=status))


@app.post("/assessments/{assessment_id}/create-pr", response_model=None)
async def create_pr(assessment_id: str):
    """Commit manifests to GitOps infra repo (or app repo as fallback)."""
    from agentit.portal.github_pr import commit_to_infra_repo, ensure_applicationset

    s = await get_store()
    report = await s.get(assessment_id)
    files = await s.get_onboarding(assessment_id)
    if report is None or files is None:
        raise HTTPException(status_code=404, detail="Assessment or onboarding not found")

    try:
        if report.infra_repo_url:
            result = await _with_timeout(asyncio.to_thread(
                commit_to_infra_repo, report.infra_repo_url, report.repo_name, files,
            ))
            await asyncio.to_thread(ensure_applicationset, report.infra_repo_url)
        else:
            result = await _with_timeout(asyncio.to_thread(
                create_onboarding_pr, report.repo_url, report.repo_name, files,
            ))
    except Exception as exc:
        log.exception("PR creation failed for %s", report.repo_name)
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote(str(exc)[:200])}",
            status_code=303,
        )

    if "error" in result:
        log.warning("PR creation error for %s: %s", report.repo_name, result["error"])
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote(result['error'][:200])}",
            status_code=303,
        )
    await s.update_pr_url(assessment_id, result["pr_url"])
    await s.log_event("portal", "pr-created", report.repo_name,
                       "info", f"PR created: {result['pr_url']}")
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard-results?pr_url={result['pr_url']}",
        status_code=303,
    )


@app.post("/assessments/{assessment_id}/create-agent-prs", response_model=None)
async def create_agent_prs_route(assessment_id: str):
    """Create per-agent branches and PRs."""
    from agentit.portal.github_pr import create_agent_prs

    s = await get_store()
    report = await s.get(assessment_id)
    files = await s.get_onboarding(assessment_id)
    if report is None or files is None:
        raise HTTPException(status_code=404, detail="Assessment or onboarding not found")

    grouped: dict[str, list[dict]] = {}
    for f in files:
        grouped.setdefault(f["category"], []).append(f)

    agent_results = [
        {"agent_name": cat, "category": cat, "files": cat_files}
        for cat, cat_files in grouped.items()
    ]

    results = await asyncio.to_thread(
        create_agent_prs, report.repo_url, report.repo_name, agent_results,
    )

    successful = [r for r in results if "pr_url" in r]
    errors = [r for r in results if "error" in r]

    if successful:
        pr_list = ", ".join(f"{r['agent_name']}" for r in successful)
        all_pr_urls = " | ".join(r["pr_url"] for r in successful)
        await s.update_pr_url(assessment_id, all_pr_urls)
        await s.log_event("orchestrator", "agent-prs-created", report.repo_name,
                           "info", f"Created {len(successful)} per-agent PRs: {pr_list}")

    if errors and not successful:
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote(errors[0].get('error', 'Unknown')[:200])}",
            status_code=303,
        )

    pr_urls = "|".join(f"{r['agent_name']}={r['pr_url']}" for r in successful)
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard-results?agent_prs={quote(pr_urls)}",
        status_code=303,
    )


# ── Agents ────────────────────────────────────────────────────────────


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request) -> HTMLResponse:
    s = await get_store()
    agents = await s.list_agents()

    for a in agents:
        if not a.get("capabilities") or a["capabilities"] in ("[]", ""):
            a["capabilities"] = AGENT_CAPABILITIES.get(a["agent_name"], "")

    # Merge long-lived watcher agents that aren't in the registry
    registered_names = {a["agent_name"] for a in agents}
    for w in _WATCHER_AGENTS:
        if w["name"] not in registered_names:
            agents.append({
                "agent_name": w["name"],
                "category": w["mode"],
                "status": "deployed",
                "capabilities": AGENT_CAPABILITIES.get(w["name"], f"interval: {w['interval']}"),
                "registered_at": "—",
                "last_heartbeat": "—",
            })

    agent_stats = {a["agent"]: a for a in (await s.get_agent_stats())} if hasattr(s, 'get_agent_stats') else {}

    active = sum(1 for a in agents if a["status"] == "active")
    last_hb = max((a["last_heartbeat"] or "" for a in agents), default="—")
    return templates.TemplateResponse(request, "agents.html", {
        "agents": agents,
        "total": len(agents),
        "active": active,
        "last_heartbeat": last_hb[:19] if last_hb != "—" else "—",
        "agent_stats": agent_stats,
    })


@app.get("/agents/{agent_name}", response_class=HTMLResponse)
async def agent_detail(request: Request, agent_name: str) -> HTMLResponse:
    s = await get_store()
    agents = await s.list_agents()
    agent = next((a for a in agents if a["agent_name"] == agent_name), None)

    # Long-lived agents may not be in the registry -- create a synthetic entry
    if agent is None:
        watcher = next((w for w in _WATCHER_AGENTS if w["name"] == agent_name), None)
        if watcher is not None:
            agent = {
                "agent_name": agent_name,
                "category": watcher["mode"],
                "status": "deployed",
                "capabilities": f"interval: {watcher['interval']}",
                "registered_at": "—",
                "last_heartbeat": "—",
            }
        else:
            raise HTTPException(status_code=404, detail="Agent not found")

    events = await s.list_events_by_agent(agent_name, limit=50)
    remediations = await s.list_remediations_by_agent(agent_name)
    pending = sum(1 for r in remediations if r["status"] not in ("completed", "applied"))
    completed = sum(1 for r in remediations if r["status"] in ("completed", "applied"))
    agent_runs = await s.list_agent_runs(agent_name, limit=50) if hasattr(s, 'list_agent_runs') else []

    return templates.TemplateResponse(request, "agent_detail.html", {
        "agent": agent,
        "events": events,
        "remediations": remediations,
        "pending": pending,
        "completed": completed,
        "agent_runs": agent_runs,
    })


@app.get("/api/agents")
async def api_agents(status: str = "active"):
    s = await get_store()
    return JSONResponse(await s.list_agents(status=status))


# ── Workflows ─────────────────────────────────────────────────────────


@app.get("/workflows")
async def workflows_redirect():
    return RedirectResponse(url="/capabilities", status_code=301)


# ── Capabilities ─────────────────────────────────────────────────────


@app.get("/capabilities", response_class=HTMLResponse)
async def capabilities_page(request: Request) -> HTMLResponse:
    from agentit.remediation.registry import FIX_REGISTRY

    skills = _cached_skills()
    checks = _cached_checks()

    s = await get_store()
    effectiveness = await s.get_skill_effectiveness()
    recent_activity = await s.get_recent_skill_activity(limit=20)
    catalog_changes = await s.list_events_by_agent("skill-inventory", limit=10)

    # Group skills by domain
    skills_by_domain: dict[str, list] = {}
    for skill in skills:
        skills_by_domain.setdefault(skill.domain, []).append(skill)

    # Group checks by dimension
    checks_by_dimension: dict[str, list] = {}
    for check in checks:
        checks_by_dimension.setdefault(check.dimension, []).append(check)

    total_skills = len(skills)
    active_skills = sum(1 for sk in skills if sk.status == "active")
    deprecated_skills = sum(1 for sk in skills if sk.status == "deprecated")
    total_checks = len(checks)

    from agentit.agents.capabilities import get_onboarding_agents, WATCHER_AGENTS
    agents = get_onboarding_agents()
    watchers = WATCHER_AGENTS
    fix_categories = [
        {"category": cat, "agent": agent_name, "method": method.lstrip("_").replace("_", " ")}
        for cat, (agent_name, method) in sorted(FIX_REGISTRY.items())
    ]
    retention_days = get_retention_days()

    return templates.TemplateResponse(request, "capabilities.html", {
        "skills_by_domain": skills_by_domain,
        "checks_by_dimension": checks_by_dimension,
        "effectiveness": effectiveness,
        "recent_activity": recent_activity,
        "catalog_changes": catalog_changes,
        "total_skills": total_skills,
        "active_skills": active_skills,
        "deprecated_skills": deprecated_skills,
        "total_checks": total_checks,
        "agents": agents,
        "watchers": watchers,
        "fix_categories": fix_categories,
        "retention_days": retention_days,
    })


@app.post("/capabilities/learn", response_model=None)
async def capabilities_learn_route(request: Request):
    """Research CVEs via LLM and generate new skills.

    Portal entry point for what was previously only reachable via the CLI's
    ``agentit learn`` command — the research/skill-generation loop had no UI
    trigger at all before this.
    """
    llm_client = _get_llm_client()
    if llm_client is None:
        return RedirectResponse(
            url=f"/capabilities?error={quote('LLM unavailable — set ANTHROPIC_API_KEY or ANTHROPIC_VERTEX_PROJECT_ID to enable skill research.')}",
            status_code=303,
        )

    from agentit.learning_agent import (
        check_skill_exists,
        generate_skill_from_research,
        research_cves,
        save_skill,
    )

    def _run() -> tuple[list[str], list[str]]:
        saved: list[str] = []
        skipped: list[str] = []
        skills_dir = Path("skills")
        for item in research_cves(llm_client, limit=3):
            item_name = item.get("id") or item.get("title") or item.get("name", "")
            if item_name and check_skill_exists(skills_dir, item_name, "security"):
                skipped.append(item_name)
                continue
            content = generate_skill_from_research(llm_client, item, domain="security")
            if not content:
                continue
            path = save_skill(content, skills_dir, domain="security")
            if path:
                saved.append(path.stem)
        return saved, skipped

    try:
        saved, skipped = await _with_timeout(asyncio.to_thread(_run), timeout=180)
    except Exception as exc:
        log.exception("Skill research failed")
        return RedirectResponse(
            url=f"/capabilities?error={quote(f'Skill research failed: {exc}'[:200])}",
            status_code=303,
        )

    s = await get_store()
    if saved:
        _skills_cache["data"] = None  # bust the 60s cache so new skills show immediately
        await s.log_event("learning-agent", "skills-generated", None, "info",
                           f"Generated {len(saved)} new skill(s): {', '.join(saved)}")
        msg = f"Generated {len(saved)} new skill(s): {', '.join(saved)}"
        if skipped:
            msg += f" ({len(skipped)} already existed)"
    elif skipped:
        msg = f"No new skills — {len(skipped)} researched CVE(s) already have matching skills."
    else:
        msg = "No new skills generated — research returned nothing usable this time."
    return RedirectResponse(url=f"/capabilities?success={quote(msg)}", status_code=303)


@app.post("/capabilities/skills/activate", response_model=None)
async def activate_skill_route(request: Request):
    """Promote a draft skill to active. Portal equivalent of `agentit activate-skill`.

    Draft skills are only ever written by the learning agent (research
    button, skill-learner watcher, or CLI) — this is the human-review step
    that lets the skill engine actually start matching them.
    """
    form = await request.form()
    skill_path_raw = str(form.get("skill_path", ""))

    skills_root = Path("skills").resolve()
    try:
        target = Path(skill_path_raw).resolve()
        target.relative_to(skills_root)
    except (ValueError, OSError):
        return RedirectResponse(
            url=f"/capabilities?error={quote('Invalid skill path')}", status_code=303,
        )

    if not target.is_file():
        return RedirectResponse(url=f"/capabilities?error={quote('Skill file not found')}", status_code=303)

    content = target.read_text(encoding="utf-8")
    if "status: draft" not in content:
        return RedirectResponse(
            url=f"/capabilities?error={quote('Skill is not in draft status')}", status_code=303,
        )

    target.write_text(content.replace("status: draft", "status: active", 1), encoding="utf-8")
    _skills_cache["data"] = None
    s = await get_store()
    await s.log_event("portal", "skill-activated", None, "info", f"Activated skill: {target.stem}")
    return RedirectResponse(url=f"/capabilities?success={quote(f'Activated: {target.stem}')}", status_code=303)


# ── Remediations ──────────────────────────────────────────────────────


@app.get("/assessments/{assessment_id}/remediations", response_class=HTMLResponse)
async def remediations_page(request: Request, assessment_id: str) -> HTMLResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    remediations = await s.list_remediations(assessment_id)
    pending = sum(1 for r in remediations if r["status"] not in ("completed", "applied"))
    completed = sum(1 for r in remediations if r["status"] in ("completed", "applied"))
    return templates.TemplateResponse(request, "remediations.html", {
        "report": report,
        "remediations": remediations,
        "assessment_id": assessment_id,
        "total": len(remediations),
        "pending": pending,
        "completed": completed,
    })


@app.post("/assessments/{assessment_id}/remediations/{rem_id}/complete", response_model=None)
async def complete_remediation(assessment_id: str, rem_id: str):
    s = await get_store()
    remediations = await s.list_remediations(assessment_id)
    if not any(r["id"] == rem_id for r in remediations):
        raise HTTPException(status_code=404, detail="Remediation not found for this assessment")
    await s.complete_remediation(rem_id)
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/remediations", status_code=303,
    )


@app.post("/assessments/{assessment_id}/remediations/{rem_id}/status", response_model=None)
async def update_remediation_status(request: Request, assessment_id: str, rem_id: str):
    form = await request.form()
    status = str(form.get("status", ""))
    if status not in ("generated", "applied", "blocked", "completed"):
        raise HTTPException(400, "Invalid status")
    redirect = str(form.get("redirect", ""))
    s = await get_store()
    await s.update_remediation_status(rem_id, status)
    dest = f"/assessments/{assessment_id}/remediations"
    if redirect.startswith("/agents/"):
        dest = redirect
    return RedirectResponse(url=dest, status_code=303)


@app.get("/api/assessments/{assessment_id}/remediations")
async def api_remediations(assessment_id: str):
    s = await get_store()
    return JSONResponse(await s.list_remediations(assessment_id))


@app.get("/api/assessments/{assessment_id}/resource-recommendations")
async def resource_recommendations(assessment_id: str):
    """Get resource tuning recommendations based on Prometheus data."""
    from agentit.resource_tuner import analyze_resource_usage

    s = await get_store()
    report = await s.get(assessment_id)
    if not report:
        raise HTTPException(404, "Assessment not found")
    recs = analyze_resource_usage(report.repo_name, report.repo_name)
    return {
        "recommendations": [
            {
                "type": r.resource_type,
                "current": r.current_value,
                "recommended": r.recommended_value,
                "reason": r.reason,
                "confidence": r.confidence,
            }
            for r in recs
        ]
    }


@app.get("/api/assessments/{assessment_id}/dependencies")
async def dependency_status(assessment_id: str):
    """Get dependency update status from GitHub PRs."""
    from agentit.dependency_manager import process_dependency_prs

    s = await get_store()
    report = await s.get(assessment_id)
    if not report:
        raise HTTPException(404, "Assessment not found")
    updates = process_dependency_prs(report.repo_url)
    return {
        "updates": [
            {
                "name": u.name,
                "old": u.old_version,
                "new": u.new_version,
                "type": u.update_type,
                "risk": u.risk_level,
                "auto_mergeable": u.auto_mergeable,
                "pr_url": u.pr_url,
            }
            for u in updates
        ]
    }


# ── Property Verification ─────────────────────────────────────────────


@app.get("/api/assessments/{assessment_id}/verify")
async def verify_properties(assessment_id: str):
    """Verify enterprise properties hold against this assessment's generated manifests.

    NOTE: this is a standalone API endpoint, not (yet) wired into the
    automatic onboarding/apply path -- verification only runs when this
    endpoint is called explicitly.
    """
    s = await get_store()
    report = await s.get(assessment_id)
    if not report:
        raise HTTPException(404, "Assessment not found")
    files_data = await s.get_onboarding(assessment_id)
    if files_data is None:
        raise HTTPException(404, "Onboarding not found")

    from agentit.agents.base import GeneratedFile
    from agentit.property_verifier import verify_all_properties
    files = [
        GeneratedFile(
            path=f["path"],
            content=f["content"],
            description=f.get("description", f["path"]),
        )
        for f in files_data
    ]
    results = verify_all_properties(files)
    return {
        "app": report.repo_name,
        "results": [{"property": r.property_name, "passed": r.passed,
                     "checks": r.checks, "summary": r.summary()} for r in results],
        "all_passed": all(r.passed for r in results),
    }


# ── Platform Drift ────────────────────────────────────────────────────


@app.get("/api/platform/drift")
async def platform_drift():
    """Check for API drift on the cluster."""
    from agentit.platform_context import discover_platform, offline_context
    from agentit.api_drift_detector import detect_drift
    try:
        ctx = discover_platform()
    except Exception:
        ctx = offline_context()
    drift = detect_drift(ctx.available_kinds, ctx.installed_operators)
    return {
        "platform": ctx.summary(),
        "drift": {
            "removed_apis": drift.removed_apis,
            "deprecated_apis": [d.get("api", "") for d in drift.deprecated_apis] if hasattr(drift, 'deprecated_apis') and isinstance(drift.deprecated_apis, list) and drift.deprecated_apis and isinstance(drift.deprecated_apis[0], dict) else drift.deprecated_apis,
            "new_apis": drift.new_apis[:20],
            "has_breaking_changes": drift.has_breaking_changes,
        },
        "summary": drift.summary(),
    }


# ── SLOs ──────────────────────────────────────────────────────────────


@app.get("/assessments/{assessment_id}/slos", response_class=HTMLResponse)
async def slos_page(request: Request, assessment_id: str) -> HTMLResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    slos = await s.list_slos(assessment_id)
    met = sum(1 for sl in slos if sl["status"] == "met")
    breached = sum(1 for sl in slos if sl["status"] == "breached")
    return templates.TemplateResponse(request, "slos.html", {
        "report": report,
        "slos": slos,
        "assessment_id": assessment_id,
        "total": len(slos),
        "met": met,
        "breached": breached,
    })


@app.post("/assessments/{assessment_id}/slos/add", response_model=None)
async def add_slo(request: Request, assessment_id: str):
    s = await get_store()
    if await s.get(assessment_id) is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    form = await request.form()
    metric_name = str(form.get("metric_name", "")).strip()
    target_str = str(form.get("target_value", "")).strip()
    if not metric_name or not target_str:
        raise HTTPException(status_code=400, detail="metric_name and target_value required")
    try:
        target_value = float(target_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="target_value must be a number")
    await s.save_slo(assessment_id, metric_name, target_value)
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/slos", status_code=303,
    )


@app.get("/api/assessments/{assessment_id}/slos")
async def api_slos(assessment_id: str):
    s = await get_store()
    return JSONResponse(await s.list_slos(assessment_id))


# ── Settings + Auto-Mode ─────────────────────────────────────────────


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    s = get_store()
    auto_mode = s.get_setting("auto_mode") in ("true", "1", "on")
    llm_available = _get_llm_client() is not None
    recent_actions = s.list_events_by_agent("auto-mode", limit=20)
    retention_days = get_retention_days()
    purge_result = request.query_params.get("purged")
    return templates.TemplateResponse(request, "settings.html", {
        "auto_mode": auto_mode,
        "llm_available": llm_available,
        "recent_actions": recent_actions,
        "retention_days": retention_days,
        "purge_result": purge_result,
    })


@app.post("/settings/purge", response_model=None)
async def purge_old_data(request: Request):
    retention = get_retention_days()
    s = get_store()
    counts = s.purge_old_data(retention_days=retention)
    total = sum(counts.values())
    audit_log(actor="portal-user", action="purge", resource="store",
              details={"retention_days": retention, "rows_deleted": total, "by_table": counts})
    return RedirectResponse(url=f"/settings?purged={total}", status_code=303)


@app.post("/settings/auto-mode", response_model=None)
async def toggle_auto_mode(request: Request):
    form = await request.form()
    value = str(form.get("value", "false")).lower()
    s = get_store()
    s.set_setting("auto_mode", value)
    s.log_event(
        "portal", "auto-mode-toggled", None,
        "info", f"Auto-mode {'enabled' if value == 'true' else 'disabled'}",
    )
    audit_log(actor="portal-user", action="auto-mode-toggle", resource="settings:auto_mode",
              details={"value": value})
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/api/settings")
async def api_settings():
    return JSONResponse(get_store().list_settings())


@app.get("/api/export")
async def export_data():
    """Export all data as JSON for backup/migration."""
    return get_store().export_all()


@app.get("/api/settings/{key}")
async def api_get_setting(key: str):
    val = get_store().get_setting(key)
    if val is None:
        raise HTTPException(404, f"Setting '{key}' not found")
    return JSONResponse({"key": key, "value": val})


@app.post("/api/settings/{key}")
async def api_set_setting(request: Request, key: str):
    body = await request.json()
    value = body.get("value")
    if value is None:
        raise HTTPException(400, "value required")
    get_store().set_setting(key, str(value))
    return JSONResponse({"key": key, "value": str(value)})
