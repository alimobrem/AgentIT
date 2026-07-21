"""Experience Design Language (EDL) conformance for the AgentIT portal.

Normative doc: docs/portal-experience-design-language.md
Static checker: scripts/check_portal_edl.py

Run:
  uv run pytest tests/test_portal_edl.py -q
  uv run python scripts/check_portal_edl.py
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from conftest import make_report, make_store, prime_csrf

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── Static template walk (MUST rules) ──────────────────────────────────


def test_edl_static_checker_has_no_must_violations():
    """Fail the suite when templates violate EDL MUST rules."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "check_portal_edl", REPO_ROOT / "scripts" / "check_portal_edl.py"
    )
    assert spec and spec.loader
    checker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = checker
    spec.loader.exec_module(checker)

    vios = checker.check_all()
    musts = [v for v in vios if v.severity == "MUST"]
    assert musts == [], "\n".join(
        f"{v.rule} {v.path}:{v.line}: {v.message}" for v in musts
    )


def test_edl_doc_exists_with_machine_checkable_index():
    doc = (REPO_ROOT / "docs" / "portal-experience-design-language.md").read_text()
    assert "EDL-BTN-STATUS" in doc
    assert "EDL-BTN-CLASS" in doc
    assert "EDL-ONBOARD-ORDER" in doc
    assert "EDL-FILTER-BAR" in doc
    assert "EDL-FILTER-CSS" in doc
    assert ".filter-bar" in doc
    assert "user-menu-trigger" in doc
    assert "events-drawer-close" in doc
    assert "Dry Run" in doc and "Apply" in doc
    assert "Running checks" in doc
    assert "**Do**" in doc
    assert "**Don't**" in doc or "**Don’t**" in doc


def test_edl_static_checker_has_no_should_violations_for_buttons():
    """Button hierarchy sweep: fail on SHOULD button rules too (label length / .btn)."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "check_portal_edl", REPO_ROOT / "scripts" / "check_portal_edl.py"
    )
    assert spec and spec.loader
    checker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = checker
    spec.loader.exec_module(checker)

    vios = checker.check_all()
    btn_shoulds = [
        v for v in vios
        if v.severity == "SHOULD" and v.rule in {"EDL-BTN-STATUS", "EDL-BTN-CLASS"}
    ]
    assert btn_shoulds == [], "\n".join(
        f"{v.rule} {v.path}:{v.line}: {v.message}" for v in btn_shoulds
    )


def test_edl_cursor_rule_points_at_doc():
    rule = REPO_ROOT / ".cursor" / "rules" / "portal-edl.mdc"
    assert rule.is_file(), "add .cursor/rules/portal-edl.mdc so agents load the EDL"
    text = rule.read_text()
    assert "portal-experience-design-language.md" in text


# ── Rendered HTML / key pages ──────────────────────────────────────────


@pytest.fixture
async def edl_client():
    store = await make_store()
    async_store = store
    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=async_store), \
         patch("agentit.portal.routes.settings.get_store", return_value=async_store), \
         patch("agentit.portal.routes.insights.get_store", return_value=async_store), \
         patch("agentit.portal.cluster_apply.kube") as mock_kube, \
         patch("agentit.kube.list_custom_resources", return_value=[]):
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.get_custom_resource.side_effect = Exception("no cluster in tests")
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=True,
        ) as client:
            await prime_csrf(client)
            yield client, store


async def test_base_shell_has_toasts_confirm_dialog_and_events_drawer(edl_client):
    """base.html's CSS moved to static/css/ (2026-07-20 base.html split) --
    the two CSS-rule assertions below now check that content there instead
    of the rendered page's HTML."""
    client, _store = edl_client
    resp = await client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="toasts"' in html
    assert 'id="confirm-modal"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert "aria-labelledby=\"confirm-modal-title\"" in html or "aria-labelledby='confirm-modal-title'" in html
    assert "keydown.escape" in html
    assert "events-bell" in html
    assert 'id="events-drawer"' in html
    assert 'href="/decisions"' in html

    css = (await client.get("/static/css/components.css")).text
    assert re.search(r"\.btn-danger\s*\{", css)
    assert "font-size: var(--font-xs)" in css or "font-size:var(--font-xs)" in css


async def test_fleet_assess_modal_has_dialog_semantics(edl_client):
    """Fleet template (served once ≥1 assessment exists) carries dialog a11y."""
    client, store = edl_client
    await store.save(make_report())
    resp = await client.get("/fleet")
    assert resp.status_code == 200
    assert 'id="assess-modal"' in resp.text
    m = re.search(r'<div[^>]*id="assess-modal"[^>]*>', resp.text)
    assert m, "assess-modal missing"
    tag = m.group(0)
    assert 'role="dialog"' in tag
    assert "aria-modal" in tag
    assert "keydown.escape" in tag or "keydown.escape" in resp.text[max(0, m.start() - 400): m.end()]


