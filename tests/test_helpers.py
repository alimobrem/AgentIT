"""Tests for agentit.portal.helpers -- currently just get_current_user (Part
1 of the auth/CSRF/webhook-token hardening: OAuth-proxy identity forwarding).
"""
from __future__ import annotations

from starlette.requests import Request

from agentit.portal.helpers import get_current_user


def _make_request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
    }
    return Request(scope)


def test_falls_back_to_portal_user_when_header_absent():
    """auth.enabled=false (the default) means no oauth-proxy sidecar, so the
    X-Forwarded-User header is never set -- must not break dev/local/tests."""
    request = _make_request()
    assert get_current_user(request) == "portal-user"


def test_reads_x_forwarded_user_header_when_present():
    """auth.enabled=true: the oauth-proxy sidecar sets this header after a
    successful cluster OAuth login (--pass-user-headers=true)."""
    request = _make_request({"X-Forwarded-User": "alice@example.com"})
    assert get_current_user(request) == "alice@example.com"


def test_empty_header_value_falls_back_to_portal_user():
    request = _make_request({"X-Forwarded-User": ""})
    assert get_current_user(request) == "portal-user"
