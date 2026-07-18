"""Tests for the final link in the onboarding-loop vision
(docs/onboarding-loop-vision-gap-analysis.md Phase 3): once an onboarding
job's manifests are saved, automatically run Dry Run and, if it passes,
Deliver -- so the full chain becomes Assess -> Onboard -> Dry Run ->
Deliver (PR opened) with zero human clicks, ending at a real, un-skippable
checkpoint (a PR is open, waiting for human merge).

Direct Apply has been removed entirely (90b95b2..423b508) -- every
auto-chained delivery lands on the GitOps commit+PR mechanism (or a hard
``MECHANISM_NONE`` refusal), never a live cluster mutation, so these tests
never need to mock ``kube.apply_yaml``/``apply_manifests_to_cluster`` for
the cluster-config category the way pre-removal tests did.

Covers:
  - The chain is opt-outable, not silent (mirrors f215d13's
    ``continue_onboard`` convention) -- default on, visible "will run
    automatically" messaging while onboarding is in progress.
  - A clean run auto-chains through Dry Run -> Deliver end to end (mocked
    GitHub PR creation) and lands the job on a real terminal state.
  - A failing Dry Run halts the chain before any real delivery is
    attempted -- Dry Run stays a real, respected gate.
  - The secret-block and placeholder-guard apply identically whether
    Deliver is triggered by a human or by this automatic chain.
  - The onboarding SSE progress mechanism reflects the new Dry Run /
    Delivering stages, not just "onboarding done".
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from agentit.portal.routes import assessments
from conftest import make_report, make_store, prime_csrf


def _cluster_config_file(path: str = "netpol.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": "network policy",
    }


def _secret_file(path: str = "db-secret.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ndata:\n  password: c2VjcmV0\n",
        "description": "should never be delivered",
    }


def _placeholder_file(path: str = "cost-cronjob.yaml") -> dict:
    return {
        "category": "cost",
        "path": path,
        "content": (
            "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: cost\n"
            "spec:\n  jobTemplate:\n    spec:\n      template:\n        spec:\n"
            "          containers:\n          - name: job\n"
            "            image: REPLACE_WITH_AGENTIT_IMAGE\n"
        ),
        "description": "unresolved image placeholder",
    }


_ORCH_SUMMARY = {"agents": [], "conflicts": [], "recommendation": "READY", "auto_approve": False, "gates": []}

# Most of this file's scenarios exercise the chain from the point manifests
# are already generated -- they need the orchestrator's own plan to have
# called this batch auto-approvable (`AutoMode.should_auto_apply()`'s
# "orchestrator says no auto_approve -> gate" rule, same as every other
# AutoMode caller) to reach Dry Run/Deliver at all now that the safety
# check below gates entry into the chain.
_ORCH_SUMMARY_AUTO_APPROVE = {**_ORCH_SUMMARY, "auto_approve": True}


def _safe_llm() -> MagicMock:
    llm = MagicMock()
    llm.classify_action.return_value = {
        "is_destructive": False, "confidence": 0.95, "reason": "Adds a NetworkPolicy",
    }
    return llm


def _destructive_llm() -> MagicMock:
    llm = MagicMock()
    llm.classify_action.return_value = {
        "is_destructive": True, "confidence": 0.95, "reason": "Deletes the production namespace",
    }
    return llm


def _low_confidence_llm() -> MagicMock:
    llm = MagicMock()
    llm.classify_action.return_value = {
        "is_destructive": False, "confidence": 0.4, "reason": "Unsure what this manifest does",
    }
    return llm


@pytest.fixture
async def auto_deliver_client():
    store = await make_store()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
        await prime_csrf(client)
        with patch("agentit.portal.app.get_store", return_value=store), \
             patch("agentit.portal.routes.assessments.get_store", return_value=store):
            yield client, store


async def _seed_assessment(store, *, repo_name: str = "auto-chain-app", infra_repo_url: str | None = None) -> str:
    report = make_report(repo_name=repo_name)
    report.infra_repo_url = infra_repo_url
    return await store.save(report)


class TestAutoDeliverIsOptOutableNotSilent:
    """Mirrors f215d13's continue_onboard convention exactly: on by
    default, but a caller can still explicitly opt out, and the opt-out
    mechanism stays available even though no shipped caller uses it."""

    async def test_onboard_submit_defaults_to_auto_deliver_on(self, auto_deliver_client):
        client, store = auto_deliver_client
        aid = await _seed_assessment(store)

        resp = await client.post(f"/assessments/{aid}/onboard", data={}, follow_redirects=False)
        assert resp.status_code == 303
        job_id = resp.headers["location"].rsplit("/", 1)[1]
        job = await store.get_remediation_job(job_id)
        assert "auto_deliver" in job["steps_completed"]

    async def test_explicit_opt_out_still_works(self, auto_deliver_client):
        client, store = auto_deliver_client
        aid = await _seed_assessment(store)

        resp = await client.post(
            f"/assessments/{aid}/onboard", data={"auto_deliver": "0"}, follow_redirects=False,
        )
        job_id = resp.headers["location"].rsplit("/", 1)[1]
        job = await store.get_remediation_job(job_id)
        assert "auto_deliver" not in job["steps_completed"]

        # The opted-out job must reach plain "completed" -- never attempt a
        # chain, never touch route_and_deliver at all.
        with patch("agentit.portal.delivery.route_and_deliver") as mock_route:
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", False)
        mock_route.assert_not_called()
        job = await store.get_remediation_job(job_id)
        assert job["status"] == "completed"
        deliveries = await store.list_deliveries(aid)
        assert deliveries == []

    async def test_progress_page_shows_automatic_chaining_message(self, auto_deliver_client):
        """Requirement: the user must see something like "Dry Run and
        Deliver will run automatically" while onboarding is in progress --
        not be surprised by a PR appearing with no warning."""
        client, store = auto_deliver_client
        aid = await _seed_assessment(store, repo_name="auto-chain-message-app")
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.update_remediation_job(job_id, "running", "Running onboarding agents...")

        resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}")
        assert resp.status_code == 200
        assert "will run automatically" in resp.text
        assert "Dry Run" in resp.text

    async def test_opted_out_progress_page_shows_no_automatic_chaining_message(self, auto_deliver_client):
        """Regression guard: the old (opted-out) 3-stage flow must not
        claim automation that isn't happening."""
        client, store = auto_deliver_client
        aid = await _seed_assessment(store, repo_name="auto-chain-no-message-app")
        job_id = await store.create_remediation_job(aid, auto_deliver=False)
        await store.update_remediation_job(job_id, "running", "Running onboarding agents...")

        resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}")
        assert resp.status_code == 200
        assert "will run automatically" not in resp.text


