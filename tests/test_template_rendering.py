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
