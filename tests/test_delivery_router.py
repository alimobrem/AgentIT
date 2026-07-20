"""Tests for the unified apply flow's router (portal/delivery.py) --
classification into docs/unified-apply-flow.md's taxonomy, GitOps
registration detection, and end-to-end routing through
``route_and_deliver()`` for each category.
"""
from __future__ import annotations

from unittest.mock import patch

from agentit import kube
from agentit.portal.delivery import (
    CATEGORY_CICD_SHARED_NAMESPACE,
    CATEGORY_CLUSTER_CONFIG,
    CATEGORY_MANIFEST_AT_REST,
    CATEGORY_NARRATIVE_REPORT,
    CATEGORY_SECRET_BLOCKED,
    CATEGORY_SOURCE_PATCH,
    MECHANISM_APP_REPO_PR,
    MECHANISM_INFRA_REPO_COMMIT,
    MECHANISM_NONE,
    MECHANISM_SOURCE_REPO_PR,
    DeliveryInProgressError,
    classify_file,
    confirmation_text,
    has_unresolved_placeholders,
    is_gitops_registered,
    resolve_cluster_config_mechanism,
    route_and_deliver,
)
from conftest import make_async_store, make_report


def _cluster_config_file() -> dict:
    return {
        "category": "skills",
        "path": "app-network-policy.yaml",
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": "network policy",
    }


def _cicd_file() -> dict:
    return {
        "category": "skills",
        "path": "pipeline.yaml",
        "content": (
            "apiVersion: tekton.dev/v1\nkind: Pipeline\n"
            "metadata:\n  name: build\n  namespace: openshift-pipelines\n"
        ),
        "description": "tekton pipeline",
    }


def _source_patch_file() -> dict:
    return {
        "category": "codechange",
        "path": "patch-01-Dockerfile",
        "content": "FROM ubi9\n",
        "description": "Dockerfile fix",
        "target_path": "Dockerfile",
    }


def _narrative_report_file() -> dict:
    return {
        "category": "dependency",
        "path": "dependency-report.md",
        "content": "# Dependency report\n",
        "description": "dependency report",
    }


def _manifest_at_rest_file() -> dict:
    return {
        "category": "dependency",
        "path": "renovate.json",
        "content": '{"extends": ["config:base"]}',
        "description": "Renovate config",
    }


def _secret_file() -> dict:
    return {
        "category": "skills",
        "path": "db-secret.yaml",
        "content": "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ndata:\n  password: c2VjcmV0\n",
        "description": "should never be delivered",
    }


def _placeholder_cronjob_file() -> dict:
    return {
        "category": "cost",
        "path": "cost-cronjob.yaml",
        "content": (
            "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: cost\n"
            "spec:\n  jobTemplate:\n    spec:\n      template:\n        spec:\n"
            "          containers:\n          - name: job\n"
            "            image: REPLACE_WITH_AGENTIT_IMAGE\n"
        ),
        "description": "unresolved image placeholder",
    }


class TestConfirmationText:
    """``confirmation_text()``'s dedicated ``MECHANISM_DIRECT_APPLY`` branch
    (which used to name the *target cluster*, not just the *action* --
    see the incident in ``kube.get_current_cluster_identity()``'s
    docstring) was deleted 2026-07-20: every real caller always passes a
    freshly-computed mechanism, never one read back from a stored
    ``deliveries`` row, and ``resolve_cluster_config_mechanism()`` can
    never select ``MECHANISM_DIRECT_APPLY`` as a live outcome."""

    def test_infra_repo_commit_does_not_call_cluster_identity(self):
        """The GitOps-commit path never touches a cluster client at all --
        confirming this never regresses into an unnecessary kube call."""
        with patch("agentit.portal.delivery.kube.get_current_cluster_identity") as mock_identity:
            confirmation_text(MECHANISM_INFRA_REPO_COMMIT, infra_repo_url="https://github.com/org/infra-gitops")
        mock_identity.assert_not_called()


