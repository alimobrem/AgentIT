"""Template rendering regression tests — every page renders with expected elements."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "agentit" / "portal" / "templates"


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
    def test_fleet_has_nav(self, portal_client):
        client, _, _ = portal_client
        text = client.get("/").text
        assert "AgentIT" in text

    def test_assess_form_has_inputs(self, portal_client):
        client, _, _ = portal_client
        text = client.get("/assess").text
        assert "<form" in text
        assert "repo" in text.lower()

    def test_assessment_detail_has_scores(self, portal_client):
        client, _, aid = portal_client
        text = client.get(f"/assessments/{aid}").text
        assert "security" in text.lower()
        assert "/100" in text

    def test_events_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/events").status_code == 200

    def test_gates_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/gates").status_code == 200

    def test_agents_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/agents").status_code == 200

    def test_settings_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/settings").status_code == 200

    def test_schedules_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/schedules").status_code == 200

    def test_workflows_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/workflows").status_code == 200

    def test_health_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/health").status_code == 200

    def test_dlq_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/events/dlq").status_code == 200

    def test_onboard_results_renders(self, portal_client):
        client, _, aid = portal_client
        assert client.get(f"/assessments/{aid}/onboard-results").status_code == 200

    def test_remediations_renders(self, portal_client):
        client, _, aid = portal_client
        assert client.get(f"/assessments/{aid}/remediations").status_code == 200

    def test_slos_renders(self, portal_client):
        client, _, aid = portal_client
        assert client.get(f"/assessments/{aid}/slos").status_code == 200

    def test_slos_progress_bar_direction_for_lower_is_better_metrics(self, portal_client):
        """Regression: for lower-is-better metrics (error_rate, latency),
        the progress bar previously used current/target directly, so a
        HEALTHY (low) current_value rendered as a near-empty RED bar and a
        BREACHED (high) current_value rendered as a full GREEN bar --
        exactly backwards."""
        client, store, aid = portal_client
        healthy_sid = store.save_slo(aid, "error_rate", 0.5)
        store.update_slo(healthy_sid, 0.05, "met")  # well under target -> healthy
        breached_sid = store.save_slo(aid, "error_rate", 0.5)
        store.update_slo(breached_sid, 5.0, "breached")  # well over target -> breached

        resp = client.get(f"/assessments/{aid}/slos")
        assert resp.status_code == 200
        rows = re.findall(r"<tr[^>]*>.*?</tr>", resp.text, re.DOTALL)

        healthy_row = next(r for r in rows if "0.05" in r)
        assert "score-green" in healthy_row, healthy_row
        assert "score-red" not in healthy_row

        breached_row = next(r for r in rows if "5.00" in r)
        assert "score-red" in breached_row, breached_row
        assert "score-green" not in breached_row

    def test_404_page(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/nonexistent-xyz").status_code == 404

    def test_all_pages_have_css(self, portal_client):
        client, _, aid = portal_client
        for page in ["/", "/assess", f"/assessments/{aid}", "/events", "/gates",
                     "/agents", "/settings", "/workflows", "/health"]:
            resp = client.get(page)
            assert resp.status_code == 200, f"{page} returned {resp.status_code}"
            assert "<style" in resp.text, f"{page} missing CSS"