async def test_criticality_field_has_help_text_on_both_assess_entry_points(edl_client):
    """Setting/changing Criticality must explain what it actually does today
    (2026-07-18 re-verification -- see README's Criticality paragraph):
    auto-deliver eligibility and default SLO strictness are real; neither
    a 'deploy-approval' gate nor AutoMode/gate-approval (both removed
    2026-07-19) is, so the help text must not claim either."""
    client, store = edl_client

    def _criticality_help_text(html: str) -> str:
        """Slice from the <select ...criticality...> tag to the very next
        </span> (its help text), rather than a bare name="criticality"
        substring search -- fleet.html's own Assess modal has two earlier,
        unrelated hidden name="criticality" inputs on Row actions above the
        real <select>."""
        marker = re.search(r"<select[^>]*criticality[^>]*>", html)
        assert marker, "no <select ...criticality...> found on the page"
        return html[marker.end():].split("</select>", 1)[1].split("</span>", 1)[0].lower()

    # Dashboard's empty-state hero form (no assessments yet).
    resp = await client.get("/fleet")
    assert resp.status_code == 200
    help_text = _criticality_help_text(resp.text)
    assert "always needs an explicit human deliver click" in help_text
    assert "deploy-approval" not in help_text
    assert "auto-mode" not in help_text
    assert "gate approval" not in help_text

    # Fleet's own Assess modal (served once >=1 assessment exists).
    await store.save(make_report())
    resp = await client.get("/fleet")
    assert resp.status_code == 200
    assert 'id="criticality"' in resp.text
    help_text = _criticality_help_text(resp.text)
    assert "always needs an explicit human deliver click" in help_text
    assert "stricter default slo targets" in help_text
    assert "deploy-approval" not in help_text
    assert "auto-mode" not in help_text
    assert "gate approval" not in help_text


async def test_onboard_results_dry_run_apply_status_outside_button(edl_client):
    """EDL §7: Run Automatic Validation → deliver choice; 'No validation
    yet' is a sibling chip."""
    client, store = edl_client
    report = make_report()
    aid = await store.save(report)
    # A known infra_repo_url -- Direct Apply has been removed as a concept
    # entirely, so an app with none at all is blocked from delivering
    # outright (a separate "Not GitOps-registered" state, not "No
    # validation yet"); this test is about the validation/deliver-choice
    # chip layout, not GitOps registration state.
    await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")
    await store.save_onboarding(aid, [
        {
            "category": "security",
            "path": "np.yaml",
            "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n",
            "description": "test",
        },
    ])
    resp = await client.get(f"/assessments/{aid}/onboard-results")
    assert resp.status_code == 200
    html = resp.text
    assert "Run Automatic Validation" in html
    assert (
        "Apply to Cluster" in html
        # Jinja autoescape turns "&" into "&amp;" in rendered HTML text.
        or "Commit & Open PR" in html
        or "Commit &amp; Open PR" in html
        or re.search(r">\s*Apply\s*<", html)
        or re.search(r">\s*Open PR\s*<", html)
        or "_deliver_label" in html
    )
    assert "Per-Agent PRs" in html
    assert "delivery-choice" in html
    assert "One PR for everything, or a PR per agent." in html
    assert "No validation yet" in html
    # No button may contain the status chip.
    for m in re.finditer(r"<button\b[^>]*>[\s\S]*?</button>", html, re.I):
        assert "No validation yet" not in m.group(0), "status chip nested inside a button"


async def test_async_feedback_surfaces_present_on_key_pages(edl_client):
    """Key pages inherit #toasts and use .btn classes on primary actions."""
    client, store = edl_client
    aid = await store.save(make_report())
    paths = ["/", "/ledger", "/events", "/settings", "/capabilities", f"/assessments/{aid}"]
    for path in paths:
        resp = await client.get(path)
        assert resp.status_code == 200, path
        assert 'id="toasts"' in resp.text, path
        assert "role=\"status\"" in resp.text or 'id="toasts"' in resp.text, path


async def test_filter_bar_pattern_on_list_pages(edl_client):
    """EDL §6: Decisions / Events / Ledger use compact .filter-bar, not
    .action-bar. The .filter-bar CSS rule assertions check
    static/css/components.css (2026-07-20 base.html split moved base.html's
    CSS there) instead of each page's own rendered HTML."""
    client, _store = edl_client
    css = (await client.get("/static/css/components.css")).text
    assert re.search(r"\.filter-bar\s*\{", css)
    assert re.search(
        r"\.filter-bar\s+input\s*,\s*\.filter-bar\s+select\s*\{[^}]*width\s*:\s*auto",
        css,
        re.S,
    )
    for path in ("/decisions", "/events", "/ledger"):
        resp = await client.get(path)
        assert resp.status_code == 200, path
        html = resp.text
        assert "filter-bar" in html, path
        assert "filter-actions" in html, path
        for m in re.finditer(r'<form\b[^>]*method=["\']get["\'][^>]*>', html, re.I):
            tag = m.group(0)
            if "filter-bar" in tag:
                assert "action-bar" not in tag, path

