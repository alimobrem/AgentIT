"""Shared client for calling this portal's own internal-token-gated
``/api/webhook/*`` routes (``portal/routes/webhooks.py``'s
``verify_internal_token``) from a separate pod.

Every long-lived watcher/loop that runs outside the portal process
(``RemediationLoop``, ``SkillLearner``, and any future cross-pod caller)
needs to attach the exact same ``X-Internal-Webhook-Token`` header the
receiving side checks for. Before this module existed, each caller built
its own ``httpx.AsyncClient`` and re-derived the "read
``AGENTIT_INTERNAL_WEBHOOK_TOKEN``, attach the header if set" logic by
hand -- inconsistently: ``RemediationLoop``'s client shipped without the
header at all for a while (a real incident, confirmed live via repeated
"loop-failed" events with "Missing or invalid internal webhook token"),
while ``SkillLearner`` built the header correctly but per-call instead of
once at construction. This module is now the one place that knows how to
build a correctly-configured client, so there's structurally only one way
to make this call, not one fixed instance plus an ad-hoc pattern
elsewhere.
"""

from __future__ import annotations

import os

import httpx

INTERNAL_TOKEN_HEADER = "X-Internal-Webhook-Token"


def internal_webhook_headers() -> dict[str, str]:
    """Headers to attach to a call into this portal's own
    ``/api/webhook/*`` routes.

    Fails open (returns no token header at all) when
    ``AGENTIT_INTERNAL_WEBHOOK_TOKEN`` isn't configured in this process's
    env -- mirroring ``verify_internal_token``'s own fail-open behavior on
    the receiving side (``portal/routes/webhooks.py``), so local dev/tests
    that never configure the secret keep working, and a real deployment
    (where the Secret is always templated) never silently sends an empty
    or wrong token.
    """
    token = os.environ.get("AGENTIT_INTERNAL_WEBHOOK_TOKEN")
    if not token:
        return {}
    return {INTERNAL_TOKEN_HEADER: token}


def internal_webhook_client(**kwargs: object) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` pre-configured with the internal
    webhook token header (attached once, at construction, via ``headers``
    passed through to ``httpx.AsyncClient`` -- so every request this
    client ever sends carries it automatically, the same way a browser
    session carries a cookie).

    Any keyword arguments (``timeout``, ``base_url``, ``transport``, ...)
    are passed straight through to ``httpx.AsyncClient``. Callers that also
    need their own default headers (rare) may pass ``headers=`` explicitly;
    the token header always wins by being merged in last.
    """
    headers = dict(kwargs.pop("headers", None) or {})
    headers.update(internal_webhook_headers())
    return httpx.AsyncClient(headers=headers, **kwargs)
