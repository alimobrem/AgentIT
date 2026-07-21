"""Tests for ``portal/auto_delivery.py`` -- the automatic pre-PR pipeline
that replaces a human manually clicking Dry Run, then Fix, then Dry Run
again, then Deliver. Covers:

- The iterative validate -> fix -> re-validate loop actually retries and
  converges once its own fix genuinely resolves the failure
  (``TestValidateAndFixManifests::test_converges_after_fixing_missing_rbac``).
- It gives up HONESTLY after ``MAX_VALIDATION_ITERATIONS`` -- genuinely
  retrying that many times, never silently declaring success
  (``test_gives_up_honestly_after_max_iterations_when_fix_never_resolves``).
- It never wastes iterations on a failure nothing here can act on, and
  never "fixes" a property that was never actually a finding for this
  assessment (scope-creep guard).
- ``review_final_manifests()`` degrades to "no opinion" (not "rejected")
  when no LLM client is configured.
- ``notify_pr_ready()`` sources PR urls straight from a delivery's own
  ``outcomes``, never from a ``gates`` query.
- ``auto_validate_and_deliver()``'s three honest outcomes: delivered,
  needs_attention (validation never converged), delivery_failed
  (validation converged but the real delivery opened no PR).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agentit.models import DimensionScore, Finding, Severity
from agentit.portal.auto_delivery import (
    MAX_VALIDATION_ITERATIONS,
    auto_validate_and_deliver,
    notify_pr_ready,
    review_final_manifests,
    validate_and_fix_manifests,
)
from conftest import make_report, make_store

# API dry-run (SSA dryRun=All) is hermetic-offline in the suite; these tests
# assert property/GitOps-registration behavior, not kube connectivity.
_CLEAN_DRY_RUN = {
    "applied": ["app-config.yaml"], "skipped": [], "errors": [],
    "conflicts": [], "missing_operators": {}, "repo_files": [],
}


@pytest.fixture(autouse=True)
def _mock_api_dry_run_success():
    with patch(
        "agentit.portal.cluster_apply.dry_run_manifests_against_cluster",
        return_value=_CLEAN_DRY_RUN,
    ):
        yield


def _configmap_file(path: str = "app-config.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cfg\ndata:\n  a: b\n",
        "description": "app config",
        # Phase A: Scan PRs must map to an open finding (make_report default = "test").
        "finding_addressed": "test",
        "skill_name": "test",
    }


def _report_with_finding(category: str, *, repo_name: str, infra_repo_url: str | None = None):
    report = make_report(
        repo_name=repo_name,
        scores=[DimensionScore(
            dimension="security", score=40, max_score=100,
            findings=[Finding(category=category, severity=Severity.high,
                               description=f"missing {category}", recommendation=f"add {category}")],
        )],
    )
    report.infra_repo_url = infra_repo_url
    return report


class TestValidateAndFixManifests:
    async def test_converges_after_fixing_missing_rbac(self):
        """Uses the REAL RemediationDispatcher/SkillEngine (rbac.md is a
        deterministic, offline template skill -- no LLM/network call), so
        this proves the loop genuinely regenerates a real fix and re-checks
        it, not a mocked/faked "it worked" shortcut."""
        store = await make_store()
        report = _report_with_finding(
            "rbac", repo_name="rbac-fix-app",
            infra_repo_url="https://github.com/org/rbac-fix-app-gitops",
        )
        aid = await store.save(report)

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None):
            result = await validate_and_fix_manifests(
                [_configmap_file()], app_name=report.repo_name, namespace="ns", report=report,
                store=store, assessment_id=aid, actor="tester",
            )

        assert result["clean"] is True
        # Converged on the second look, after the first iteration's fix.
        assert len(result["iterations"]) == 2
        assert result["iterations"][0]["failed_properties"] == ["RBAC"]
        assert result["iterations"][0]["fixed_categories"] == ["RBAC"]
        assert result["iterations"][1]["failed_properties"] == []
        # The real rbac skill's output actually landed in the final batch.
        paths = {f["path"] for f in result["files"]}
        assert any("rbac" in p for p in paths)

    async def test_gives_up_honestly_after_max_iterations_when_fix_never_resolves(self):
        """The dispatcher is mocked to always return a fix that does NOT
        actually satisfy the RBAC property (no ServiceAccount/Role/
        RoleBinding) -- proving the loop retries the full bounded count
        rather than stopping early or faking convergence."""
        store = await make_store()
        report = _report_with_finding(
            "rbac", repo_name="rbac-never-fixed-app",
            infra_repo_url="https://github.com/org/rbac-never-fixed-app-gitops",
        )
        aid = await store.save(report)

        bogus_fix = {
            "files": [{
                "category": "security", "path": "bogus-rbac.yaml",
                "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: not-actually-rbac\n",
                "description": "does not satisfy the RBAC property",
            }],
            "agent": "security", "method": "rbac", "error": None,
        }

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.remediation.dispatcher.RemediationDispatcher.dispatch", return_value=bogus_fix) as mock_dispatch:
            result = await validate_and_fix_manifests(
                [_configmap_file()], app_name=report.repo_name, namespace="ns", report=report,
                store=store, assessment_id=aid, actor="tester",
            )

        assert result["clean"] is False
        assert len(result["iterations"]) == MAX_VALIDATION_ITERATIONS
        assert mock_dispatch.call_count == MAX_VALIDATION_ITERATIONS
        assert "RBAC" in result["remaining_issues"]
        # Every iteration genuinely re-checked and genuinely re-attempted a
        # fix -- never a single "tried once, gave up" shortcut.
        assert all(it["fixed_categories"] == ["RBAC"] for it in result["iterations"])
        assert all(it["failed_properties"] == ["RBAC"] for it in result["iterations"])

    async def test_stops_early_when_nothing_can_be_fixed(self):
        """No infra repo known at all -- a structural dry-run error no
        regeneration can address. The loop must not waste the remaining
        iterations retrying something nothing here can act on."""
        store = await make_store()
        report = make_report(repo_name="no-infra-app")
        aid = await store.save(report)

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None):
            result = await validate_and_fix_manifests(
                [_configmap_file()], app_name=report.repo_name, namespace="ns", report=report,
                store=store, assessment_id=aid, actor="tester",
            )

        assert result["clean"] is False
        assert len(result["iterations"]) == 1
        assert any("GitOps" in e for e in result["iterations"][0]["dry_run_errors"])

    async def test_never_fixes_a_property_that_was_not_a_real_finding(self):
        """property_verifier checks all four properties unconditionally --
        this assessment never flagged RBAC (or any of the other three) as a
        finding, so the loop must not silently inject an unrequested fix
        into every onboarding, AND must not treat an irrelevant, never-
        going-to-be-fixed property as blocking convergence: this onboarding
        never claimed to guarantee RBAC/HPA/NetworkPolicy/monitoring in the
        first place, so their absence isn't something this validation
        should hold up a PR for."""
        store = await make_store()
        report = make_report(repo_name="no-rbac-finding-app")  # only a generic "test" finding
        report.infra_repo_url = "https://github.com/org/no-rbac-finding-app-gitops"
        aid = await store.save(report)

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.remediation.dispatcher.RemediationDispatcher.dispatch") as mock_dispatch:
            result = await validate_and_fix_manifests(
                [_configmap_file()], app_name=report.repo_name, namespace="ns", report=report,
                store=store, assessment_id=aid, actor="tester",
            )

        mock_dispatch.assert_not_called()
        assert result["clean"] is True
        assert len(result["iterations"]) == 1
        assert result["iterations"][0]["failed_properties"] == []


class TestReviewFinalManifests:
    async def test_returns_none_without_llm_client(self):
        report = make_report(repo_name="no-llm-app")
        result = await review_final_manifests(None, [_configmap_file()], report)
        assert result is None

    async def test_delegates_to_llm_client(self):
        report = make_report(repo_name="llm-app")

        class _FakeLLM:
            def review_final_manifests(self, files, app_summary):
                assert len(files) == 1
                assert "llm-app" in app_summary
                return {"approved": False, "confidence": 0.4, "reason": "looks incomplete", "concerns": ["x"]}

        result = await review_final_manifests(_FakeLLM(), [_configmap_file()], report)
        assert result == {"approved": False, "confidence": 0.4, "reason": "looks incomplete", "concerns": ["x"]}


class TestNotifyPrReady:
    async def test_sources_pr_urls_from_outcomes_not_gates(self):
        store = await make_store()
        report = make_report(repo_name="notify-app")
        aid = await store.save(report)

        delivery = {
            "delivery_id": "d1",
            "outcomes": {
                "cluster_config": {"mechanism": "infra-repo-commit", "dry_run": False,
                                    "pr_url": "https://github.com/org/infra/pull/9",
                                    "commit_url": "https://github.com/org/infra/commit/abc"},
            },
        }
        pr_urls = await notify_pr_ready(store, report.repo_name, aid, delivery, review=None)

        assert pr_urls == ["https://github.com/org/infra/pull/9"]
        # The `gates` table/generic gate-resolution machinery has been
        # removed entirely (2026-07-19) -- this signal is sourced purely
        # from the delivery's own outcomes, confirmed above.
        events = await store.list_events_by_correlation_id(aid)
        assert any(e["action"] == "onboarding-pr-ready" for e in events)

    async def test_returns_empty_list_when_no_pr_was_opened(self):
        store = await make_store()
        report = make_report(repo_name="notify-empty-app")
        aid = await store.save(report)

        delivery = {"delivery_id": "d2", "outcomes": {"cluster_config": {"error": "boom"}}}
        pr_urls = await notify_pr_ready(store, report.repo_name, aid, delivery, review=None)
        assert pr_urls == []


class TestAutoValidateAndDeliver:
    async def test_delivers_and_notifies_when_clean(self):
        store = await make_store()
        report = make_report(repo_name="deliver-app")
        report.infra_repo_url = "https://github.com/org/deliver-app-gitops"
        aid = await store.save(report)

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo",
                   return_value={"pr_url": "https://github.com/org/deliver-app-gitops/pull/1",
                                 "commit_url": "https://github.com/org/deliver-app-gitops/commit/abc",
                                 "files_committed": 1}), \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            result = await auto_validate_and_deliver(
                store=store, report=report, app_name=report.repo_name, namespace="ns",
                assessment_id=aid, actor="auto-delivery", files=[_configmap_file()],
                orchestration={},
                target_findings=[("test", "minor")],
            )

        assert result["status"] == "delivered"
        assert result["pr_urls"] == ["https://github.com/org/deliver-app-gitops/pull/1"]
        # No LLM configured in tests (_hermetic_llm_env) -- degrades to "no
        # opinion", never treated as a rejection.
        assert result["review"] is None

        saved = await store.get_onboarding(aid)
        assert saved is not None

    async def test_needs_attention_never_calls_real_deliver(self):
        """When validation can't converge, the real (non-dry-run) delivery
        must never be attempted -- an honest stop, not a partial/blind
        delivery attempt."""
        store = await make_store()
        report = make_report(repo_name="stuck-app")  # no infra_repo_url -- can never converge
        aid = await store.save(report)

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            result = await auto_validate_and_deliver(
                store=store, report=report, app_name=report.repo_name, namespace="ns",
                assessment_id=aid, actor="auto-delivery", files=[_configmap_file()],
                orchestration={},
            )

        assert result["status"] == "needs_attention"
        assert "reason" in result and result["reason"]
        mock_commit.assert_not_called()
        # Best-effort manifests are still saved for a human to pick up.
        saved = await store.get_onboarding(aid)
        assert saved is not None

    async def test_delivery_failure_after_convergence_is_reported_honestly(self):
        """Validation converges (no infra-repo/property issues), but the
        real commit call itself fails -- must be reported as
        delivery_failed, never silently treated as success."""
        store = await make_store()
        report = make_report(repo_name="commit-fails-app")
        report.infra_repo_url = "https://github.com/org/commit-fails-app-gitops"
        aid = await store.save(report)

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo",
                   return_value={"error": "GitHub API unavailable"}):
            result = await auto_validate_and_deliver(
                store=store, report=report, app_name=report.repo_name, namespace="ns",
                assessment_id=aid, actor="auto-delivery", files=[_configmap_file()],
                orchestration={},
                target_findings=[("test", "minor")],
            )

        assert result["status"] == "delivery_failed"
        assert "GitHub API unavailable" in result["reason"]

    async def test_self_managed_chart_gate_refused_is_needs_attention(self):
        """#116-shaped raw skill dump into chart/: gate refuses → needs_attention,
        never delivered / never a green PR-ready path."""
        store = await make_store()
        report = make_report(
            repo_name="agentit",
            repo_url="https://github.com/alimobrem/AgentIT",
        )
        report.infra_repo_url = "https://github.com/alimobrem/agentit-gitops"
        aid = await store.save(report)

        raw_skill_dump = {
            "category": "skills",
            "path": "agentit-pdb.yaml",
            "content": (
                "apiVersion: policy/v1\n"
                "kind: PodDisruptionBudget\n"
                "metadata:\n"
                "  name: agentit-pdb\n"
                "spec:\n"
                "  maxUnavailable: 1\n"
            ),
            "description": "Generated by skill pdb",
            "finding_addressed": "test",
            "skill_name": "pdb",
        }

        with patch("agentit.portal.github_pr.create_source_patch_pr") as mock_source, \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.path_exists_on_default_branch", return_value=False), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None):
            result = await auto_validate_and_deliver(
                store=store, report=report, app_name="agentit", namespace="agentit",
                assessment_id=aid, actor="auto-delivery", files=[raw_skill_dump],
                orchestration={},
                target_findings=[("test", "minor")],
            )

        assert result["status"] == "needs_attention"
        assert (
            "Helm-shaped" in result["reason"]
            or "fleet-style" in result["reason"]
            or "filter dropped" in result["reason"]
            or "forbidden" in result["reason"]
        )
        mock_source.assert_not_called()
        mock_commit.assert_not_called()
        assert "pr_urls" not in result