class TestFullAutoChainSucceeds:
    """End-to-end: onboarding -> automatic Dry Run -> automatic Deliver,
    with GitHub PR creation mocked at the boundary (per the task's
    explicit instruction not to hit a real GitHub API in tests)."""

    async def test_clean_run_reaches_completed_with_a_real_pr_and_gate(self, auto_deliver_client):
        client, store = auto_deliver_client
        aid = await _seed_assessment(
            store, repo_name="auto-chain-success-app",
            infra_repo_url="https://github.com/org/auto-chain-success-app-gitops",
        )
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.set_setting("auto_mode", "true")

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_safe_llm()), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset") as mock_ensure:
            mock_commit.return_value = {
                "pr_url": "https://github.com/org/auto-chain-success-app-gitops/pull/7",
                "commit_url": "https://github.com/org/auto-chain-success-app-gitops/commit/cafef00d",
                "files_committed": 1,
            }
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        # Dry Run happened first, then the real commit -- never skipped.
        mock_commit.assert_called_once()
        mock_ensure.assert_called_once()

        job = await store.get_remediation_job(job_id)
        assert job["status"] == "completed"
        assert "pull/7" in job["current_step"]

        deliveries = await store.list_deliveries(aid)
        # One delivery row for the dry run, one for the real commit.
        assert len(deliveries) == 2
        real_delivery = next(d for d in deliveries if not d["details"]["dry_run"])
        assert real_delivery["status"] == "delivered"

        gates = await store.list_gates(status="pending")
        assert any(g["gate_type"] == "gitops-pr-pending" and g["assessment_id"] == aid for g in gates)

        events = await store.list_events(target_app="auto-chain-success-app")
        assert any(e["action"] == "onboard-auto-delivered" for e in events)

    async def test_progress_get_redirects_to_onboard_results_with_pr_flash(self, auto_deliver_client):
        """Requirement 5: once the chain finishes (PR opened), a human
        lands on Onboard Results with the same PR flash a manual "Commit &
        Open PR" click would produce -- not silently reliant on Delivery
        History alone."""
        client, store = auto_deliver_client
        aid = await _seed_assessment(
            store, repo_name="auto-chain-flash-app",
            infra_repo_url="https://github.com/org/auto-chain-flash-app-gitops",
        )
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.set_setting("auto_mode", "true")

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_safe_llm()), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.return_value = {
                "pr_url": "https://github.com/org/auto-chain-flash-app-gitops/pull/9",
                "commit_url": "https://github.com/org/auto-chain-flash-app-gitops/commit/deadbeef",
                "files_committed": 1,
            }
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}", follow_redirects=False)
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert f"/assessments/{aid}/onboard-results" in location
        assert "pr_url=" in location
        assert "pr_url_repo=gitops" in location

        flash = await client.get(location)
        assert "PR opened against the GitOps repo" in flash.text
        assert "pull/9" in flash.text


