"""Webhook push remediable findings queue gated auto_validate_and_deliver.

Push re-assessment already chains onboard → auto_validate_and_deliver
(finding_gate + clear-evidence; human gate = merge). Remediable
auto_fixable findings must log auto-delivery-queued — not the old
fix-generated / fix-not-delivered dispatcher dead-end.
"""
from __future__ import annotations

from unittest.mock import patch

from agentit.models import DimensionScore, Finding, Severity
from conftest import make_report


def _report_with_network_finding(**kwargs):
    """A report shaped like `make_report()`'s default "test-app", but with
    a "network" finding — remediable auto_pr via SOLUTION_CONTRACTS."""
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


class TestWebhookAutoFixQueuesGatedDelivery:
    async def test_remediable_finding_queues_auto_delivery(self, portal_client):
        """End-to-end: a git push that surfaces a remediable finding queues
        gated auto_validate_and_deliver via the onboard job — never the
        old fix-not-delivered dead-end, and never an autonomous apply."""
        client, store, old_aid = portal_client
        old_report = await store.get(old_aid)
        repo_url = old_report.repo_url

        new_report = _report_with_network_finding(
            repo_name=old_report.repo_name, repo_url=repo_url,
        )

        with patch(
            "agentit.portal.routes.webhooks.clone_assess_cleanup",
            return_value=new_report,
        ), patch(
            "agentit.portal.services.onboard_pipeline._run_onboarding_job",
            return_value=None,
        ), patch(
            "agentit.portal.github_pr.commit_to_infra_repo",
        ) as mock_commit:
            resp = await client.post(
                "/api/webhook/github-push",
                json=_push_body(repo_url),
                headers={"X-GitHub-Event": "push"},
            )

        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

        events = await store.list_events(target_app=old_report.repo_name, limit=50)
        queued = [e for e in events if e["action"] == "auto-delivery-queued"]
        assert len(queued) == 1
        assert "network" in queued[0]["summary"]
        assert "onboard job" in queued[0]["summary"]

        assert not any(e["action"] == "fix-not-delivered" for e in events)
        assert not any(e["action"] == "fix-generated" for e in events)
        mock_commit.assert_not_called()

    async def test_queued_regardless_of_llm_availability(self, portal_client):
        """Queuing gated delivery does not depend on an LLM client."""
        client, store, old_aid = portal_client
        old_report = await store.get(old_aid)
        repo_url = old_report.repo_url
        new_report = _report_with_network_finding(
            repo_name=old_report.repo_name, repo_url=repo_url,
        )

        with patch(
            "agentit.portal.routes.webhooks.clone_assess_cleanup",
            return_value=new_report,
        ), patch(
            "agentit.portal.services.onboard_pipeline._run_onboarding_job",
            return_value=None,
        ):
            resp = await client.post(
                "/api/webhook/github-push",
                json=_push_body(repo_url),
                headers={"X-GitHub-Event": "push"},
            )

        assert resp.status_code == 202
        events = await store.list_events(target_app=old_report.repo_name, limit=50)
        actions = [e["action"] for e in events]
        assert "auto-delivery-queued" in actions
        assert "fix-not-delivered" not in actions
