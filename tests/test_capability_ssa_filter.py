"""P0: capability-gated cluster packs + SSA soft-skip + source unblocked.

pulse-agent class: missing Tekton/Kyverno CRDs and Forbidden RBAC must not
count as converge failure; source-layer PRs must still open; UI must not
promise PRs after validation failed / skips.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agentit.models import DimensionScore, Finding, Severity
from agentit.portal.cluster_apply import (
    classify_dry_run_error,
    dry_run_manifests_against_cluster,
    filter_files_for_cluster_capabilities,
)
from agentit.portal.auto_delivery import auto_validate_and_deliver, validate_and_fix_manifests
from conftest import make_report, make_store


def _yaml_file(path: str, kind: str, api_version: str, name: str = "x") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": (
            f"apiVersion: {api_version}\nkind: {kind}\n"
            f"metadata:\n  name: {name}\n"
        ),
        "description": path,
        "finding_addressed": "rbac",
        "skill_name": "rbac",
    }


def _source_file() -> dict:
    return {
        "category": "codechange",
        "path": "Containerfile",
        "content": "FROM registry.access.redhat.com/ubi9/ubi:latest\n",
        "description": "Pin base image",
        "finding_addressed": "container",
        "skill_name": "containerfile",
        "target_path": "Containerfile",
    }


class TestClassifySoftSkips:
    def test_bare_not_found_is_soft(self):
        assert classify_dry_run_error("Task/build: Not Found") == "soft"
        assert classify_dry_run_error("Policy/x: (404)") == "soft"

    def test_forbidden_is_soft(self):
        assert classify_dry_run_error("ResourceQuota/rq: Forbidden") == "soft"


class TestFilterFilesForClusterCapabilities:
    def test_drops_kyverno_and_tekton_when_apis_absent(self):
        files = [
            _yaml_file("policy.yaml", "Policy", "kyverno.io/v1", "deny-root"),
            _yaml_file("task.yaml", "Task", "tekton.dev/v1", "build"),
            _yaml_file("cm.yaml", "ConfigMap", "v1", "app-config"),
            _source_file(),
        ]
        # Probe shape: core kinds present; Policy/Task absent.
        available = {
            "configmap", "configmaps", "deployment", "deployments",
            "pod", "pods", "service", "services",
        }
        kept, skips = filter_files_for_cluster_capabilities(
            files, available_kinds=available,
        )
        paths = {f["path"] for f in kept}
        assert "cm.yaml" in paths
        assert "Containerfile" in paths
        assert "policy.yaml" not in paths
        assert "task.yaml" not in paths
        assert len(skips) >= 2
        assert any("Policy" in s for s in skips)
        assert any("Task" in s for s in skips)

    def test_keeps_optional_crs_when_probe_empty(self):
        """Empty probe must not wipe the pack — SSA soft-skip handles it."""
        files = [_yaml_file("policy.yaml", "Policy", "kyverno.io/v1")]
        kept, skips = filter_files_for_cluster_capabilities(files, available_kinds=set())
        assert len(kept) == 1
        assert skips == []


class TestDryRunSkipsMissingCrdAndForbidden:
    def test_missing_crd_pre_skipped_when_kinds_known(self):
        files = [_yaml_file(
            "pipeline.yaml", "Pipeline", "tekton.dev/v1", "build",
        )]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            result = dry_run_manifests_against_cluster(
                files, namespace="ns",
                available_kinds={"configmap", "deployments"},
            )
        mock_apply.assert_not_called()
        assert result["errors"] == []
        assert "pipeline.yaml" in result["skipped_paths"]
        assert result["warnings"]

    def test_forbidden_lands_in_skipped_paths_not_errors(self):
        files = [_yaml_file(
            "quota.yaml", "ResourceQuota", "v1", "app-quota",
        )]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": False,
                "error": "ResourceQuota/app-quota: Forbidden",
                "errors": ["ResourceQuota/app-quota: Forbidden"],
                "conflict": False, "conflict_details": [],
            }
            result = dry_run_manifests_against_cluster(
                files, namespace="ns", available_kinds=set(),
            )
        assert result["errors"] == []
        assert "quota.yaml" in result["skipped_paths"]
        assert any("Forbidden" in w for w in result["warnings"])


class TestSourceNotBlockedByClusterDryRun:
    @pytest.mark.asyncio
    async def test_source_pr_opens_when_cluster_validation_fails(self):
        store = await make_store()
        report = make_report(
            repo_name="pulse-class-app",
            scores=[DimensionScore(
                dimension="security", score=40, max_score=100,
                findings=[Finding(
                    category="container", severity=Severity.high,
                    description="unpinned base image",
                    recommendation="pin base image",
                )],
            )],
        )
        report.infra_repo_url = "https://github.com/org/pulse-class-app-gitops"
        aid = await store.save(report)

        cluster_bad = _yaml_file("policy.yaml", "Policy", "kyverno.io/v1")
        source = _source_file()

        with patch(
            "agentit.portal.cluster_apply.filter_files_for_cluster_capabilities",
            return_value=([cluster_bad, source], ["policy.yaml: skipped — API(s) not on cluster"]),
        ), patch(
            "agentit.portal.auto_delivery.validate_and_fix_manifests",
            return_value={
                "files": [],
                "clean": False,
                "iterations": [{"dry_run_errors": ["schema boom"]}],
                "warnings": [],
                "skipped_paths": [],
                "remaining_issues": ["cluster_config: schema boom"],
            },
        ), patch(
            "agentit.portal.auto_delivery._dry_run_check",
            return_value=([], [], set(), []),
        ), patch(
            "agentit.portal.auto_delivery._check_properties", return_value=[],
        ), patch(
            "agentit.portal.github_pr.create_source_patch_pr",
            return_value={
                "pr_url": "https://github.com/org/pulse-class-app/pull/7",
                "files_committed": 1,
            },
        ), patch(
            "agentit.portal.delivery.kube.get_custom_resource", return_value=None,
        ), patch(
            "agentit.portal.quality_prs.clear_evidence_simulation_ok",
            return_value=(True, "ok"),
        ), patch(
            "agentit.remediation.source_patches.apply_containerfile_pin_only",
            side_effect=lambda files, **kw: files,
        ):
            result = await auto_validate_and_deliver(
                store=store, report=report, app_name=report.repo_name,
                namespace="ns", assessment_id=aid, actor="auto-delivery",
                files=[cluster_bad, source], orchestration={},
                target_findings=[("container", "unpinned base image")],
            )

        assert result["status"] == "delivered"
        assert result["pr_urls"] == ["https://github.com/org/pulse-class-app/pull/7"]

    @pytest.mark.asyncio
    async def test_soft_only_skips_converge_without_burning_retries(self):
        store = await make_store()
        report = make_report(repo_name="soft-skip-app")
        report.infra_repo_url = "https://github.com/org/soft-skip-app-gitops"
        aid = await store.save(report)

        soft_only = {
            "applied": [], "skipped": ["role.yaml"], "skipped_paths": ["role.yaml"],
            "errors": [],
            "warnings": ["role.yaml: Role/reader: Forbidden"],
            "conflicts": [],
            "missing_operators": {},
            "repo_files": [],
        }
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch(
                 "agentit.portal.cluster_apply.dry_run_manifests_against_cluster",
                 return_value=soft_only,
             ), patch(
                 "agentit.portal.cluster_apply.filter_files_for_cluster_capabilities",
                 side_effect=lambda files, available_kinds=None: (files, []),
             ):
            result = await validate_and_fix_manifests(
                [_yaml_file("role.yaml", "Role", "rbac.authorization.k8s.io/v1", "reader")],
                app_name=report.repo_name, namespace="ns", report=report,
                store=store, assessment_id=aid, actor="tester",
            )

        assert result["clean"] is True
        assert result["skipped_paths"] == ["role.yaml"]
        assert len(result["iterations"]) == 1


class TestPulseAgentAcceptanceScenario:
    """Observe-only pulse-agent evidence (score ~66, 18 findings, 8 files):

    - Dry-run: Tekton Task/Pipeline/PDB Not Found; Kyverno missing;
      ResourceQuota Forbidden — must soft-skip, not burn 3 hard attempts
    - Stuck tip: auto-validation-needs-attention, registered=false, 0 PRs
    - HPA invent refuse stays correct (not retested here)
    """

    def test_pulse_pack_kinds_soft_classify(self):
        for msg in (
            "Task/build: Not Found",
            "Pipeline/ci: Not Found",
            "PodDisruptionBudget/app-pdb: Not Found",
            "Policy (kyverno.io/v1) not found on cluster: no matches for kind",
            "ResourceQuota/app-quota: Forbidden",
        ):
            assert classify_dry_run_error(msg) == "soft", msg

    def test_capability_filter_drops_tekton_kyverno_keeps_core(self):
        pack = [
            _yaml_file("task.yaml", "Task", "tekton.dev/v1", "build"),
            _yaml_file("pipeline.yaml", "Pipeline", "tekton.dev/v1", "ci"),
            _yaml_file("policy.yaml", "Policy", "kyverno.io/v1", "deny-root"),
            _yaml_file("pdb.yaml", "PodDisruptionBudget", "policy/v1", "app-pdb"),
            _yaml_file("quota.yaml", "ResourceQuota", "v1", "app-quota"),
            _yaml_file("cm.yaml", "ConfigMap", "v1", "app-config"),
        ]
        available = {
            "configmap", "configmaps", "poddisruptionbudget", "poddisruptionbudgets",
            "resourcequota", "resourcequotas", "deployment", "deployments",
        }
        kept, skips = filter_files_for_cluster_capabilities(pack, available_kinds=available)
        paths = {f["path"] for f in kept}
        assert "task.yaml" not in paths
        assert "pipeline.yaml" not in paths
        assert "policy.yaml" not in paths
        assert "pdb.yaml" in paths  # core kind — SSA soft-skip if Not Found
        assert "quota.yaml" in paths
        assert "cm.yaml" in paths
        assert len(skips) >= 3

    @pytest.mark.asyncio
    async def test_unregistered_catalog_pack_skips_without_three_attempts(self):
        """registered=false + catalog pack → honest needs_attention, ≤1
        validate iteration (no 3-attempt converge theater on Not Found)."""
        store = await make_store()
        # No infra_repo_url → registered=false for dry-run GitOps check.
        report = make_report(repo_name="pulse-agent")
        aid = await store.save(report)

        pack = [
            _yaml_file("task.yaml", "Task", "tekton.dev/v1", "build"),
            _yaml_file("pipeline.yaml", "Pipeline", "tekton.dev/v1", "ci"),
            _yaml_file("policy.yaml", "Policy", "kyverno.io/v1", "deny-root"),
            _yaml_file("pdb.yaml", "PodDisruptionBudget", "policy/v1", "app-pdb"),
            _yaml_file("quota.yaml", "ResourceQuota", "v1", "app-quota"),
            _yaml_file("netpol.yaml", "NetworkPolicy", "networking.k8s.io/v1", "deny"),
            _yaml_file("cm.yaml", "ConfigMap", "v1", "app-config"),
            _yaml_file("sa.yaml", "ServiceAccount", "v1", "app"),
        ]

        def _fake_dry_run(files, namespace="default", available_kinds=None):
            skipped_paths, warnings, errors = [], [], []
            for f in files:
                path = f["path"]
                content = f.get("content") or ""
                if "kind: Task" in content or "kind: Pipeline" in content or "kind: Policy" in content:
                    skipped_paths.append(path)
                    warnings.append(f"{path}: Not Found")
                elif "kind: PodDisruptionBudget" in content:
                    skipped_paths.append(path)
                    warnings.append(f"{path}: PodDisruptionBudget/app-pdb: Not Found")
                elif "kind: ResourceQuota" in content:
                    skipped_paths.append(path)
                    warnings.append(f"{path}: ResourceQuota/app-quota: Forbidden")
                else:
                    # Remaining core YAML still hits GitOps-unregistered hard path
                    # via route_and_deliver — simulate soft-only here so the
                    # capability/soft path is what we assert.
                    skipped_paths.append(path)
                    warnings.append(f"{path}: skipped — missing GitOps registration")
            return {
                "applied": [], "skipped": list(skipped_paths),
                "skipped_paths": skipped_paths, "errors": errors,
                "warnings": warnings, "conflicts": [],
                "missing_operators": {}, "repo_files": [],
            }

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch(
                 "agentit.portal.cluster_apply.probe_available_kinds",
                 return_value={
                     "configmap", "serviceaccount", "networkpolicy",
                     "poddisruptionbudget", "resourcequota",
                 },
             ), patch(
                 "agentit.portal.cluster_apply.dry_run_manifests_against_cluster",
                 side_effect=_fake_dry_run,
             ), patch(
                 "agentit.portal.github_pr.commit_to_infra_repo",
             ) as mock_commit, patch(
                 "agentit.portal.github_pr.create_source_patch_pr",
             ) as mock_source:
            result = await auto_validate_and_deliver(
                store=store, report=report, app_name="pulse-agent",
                namespace="pulse-agent", assessment_id=aid, actor="auto-delivery",
                files=pack, orchestration={},
                target_findings=[("network", "missing network policy")],
            )

        assert result["status"] == "needs_attention"
        assert "could not converge after 3" not in (result.get("reason") or "").lower()
        iters = result.get("iterations") or []
        assert len(iters) <= 1
        mock_commit.assert_not_called()
        mock_source.assert_not_called()
        # Skip reasons recorded for honest UI.
        assert result.get("skip_reasons") or result.get("reason")
