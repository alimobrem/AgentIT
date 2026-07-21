"""Template rendering regression tests — every page renders with expected elements."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "agentit" / "portal" / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "agentit" / "portal" / "static"


class TestAlpineScoping:
    """Every @click / x-ref must be inside an x-data scope."""

    @pytest.fixture(scope="class")
    def template_files(self):
        return list(TEMPLATES_DIR.glob("*.html"))

    def _find_alpine_attrs(self, html: str) -> list[tuple[int, str, str]]:
        """Return (line_number, attr, context) for @click and x-ref attrs."""
        hits = []
        for i, line in enumerate(html.splitlines(), 1):
            if "@click" in line or "x-on:click" in line:
                hits.append((i, "@click", line.strip()[:120]))
            if "x-ref" in line:
                hits.append((i, "x-ref", line.strip()[:120]))
        return hits

    def _has_xdata_ancestor(self, html: str, line_num: int) -> bool:
        """Check if a line is nested inside an element with x-data.

        Walk backward from the target line, tracking open/close tags with x-data.
        A simple heuristic: count x-data occurrences in lines before the target
        that haven't been closed by their matching end tag.
        """
        lines = html.splitlines()[:line_num]
        depth = 0
        for line in lines:
            if "x-data" in line:
                depth += 1
            # </div>, </form> etc. that close an x-data block
            # This is approximate — good enough for catching the common bug
        # If any x-data appeared before this line, check if we're inside it
        # More precise: count unclosed x-data blocks by tracking tag depth
        text_before = "\n".join(lines)
        xdata_count = text_before.count("x-data")
        return xdata_count > 0

    def _precise_check(self, html: str, target_line: int) -> bool:
        """Check if @click at target_line has an x-data ancestor.

        Joins the HTML into a single string and uses regex to find all
        opening tags with their full attribute blocks (including multi-line).
        Then checks if any enclosing tag has x-data.
        """
        lines = html.splitlines()
        # Check all lines from start up to and including target for x-data
        # in any opening tag that hasn't been closed before target_line
        text_before = "\n".join(lines[:target_line])

        # Find all opening tags with x-data before our target line
        # Match multi-line tags: <tag ... x-data ... >
        for m in re.finditer(r"<(\w+)([^>]*?)>", text_before, re.DOTALL):
            attrs = m.group(2)
            tag_name = m.group(1)
            if "x-data" in attrs:
                # Check this tag hasn't been closed before target
                tag_end = text_before.count(f"</{tag_name}>")
                tag_start_with_xdata = len(
                    [x for x in re.finditer(rf"<{tag_name}[^>]*x-data[^>]*>", text_before, re.DOTALL)]
                )
                if tag_start_with_xdata > tag_end:
                    return True

        # Scan all lines above target for x-data on any unclosed ancestor
        for i in range(target_line - 2, -1, -1):
            if "x-data" in lines[i]:
                return True

        return False

    @pytest.mark.parametrize("template_name", [
        f.name for f in TEMPLATES_DIR.glob("*.html") if f.name != "base.html"
    ])
    def test_no_orphaned_alpine_click(self, template_name):
        """@click handlers must be inside an x-data scope."""
        path = TEMPLATES_DIR / template_name
        html = path.read_text(encoding="utf-8")
        hits = self._find_alpine_attrs(html)
        click_hits = [(ln, ctx) for ln, attr, ctx in hits if attr == "@click"]

        orphans = []
        for line_num, context in click_hits:
            if not self._precise_check(html, line_num):
                orphans.append(f"  line {line_num}: {context}")

        assert not orphans, (
            f"{template_name} has @click outside x-data scope:\n"
            + "\n".join(orphans)
        )


class TestUrlParamToastsSurviveHtmxBoost:
    """Boosted form POSTs (Register for GitOps, Deliver, ...) redirect with
    ?error=/?success= and swap <body> via htmx. alpine:initialized only fires
    once per full page load, so without an explicit re-call after
    Alpine.destroyTree/initTree the flash toast never appears and the click
    looks like a no-op. Confirmed live against the deployed portal before
    showUrlParamToasts() was wired into htmx:afterSettle.
    """

    def test_show_url_param_toasts_helper_exists(self):
        html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        assert "function showUrlParamToasts()" in html
        assert "document.addEventListener('alpine:initialized', showUrlParamToasts)" in html

    def test_htmx_after_settle_reinvokes_url_param_toasts_on_boost(self):
        html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        reiniter_idx = html.find("Alpine.destroyTree(document.body)")
        assert reiniter_idx != -1
        toast_idx = html.find("showUrlParamToasts()", reiniter_idx)
        assert toast_idx != -1, (
            "base.html must call showUrlParamToasts() after Alpine.initTree "
            "on boosted htmx:afterSettle — otherwise Register/Deliver flash "
            "messages are silently dropped"
        )
        else_idx = html.find("else if (e.detail.target", reiniter_idx)
        assert else_idx == -1 or toast_idx < else_idx


class TestConfirmModalOutsideClick:
    """Regression: the shared confirm-modal's outside-click handler must use
    the `capture` modifier.

    Every "Apply to Cluster" / "Delete" / "Install Operator" / etc. button in
    the app opens the shared #confirm-modal (base.html) via
    `$dispatch('show-confirm', ...)` from a plain (non-capture) @click. Since
    the trigger button lives outside `.modal`, the *same* click that opens
    the modal also bubbles to document and satisfies `.modal`'s
    `@click.outside="cancel()"` check — instantly closing the modal it just
    opened, with no console error, no visible flash: the button appears to
    do nothing. Registering the outside-click listener for the *capture*
    phase (`@click.outside.capture`) makes it run before the triggering
    click reaches the button (while the modal is still closed, a no-op), and
    — because capture-only listeners never re-fire during bubble — it does
    not fire again after the button's own handler opens the modal. Later,
    genuinely separate clicks elsewhere still close it normally. Confirmed
    live via Playwright against the deployed portal: a real click on Fleet's
    "Delete" button (and onboard-results' "Apply to Cluster") opened then
    closed #confirm-modal within under 1ms, before this fix.
    """

    def test_confirm_modal_uses_capture_outside_click(self):
        html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        assert "id=\"confirm-modal\"" in html, "base.html should define #confirm-modal"
        # The bare (non-capture) form must not be present anywhere in base.html —
        # that's exactly the self-closing bug this test guards against.
        assert "@click.outside=\"cancel()\"" not in html, (
            "base.html's #confirm-modal uses @click.outside without .capture — "
            "this closes the modal on the SAME click that opens it (see class docstring)."
        )
        assert "@click.outside.capture=\"cancel()\"" in html, (
            "base.html's #confirm-modal must use @click.outside.capture=\"cancel()\" "
            "so the triggering click doesn't immediately re-close the modal it opened."
        )

    @pytest.mark.parametrize("template_name", [
        f.name for f in TEMPLATES_DIR.glob("*.html") if f.name != "base.html"
    ])
    def test_show_confirm_triggers_are_plain_click(self, template_name):
        """Every $dispatch('show-confirm', ...) trigger relies on the shared
        modal's outside-click guard being capture-based (tested above), not
        on any local workaround (e.g. @click.stop) — if a template starts
        adding its own workaround, the shared base.html fix is either
        missing or someone is band-aiding around it per-button. Either way,
        surface it so it gets reconciled in one place."""
        path = TEMPLATES_DIR / template_name
        html = path.read_text(encoding="utf-8")
        for i, line in enumerate(html.splitlines(), 1):
            if "show-confirm" in line and "$dispatch" in line:
                stripped = line.strip()
                assert "@click.stop=" not in stripped and "@click.outside" not in stripped, (
                    f"{template_name}:{i} has a local workaround around the "
                    f"show-confirm dispatch — fix belongs in base.html's "
                    f"#confirm-modal instead:\n  {stripped[:160]}"
                )


class TestTojsonForceescape:
    """Regression: `| tojson` produces a JSON string, which always starts
    and ends with a literal `"`. Embedding that raw inside an
    already-`"`-delimited HTML attribute (e.g. `@click="..."`) makes a
    browser's HTML parser terminate the attribute at that very first
    literal quote -- silently truncating the Alpine expression mid-way, no
    error, no visual feedback, the click handler just doesn't fire.
    `| forceescape` immediately after `| tojson` HTML-entity-encodes those
    quotes (`&quot;`) so the attribute value survives intact. Confirmed
    live: exactly this bug broke both onboard_results.html's own "Deliver"
    button and the shared `_macros.html::gate_card` "Approve & Deliver"
    button (used by both Admin Review and the per-app Ledger tab) --
    every other `tojson`-in-an-attribute usage in the template tree
    already had `| forceescape` (e.g. fleet.html's Delete button,
    onboard_results.html's Install Operator button).
    """

    @pytest.mark.parametrize(
        "template_path", sorted(TEMPLATES_DIR.glob("*.html")), ids=lambda p: p.name
    )
    def test_every_tojson_usage_is_forceescaped(self, template_path):
        html = template_path.read_text(encoding="utf-8")
        bad = []
        for i, line in enumerate(html.splitlines(), 1):
            if "tojson" in line and "forceescape" not in line:
                bad.append(f"  line {i}: {line.strip()[:160]}")

        assert not bad, (
            f"{template_path.name} has `| tojson` without a following "
            f"`| forceescape` on the same line -- this truncates the "
            f"enclosing HTML attribute at the JSON string's own leading "
            f"quote (see class docstring):\n" + "\n".join(bad)
        )


class _ClickAttrCapture(HTMLParser):
    """Captures `@click` attribute values the way a real browser's HTML
    parser would -- i.e. terminating a double-quoted attribute at the
    first literal `"` it encounters, even mid-value. Used to prove a
    rendered attribute is a single, complete, unbroken JS expression, not
    just that the page returned 200 OK (which a truncated attribute still
    does -- the breakage is purely client-side)."""

    def __init__(self):
        super().__init__()
        self.clicks: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "@click" and value:
                self.clicks.append(value)


class TestDeliverButtonClickAttributeIntact:
    """Confirms the fix above actually produces a valid, unbroken @click
    expression in real rendered output -- not just that Jinja itself
    doesn't error. Parses the response HTML with html.parser (see
    _ClickAttrCapture), which reproduces the exact browser behavior that
    silently broke these buttons before `| forceescape` was added."""

    async def test_onboard_results_deliver_click_attr_is_unbroken(self, portal_client):
        client, store, aid = portal_client
        # A known infra_repo_url -- Direct Apply has been removed as a
        # concept entirely, so Deliver's real @click confirm only ever
        # exists once GitOps registration is known (otherwise Deliver is
        # blocked outright with no @click at all -- see the "no infra repo"
        # coverage elsewhere, e.g. test_customer_trust_ux.py). The primary
        # Deliver button is also soft-gated on a successful Dry Run (the
        # Override bypass has been removed too), so run one first.
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}):
            await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False)

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200

        parser = _ClickAttrCapture()
        parser.feed(resp.text)
        # Note: Jinja tojson emits the em dash as \\u2014, so match on
        # "Confirm Commit" + mechanism label rather than a literal "—".
        deliver_clicks = [
            c for c in parser.clicks
            if "Confirm Commit" in c and "Open PR" in c
        ]
        assert deliver_clicks, "no @click attribute found for the primary Deliver button"

        click = deliver_clicks[0]
        assert "AgentIT will:" in click, (
            f"Deliver button's @click attribute was truncated before the "
            f"delivery_confirmation text -- likely a bare `| tojson` "
            f"(without `| forceescape`) broke out of the HTML attribute "
            f"early:\n{click!r}"
        )
        assert (
            "does not mutate the cluster" in click
            or "cluster is not mutated" in click
        ), (
            f"Deliver button's @click attribute was truncated -- it never "
            f"reaches the honest GitOps-commit consequence tail:\n{click!r}"
        )
        assert "cannot be undone" not in click
        assert "modifies production" not in click
        assert click.rstrip().endswith("})"), (
            f"Deliver button's @click attribute doesn't end with a "
            f"complete function call -- looks truncated:\n{click!r}"
        )

    async def test_pr_action_card_click_attr_is_unbroken(self, portal_client):
        """`pr_action_card()` (the real Merge/Close action, replacing the
        retired `gate_card()`) is shared across every PR-backed delivery
        category -- any open PR on the fleet-wide Ledger's "Waiting for
        your approval" section exercises the exact same macro/click-
        attribute rendering (Assessment Detail's own Ledger tab shows
        PR history read-only; the real Merge/Close actions live here)."""
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/test-app/pull/1"
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": 1}, mechanism="cluster_config:infra-repo-commit",
            status="delivered", details={"outcomes": {"cluster_config": {"pr_url": pr_url}}},
        )

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": pr_url, "title": "fix", "merged_at": ""},
        ):
            resp = await client.get("/ledger")
        assert resp.status_code == 200

        parser = _ClickAttrCapture()
        parser.feed(resp.text)
        merge_clicks = [c for c in parser.clicks if "Merge PR" in c]
        assert merge_clicks, "no @click attribute found for the Merge PR button"

        click = merge_clicks[0]
        assert "This action modifies production resources" in click, (
            f"pr_action_card's Merge PR @click attribute was "
            f"truncated -- it never reaches the tail of the JS "
            f"expression:\n{click!r}"
        )
        assert click.rstrip().endswith("})"), (
            f"pr_action_card's Merge PR @click attribute doesn't end "
            f"with a complete function call -- looks truncated:\n{click!r}"
        )


class TestOnboardResultsApplyErrorShowsFullMessage:
    """onboard_results.html's Apply Report "Errors" list used to render
    `f.split(':')[0]` -- for the real error shape cluster_apply.py
    produces (`f"{fpath}: {result['error']}"`), that discarded everything
    after the first colon, showing only the filename with no reason at
    all. Confirms the fix shows the full reason too, split on only the
    first colon (a reason that itself contains a colon must survive
    intact)."""

    async def test_full_error_reason_rendered_not_just_filename(self, portal_client):
        client, store, aid = portal_client
        await store.save_apply_results(
            aid,
            {
                "applied": [],
                "skipped": [],
                "errors": [
                    "app-network-policy.yaml: (403)\nReason: Forbidden\n"
                    "HTTP response body: cannot create resource in namespace: test-app",
                ],
                "repo_files": [],
            },
            "test-app",
            dry_run=True,
        )

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "app-network-policy.yaml" in resp.text
        assert "cannot create resource in namespace: test-app" in resp.text, (
            "Only the filename fragment before the first colon was shown -- "
            "the actual failure reason after it is missing from the page."
        )


class TestFormActions:
    """Every form action URL should match a registered route."""

    @pytest.fixture(scope="class")
    def route_paths(self):
        from agentit.portal.app import app
        paths = set()
        for route in app.routes:
            if hasattr(route, "path"):
                # Normalize path params to {param} pattern
                paths.add(route.path)
        return paths

    def _extract_form_actions(self, html: str) -> list[tuple[int, str]]:
        """Return (line_number, action_url) for every <form action=...>."""
        results = []
        for i, line in enumerate(html.splitlines(), 1):
            for m in re.finditer(r'action="([^"]*)"', line):
                url = m.group(1)
                if "{{" in url:
                    # Normalize Jinja2 template vars to route params
                    url = re.sub(r"\{\{[^}]+\}\}", "PARAM", url)
                results.append((i, url))
        return results

    def _matches_route(self, url: str, route_paths: set[str]) -> bool:
        """Check if a URL matches any registered route pattern."""
        for route in route_paths:
            # Convert route params to regex
            pattern = re.sub(r"\{[^}]+\}", "[^/]+", route)
            pattern = re.sub(r"PARAM", "[^/]+", url)
            if re.fullmatch(pattern, url):
                return True
            # Also try replacing PARAM in the url with a dummy value
            test_url = url.replace("PARAM", "test123")
            route_pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", route) + "$"
            if re.fullmatch(route_pattern, test_url):
                return True
        return False

    @pytest.mark.parametrize("template_name", [
        f.name for f in TEMPLATES_DIR.glob("*.html") if f.name != "base.html"
    ])
    def test_form_actions_match_routes(self, template_name, route_paths):
        """Every form action= URL must correspond to a registered route."""
        path = TEMPLATES_DIR / template_name
        html = path.read_text(encoding="utf-8")
        actions = self._extract_form_actions(html)

        unmatched = []
        for line_num, url in actions:
            if not self._matches_route(url, route_paths):
                unmatched.append(f"  line {line_num}: {url}")

        assert not unmatched, (
            f"{template_name} has form actions with no matching route:\n"
            + "\n".join(unmatched)
        )


class TestTimestamps:
    """All timestamps must use data-timestamp for human-readable display."""

    TIMESTAMP_FIELDS = re.compile(
        r"\{\{\s*\w+\.(created_at|timestamp|assessed_at|registered_at|"
        r"last_heartbeat|completed_at|resolved_at|merged_at|updated_at)"
        r"\[:\d+\]\s*\}\}"
    )

    @pytest.mark.parametrize("template_name", [
        f.name for f in TEMPLATES_DIR.glob("*.html") if f.name != "base.html"
    ])
    def test_timestamps_use_data_attribute(self, template_name):
        """Every displayed timestamp must be wrapped in <span data-timestamp=...>."""
        path = TEMPLATES_DIR / template_name
        html = path.read_text(encoding="utf-8")
        lines = html.splitlines()

        raw = []
        for i, line in enumerate(lines, 1):
            if self.TIMESTAMP_FIELDS.search(line) and "data-timestamp" not in line:
                raw.append(f"  line {i}: {line.strip()[:120]}")

        assert not raw, (
            f"{template_name} has raw timestamps without data-timestamp:\n"
            + "\n".join(raw)
        )


class TestNoInlineCSS:
    """No inline `style="..."` attributes except CSS custom-property hooks.

    Setting a `--custom-prop` for a per-instance dynamic value (e.g. a score
    bar's fill width) is allowed — the actual visual rule lives in base.html
    and reads the variable. Inlining real style rules (width, color, display,
    etc.) directly is not.
    """

    STYLE_ATTR = re.compile(r'style="([^"]*)"')
    CUSTOM_PROP_ONLY = re.compile(r"^\s*(--[\w-]+\s*:\s*[^;]+;?\s*)+$")

    @pytest.mark.parametrize("template_name", [
        f.name for f in TEMPLATES_DIR.glob("*.html") if f.name != "base.html"
    ])
    def test_no_inline_style_rules(self, template_name):
        """style= attributes may only set CSS custom properties, not rules."""
        path = TEMPLATES_DIR / template_name
        html = path.read_text(encoding="utf-8")

        violations = []
        for i, line in enumerate(html.splitlines(), 1):
            for m in self.STYLE_ATTR.finditer(line):
                declarations = m.group(1)
                if not self.CUSTOM_PROP_ONLY.match(declarations):
                    violations.append(f"  line {i}: style=\"{declarations}\"")

        assert not violations, (
            f"{template_name} has inline CSS rules (only --custom-props "
            f"are allowed inline; add real rules to base.html):\n"
            + "\n".join(violations)
        )


class TestHTMXErrorHandling:
    """Verify the portal has error handling for htmx requests."""

    def test_base_has_error_handlers(self):
        html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        assert "htmx:responseError" in html or "htmx:sendError" in html, \
            "base.html should handle htmx errors"

    def test_base_has_loading_reset(self):
        html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        assert "completeLoading" in html or "cursor" in html, \
            "base.html should reset cursor/loading state"

    def test_base_has_dead_button_detector(self):
        html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        assert "Dead button" in html, \
            "base.html should have dead-button detector"


class TestTemplateRendering:
    async def test_fleet_has_nav(self, portal_client):
        client, _, _ = portal_client
        text = (await client.get("/")).text
        assert "AgentIT" in text

    async def test_assess_form_has_inputs(self, portal_client):
        client, _, _ = portal_client
        text = (await client.get("/assess")).text
        assert "<form" in text
        assert "repo" in text.lower()

    async def test_assessment_detail_has_scores(self, portal_client):
        client, _, aid = portal_client
        text = (await client.get(f"/assessments/{aid}")).text
        assert "security" in text.lower()
        assert "/100" in text

    async def test_events_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert (await client.get("/events")).status_code == 200

    async def test_ledger_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert (await client.get("/ledger")).status_code == 200

    async def test_agents_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert (await client.get("/agents")).status_code == 200

    async def test_settings_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert (await client.get("/settings")).status_code == 200

    async def test_schedules_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert (await client.get("/schedules")).status_code == 200

    async def test_workflows_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert (await client.get("/workflows")).status_code == 200

    async def test_health_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert (await client.get("/health")).status_code == 200

    async def test_dlq_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert (await client.get("/events/dlq")).status_code == 200

    async def test_onboard_results_renders(self, portal_client):
        client, _, aid = portal_client
        assert (await client.get(f"/assessments/{aid}/onboard-results")).status_code == 200

    async def test_slos_renders(self, portal_client):
        client, _, aid = portal_client
        assert (await client.get(f"/assessments/{aid}/slos")).status_code == 200

    async def test_slos_progress_bar_direction_for_lower_is_better_metrics(self, portal_client):
        """Regression: for lower-is-better metrics (error_rate, latency),
        the progress bar previously used current/target directly, so a
        HEALTHY (low) current_value rendered as a near-empty RED bar and a
        BREACHED (high) current_value rendered as a full GREEN bar --
        exactly backwards."""
        client, store, aid = portal_client
        healthy_sid = await store.save_slo(aid, "error_rate", 0.5)
        await store.update_slo(healthy_sid, 0.05, "met")  # well under target -> healthy
        breached_sid = await store.save_slo(aid, "error_rate", 0.5)
        await store.update_slo(breached_sid, 5.0, "breached")  # well over target -> breached

        resp = await client.get(f"/assessments/{aid}/slos")
        assert resp.status_code == 200
        rows = re.findall(r"<tr[^>]*>.*?</tr>", resp.text, re.DOTALL)

        healthy_row = next(r for r in rows if "0.05" in r)
        assert "score-green" in healthy_row, healthy_row
        assert "score-red" not in healthy_row

        breached_row = next(r for r in rows if "5.00" in r)
        assert "score-red" in breached_row, breached_row
        assert "score-green" not in breached_row

    async def test_slos_no_data_renders_empty_bar_not_full_green(self, portal_client):
        """Regression: for lower_is_better metrics (error_rate,
        latency_p99_ms), when current_value is None/falsy (no real
        telemetry yet), `pct` previously defaulted to 100 -- a brand-new
        app with zero real data showed a full GREEN bar for these two
        metrics, contradicting the adjacent "unknown" status badge and
        the project's no-fabricated-data principle. It must default to 0,
        matching the (already-correct) default for every other metric
        direction (e.g. availability)."""
        client, store, aid = portal_client
        no_data_lower_sid = await store.save_slo(aid, "error_rate", 0.5)
        no_data_other_sid = await store.save_slo(aid, "availability", 99.9)

        resp = await client.get(f"/assessments/{aid}/slos")
        assert resp.status_code == 200
        rows = re.findall(r"<tr[^>]*>.*?</tr>", resp.text, re.DOTALL)

        lower_row = next(r for r in rows if "error_rate" in r)
        assert "--pct: 0" in lower_row or "--pct: 0.0" in lower_row, (
            f"error_rate SLO with no current_value must render at 0%, not "
            f"a full/high bar:\n{lower_row}"
        )
        assert "score-green" not in lower_row, (
            f"error_rate SLO with no current_value must not render a "
            f"misleading full green bar:\n{lower_row}"
        )
        assert "badge-info" in lower_row and ">unknown<" in lower_row

        other_row = next(r for r in rows if "availability" in r)
        assert "--pct: 0" in other_row or "--pct: 0.0" in other_row, (
            f"availability SLO with no current_value must also render at "
            f"0% (unchanged behavior, confirms both metric directions "
            f"now agree):\n{other_row}"
        )

    async def test_404_page(self, portal_client):
        client, _, _ = portal_client
        assert (await client.get("/nonexistent-xyz")).status_code == 404

    async def test_all_pages_have_css(self, portal_client):
        """base.html's CSS moved from an inline <style> block to real static
        files (2026-07-20) -- every page must still reference both, and the
        referenced URLs must actually resolve (not just be present as text),
        so a typo'd href/mount path fails this test instead of silently
        shipping an unstyled page."""
        client, _, aid = portal_client
        for page in ["/", "/assess", f"/assessments/{aid}", "/events", "/ledger",
                     "/agents", "/settings", "/workflows", "/health"]:
            resp = await client.get(page)
            assert resp.status_code == 200, f"{page} returned {resp.status_code}"
            assert '<link rel="stylesheet" href="/static/css/base.css' in resp.text, f"{page} missing base.css link"
            assert '<link rel="stylesheet" href="/static/css/components.css' in resp.text, f"{page} missing components.css link"

    async def test_static_css_actually_resolves(self, portal_client):
        """Regression guard for the base.html CSS extraction (2026-07-20):
        confirms the /static mount actually serves real, non-empty CSS --
        catching a broken StaticFiles mount or a typo'd static/ path that
        `test_all_pages_have_css` (a pure string-match on the <link> href)
        could not, since that test never fetches the URL."""
        client, _, _ = portal_client
        for path, marker in [
            ("/static/css/base.css", ":root"),
            ("/static/css/components.css", ".stat-grid"),
        ]:
            resp = await client.get(path)
            assert resp.status_code == 200, f"{path} returned {resp.status_code}"
            assert marker in resp.text, f"{path} missing expected content {marker!r}"


class TestAuthNav:
    """Nav bar's Logout link / "Logged in as" text (base.html) must key off
    the *request's* X-Forwarded-User header, not a static Helm value -- the
    same rendered template/image is served whether auth.enabled is true or
    false, so auth.enabled=false deployments (and every test in this file
    that doesn't pass the header) must never show a Logout link to nothing.
    """

    async def test_no_logout_link_without_forwarded_user_header(self, portal_client):
        client, _, _ = portal_client
        text = (await client.get("/")).text
        assert "Logout" not in text
        assert "Logged in as" not in text

    async def test_logout_link_appears_with_forwarded_user_header(self, portal_client):
        client, _, _ = portal_client
        text = (await client.get("/", headers={"X-Forwarded-User": "alice@example.com"})).text
        assert "Logged in as alice@example.com" in text
        assert "Logout" in text

    async def test_logout_href_matches_oauth_proxy_sign_out_path(self, portal_client):
        """The rendered Logout href must be the *real* oauth-proxy sign-out
        path (from helpers.OAUTH_PROXY_SIGN_OUT_PATH, itself checked against
        chart/templates/deployment.yaml in test_helpers.py) -- not a
        separately hardcoded string in the template that could drift from
        the actual deployed proxy configuration."""
        from agentit.portal.helpers import OAUTH_PROXY_SIGN_OUT_PATH

        client, _, _ = portal_client
        text = (await client.get("/", headers={"X-Forwarded-User": "alice@example.com"})).text
        assert f'href="{OAUTH_PROXY_SIGN_OUT_PATH}"' in text