class TestClassifyFile:
    """One test per taxonomy category (docs/unified-apply-flow.md section (D))."""

    def test_cluster_app_config(self):
        assert classify_file(_cluster_config_file()) == CATEGORY_CLUSTER_CONFIG

    def test_cicd_shared_namespace(self):
        assert classify_file(_cicd_file()) == CATEGORY_CICD_SHARED_NAMESPACE

    def test_source_patch_from_codechange_category(self):
        assert classify_file(_source_patch_file()) == CATEGORY_SOURCE_PATCH

    def test_codechange_summary_is_narrative_not_source_patch(self):
        summary = {"category": "codechange", "path": "code-changes-summary.md", "content": "# summary"}
        assert classify_file(summary) == CATEGORY_NARRATIVE_REPORT

    def test_narrative_report_excluded_from_delivery(self):
        assert classify_file(_narrative_report_file()) == CATEGORY_NARRATIVE_REPORT

    def test_manifest_at_rest_for_non_yaml_config(self):
        assert classify_file(_manifest_at_rest_file()) == CATEGORY_MANIFEST_AT_REST

    def test_secret_kind_is_hard_blocked(self):
        assert classify_file(_secret_file()) == CATEGORY_SECRET_BLOCKED

    def test_unparseable_yaml_falls_back_to_manifest_at_rest(self):
        entry = {"category": "skills", "path": "broken.yaml", "content": ": : :not yaml", "description": ""}
        assert classify_file(entry) == CATEGORY_MANIFEST_AT_REST

    def test_missing_category_key_does_not_crash(self):
        """AutoMode's existing tests pass file dicts with no `category` key
        at all -- classify_file must default gracefully, not KeyError."""
        entry = {"path": "x.yaml", "content": "kind: Pod"}
        # `kind: Pod` alone has no apiVersion/metadata -- not a parseable
        # K8s doc via _parse_manifest's own yaml.safe_load_all, so this is
        # manifest_at_rest, not cluster_config. Either way, no crash.
        assert classify_file(entry) in (CATEGORY_CLUSTER_CONFIG, CATEGORY_MANIFEST_AT_REST)


class TestIsGitopsRegistered:
    async def test_falls_back_to_infra_repo_url_when_kube_unreachable(self):
        """Explicitly force the kube call to fail with the same exception
        type a genuinely unreachable cluster raises (`kube.KubeError` --
        `kube.get_custom_resource()` catches the real connection error from
        the `kubernetes`/`urllib3` client and wraps it in this type before
        it ever reaches `is_gitops_registered()`), so this test is
        deterministic regardless of ambient `KUBECONFIG`/cluster
        reachability rather than depending on an invalid `KUBECONFIG` in
        the test environment. Registration then falls back to
        `report.infra_repo_url`."""
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        with patch(
            "agentit.portal.delivery.kube.get_custom_resource",
            side_effect=kube.KubeError("Failed to get argoproj.io/v1alpha1 applications/managed-test-app: connection refused"),
        ):
            registered, url = await is_gitops_registered("test-app", report)
        assert registered is True
        assert url == "https://github.com/org/infra-gitops"

    async def test_not_registered_when_no_report_and_kube_unreachable(self):
        with patch(
            "agentit.portal.delivery.kube.get_custom_resource",
            side_effect=kube.KubeError("Failed to get argoproj.io/v1alpha1 applications/managed-test-app: connection refused"),
        ):
            registered, url = await is_gitops_registered("test-app", None)
        assert registered is False
        assert url is None

    async def test_registered_signal_wins_over_infra_repo_url_when_kube_succeeds(self):
        """A successful kube call that finds no Application means NOT
        registered, even if `infra_repo_url` happens to be set -- the
        design doc's plumbing-gap fix explicitly prefers the live signal."""
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None):
            registered, url = await is_gitops_registered("test-app", report)
        assert registered is False
        assert url == "https://github.com/org/infra-gitops"

    async def test_registered_true_when_application_exists(self):
        report = make_report()
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {"name": "managed-test-app"}}) as mock_get:
            registered, _url = await is_gitops_registered("test-app", report)
        assert registered is True
        mock_get.assert_called_once_with(
            "argoproj.io", "v1alpha1", "applications", "managed-test-app",
            namespace="openshift-gitops",
        )

    async def test_registered_true_for_self_managed_app_with_matching_source(self):
        """Apps that register themselves into their own fleet (e.g. AgentIT
        via `register-self-in-fleet`) are deliberately excluded from the
        shared apps/*-directory ApplicationSet (github_pr.
        ensure_applicationset()) and instead run under a hand-crafted
        Application named for the app itself, not `managed-{app}`. Falls
        back to that literal name and counts it as registered when its
        source repo actually matches this app's own repo."""
        report = make_report(repo_name="test-app", repo_url="https://github.com/org/test-app")
        with patch("agentit.portal.delivery.kube.get_custom_resource") as mock_get:
            mock_get.side_effect = [
                None,  # "managed-test-app" not found
                {"spec": {"source": {"repoURL": "https://github.com/org/test-app.git"}}},
            ]
            registered, _url = await is_gitops_registered("test-app", report)
        assert registered is True
        assert mock_get.call_args_list[1].args == (
            "argoproj.io", "v1alpha1", "applications", "test-app",
        )

    async def test_not_registered_when_same_named_app_source_does_not_match(self):
        """A live Application that merely happens to share the app's name
        (e.g. an unrelated demo Application pointed at a placeholder repo)
        must not be mistaken for this app's own self-managed deployment."""
        report = make_report(repo_name="test-app", repo_url="https://github.com/org/test-app")
        with patch("agentit.portal.delivery.kube.get_custom_resource") as mock_get:
            mock_get.side_effect = [
                None,  # "managed-test-app" not found
                {"spec": {"source": {"repoURL": "https://github.com/someone-else/test-app.git"}}},
            ]
            registered, _url = await is_gitops_registered("test-app", report)
        assert registered is False


