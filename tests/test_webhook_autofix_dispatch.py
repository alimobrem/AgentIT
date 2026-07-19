"""Tests for the auto-fix dispatch wiring in `webhook_github_push`
(routes/webhooks.py) -- docs/onboarding-loop-vision-gap-analysis.md Phase 0
item 1. `RemediationDispatcher.dispatch()`'s return value used to be
discarded entirely inside the `diff.auto_fixable` loop: the generated fix
files were produced and immediately thrown away, with no logged event and
no delivery attempt. This now logs a durable "fix-generated" event and
delivers via `AutoMode.execute()`, since this branch only ever runs once
the repo owner has already turned the global `auto_mode` setting on. (A
prior version of this fix also persisted a `remediations` row -- that
table has since been removed as a standalone concept entirely; the real
outcome is the `deliveries`/`gates` rows `AutoMode.execute()` produces,
asserted below.)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

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
    async def test_dispatched_finding_is_saved_and_delivered_not_discarded(self, portal_client):
        """End-to-end: a git push triggers a re-assessment that surfaces a
        new, auto-fixable "network" finding with `auto_mode` on. Before this
        fix, `dispatcher.dispatch()`'s result was discarded -- no logged
        event, no delivery, no trace at all. It must now show up both as a
        durable "fix-generated" event and as a real delivery through the
        unified router (`AutoMode.execute()` -> `route_and_deliver()`), not
        just a generated-and-forgotten file."""
        client, store, old_aid = portal_client
        old_report = await store.get(old_aid)
        repo_url = old_report.repo_url

        await store.set_setting("auto_mode", "true")

        new_report = _report_with_network_finding(repo_name=old_report.repo_name, repo_url=repo_url)
        # Direct Apply has been removed as a concept entirely -- a known
        # infra_repo_url is required for AutoMode's delivery to actually go
        # anywhere (see resolve_cluster_config_mechanism()); this now
        # exercises the GitOps commit+PR path rather than a direct apply.
        new_report.infra_repo_url = "https://github.com/org/infra-gitops"

        safe_llm = MagicMock()
        safe_llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95,
            "reason": "Adds a NetworkPolicy -- not destructive",
        }

        with patch("agentit.portal.routes.webhooks.clone_assess_cleanup", return_value=new_report), \
             patch("agentit.portal.routes.webhooks.get_llm_client", return_value=safe_llm), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"), \
             patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            resp = await client.post(
                "/api/webhook/github-push",
                json=_push_body(repo_url),
                headers={"X-GitHub-Event": "push"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "assessed"
        new_assessment_id = body["assessment_id"]

        # 1. Persisted as a durable, queryable fact -- not just generated
        # in memory and discarded.
        events = await store.list_events(target_app=old_report.repo_name, limit=50)
        fix_generated = [e for e in events if e["action"] == "fix-generated"]
        assert len(fix_generated) == 1
        assert "security" in fix_generated[0]["summary"]

        # 2. Actually delivered through the shared router, not left sitting
        # ungenerated-from-the-router's-perspective: route_and_deliver()
        # always creates a `deliveries` row, which only happens if
        # AutoMode.execute() was really invoked with the dispatched files.
        deliveries = await store.list_deliveries(new_assessment_id)
        assert len(deliveries) == 1
        assert deliveries[0]["app_name"] == old_report.repo_name

        # GitOps-registered -- AutoMode commits to the infra repo and opens
        # a PR (never a direct apply) and never touches the cluster at all.
        mock_commit.assert_called_once()
        mock_apply.assert_not_called()

    async def test_dispatch_still_logs_a_visible_event_even_when_llm_unavailable(self, portal_client):
        """Fail-closed case: with no LLM client available (this suite's
        hermetic default), AutoMode gates for human review instead of
        applying -- but the fix must still be persisted and the outcome
        still logged, never silently dropped the way the pre-fix code
        dropped it unconditionally."""
        client, store, old_aid = portal_client
        old_report = await store.get(old_aid)
        repo_url = old_report.repo_url

        await store.set_setting("auto_mode", "true")
        new_report = _report_with_network_finding(repo_name=old_report.repo_name, repo_url=repo_url)

        with patch("agentit.portal.routes.webhooks.clone_assess_cleanup", return_value=new_report), \
             patch("agentit.portal.routes.webhooks.get_llm_client", return_value=None):
            resp = await client.post(
                "/api/webhook/github-push",
                json=_push_body(repo_url),
                headers={"X-GitHub-Event": "push"},
            )

        assert resp.status_code == 200
        new_assessment_id = resp.json()["assessment_id"]

        events = await store.list_events(target_app=old_report.repo_name, limit=50)
        actions = [e["action"] for e in events]
        assert "fix-generated" in actions
        assert "gated" in actions
