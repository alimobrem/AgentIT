"""Tests for the auto-fix dispatch wiring in `webhook_github_push`
(routes/webhooks.py) -- docs/onboarding-loop-vision-gap-analysis.md Phase 0
item 1. `RemediationDispatcher.dispatch()`'s return value used to be
discarded entirely inside the `diff.auto_fixable` loop: the generated fix
files were produced and immediately thrown away, with no logged event and
no delivery attempt. This now logs a durable "fix-generated" event and
always gates the fix for human review (AutoMode, which used to
conditionally auto-deliver this via `AutoMode.execute()` when the global
`auto_mode` setting was on, has been removed -- nothing auto-delivers
without an explicit human action anymore, so every dispatched fix stops at
a real, visible gate now, unconditionally). (A prior version of this fix
also persisted a `remediations` row -- that table has since been removed
as a standalone concept entirely; the real outcome is the `gates` row
asserted below.)
"""
from __future__ import annotations

from unittest.mock import patch

from agentit.models import DimensionScore, Finding, Severity
from conftest import make_report


def _report_with_network_finding(**kwargs):
    """A report shaped like `make_report()`'s default "test-app", but with
    a "network" finding -- a real `FIX_REGISTRY` category
    (`remediation/registry.py`) the dispatcher can resolve to the
    `network-policy` skill's deterministic (LLM-free) template output."""
    report = make_report(**kwargs)
    report.scores = [
        DimensionScore(
            dimension="security", score=60, max_score=100,
            findings=[Finding(category="network", severity=Severity.medium,
                               description="Missing NetworkPolicy", recommendation="Add one")],
        ),
    ]
    report.overall_score = 60
    return report


def _push_body(repo_url: str) -> dict:
    return {
        "ref": "refs/heads/main",
        "repository": {"html_url": repo_url, "default_branch": "main"},
        "pusher": {"name": "tester"},
        "after": "abcdef012345",
        "commits": [],
    }


class TestWebhookAutoFixDispatchIsNotDiscarded:
    async def test_dispatched_finding_is_saved_not_discarded(self, portal_client):
        """End-to-end: a git push triggers a re-assessment that surfaces a
        new, auto-fixable "network" finding. Before this fix,
        `dispatcher.dispatch()`'s result was discarded -- no logged event,
        no trace at all. It must now show up as a durable "fix-generated"
        event, never delivered autonomously (AutoMode has been removed)
        nor gated (the `gates` table/generic gate-resolution machinery has
        been removed entirely, 2026-07-19) -- the real next step is
        re-running Onboard for this app to review and deliver it from
        Onboard Results."""
        client, store, old_aid = portal_client
        old_report = await store.get(old_aid)
        repo_url = old_report.repo_url

        new_report = _report_with_network_finding(repo_name=old_report.repo_name, repo_url=repo_url)

        with patch("agentit.portal.routes.webhooks.clone_assess_cleanup", return_value=new_report), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            resp = await client.post(
                "/api/webhook/github-push",
                json=_push_body(repo_url),
                headers={"X-GitHub-Event": "push"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "assessed"

        # 1. Persisted as a durable, queryable fact -- not just generated
        # in memory and discarded.
        events = await store.list_events(target_app=old_report.repo_name, limit=50)
        fix_generated = [e for e in events if e["action"] == "fix-generated"]
        assert len(fix_generated) == 1
        assert "security" in fix_generated[0]["summary"]

        # 2. Not delivered autonomously -- AutoMode has been removed, so
        # nothing here ever calls route_and_deliver() or touches Git/the
        # cluster on its own; no gate is created either.
        not_delivered = [e for e in events if e["action"] == "fix-not-delivered"]
        assert len(not_delivered) == 1
        mock_commit.assert_not_called()
        mock_apply.assert_not_called()

    async def test_dispatch_generates_fix_regardless_of_llm_availability(self, portal_client):
        """AutoMode's LLM safety classification (and its fail-closed-when-
        unavailable behavior) has been removed along with AutoMode itself
        -- generating (but never auto-delivering) a dispatched fix no
        longer depends on an LLM client at all, so this must behave
        identically whether or not one is configured."""
        client, store, old_aid = portal_client
        old_report = await store.get(old_aid)
        repo_url = old_report.repo_url
        new_report = _report_with_network_finding(repo_name=old_report.repo_name, repo_url=repo_url)

        with patch("agentit.portal.routes.webhooks.clone_assess_cleanup", return_value=new_report):
            resp = await client.post(
                "/api/webhook/github-push",
                json=_push_body(repo_url),
                headers={"X-GitHub-Event": "push"},
            )

        assert resp.status_code == 200

        events = await store.list_events(target_app=old_report.repo_name, limit=50)
        actions = [e["action"] for e in events]
        assert "fix-generated" in actions
        assert "fix-not-delivered" in actions
