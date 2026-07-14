"""Lightweight in-memory rate limiting for the portal's own routes.

Not a substitute for a real API gateway/WAF: this is a per-process, 60s
sliding window keyed by client IP + route group ("webhook" vs "default").
With `replicaCount > 1` each pod tracks its own counters independently, so
the effective cluster-wide ceiling is roughly (per-pod limit * replica
count) -- acceptable for this module's actual purpose (cheap insurance
against a single runaway or replayed webhook loop hitting routes that can
apply changes to a live cluster via auto-mode), not a guarantee of an exact
global limit. See docs/deployment.md and chart/values.yaml's `rateLimit`.

Disabled unless `AGENTIT_RATE_LIMIT_ENABLED=true` (set via the chart's
`rateLimit.enabled`), so this changes no behavior for any existing
deployment or for local dev/tests that never set it.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque

_WINDOW_SECONDS = 60.0
_hits: dict[str, deque[float]] = defaultdict(deque)
_calls_since_purge = 0
_PURGE_EVERY = 1000

# Never rate-limited: kubelet polls /healthz and /readyz every few seconds
# (deployment.yaml's probes), and /metrics is scraped by Prometheus on its
# own short interval -- limiting either would make a pod flap on its own
# liveness/readiness check or blind the ServiceMonitor.
_EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


def is_enabled() -> bool:
    return os.environ.get("AGENTIT_RATE_LIMIT_ENABLED", "false").strip().lower() == "true"


def _limit_for_path(path: str) -> int:
    if path.startswith("/api/webhook/"):
        return int(os.environ.get("AGENTIT_RATE_LIMIT_WEBHOOK_PER_MIN", "30"))
    return int(os.environ.get("AGENTIT_RATE_LIMIT_DEFAULT_PER_MIN", "120"))


def _purge_empty_buckets() -> None:
    """Drops buckets that have aged out completely, so a long-lived process
    doesn't accumulate one dict entry per distinct client key forever."""
    for key in [k for k, v in _hits.items() if not v]:
        del _hits[key]


def check_rate_limit(client_key: str, path: str) -> bool:
    """Returns True if the request is allowed, False if it should be 429'd."""
    global _calls_since_purge
    if not is_enabled() or path in _EXEMPT_PATHS:
        return True

    limit = _limit_for_path(path)
    group = "webhook" if path.startswith("/api/webhook/") else "default"
    bucket = _hits[f"{client_key}:{group}"]

    now = time.monotonic()
    while bucket and now - bucket[0] > _WINDOW_SECONDS:
        bucket.popleft()

    _calls_since_purge += 1
    if _calls_since_purge >= _PURGE_EVERY:
        _calls_since_purge = 0
        _purge_empty_buckets()

    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def client_key_for(request) -> str:
    """Best-effort client identity: X-Forwarded-For's first hop (the Route
    terminates TLS and proxies through the router, so request.client.host
    would otherwise always be the router's own pod IP), falling back to
    request.client.host for direct/in-cluster callers (Argo Events Sensors
    hitting the Service directly)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
