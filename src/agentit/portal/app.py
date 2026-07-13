from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from markupsafe import Markup

from agentit.logging_config import configure_logging

configure_logging()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from agentit.portal.helpers import (
    get_store,
    get_retention_days,
    get_current_user,
    is_authenticated,
    OAUTH_PROXY_SIGN_OUT_PATH,
    safe_url as _safe_url,
    format_dimension as _format_dimension,
)
from agentit.portal.csrf import (
    CSRF_COOKIE_NAME,
    STATE_CHANGING_METHODS,
    generate_csrf_token,
    is_csrf_exempt,
    verify_csrf,
)
from agentit.agent_registry_cleanup import prune_stale_agents_and_log
from agentit.skill_inventory import diff_and_log_inventory_changes

log = logging.getLogger(__name__)

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


def _auth_context_processor(request: Request) -> dict:
    """Exposes `{{ current_user }}` / `{{ is_authenticated }}` / `{{
    oauth_sign_out_path }}` to every template -- base.html's nav bar uses
    these to show a "Logged in as" identity and a Logout link, using the
    exact same `get_current_user()` already attributed to `resolved_by` on
    gate actions (see routes/gates.py), so the nav display and the audit
    trail never disagree about who's making a request."""
    return {
        "current_user": get_current_user(request),
        "is_authenticated": is_authenticated(request),
        "oauth_sign_out_path": OAUTH_PROXY_SIGN_OUT_PATH,
    }


templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR),
    context_processors=[_csrf_context_processor, _auth_context_processor],
)


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
    gates, diff the skill/check inventory, prune stale agent_registry rows.
    Daily: purge old data."""
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

        try:
            s = await get_store()
            pruned = prune_stale_agents_and_log(s.raw if hasattr(s, "raw") else s)
            if pruned:
                log.info("Background: pruned %d stale agent registration(s): %s",
                          len(pruned), ", ".join(pruned))
        except Exception:
            log.debug("Background agent-registry prune failed", exc_info=True)


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
from agentit.portal.routes.fleet import router as fleet_router  # noqa: E402
from agentit.portal.routes.assessments import router as assessments_router  # noqa: E402
from agentit.portal.routes.gates import router as gates_router  # noqa: E402
from agentit.portal.routes.capabilities import router as capabilities_router  # noqa: E402
from agentit.portal.routes.settings import router as settings_router  # noqa: E402
from agentit.portal.routes.insights import router as insights_router  # noqa: E402
from agentit.portal.routes.remediations import router as remediations_router  # noqa: E402
from agentit.portal.routes.slos import router as slos_router  # noqa: E402

app.include_router(webhooks_router)
app.include_router(health_router)
app.include_router(schedules_router)
app.include_router(fleet_router)
app.include_router(assessments_router)
app.include_router(gates_router)
app.include_router(capabilities_router)
app.include_router(settings_router)
app.include_router(insights_router)
app.include_router(remediations_router)
app.include_router(slos_router)

