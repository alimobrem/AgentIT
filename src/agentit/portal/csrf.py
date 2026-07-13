"""Double-submit-cookie CSRF protection for browser-originated form posts.

The portal has no session store, so the classic synchronizer-token pattern
(token tied to a server-side session) doesn't apply cleanly here -- double
submit cookie is the standard retrofit for a stateless app like this one:

1. On any request, the app ensures a random token is set as a cookie
   readable by JS (`CSRF_COOKIE_NAME`, not HttpOnly -- see below for why
   that's still safe).
2. The page (base.html) echoes that same value back on every POST/PUT/PATCH/
   DELETE as the `X-CSRF-Token` header, auto-attached by an
   `htmx:configRequest` handler for every htmx-boosted request -- this app
   boosts the whole `<body>`, so that covers every plain
   `<form method="post">` without editing each template. A `csrf_token` form
   field is also accepted as a fallback for any non-htmx/non-JS submission.
3. On the way in, `verify_csrf` checks the cookie value against whichever of
   those the request carried, using a constant-time compare.

This defeats cross-site attacks because a page on another origin can cause a
request that includes the victim's cookie automatically, but same-origin
policy prevents it from *reading* that cookie's value to also set a matching
header/field -- it can forge the request, but not the proof that it saw the
token.
"""
from __future__ import annotations

import hmac
import logging
import secrets

from starlette.requests import Request

log = logging.getLogger(__name__)

CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_FORM_FIELD = "csrf_token"

# Browser form/JS submissions never hit these -- Part 3 (webhooks.py) secures
# them with a separate shared-secret mechanism instead, and they don't carry
# the CSRF cookie in the first place (they're not driven by base.html).
EXEMPT_PATH_PREFIXES = ("/api/webhook/",)
EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def is_csrf_exempt(path: str) -> bool:
    if path in EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in EXEMPT_PATH_PREFIXES)


async def get_submitted_token(request: Request) -> str | None:
    """Read the caller's proof-of-token from the header, falling back to a
    parsed form field. Checks the header first since that's what the
    htmx:configRequest handler in base.html attaches to every boosted
    request -- avoids parsing (and consuming) the request body when we don't
    need to."""
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if header_token:
        return header_token
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        try:
            form = await request.form()
        except Exception:
            log.debug("Failed to parse form body while looking for CSRF token", exc_info=True)
            return None
        value = form.get(CSRF_FORM_FIELD)
        return str(value) if value is not None else None
    return None


async def verify_csrf(request: Request) -> bool:
    """True if the request's cookie and submitted token match (and both are
    present). Constant-time compare to avoid a timing side-channel."""
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token:
        return False
    submitted_token = await get_submitted_token(request)
    if not submitted_token:
        return False
    return hmac.compare_digest(cookie_token, submitted_token)
