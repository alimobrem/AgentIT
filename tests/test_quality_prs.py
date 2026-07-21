"""Phases A–F quality bar for Scan-opened PRs (docs/plan-quality-helpful-prs.md)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agentit.models import DimensionScore, Finding, Severity
from agentit.portal.auto_delivery import auto_validate_and_deliver
from agentit.portal.quality_prs import (
    MAX_FILES_PER_CLUSTER_PR,
    build_helpful_pr_body,
    filter_files_to_open_findings,
    finding_gate_allows_pr,
    partition_by_finding_cluster,
    resolve_target_findings,
)
from conftest import make_report, make_store

_CLEAN_DRY_RUN = {
    "applied": ["rbac.yaml"], "skipped": [], "errors": [],
    "conflicts": [], "missing_operators": {}, "repo_files": [],
}


@pytest.fixture(autouse=True)
def _mock_api_dry_run_success():
    with patch(
        "agentit.portal.cluster_apply.dry_run_manifests_against_cluster",
        return_value=_CLEAN_DRY_RUN,
    ):
        yield


def _rbac_file(**extra) -> dict:
    base = {
        "category": "skills",
        "path": "rbac.yaml",
        "content": (
            "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: app\n"
            "---\napiVersion: rbac.authorization.k8s.io/v1\nkind: Role\n"
            "metadata:\n  name: app\nrules: []\n"
            "---\napiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\n"
            "metadata:\n  name: app\nroleRef:\n  kind: Role\n  name: app\n"
            "subjects:\n- kind: ServiceAccount\n  name: app\n"
        ),
        "description": "add RBAC for the workload",
        "skill_name": "rbac",
        "finding_addressed": "rbac",
    }
    base.update(extra)
    return base


def _network_file(**extra) -> dict:
    base = {
        "category": "skills",
        "path": "network-policy.yaml",
        "content": (
            "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
            "metadata:\n  name: deny-all\nspec:\n  podSelector: {}\n"
        ),
        "description": "default-deny NetworkPolicy",
        "skill_name": "network-policy",
        "finding_addressed": "network",
    }
    base.update(extra)
    return base


def _report_with_findings(*categories: str, repo_name: str = "quality-app"):
    findings = [
        Finding(category=c, severity=Severity.high, description=f"missing {c}",
                recommendation=f"add {c}")
        for c in categories
    ]
    report = make_report(
        repo_name=repo_name,
        scores=[DimensionScore(
            dimension="security", score=40, max_score=100, findings=findings,
        )],
    )
    report.infra_repo_url = f"https://github.com/org/{repo_name}-gitops"
    return report


class TestFindingGatePhaseA:
    def test_refuses_empty_findings_without_score_delta(self):
        assert finding_gate_allows_pr([]) is False
        assert finding_gate_allows_pr([], score_delta_claimed=None) is False

    def test_allows_open_findings(self):
        assert finding_gate_allows_pr([("rbac", "missing rbac")]) is True

    def test_allows_material_score_delta_claim(self):
        assert finding_gate_allows_pr([], score_delta_claimed=8.0) is True
        assert finding_gate_allows_pr([], score_delta_claimed=2.0) is False

    def test_resolve_from_report(self):
        report = _report_with_findings("rbac", "network")
        keys = resolve_target_findings(report, None)
        assert ("rbac", "missing rbac") in keys
        assert ("network", "missing network") in keys

    def test_filter_drops_unrelated_templates_124_class(self):
        """#124-class: many unrelated files + few findings → keep only linked ones."""
        findings = [("rbac", "missing rbac")]
        files = [
            _rbac_file(),
            {
                "category": "skills", "path": "otel.yaml", "content": "kind: ConfigMap\n",
                "description": "otel fluff", "skill_name": "otel-collector",
            },
            {
                "category": "skills", "path": "kyverno.yaml", "content": "kind: ConfigMap\n",
                "description": "policy fluff", "skill_name": "kyverno-require-labels",
            },
        ]
        kept, drops = filter_files_to_open_findings(files, findings)
        assert len(kept) == 1
        assert kept[0]["path"] == "rbac.yaml"
        assert len(drops) == 2

    def test_filter_keeps_via_skill_registry_bridge(self):
        findings = [("network", "missing network policy")]
        files = [{
            "category": "skills",
            "path": "netpol.yaml",
            "content": "kind: NetworkPolicy\n",
            "description": "netpol",
            "skill_name": "network-policy",
        }]
        kept, drops = filter_files_to_open_findings(files, findings)
        assert len(kept) == 1
        assert drops == []