class TestFailingDryRunHaltsTheChain:
    """Dry Run is a real, respected gate -- if it fails, the chain must
    stop before any real delivery is attempted."""

    async def test_no_infra_repo_blocks_before_any_real_commit_is_attempted(self, auto_deliver_client):
        client, store = auto_deliver_client
        # No infra_repo_url at all -- resolve_cluster_config_mechanism()
        # can only produce MECHANISM_NONE, a real dry-run "error" outcome.
        aid = await _seed_assessment(store, repo_name="auto-chain-dryfail-app", infra_repo_url=None)
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.set_setting("auto_mode", "true")

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_safe_llm()), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        mock_commit.assert_not_called()

        job = await store.get_remediation_job(job_id)
        assert job["status"] == "dry_run_failed"
        assert "Dry Run" in job["current_step"]
        assert job["error"]

        # No gate, no gitops-pr-pending -- nothing was ever delivered.
        gates = await store.list_gates(status="pending")
        assert not any(g["assessment_id"] == aid for g in gates)

        events = await store.list_events(target_app="auto-chain-dryfail-app")
        assert any(e["action"] == "onboard-auto-deliver-blocked" for e in events)

    async def test_progress_get_redirects_to_onboard_results_not_assessment_detail(self, auto_deliver_client):
        """Manifests already exist by this point -- a halted chain must
        land a human on Onboard Results (where they can retry), never
        bounced back to Assessment Detail the way a pre-manifest
        onboarding failure does."""
        client, store = auto_deliver_client
        aid = await _seed_assessment(store, repo_name="auto-chain-dryfail-redirect-app", infra_repo_url=None)
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.set_setting("auto_mode", "true")

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_safe_llm()):
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}", follow_redirects=False)
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert f"/assessments/{aid}/onboard-results" in location
        assert "error=" in location

        flash = await client.get(location)
        assert flash.status_code == 200
        assert "manifest-card" in flash.text  # the generated manifest is still shown

    async def test_deliver_stage_failure_also_halts_without_masking_the_dry_run_pass(self, auto_deliver_client):
        """A real (non-dry-run) commit failure must land the job on
        deliver_failed, distinct from a dry_run_failed -- Dry Run passing
        must not be silently overwritten by a later failure."""
        client, store = auto_deliver_client
        aid = await _seed_assessment(
            store, repo_name="auto-chain-deliverfail-app",
            infra_repo_url="https://github.com/org/auto-chain-deliverfail-app-gitops",
        )
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.set_setting("auto_mode", "true")

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_safe_llm()), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            mock_commit.return_value = {"error": "GitHub API error: 500 Internal Server Error"}
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        job = await store.get_remediation_job(job_id)
        assert job["status"] == "deliver_failed"
        assert "Deliver" in job["current_step"]
        assert "500" in job["error"]