class TestResolveClusterConfigMechanism:
    """Direct coverage of the shared decision function every mechanism-
    predicting caller now goes through. Direct Apply has been removed as a
    concept entirely (product directive: all apps must use GitOps, no
    fallback) -- this can never select MECHANISM_DIRECT_APPLY as a live
    outcome. `registered` (whether a live Argo CD Application already
    exists) is no longer even a parameter: knowing an infra repo URL is the
    only thing that ever matters for whether to commit there (see
    docs/onboarding-loop-vision-gap-analysis.md §1's bootstrap-circularity
    fix -- the very first delivery for a known infra repo still commits,
    live-registered or not)."""

    def test_no_infra_repo_refuses_with_no_direct_apply_fallback(self):
        """Only reachable for an assessment saved before GitOps
        registration became mandatory -- refuses outright rather than
        falling back to a direct apply."""
        assert resolve_cluster_config_mechanism(None) == MECHANISM_NONE

    def test_not_yet_registered_with_infra_repo_bootstraps_infra_commit(self):
        """The exact bootstrap case: no live Application yet, but an infra
        repo URL is known -- must commit there, not direct-apply, or the
        app can never reach registered=True at all."""
        assert (
            resolve_cluster_config_mechanism("https://github.com/org/infra-gitops")
            == MECHANISM_INFRA_REPO_COMMIT
        )

    def test_registered_with_infra_repo_commits(self):
        assert (
            resolve_cluster_config_mechanism("https://github.com/org/infra-gitops")
            == MECHANISM_INFRA_REPO_COMMIT
        )