class TestClusterPhaseB:
    def test_one_cluster_per_finding_category(self):
        findings = [("rbac", "missing rbac"), ("network", "missing network")]
        files = [
            {**_rbac_file(), "_finding_cluster": "rbac"},
            {**_network_file(), "_finding_cluster": "network"},
        ]
        clusters = partition_by_finding_cluster(files, findings)
        assert len(clusters) == 2
        keys = {c.key for c in clusters}
        assert keys == {"rbac", "network"}
        assert all(len(c.files) == 1 for c in clusters)

    def test_caps_files_per_pr(self):
        findings = [("rbac", "missing rbac")]
        files = [
            {**_rbac_file(path=f"rbac-{i}.yaml"), "_finding_cluster": "rbac"}
            for i in range(MAX_FILES_PER_CLUSTER_PR + 2)
        ]
        clusters = partition_by_finding_cluster(files, findings)
        assert len(clusters) == 2
        assert len(clusters[0].files) == MAX_FILES_PER_CLUSTER_PR
        assert len(clusters[1].files) == 2


class TestPrBodyPhaseD:
    def test_body_has_finding_change_outcome_and_no_auto_merge(self):
        body = build_helpful_pr_body(
            title_line="AgentIT Scan: rbac for pinky",
            target_findings=[("rbac", "missing rbac")],
            files=[_rbac_file()],
            drop_reasons=["otel.yaml: not tied to an open finding"],
        )
        assert "### Targeted findings" in body
        assert "`rbac`" in body
        assert "### Expected effect" in body
        assert "### Validation" in body
        assert "`rbac.yaml`" in body
        assert "Not included" in body
        assert "does **not** auto-merge" in body
        assert "Argo deploys after merge" in body


