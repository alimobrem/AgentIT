"""Tests for the `gitops-pr-pending` gate's PR URL rendering as a real,
clickable `<a href>` instead of inert plain text --
docs/onboarding-loop-vision-gap-analysis.md Phase 0 item 2.

`delivery.py`'s `route_and_deliver()` now passes a structured `pr_url` to
`store.create_gate()`, and `_macros.html`'s `gate_card()` macro renders it
as a real anchor tag -- distinct from (and in addition to) `gate.summary`,
which still embeds the URL as plain, auto-escaped text.

2026-07-19: Assessment Detail no longer renders `gate_card()` for a
PR-backed gate type like `gitops-pr-pending` at all (that was the exact
"another Approve and Deliver" duplication reported against that page) --
its `gate_card()`/"View pull request" coverage now lives only on the
fleet-wide Ledger page (`/ledger`), which still uses the macro for these
gates. Assessment Detail's own Ledger tab still shows the same PR as a
real link too, just via its PR history table (pr_tracking.py), not
`gate_card`.
"""
from __future__ import annotations

from conftest import make_report


class TestGateCardRendersRealPrLink:
    async def test_gitops_pr_pending_gate_renders_clickable_pr_link_on_ledger_page(self, portal_client):
        client, store, _seed_aid = portal_client
        report = make_report(repo_name="gitops-pr-link-app")
        aid = await store.save(report)
        pr_url = "https://github.com/org/gitops-infra/pull/42"
        await store.create_gate(
            aid, "gitops-pr-pending",
            "AgentIT will: commit to `https://github.com/org/gitops-infra` and open a PR. "
            f"PR opened: {pr_url}. Approving this gate merges the PR -- AgentIT never auto-merges.",
            pr_url=pr_url,
        )

        resp = await client.get("/ledger")
        assert resp.status_code == 200
        # A real anchor tag pointing at the PR -- not just the bare URL
        # rendered inert as escaped plain text inside gate.summary.
        assert f'<a href="{pr_url}"' in resp.text
        assert "View pull request" in resp.text

    async def test_gitops_pr_pending_gate_renders_clickable_pr_link_on_assessment_ledger_tab(self, portal_client):
        """Assessment Detail's own Ledger tab covers the same PR via its
        real PR history table (not `gate_card`) -- still a real link,
        never a bare/inert URL."""
        client, store, _seed_aid = portal_client
        report = make_report(repo_name="gitops-pr-link-app-2")
        aid = await store.save(report)
        pr_url = "https://github.com/org/gitops-infra/pull/43"
        await store.create_gate(
            aid, "gitops-pr-pending",
            f"AgentIT will: commit to `https://github.com/org/gitops-infra` and open a PR. "
            f"PR opened: {pr_url}. Approving this gate merges the PR -- AgentIT never auto-merges.",
            pr_url=pr_url,
        )

        resp = await client.get(f"/assessments/{aid}?tab=ledger")
        assert resp.status_code == 200
        assert f'<a href="{pr_url}"' in resp.text
        # Not gate_card's own rendering -- that macro no longer runs for
        # this gate type on this page.
        assert "View pull request" not in resp.text

    async def test_gate_without_pr_url_renders_no_pr_link(self, portal_client):
        """Regression guard: every other gate type (no `pr_url` set) must
        not grow a spurious "View pull request" link."""
        client, store, _seed_aid = portal_client
        report = make_report(repo_name="no-pr-link-app")
        aid = await store.save(report)
        await store.create_gate(aid, "auto-mode-review", "Auto-mode gated: low confidence")

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "View pull request" not in resp.text

    async def test_create_gate_persists_structured_pr_url(self, portal_client):
        """Unit-level coverage of the store method itself, independent of
        any template rendering."""
        _client, store, _seed_aid = portal_client
        report = make_report(repo_name="pr-url-persist-app")
        aid = await store.save(report)
        pr_url = "https://github.com/org/gitops-infra/pull/7"
        gate_id = await store.create_gate(aid, "gitops-pr-pending", "PR opened", pr_url=pr_url)

        gates = await store.list_gates_for_assessment(aid)
        gate = next(g for g in gates if g["id"] == gate_id)
        assert gate["pr_url"] == pr_url

    async def test_create_gate_without_pr_url_defaults_to_none(self, portal_client):
        _client, store, _seed_aid = portal_client
        report = make_report(repo_name="no-pr-url-app")
        aid = await store.save(report)
        gate_id = await store.create_gate(aid, "auto-mode-review", "gated")

        gates = await store.list_gates_for_assessment(aid)
        gate = next(g for g in gates if g["id"] == gate_id)
        assert gate["pr_url"] is None