class TestRouteAndDeliverClusterConfig:
    async def test_no_infra_repo_refuses_with_no_direct_apply_fallback(self):
        """Direct Apply has been removed as a concept entirely (including
        cluster_apply.apply_manifests_to_cluster, which no longer exists)
        -- an app with no known infra repo at all (only possible for an
        assessment saved before GitOps registration became mandatory)
        cannot be delivered, full stop. Never falls back to mutating the
        cluster directly."""
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        result = await route_and_deliver(
            [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
            report=report, store=store, assessment_id=aid,
            actor="tester", dry_run=False,
        )
        assert result["registered"] is False
        assert result["mechanisms"]["cluster_config"] == MECHANISM_NONE
        outcome = result["outcomes"]["cluster_config"]
        assert "error" in outcome
        assert "GitOps" in outcome["error"]
        delivery = await raw.get_delivery(result["delivery_id"])
        assert delivery["status"] == "partial"

    async def test_registered_routes_to_infra_repo_commit(self):
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset") as mock_ensure:
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        assert result["registered"] is True
        assert result["mechanisms"]["cluster_config"] == MECHANISM_INFRA_REPO_COMMIT
        mock_commit.assert_called_once()
        mock_ensure.assert_called_once()
        # No gate is created (the `gates` table/generic gate-resolution
        # machinery has been removed entirely, 2026-07-19) -- the PR itself
        # is the only durable record; pr_tracking.py derives "waiting for
        # your approval" from its live GitHub state.
        assert result["outcomes"]["cluster_config"]["pr_url"] == "https://github.com/org/infra-gitops/pull/1"

    async def test_concurrent_deliveries_for_same_app_only_one_proceeds(self):
        """Regression guard for the delivery-commit race: two genuinely
        overlapping `route_and_deliver()` calls for the same app must not
        both reach `commit_to_infra_repo()` concurrently -- see
        `store.claim_delivery_lock()`'s own docstring for the exact race
        this closes (a fixed `agentit/{app}` branch + force-push-on-
        conflict fallback, with no optimistic-concurrency check between
        reading `base_sha` and pushing). `commit_to_infra_repo` sleeps
        briefly here to widen the race window the way the real,
        network-bound GitHub API calls would."""
        import asyncio
        import time

        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)

        def _slow_commit(*args, **kwargs):
            time.sleep(0.3)
            return {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                    "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo", side_effect=_slow_commit) as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            results = await asyncio.gather(
                route_and_deliver(
                    [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                    report=report, store=store, assessment_id=aid,
                    actor="tester", dry_run=False,
                ),
                route_and_deliver(
                    [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                    report=report, store=store, assessment_id=aid,
                    actor="tester", dry_run=False,
                ),
                return_exceptions=True,
            )

        succeeded = [r for r in results if not isinstance(r, Exception)]
        rejected = [r for r in results if isinstance(r, DeliveryInProgressError)]
        assert len(succeeded) == 1
        assert len(rejected) == 1
        # The lock genuinely prevented the second caller from ever reaching
        # the GitHub call -- not just from double-counting a result.
        assert mock_commit.call_count == 1
        assert succeeded[0]["outcomes"]["cluster_config"]["pr_url"] == "https://github.com/org/infra-gitops/pull/1"

    async def test_dry_run_never_takes_the_delivery_lock(self):
        """Dry-run previews must stay instant and available even while a
        real delivery for the same app is in flight -- a dry run never
        commits anything for real (deliver_with_verification()'s own
        `if dry_run: return ...` guard), so there's nothing to race and no
        reason to serialize it behind the same-app lock."""
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)

        # Claim the lock directly, simulating a real delivery already in
        # flight for this app -- a concurrent dry-run call must not be
        # blocked by it.
        assert await raw.claim_delivery_lock(f"delivery:{report.repo_name}") is True

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}):
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=True,
            )
        assert result["outcomes"]["cluster_config"]["dry_run"] is True

    async def test_not_yet_registered_with_infra_repo_url_bootstraps_infra_commit(self):
        """The bootstrap-circularity fix (docs/onboarding-loop-vision-gap-
        analysis.md §1): a brand-new app has `infra_repo_url` set (an
        assessment already ran `_auto_create_infra_repo`/`register_gitops`)
        but no live Argo CD `Application` exists yet, because nothing has
        ever committed `apps/{app}/` into the infra repo for Argo's
        ApplicationSet to discover in the first place. Before this fix,
        `registered=False` here fell through to `MECHANISM_DIRECT_APPLY`
        -- which never commits anything to the infra repo, so the app
        could never reach `registered=True` via this path: a closed loop
        with no escape. It must now route to `MECHANISM_INFRA_REPO_COMMIT`
        instead, bootstrapping `apps/{app}/` so Argo's ApplicationSet can
        finally discover it."""
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset") as mock_ensure:
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        assert result["registered"] is False
        assert result["mechanisms"]["cluster_config"] == MECHANISM_INFRA_REPO_COMMIT
        mock_commit.assert_called_once()
        mock_ensure.assert_called_once()
        # And it still opens the real PR, exactly like the already-
        # registered infra-repo-commit path -- a human still merges,
        # AgentIT still never auto-merges, for the bootstrap delivery too.
        assert result["outcomes"]["cluster_config"]["pr_url"] == "https://github.com/org/infra-gitops/pull/1"

    async def test_placeholder_files_are_not_committed(self):
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            result = await route_and_deliver(
                [_placeholder_cronjob_file(), _cluster_config_file()],
                app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        assert "cost-cronjob.yaml" in result["placeholder_blocked"]
        assert has_unresolved_placeholders(_placeholder_cronjob_file()["content"])
        # Only the non-placeholder cluster_config file is committed.
        committed = mock_commit.call_args[0][2]
        assert [f["path"] for f in committed] == ["app-network-policy.yaml"]

    async def test_dry_run_skips_infra_commit_call(self):
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=True,
            )
        mock_commit.assert_not_called()
        assert result["outcomes"]["cluster_config"]["dry_run"] is True