class TestAutoDeliveryQualityGate:
    async def test_refuses_pr_when_no_findings(self):
        store = await make_store()
        report = make_report(
            repo_name="no-findings-app",
            scores=[DimensionScore(dimension="security", score=90, max_score=100, findings=[])],
        )
        report.infra_repo_url = "https://github.com/org/no-findings-app-gitops"
        aid = await store.save(report)

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.create_source_patch_pr") as mock_source:
            result = await auto_validate_and_deliver(
                store=store, report=report, app_name=report.repo_name, namespace="ns",
                assessment_id=aid, actor="auto-delivery",
                files=[_rbac_file(finding_addressed="rbac")],
                orchestration={},
                target_findings=[],
            )

        assert result["status"] == "needs_attention"
        assert "No open findings" in result["reason"]
        mock_commit.assert_not_called()
        mock_source.assert_not_called()

    async def test_refuses_when_files_do_not_map_to_findings(self):
        store = await make_store()
        # Non-property finding so validate/fix does not inject a matching fix.
        report = _report_with_findings("sbom", repo_name="unmap-app")
        aid = await store.save(report)
        unrelated = {
            "category": "skills", "path": "otel.yaml",
            "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: otel\n",
            "description": "unrelated", "skill_name": "otel-collector",
        }

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            result = await auto_validate_and_deliver(
                store=store, report=report, app_name=report.repo_name, namespace="ns",
                assessment_id=aid, actor="auto-delivery", files=[unrelated],
                orchestration={},
                target_findings=[("sbom", "missing sbom")],
            )

        assert result["status"] == "needs_attention"
        assert "map to open findings" in result["reason"]
        mock_commit.assert_not_called()

    async def test_opens_separate_prs_per_finding_cluster(self):
        store = await make_store()
        report = _report_with_findings("rbac", "network", repo_name="cluster-app")
        aid = await store.save(report)
        files = [_rbac_file(), _network_file()]
        calls: list[dict] = []

        def _fake_commit(infra_url, app, files, branch=None, pr_context=None):
            calls.append({"files": files, "branch": branch, "pr_context": pr_context})
            n = len(calls)
            return {
                "pr_url": f"https://github.com/org/cluster-app-gitops/pull/{n}",
                "commit_url": f"https://github.com/org/cluster-app-gitops/commit/abc{n}",
                "files_committed": len(files),
            }

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.auto_delivery.validate_and_fix_manifests",
                   return_value={"files": files, "clean": True, "iterations": []}), \
             patch("agentit.portal.auto_delivery._dry_run_check",
                   return_value=([], set())), \
             patch("agentit.portal.auto_delivery._check_properties", return_value=[]), \
             patch("agentit.portal.github_pr.commit_to_infra_repo", side_effect=_fake_commit), \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            result = await auto_validate_and_deliver(
                store=store, report=report, app_name=report.repo_name, namespace="ns",
                assessment_id=aid, actor="auto-delivery",
                files=files,
                orchestration={},
                target_findings=[("rbac", "missing rbac"), ("network", "missing network")],
            )

        assert result["status"] == "delivered"
        assert len(result["pr_urls"]) == 2
        assert len(calls) == 2
        # Phase D: each PR body explains findings
        for call in calls:
            assert call["pr_context"] is not None
            assert "Targeted findings" in call["pr_context"]["body"]
            assert "does **not** auto-merge" in call["pr_context"]["body"]

    async def test_fleet_never_approves_skills_on_pr_open(self):
        """Phase E0 / F: pinky path must not record approved on open."""
        store = await make_store()
        report = _report_with_findings("rbac", repo_name="pinky")
        aid = await store.save(report)

        files = [_rbac_file(category="skills")]
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.auto_delivery.validate_and_fix_manifests",
                   return_value={"files": files, "clean": True, "iterations": []}), \
             patch("agentit.portal.auto_delivery._dry_run_check",
                   return_value=([], set())), \
             patch("agentit.portal.auto_delivery._check_properties", return_value=[]), \
             patch("agentit.portal.github_pr.commit_to_infra_repo",
                   return_value={"pr_url": "https://github.com/org/pinky-gitops/pull/1",
                                 "commit_url": "https://github.com/org/pinky-gitops/commit/abc",
                                 "files_committed": 1}), \
             patch("agentit.portal.github_pr.ensure_applicationset"), \
             patch("agentit.skill_engine.record_skill_outcomes", new_callable=AsyncMock) as mock_outcomes:
            result = await auto_validate_and_deliver(
                store=store, report=report, app_name="pinky", namespace="pinky",
                assessment_id=aid, actor="auto-delivery",
                files=files,
                orchestration={},
                target_findings=[("rbac", "missing rbac")],
            )

        assert result["status"] == "delivered"
        mock_outcomes.assert_not_called()


class TestGithubPrBodyHelpers:
    def test_create_source_patch_pr_uses_pr_context_body(self):
        from agentit.portal.github_pr import create_source_patch_pr

        captured = {}

        def _fake_open(base_url, hdrs, owner, branch, default_branch, title, body, repo_url):
            captured["title"] = title
            captured["body"] = body
            return "https://github.com/org/AgentIT/pull/99"

        with patch("agentit.portal.github_pr._get_token", return_value="t"), \
             patch("agentit.portal.github_pr._get_default_branch_and_base_sha",
                   return_value=("main", "sha")), \
             patch("agentit.portal.github_pr._commit_tree", return_value="csha"), \
             patch("agentit.portal.github_pr._create_or_update_branch_ref"), \
             patch("agentit.portal.github_pr.path_exists_on_default_branch", return_value=False), \
             patch("agentit.portal.github_pr._open_pr_with_fallback", side_effect=_fake_open):
            result = create_source_patch_pr(
                "https://github.com/org/AgentIT", "AgentIT",
                [_rbac_file(
                    target_path="chart/templates/rbac.yaml",
                    content="{{ .Values.name }}\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n",
                )],
                branch_name="agentit/agentit-rbac",
                pr_context={
                    "body": build_helpful_pr_body(
                        title_line="Scan rbac",
                        target_findings=[("rbac", "missing rbac")],
                        files=[_rbac_file()],
                    ),
                    "cluster_key": "rbac",
                },
            )
        assert result["pr_url"].endswith("/99")
        assert "Targeted findings" in captured["body"]
        assert "rbac" in captured["title"]