class TestSecretAndPlaceholderGuardsApplyAutomatically:
    """Requirement 3: the secret-block and placeholder-guard already run
    inside route_and_deliver() regardless of caller -- prove they still
    correctly block/strip problematic content when Deliver is triggered by
    the automatic chain, not just on a manual click."""

    async def test_secret_and_placeholder_files_never_reach_the_real_commit(self, auto_deliver_client):
        client, store = auto_deliver_client
        aid = await _seed_assessment(
            store, repo_name="auto-chain-guard-app",
            infra_repo_url="https://github.com/org/auto-chain-guard-app-gitops",
        )
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.set_setting("auto_mode", "true")

        files = [_secret_file(), _placeholder_file(), _cluster_config_file(path="clean-netpol.yaml")]
        with patch.object(assessments, "_run_onboarding", return_value=(files, _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_safe_llm()), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.return_value = {
                "pr_url": "https://github.com/org/auto-chain-guard-app-gitops/pull/11",
                "commit_url": "https://github.com/org/auto-chain-guard-app-gitops/commit/f00dcafe",
                "files_committed": 1,
            }
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        # Only the clean file was ever handed to the real commit call --
        # twice (once for the dry run's classification pass is a no-op,
        # once for the real commit) -- the secret/placeholder files never
        # appear in either.
        mock_commit.assert_called_once()
        committed_paths = {f["path"] for f in mock_commit.call_args[0][2]}
        assert committed_paths == {"clean-netpol.yaml"}
        assert "db-secret.yaml" not in committed_paths
        assert "cost-cronjob.yaml" not in committed_paths

        job = await store.get_remediation_job(job_id)
        assert job["status"] == "completed"

        # The dry run's own preview (the first of the two route_and_deliver()
        # calls) is the other half of "the guard applies before any commit
        # is even attempted" -- its outcome only ever lists the clean file.
        deliveries = await store.list_deliveries(aid)
        dry_run_delivery = next(d for d in deliveries if d["details"]["dry_run"])
        assert dry_run_delivery["details"]["outcomes"]["cluster_config"]["files"] == ["clean-netpol.yaml"]


class TestAutoModeSafetyCheckGatesOnboarding:
    """Closes the reinstated-safety-check gap: onboarding's own auto-deliver
    chain used to call `auto_dry_run_then_deliver()` with zero LLM review at
    all -- unlike the vuln-watcher/webhook auto-remediation paths, which
    always call `AutoMode.should_auto_apply()`/`classify_action` first. A
    low-confidence or destructive classification must fall back to
    requiring an explicit human Deliver click (never fail onboarding
    itself -- the manifests already exist); a safe, high-confidence one
    must still auto-deliver exactly as before."""

    async def test_destructive_classification_does_not_auto_deliver(self, auto_deliver_client):
        client, store = auto_deliver_client
        aid = await _seed_assessment(
            store, repo_name="auto-chain-destructive-app",
            infra_repo_url="https://github.com/org/auto-chain-destructive-app-gitops",
        )
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.set_setting("auto_mode", "true")

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_destructive_llm()), \
             patch("agentit.portal.delivery.route_and_deliver") as mock_route:
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        # The classification alone is enough to stop the chain -- Dry Run/
        # Deliver (route_and_deliver) must never even be attempted.
        mock_route.assert_not_called()

        job = await store.get_remediation_job(job_id)
        assert job["status"] == "gated_for_review"
        assert "destructive" in job["current_step"]

        deliveries = await store.list_deliveries(aid)
        assert deliveries == []

        events = await store.list_events(target_app="auto-chain-destructive-app")
        assert any(e["action"] == "onboard-auto-deliver-gated" for e in events)

    async def test_low_confidence_classification_does_not_auto_deliver(self, auto_deliver_client):
        client, store = auto_deliver_client
        aid = await _seed_assessment(
            store, repo_name="auto-chain-lowconf-app",
            infra_repo_url="https://github.com/org/auto-chain-lowconf-app-gitops",
        )
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.set_setting("auto_mode", "true")

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_low_confidence_llm()), \
             patch("agentit.portal.delivery.route_and_deliver") as mock_route:
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        mock_route.assert_not_called()
        job = await store.get_remediation_job(job_id)
        assert job["status"] == "gated_for_review"
        assert "confidence" in job["current_step"]

    async def test_gated_run_still_reaches_onboard_results_with_a_warning_not_an_error(self, auto_deliver_client):
        """Requirement: gating is not a failure -- manifests exist and a
        human can still deliver manually, so the redirect must use the
        warning flash, never the error flash reserved for genuine
        failures."""
        client, store = auto_deliver_client
        aid = await _seed_assessment(
            store, repo_name="auto-chain-gated-redirect-app",
            infra_repo_url="https://github.com/org/auto-chain-gated-redirect-app-gitops",
        )
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.set_setting("auto_mode", "true")

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_destructive_llm()):
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}", follow_redirects=False)
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert f"/assessments/{aid}/onboard-results" in location
        assert "warning=" in location
        assert "error=" not in location

        flash = await client.get(location)
        assert flash.status_code == 200
        assert "manifest-card" in flash.text  # the generated manifest is still shown for a manual Deliver

    async def test_safe_high_confidence_classification_still_auto_delivers(self, auto_deliver_client):
        """The other half of the requirement: a safe classification must
        not regress the existing, already-tested auto-chain behavior."""
        client, store = auto_deliver_client
        aid = await _seed_assessment(
            store, repo_name="auto-chain-safe-app",
            infra_repo_url="https://github.com/org/auto-chain-safe-app-gitops",
        )
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.set_setting("auto_mode", "true")

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_safe_llm()), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.return_value = {
                "pr_url": "https://github.com/org/auto-chain-safe-app-gitops/pull/13",
                "commit_url": "https://github.com/org/auto-chain-safe-app-gitops/commit/f00dcafe",
                "files_committed": 1,
            }
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        mock_commit.assert_called_once()
        job = await store.get_remediation_job(job_id)
        assert job["status"] == "completed"
        assert "pull/13" in job["current_step"]

    async def test_gating_disabled_without_auto_mode_enabled(self, auto_deliver_client):
        """`should_auto_apply()`'s own decision matrix applies here exactly
        as it does for every other AutoMode caller -- the fleet's
        `auto_mode` setting is the one shared kill-switch, so a safe LLM
        classification still gates (never auto-delivers) while it's off,
        matching the vuln-watcher/webhook paths' documented behavior."""
        client, store = auto_deliver_client
        aid = await _seed_assessment(
            store, repo_name="auto-chain-automode-off-app",
            infra_repo_url="https://github.com/org/auto-chain-automode-off-app-gitops",
        )
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        # auto_mode deliberately left at its default (disabled).

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_safe_llm()), \
             patch("agentit.portal.delivery.route_and_deliver") as mock_route:
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        mock_route.assert_not_called()
        job = await store.get_remediation_job(job_id)
        assert job["status"] == "gated_for_review"
        assert "auto-mode is disabled" in job["current_step"]

    async def test_classification_is_logged_to_the_decisions_page_for_onboarding(self, auto_deliver_client):
        """Requirement: this classification must show up on the Decisions
        page exactly like every other AutoMode caller's does -- previously
        the onboarding path never logged a `decision` event at all."""
        import asyncio as _asyncio

        from agentit.llm_decisions import DECISION_TYPE_AUTO_MODE, list_llm_decisions

        client, store = auto_deliver_client
        aid = await _seed_assessment(
            store, repo_name="auto-chain-decision-logged-app",
            infra_repo_url="https://github.com/org/auto-chain-decision-logged-app-gitops",
        )
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.set_setting("auto_mode", "true")

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.routes.assessments.get_llm_client", return_value=_destructive_llm()), \
             patch("agentit.portal.delivery.route_and_deliver"):
            await assessments._run_onboarding_job(job_id, aid, "http://testserver", True)

        # Mirrors routes/insights.py's own call site exactly (list_llm_decisions
        # runs its store calls in a worker thread, bridged back onto this
        # coroutine's event loop -- see llm_decisions.py's `_bridge`).
        loop = _asyncio.get_running_loop()
        decisions = await _asyncio.to_thread(
            list_llm_decisions, store, 500, DECISION_TYPE_AUTO_MODE, "", loop,
        )
        matching = [d for d in decisions if d["target_app"] == "auto-chain-decision-logged-app"]
        assert matching
        assert matching[0]["outcome"] == "gated"
        assert "destructive" in matching[0]["reason"]