class TestRouteAndDeliverCicdLane:
    """CI/CD manifests destined for a shared operator namespace (2026-07-18,
    replacing the removed ``cluster-admin-review`` direct-apply gate): now
    delivers via the exact same GitOps-commit-and-gate mechanism as the
    cluster/app-config category, verified live to be within ArgoCD's own
    reconciler RBAC (see the README) -- AgentIT itself never applies
    directly to a cluster for this category (or any other) anymore."""

    async def test_cicd_files_deliver_via_gitops_pr_never_a_direct_apply(self):
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            result = await route_and_deliver(
                [_cicd_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        assert result["mechanisms"][CATEGORY_CICD_SHARED_NAMESPACE] == MECHANISM_INFRA_REPO_COMMIT
        mock_commit.assert_called_once()
        outcome = result["outcomes"][CATEGORY_CICD_SHARED_NAMESPACE]
        assert outcome["pr_url"] == "https://github.com/org/infra-gitops/pull/1"
        # No gate is created anymore (the `gates` table/generic gate-
        # resolution machinery has been removed entirely, 2026-07-19) -- the
        # only durable record of this delivery is the observability event
        # logged by _deliver_via_gitops_pr(); real "waiting for your
        # approval"/merged/rejected status comes from pr_tracking.py's live
        # PR-status derivation instead.
        events = await raw.list_events(target_app=report.repo_name)
        opened_events = [e for e in events if e["action"] == "gitops-pr-opened"]
        assert len(opened_events) == 1
        assert "openshift-pipelines" in opened_events[0]["summary"]
        assert "shared, cluster-wide operator namespace" in opened_events[0]["summary"]
        # Never a direct apply -- this is now a plain GitOps PR merge-review,
        # same as cluster-config's, not an elevated-RBAC apply
        # (cluster_apply.apply_manifests_to_cluster no longer even exists).

    async def test_cicd_commit_uses_a_distinct_path_prefix_and_branch(self):
        """A human reviewer must be able to tell, from the PR/branch alone,
        that this touches a shared cluster-wide namespace, not the app's own
        -- see _CICD_SHARED_NAMESPACE_PATH_PREFIX's rationale. Asserted
        directly against commit_to_infra_repo()'s real call args rather than
        just the gate summary, since a distinct branch is also what
        prevents this commit from clobbering a same-call cluster-config
        commit to the app's own `agentit/{app}` branch (see the mixed-batch
        test below)."""
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            await route_and_deliver(
                [_cicd_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        mock_commit.assert_called_once()
        call_args = mock_commit.call_args[0]
        committed_files, branch_name = call_args[2], call_args[3]
        assert branch_name == f"agentit/{report.repo_name}-cicd-shared-namespace"
        assert all(f["category"] == "cicd-shared-namespace" for f in committed_files)

    async def test_dry_run_never_creates_a_real_commit_or_gate(self):
        """A real Dry Run must stay a pure preview -- no side effects --
        exactly like every other category. Found via the automatic Dry Run
        -> Deliver chain (assessments.py's onboarding auto-chain): a Dry
        Run that now runs unconditionally after every onboarding surfaced
        that this branch had no such guard, unlike every other mechanism
        here -- a "preview" call was silently opening a real PR."""
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            result = await route_and_deliver(
                [_cicd_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=True,
            )
        mock_commit.assert_not_called()
        events = await raw.list_events(target_app=report.repo_name)
        assert not any(e["action"] == "gitops-pr-opened" for e in events)
        outcome = result["outcomes"][CATEGORY_CICD_SHARED_NAMESPACE]
        assert outcome["dry_run"] is True
        assert "pr_url" not in outcome
        assert outcome["files"] == ["pipeline.yaml"]

    async def test_no_infra_repo_refuses_cicd_delivery_with_no_direct_apply_fallback(self):
        """Same refusal as the cluster-config category (see
        resolve_cluster_config_mechanism()): CI/CD manifests destined for a
        shared namespace can no longer fall back to a direct apply either,
        now that the elevated-RBAC direct-apply gate is gone (and
        cluster_apply.apply_manifests_to_cluster no longer even exists) --
        an assessment with no known infra repo at all (only possible pre-
        GitOps-mandatory) must refuse outright, never mutate the cluster."""
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        result = await route_and_deliver(
            [_cicd_file()], app_name=report.repo_name, namespace="ns",
            report=report, store=store, assessment_id=aid,
            actor="tester", dry_run=False,
        )
        assert result["mechanisms"][CATEGORY_CICD_SHARED_NAMESPACE] == MECHANISM_NONE
        outcome = result["outcomes"][CATEGORY_CICD_SHARED_NAMESPACE]
        assert "error" in outcome
        assert "GitOps" in outcome["error"]
        events = await raw.list_events(target_app=report.repo_name)
        assert not any(e["action"] == "gitops-pr-opened" for e in events)

    async def test_cicd_and_cluster_config_lanes_both_deliver_via_gitops_independently(self):
        """The product-owner question this guards against (originally about
        gate independence, now about mechanism independence): a single
        mixed batch containing both a cluster-config file and a cicd-
        shared-namespace file must still route/deliver each independently
        -- two separate commit_to_infra_repo() calls (different branches/
        paths), two separate gitops-pr-pending gates -- not merged into one
        commit (which would risk one clobbering the other, since
        commit_to_infra_repo() force-pushes whatever branch it's given) and
        not short-circuited by each other's registration/commit outcome."""
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {"name": "managed-test-app"}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.side_effect = [
                {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                 "commit_url": "https://github.com/org/infra-gitops/commit/aaa", "files_committed": 1},
                {"pr_url": "https://github.com/org/infra-gitops/pull/2",
                 "commit_url": "https://github.com/org/infra-gitops/commit/bbb", "files_committed": 1},
            ]
            result = await route_and_deliver(
                [_cluster_config_file(), _cicd_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        assert result["registered"] is True
        assert result["mechanisms"][CATEGORY_CLUSTER_CONFIG] == MECHANISM_INFRA_REPO_COMMIT
        assert result["mechanisms"][CATEGORY_CICD_SHARED_NAMESPACE] == MECHANISM_INFRA_REPO_COMMIT
        assert mock_commit.call_count == 2
        # cluster-config passes no explicit branch_name (commit_to_infra_
        # repo()'s own default applies -- not exercised here since the call
        # itself is mocked); cicd-shared-namespace always passes its own
        # distinct, explicit one -- see _CICD_SHARED_NAMESPACE_PATH_PREFIX.
        branches = {call.args[3] for call in mock_commit.call_args_list}
        assert branches == {None, f"agentit/{report.repo_name}-cicd-shared-namespace"}
        # Two distinct, independent PRs -- no gate is created for either
        # (the `gates` table/generic gate-resolution machinery has been
        # removed entirely, 2026-07-19); each PR's own live GitHub state is
        # the only source of truth for "waiting for your approval" now.
        cluster_pr = result["outcomes"][CATEGORY_CLUSTER_CONFIG]["pr_url"]
        cicd_pr = result["outcomes"][CATEGORY_CICD_SHARED_NAMESPACE]["pr_url"]
        assert cluster_pr != cicd_pr
        events = await raw.list_events(target_app=report.repo_name)
        opened_events = [e for e in events if e["action"] == "gitops-pr-opened"]
        assert len(opened_events) == 2
        # Neither branch performed a direct cluster apply
        # (cluster_apply.apply_manifests_to_cluster no longer even exists).

    def test_a_real_current_skill_output_still_classifies_as_cicd_shared_namespace(self):
        """Not a synthetic fixture: this is exactly what
        skills/cicd/argocd-application.md's own template renders (namespace:
        openshift-gitops hardcoded in the skill itself) -- proof that
        CATEGORY_CICD_SHARED_NAMESPACE classification is reachable via a
        real, currently-shipped skill, not dead code nobody can trigger
        anymore."""
        entry = {
            "category": "skills",
            "path": "argocd-application.yaml",
            "content": (
                "apiVersion: argoproj.io/v1alpha1\n"
                "kind: Application\n"
                "metadata:\n"
                "  name: myapp\n"
                "  namespace: openshift-gitops\n"
                "spec:\n"
                "  project: default\n"
            ),
            "description": "Argo CD Application",
        }
        assert classify_file(entry) == CATEGORY_CICD_SHARED_NAMESPACE


class TestRouteAndDeliverSourcePatch:
    async def test_source_patch_routes_to_source_repo_pr_with_target_path(self):
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        with patch("agentit.portal.github_pr.create_source_patch_pr") as mock_pr:
            mock_pr.return_value = {"pr_url": "https://github.com/org/test-app/pull/9", "branch": "agentit/codechange", "files_committed": 1}
            result = await route_and_deliver(
                [_source_patch_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        assert result["mechanisms"][CATEGORY_SOURCE_PATCH] == MECHANISM_SOURCE_REPO_PR
        mock_pr.assert_called_once()
        called_files = mock_pr.call_args[0][2]
        assert called_files[0]["target_path"] == "Dockerfile"


class TestRouteAndDeliverManifestAtRest:
    async def test_non_yaml_config_routes_to_app_repo_pr(self):
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        with patch("agentit.portal.github_pr.create_onboarding_pr") as mock_pr:
            mock_pr.return_value = {"pr_url": "https://github.com/org/test-app/pull/3", "branch": "agentit/onboarding", "files_added": 1}
            result = await route_and_deliver(
                [_manifest_at_rest_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        assert result["mechanisms"][CATEGORY_MANIFEST_AT_REST] == MECHANISM_APP_REPO_PR
        mock_pr.assert_called_once()


class TestRouteAndDeliverSecretsAndNarrative:
    async def test_secret_never_delivered(self):
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        result = await route_and_deliver(
            [_secret_file()], app_name=report.repo_name, namespace="ns",
            report=report, store=store, assessment_id=aid,
            actor="tester", dry_run=False,
        )
        assert result["blocked"] == ["db-secret.yaml"]
        assert result["mechanisms"] == {}
        assert result["outcomes"] == {}

    async def test_narrative_report_excluded_not_delivered(self):
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        result = await route_and_deliver(
            [_narrative_report_file()], app_name=report.repo_name, namespace="ns",
            report=report, store=store, assessment_id=aid,
            actor="tester", dry_run=False,
        )
        assert result["excluded"] == ["dependency-report.md"]
        assert result["mechanisms"] == {}


class TestDeliveriesTracking:
    async def test_delivery_row_created_with_categories_and_mechanism(self):
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        delivery = await raw.get_delivery(result["delivery_id"])
        assert delivery is not None
        assert delivery["assessment_id"] == aid
        assert delivery["app_name"] == report.repo_name
        assert delivery["categories"] == {"cluster_config": 1}
        assert "cluster_config:infra-repo-commit" in delivery["mechanism"]
        assert delivery["status"] == "delivered"
        assert delivery["verification"] == "unknown"

    async def test_list_deliveries_returns_rows_for_assessment(self):
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        deliveries = await raw.list_deliveries(aid)
        assert len(deliveries) == 1

    async def test_update_delivery_merges_details(self):
        store = (await make_async_store())[1]
        report = make_report()
        aid = await store.save(report)
        delivery_id = await store.create_delivery(aid, "app", {"cluster_config": 1}, "direct-apply", details={"a": 1})
        ok = await store.update_delivery(delivery_id, status="verified", verification="verified", details={"b": 2})
        assert ok is True
        d = await store.get_delivery(delivery_id)
        assert d["status"] == "verified"
        assert d["verification"] == "verified"
        assert d["details"] == {"a": 1, "b": 2}

    async def test_update_delivery_returns_false_for_unknown_id(self):
        store = (await make_async_store())[1]
        assert await store.update_delivery("nonexistent", status="verified") is False

    async def test_list_pending_gitops_deliveries_filters_by_mechanism_and_verification(self):
        store = (await make_async_store())[1]
        report = make_report()
        aid = await store.save(report)
        gitops_id = await store.create_delivery(aid, "app", {}, "infra-repo-commit")
        await store.create_delivery(aid, "app", {}, "direct-apply")
        pending = await store.list_pending_gitops_deliveries()
        assert [d["id"] for d in pending] == [gitops_id]

    async def test_delivery_records_edited_files_for_traceability(self):
        """The edit-before-apply flow's delivered-content traceability
        requirement: a file carrying the `edited` flag
        (`await store.update_onboarding_file()` sets this) must show up in the
        delivery row's `details.edited_files`, a permanent, queryable fact
        about what was actually delivered vs. what was originally
        generated -- not just a transient UI diff."""
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        edited_file = dict(_cluster_config_file())
        edited_file["original_content"] = "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n  original: true\n"
        edited_file["edited"] = True
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            result = await route_and_deliver(
                [edited_file], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        delivery = await raw.get_delivery(result["delivery_id"])
        assert delivery["details"]["edited_files"] == ["app-network-policy.yaml"]

    async def test_delivery_edited_files_empty_when_nothing_edited(self):
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False,
            )
        delivery = await raw.get_delivery(result["delivery_id"])
        assert delivery["details"]["edited_files"] == []
