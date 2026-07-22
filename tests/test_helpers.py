"""Tests for agentit.portal.helpers -- get_current_user/is_authenticated/
OAUTH_PROXY_SIGN_OUT_PATH (the oauth-proxy identity-forwarding + logout-UI
follow-up to the Part 1 auth/CSRF/webhook-token hardening).
"""
from __future__ import annotations

from pathlib import Path

from starlette.requests import Request

from agentit.portal.helpers import (
    OAUTH_PROXY_SIGN_OUT_PATH,
    _codechange_category_override,
    get_current_user,
    is_authenticated,
)

CHART_DEPLOYMENT = (
    Path(__file__).resolve().parent.parent / "chart" / "templates" / "deployment.yaml"
)


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


def test_is_authenticated_false_when_header_absent():
    """auth.enabled=false: no oauth-proxy in front, so there's no session to
    log out of -- base.html's nav must not show a Logout link in this case."""
    request = _make_request()
    assert is_authenticated(request) is False


def test_is_authenticated_false_when_header_empty():
    request = _make_request({"X-Forwarded-User": ""})
    assert is_authenticated(request) is False


def test_is_authenticated_true_when_header_present():
    """auth.enabled=true and the proxy has authenticated a real user --
    base.html's nav should show "Logged in as" + a Logout link."""
    request = _make_request({"X-Forwarded-User": "alice@example.com"})
    assert is_authenticated(request) is True


def test_oauth_proxy_sign_out_path_matches_chart_deployment():
    """OAUTH_PROXY_SIGN_OUT_PATH must stay in sync with the *actual*
    oauth-proxy configuration in chart/templates/deployment.yaml, not just
    assume the openshift/oauth-proxy default forever.

    openshift/oauth-proxy's sign-out endpoint is `<proxy-prefix>/sign_out`,
    and `--proxy-prefix` defaults to "/oauth" when not passed as an arg. If
    someone adds a `--proxy-prefix=...` override to the oauth-proxy
    container's args in the future, this test fails loudly so
    OAUTH_PROXY_SIGN_OUT_PATH (and base.html's Logout link, which reads it)
    get updated to match -- instead of silently 404ing in a real cluster.
    """
    deployment_yaml = CHART_DEPLOYMENT.read_text(encoding="utf-8")
    assert "name: oauth-proxy" in deployment_yaml, (
        "chart/templates/deployment.yaml no longer defines an oauth-proxy "
        "container -- OAUTH_PROXY_SIGN_OUT_PATH assumptions need re-checking."
    )
    # Isolate just the oauth-proxy container's args block so an unrelated
    # `--proxy-prefix`-like string elsewhere in the file can't false-negative
    # this check.
    oauth_proxy_block = deployment_yaml.split("name: oauth-proxy", 1)[1]
    assert "--proxy-prefix" not in oauth_proxy_block, (
        "chart/templates/deployment.yaml's oauth-proxy container now passes "
        "--proxy-prefix, so the default \"/oauth\" no longer applies -- "
        "update OAUTH_PROXY_SIGN_OUT_PATH (agentit/portal/helpers.py) to "
        "match the new prefix + '/sign_out'."
    )
    assert OAUTH_PROXY_SIGN_OUT_PATH == "/oauth/sign_out"


class TestCodechangeCategoryOverride:
    """run_onboarding()'s delivery.classify_file() routing decision for
    delivery: source skill output -- see _codechange_category_override's
    own docstring for the bug this closes (a multi-file, all-``.yaml``
    source patch like skills/infrastructure/helm-chart.md's Helm chart was
    wrongly left at category="skills", which delivery.classify_file() then
    parsed as a K8s manifest and routed to CATEGORY_CLUSTER_CONFIG --
    gitops apps/{app}/ -- instead of the app's own repo)."""

    def test_yaml_target_path_outside_chart_and_skills_becomes_codechange(self):
        """The regression case: skills/infrastructure/helm-chart.md's
        multi-file Helm chart target paths (helm/Chart.yaml,
        helm/templates/deployment.yaml, ...) must all become codechange."""
        assert _codechange_category_override("helm/Chart.yaml") is True
        assert _codechange_category_override("helm/values.yaml") is True
        assert _codechange_category_override("helm/templates/deployment.yaml") is True
        assert _codechange_category_override("helm/templates/service.yaml") is True

    def test_non_yaml_target_paths_still_become_codechange(self):
        """Unchanged behavior for every pre-existing delivery: source
        skill (Dockerfile, .node-version, audit.py, alembic.ini, ...)."""
        assert _codechange_category_override("Dockerfile") is True
        assert _codechange_category_override(".node-version") is True
        assert _codechange_category_override("audit.py") is True
        assert _codechange_category_override("alembic/env.py") is True

    def test_chart_prefixed_target_path_is_excluded(self):
        """AgentIT's own self-managed chart/ remap target -- must stay
        untouched (routes as cluster-shaped self-managed content, handled
        by a completely different code path in delivery.py)."""
        assert _codechange_category_override("chart/templates/foo.yaml") is False

    def test_skills_prefixed_target_path_is_excluded(self):
        """A skill-catalog markdown improvement -- must stay untouched."""
        assert _codechange_category_override("skills/infrastructure/foo.md") is False

    def test_empty_target_path_is_false(self):
        assert _codechange_category_override("") is False
        assert _codechange_category_override(None) is False