class TestProgressStreamReflectsChainStages:
    """Requirement 4: the existing onboarding SSE progress mechanism must
    reflect the Dry Run and Deliver stages too, not end at "onboarding
    done" while something invisible happens next."""

    async def test_stepper_shows_dry_run_stage_while_active(self, auto_deliver_client):
        client, store = auto_deliver_client
        aid = await _seed_assessment(store, repo_name="auto-chain-stepper-dryrun-app")
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.update_remediation_job(job_id, "dry_run", "Running automatic Dry Run...")

        resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}")
        assert resp.status_code == 200
        assert "Dry Run" in resp.text
        assert "Delivering" in resp.text
        assert "Running automatic Dry Run" in resp.text

    async def test_stepper_shows_delivering_stage_while_active(self, auto_deliver_client):
        client, store = auto_deliver_client
        aid = await _seed_assessment(store, repo_name="auto-chain-stepper-deliver-app")
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.update_remediation_job(job_id, "delivering", "Dry Run passed -- committing and opening PR...")

        resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}")
        assert resp.status_code == 200
        assert "committing and opening PR" in resp.text

    async def test_sse_stream_terminates_on_dry_run_failed(self, auto_deliver_client):
        """The SSE polling loop must treat dry_run_failed/deliver_failed as
        terminal -- it must not keep polling forever waiting for a status
        that will never change."""
        client, store = auto_deliver_client
        aid = await _seed_assessment(store, repo_name="auto-chain-sse-terminal-app")
        job_id = await store.create_remediation_job(aid, auto_deliver=True)
        await store.update_remediation_job(
            job_id, "dry_run_failed", "Automatic Dry Run failed", error="Automatic Dry Run failed: boom",
        )

        async with client.stream(
            "GET", f"/assessments/{aid}/onboard/progress/{job_id}/stream",
        ) as resp:
            assert resp.status_code == 200
            body = b"".join([chunk async for chunk in resp.aiter_bytes()])
        text = body.decode()
        assert "event: progress" in text
        assert "onboard-results" in text  # the terminal redirect script fired


class TestOnboardTerminalRedirectUrl:
    """Direct unit coverage for the shared redirect decision
    (_onboard_terminal_redirect_url) -- requirement 5's "decide what the
    final state looks like", made explicit and independently testable."""

    async def test_plain_failed_goes_to_assessment_detail(self, auto_deliver_client):
        _client, store = auto_deliver_client
        aid = await _seed_assessment(store, repo_name="redirect-plain-failed-app")
        job = {"status": "failed", "error": "boom", "steps_completed": []}
        url = await assessments._onboard_terminal_redirect_url(store, aid, job)
        assert url.startswith(f"/assessments/{aid}?error=")
        assert "onboard-results" not in url

    async def test_dry_run_failed_goes_to_onboard_results_with_error(self, auto_deliver_client):
        _client, store = auto_deliver_client
        aid = await _seed_assessment(store, repo_name="redirect-dryfail-app")
        job = {"status": "dry_run_failed", "error": "no infra repo", "steps_completed": ["auto_deliver"]}
        url = await assessments._onboard_terminal_redirect_url(store, aid, job)
        assert url == f"/assessments/{aid}/onboard-results?error=no%20infra%20repo"

    async def test_gated_for_review_goes_to_onboard_results_with_a_warning_not_an_error(self, auto_deliver_client):
        _client, store = auto_deliver_client
        aid = await _seed_assessment(store, repo_name="redirect-gated-app")
        job = {
            "status": "gated_for_review", "error": "", "current_step": "LLM flagged as destructive",
            "steps_completed": ["auto_deliver"],
        }
        url = await assessments._onboard_terminal_redirect_url(store, aid, job)
        assert url == f"/assessments/{aid}/onboard-results?warning=LLM%20flagged%20as%20destructive"

    async def test_completed_without_auto_deliver_is_a_bare_redirect(self, auto_deliver_client):
        _client, store = auto_deliver_client
        aid = await _seed_assessment(store, repo_name="redirect-plain-completed-app")
        job = {"status": "completed", "error": "", "steps_completed": []}
        url = await assessments._onboard_terminal_redirect_url(store, aid, job)
        assert url == f"/assessments/{aid}/onboard-results"

    async def test_completed_with_auto_deliver_and_no_delivery_row_is_a_bare_redirect(self, auto_deliver_client):
        _client, store = auto_deliver_client
        aid = await _seed_assessment(store, repo_name="redirect-auto-nodeliveries-app")
        job = {"status": "completed", "error": "", "steps_completed": ["auto_deliver"]}
        url = await assessments._onboard_terminal_redirect_url(store, aid, job)
        assert url == f"/assessments/{aid}/onboard-results"
