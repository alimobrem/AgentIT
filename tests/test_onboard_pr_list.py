"""Onboard Results' PR-centric redesign: the page shows a list of pull
requests -- pending-to-be-opened (pre-delivery preview) or already-opened
(real PRs) -- each with its real target repo, which agent-generated files
it covers and why (real per-file descriptions, never fabricated), and its
current real lifecycle. This replaces the old raw manifest/category-count
framing ("34 manifests across 4 categories", "Orchestration (4 Agents)").

``preview_delivery_groups()`` (delivery.py) and the file-metadata sidecar
(``agents/orchestrator.py``'s ``_write_file_metadata_manifest()``) are
covered here too -- the plumbing that lets this page show a real per-file
"why" instead of a bare filename. Real DB-backed data only: PRs with no
stored outcome are resolved via a live GitHub call, mocked here at
``github_pr.get_pr_status`` per this session's established convention (see
``test_ledger_pr_view.py``/``test_fleet_pr_tracking.py``).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.agents.base import GeneratedFile
from agentit.agents.orchestrator import _FILE_METADATA_MANIFEST, _write_file_metadata_manifest
from agentit.portal.app import app
from agentit.portal.delivery import preview_delivery_groups
from conftest import make_async_store, make_report, make_store, prime_csrf


def _cluster_config_file(
    path: str = "netpol.yaml",
    description: str = "Deny-all baseline NetworkPolicy for this app's namespace.",
) -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": description,
    }


def _source_patch_file(
    path: str = "patch-01-Dockerfile",
    description: str = "Pin base image to a supported UBI9 tag.",
) -> dict:
    return {
        "category": "codechange", "path": path,
        "content": "FROM ubi9\n", "description": description, "target_path": "Dockerfile",
    }


def _manifest_at_rest_file(
    path: str = "renovate.json",
    description: str = "Renovate config for ecosystems: pip.",
) -> dict:
    return {"category": "dependency", "path": path, "content": "{}", "description": description}


def _narrative_report_file() -> dict:
    return {
        "category": "dependency", "path": "dependency-report.md",
        "content": "# Dependency Report\n",
        "description": "Dependency risk report with ecosystem detection and known CVE checks.",
    }


def _secret_blocked_file() -> dict:
    return {
        "category": "skills", "path": "secret.yaml",
        "content": "apiVersion: v1\nkind: Secret\nmetadata:\n  name: test\ndata:\n  password: c2VjcmV0\n",
        "description": "generated secret",
    }


# ── preview_delivery_groups() -- pure classification/preview logic ────────
# No store, no cluster, no network: mirrors route_and_deliver()'s own
# classify -> exclude-blocked/narrative -> mechanism-resolution sequence
# with zero side effects.


class TestPreviewDeliveryGroups:
    def test_cluster_config_previewed_as_gitops_when_infra_repo_known(self):
        groups = preview_delivery_groups(
            [_cluster_config_file()], infra_repo_url="https://github.com/org/infra",
        )
        assert "cluster_config" in groups
        assert groups["cluster_config"]["repo_kind"] == "gitops"
        assert groups["cluster_config"]["mechanism"] == "infra-repo-commit"
        assert groups["cluster_config"]["files"][0]["path"] == "netpol.yaml"
        assert "https://github.com/org/infra" in groups["cluster_config"]["confirmation"]

    def test_cluster_config_has_no_repo_target_when_no_infra_repo_known(self):
        groups = preview_delivery_groups([_cluster_config_file()], infra_repo_url=None)
        assert groups["cluster_config"]["mechanism"] == "none"
        assert groups["cluster_config"]["repo_kind"] == ""

    def test_source_patch_previewed_as_code_repo(self):
        groups = preview_delivery_groups([_source_patch_file()], infra_repo_url=None)
        assert groups["source_patch"]["repo_kind"] == "code"
        assert groups["source_patch"]["mechanism"] == "source-repo-pr"

    def test_manifest_at_rest_previewed_as_code_repo(self):
        groups = preview_delivery_groups([_manifest_at_rest_file()], infra_repo_url=None)
        assert groups["manifest_at_rest"]["repo_kind"] == "code"
        assert groups["manifest_at_rest"]["mechanism"] == "app-repo-pr"

    def test_narrative_report_never_previewed_as_a_pr(self):
        groups = preview_delivery_groups(
            [_narrative_report_file()], infra_repo_url="https://github.com/org/infra",
        )
        assert groups == {}

    def test_secret_blocked_never_previewed_as_a_pr(self):
        groups = preview_delivery_groups(
            [_secret_blocked_file()], infra_repo_url="https://github.com/org/infra",
        )
        assert groups == {}

    def test_unresolved_placeholder_file_excluded_from_its_category(self):
        f = _source_patch_file()
        f["content"] = "image: REPLACE_WITH_AGENTIT_IMAGE\n"
        groups = preview_delivery_groups([f], infra_repo_url=None)
        assert "source_patch" not in groups

    def test_mixed_categories_all_present_independently(self):
        groups = preview_delivery_groups(
            [_cluster_config_file(), _source_patch_file(), _manifest_at_rest_file(), _narrative_report_file()],
            infra_repo_url="https://github.com/org/infra",
        )
        assert set(groups) == {"cluster_config", "source_patch", "manifest_at_rest"}


# ── _write_file_metadata_manifest() -- the sidecar carrying real intent ───


class TestWriteFileMetadataManifest:
    def test_writes_description_finding_addressed_and_skill_name(self, tmp_path: Path):
        files = [
            GeneratedFile(
                path="a.yaml", content="x", description="Real reason A",
                finding_addressed="prop-a", skill_name="skill/a",
            ),
            GeneratedFile(path="b.yaml", content="y", description="Real reason B"),
        ]
        _write_file_metadata_manifest(tmp_path, files)
        data = json.loads((tmp_path / _FILE_METADATA_MANIFEST).read_text())
        assert data["a.yaml"] == {
            "description": "Real reason A", "finding_addressed": "prop-a", "skill_name": "skill/a",
        }
        assert data["b.yaml"] == {"description": "Real reason B", "finding_addressed": "", "skill_name": ""}

    def test_no_files_writes_nothing(self, tmp_path: Path):
        _write_file_metadata_manifest(tmp_path, [])
        assert not (tmp_path / _FILE_METADATA_MANIFEST).exists()


# ── run_onboarding() end-to-end: real descriptions survive to the portal ──


class TestRunOnboardingCarriesRealDescriptions:
    async def test_skill_file_description_is_real_not_bare_path(self):
        """Skills-primary onboarding must persist real descriptions (not bare
        filenames) via the file-metadata sidecar."""
        from agentit.portal.helpers import run_onboarding

        async_store, store = await make_async_store()
        report = make_report(criticality="critical")
        aid = await store.save(report)

        with patch("agentit.portal.helpers.get_store", return_value=async_store), \
             patch("agentit.portal.helpers._store", async_store):
            files, _ = await run_onboarding(report, assessment_id=aid)

        assert files, "expected skills (or codechange) to generate files"
        skill_files = [f for f in files if f.get("category") == "skills"]
        assert skill_files, "skills should own remediations"
        for f in skill_files:
            assert f["description"], f"missing description for {f['path']}"
            assert f["description"] != f["path"], f"bare-path fallback for {f['path']}"


# ── Route-level: the redesigned Onboard Results page ──────────────────────


@pytest.fixture(autouse=True)
def _mock_kube():
    """is_gitops_registered() calls into kube; stub it so tests aren't at
    the mercy of whatever cluster KUBECONFIG happens to point to (mirrors
    test_pr_repo_labeling.py's fixture of the same name)."""
    with patch("agentit.portal.cluster_apply.kube") as mock_apply_kube, \
         patch("agentit.portal.delivery.kube") as mock_delivery_kube:
        mock_apply_kube.namespace_exists.return_value = True
        mock_apply_kube.get_api_resources.return_value = set()
        mock_apply_kube.apply_yaml.return_value = {"applied": True, "error": None}
        mock_delivery_kube.get_custom_resource.side_effect = Exception("no cluster in tests")
        yield


@pytest.fixture
async def ui_client():
    store = await make_store()
    with patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            await prime_csrf(client)
            yield client, store


class TestPullRequestsSectionPreDelivery:
    """No Dry Run/Deliver has happened yet -- every card must be an honest
    preview of what a real Deliver click will do, never claiming a PR
    already exists."""

    async def test_shows_pull_requests_heading_and_preview_cards(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="preview-app")
        aid = await store.save(report)
        await store.set_infra_repo_url(aid, "https://github.com/org/preview-infra")
        await store.save_onboarding(aid, [_cluster_config_file(), _source_patch_file()])

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "Pull Requests" in resp.text
        assert "AgentIT will open 2 pull requests" in resp.text
        assert resp.text.count("Not opened yet") == 2
        assert "GitOps repo" in resp.text
        assert "Code repo" in resp.text
        # The real per-file description, not a bare filename or a
        # fabricated one.
        assert "Deny-all baseline NetworkPolicy for this app" in resp.text
        assert "Pin base image to a supported UBI9 tag." in resp.text
        # The real infra repo URL, traced from confirmation_text() -- never
        # a guessed/generic mechanism description.
        assert "https://github.com/org/preview-infra" in resp.text

    async def test_singular_pull_request_wording(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="singular-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_source_patch_file()])

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert "AgentIT will open 1 pull request when Scan finishes delivery" in resp.text

    async def test_nothing_deliverable_shows_honest_empty_state(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="nothing-deliverable-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_narrative_report_file(), _secret_blocked_file()])

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "Nothing here can become a pull request" in resp.text
        assert "Not opened yet" not in resp.text

    async def test_manual_deliver_ctas_are_not_offered(self, ui_client):
        """Scan opens PRs; Onboard Results must not offer Commit / Per-Agent."""
        client, store = ui_client
        report = make_report(repo_name="keep-dry-run-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_cluster_config_file()])

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert 'data-action="apply"' not in resp.text
        assert 'data-action="prs"' not in resp.text
        assert 'data-action="dry-run"' not in resp.text
        assert "Commit & Open PR" not in resp.text
        assert "Commit &amp; Open PR" not in resp.text
        assert "Per-Agent PRs" not in resp.text
        assert "Download" in resp.text


class TestPullRequestsSectionPostDelivery:
    async def test_gate_tracked_pr_shows_needs_approval_with_real_link(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="gitops-pending-app")
        aid = await store.save(report)
        await store.set_infra_repo_url(aid, "https://github.com/org/gitops-pending-infra")
        await store.save_onboarding(aid, [_cluster_config_file()])
        pr_url = "https://github.com/org/gitops-pending-infra/pull/4"
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": 1}, mechanism="cluster_config:infra-repo-commit",
            status="delivered", details={"outcomes": {"cluster_config": {"pr_url": pr_url}}},
        )

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": pr_url, "title": "fix", "merged_at": ""},
        ):
            resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "Waiting for your approval" in resp.text
        assert pr_url in resp.text
        assert "Not opened yet" not in resp.text
        # Still lists the real file(s) this PR covers.
        assert "Deny-all baseline NetworkPolicy for this app" in resp.text

    async def test_merged_pr_shows_merged_badge(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="gitops-merged-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_cluster_config_file()])
        pr_url = "https://github.com/org/gitops-merged-infra/pull/5"
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": 1}, mechanism="cluster_config:infra-repo-commit",
            status="delivered", details={"outcomes": {"cluster_config": {"pr_url": pr_url}}},
        )

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "merged", "html_url": pr_url, "title": "fix", "merged_at": "2026-01-05T00:00:00"},
        ):
            resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert "Merged" in resp.text
        assert "Waiting for your approval" not in resp.text

    async def test_rejected_pr_shows_rejected_badge_and_real_reason(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="gitops-rejected-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_cluster_config_file()])
        pr_url = "https://github.com/org/gitops-rejected-infra/pull/6"
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": 1}, mechanism="cluster_config:infra-repo-commit",
            status="delivered", details={"outcomes": {"cluster_config": {"pr_url": pr_url}}},
        )
        await store.record_pr_outcome(
            pr_url, report.repo_name, "rejected",
            assessment_id=aid, category="cluster_config", reject_reason="breaks the readiness probe",
        )

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "closed", "html_url": pr_url, "title": "fix", "merged_at": ""},
        ):
            resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert "Rejected" in resp.text
        assert "breaks the readiness probe" in resp.text

    async def test_delivery_sourced_pr_labels_code_repo_and_resolves_live_state(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="source-patch-open-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_source_patch_file()])
        pr_url = "https://github.com/org/source-patch-open-app/pull/7"
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered", details={"outcomes": {"source_patch": {"pr_url": pr_url}}},
        )

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": pr_url, "title": "fix: pin base image", "merged_at": ""},
        ):
            resp = await client.get(f"/assessments/{aid}/onboard-results")

        assert resp.status_code == 200
        assert "Code repo" in resp.text
        assert "fix: pin base image" in resp.text
        assert pr_url in resp.text

    async def test_fully_auto_delivered_shows_merge_on_github_not_deliver_ctas(self, ui_client):
        """After Scan auto-delivery opens PRs, Onboard Results must frame
        merge-on-GitHub — never a second Commit / Per-Agent deliver step."""
        client, store = ui_client
        report = make_report(repo_name="fully-auto-delivered-app")
        report.infra_repo_url = "https://github.com/org/fully-auto-delivered-infra"
        aid = await store.save(report)
        await store.save_onboarding(aid, [_source_patch_file()])
        # Mirrors auto_delivery.py's own save_apply_results() call once its
        # validate/fix loop converges -- always dry_run=True, never a real
        # delivery marker.
        await store.save_apply_results(
            aid, {"applied": [], "skipped": [], "errors": [], "repo_files": []}, "ns", dry_run=True,
        )
        pr_url = "https://github.com/org/fully-auto-delivered-app/pull/11"
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered", details={"outcomes": {"source_patch": {"pr_url": pr_url}}},
        )

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": pr_url, "title": "fix: pin base image", "merged_at": ""},
        ):
            resp = await client.get(f"/assessments/{aid}/onboard-results")

        assert resp.status_code == 200
        assert 'data-action="apply"' not in resp.text
        assert 'data-action="prs"' not in resp.text
        assert 'data-action="retry-scan-delivery"' not in resp.text
        assert "Ready — choose a deliver option." not in resp.text
        assert "One PR for everything, or a PR per agent." not in resp.text
        assert "Opened by Scan" in resp.text
        assert "merge on GitHub" in resp.text
        assert "1 pull request above — review and merge on GitHub." in resp.text

    async def test_needs_attention_without_open_pr_shows_retry_scan_delivery(self, ui_client):
        """Retry Scan delivery is the only PR-creating CTA left — and only
        when the latest onboard job needs attention and nothing is open yet."""
        client, store = ui_client
        report = make_report(repo_name="needs-attention-retry-app")
        report.infra_repo_url = "https://github.com/org/needs-attention-infra"
        aid = await store.save(report)
        await store.save_onboarding(aid, [_source_patch_file()])
        job_id = await store.create_remediation_job(aid)
        await store.update_remediation_job(
            job_id, "needs_attention", current_step="deliver",
            error="validation could not converge",
        )

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert 'data-action="retry-scan-delivery"' in resp.text
        assert "Retry Scan delivery" in resp.text
        assert 'data-action="apply"' not in resp.text
        assert 'data-action="prs"' not in resp.text
        assert "Commit & Open PR" not in resp.text

    async def test_needs_attention_with_open_pr_hides_retry_scan_delivery(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="needs-attention-already-open-app")
        report.infra_repo_url = "https://github.com/org/needs-attention-already-open-infra"
        aid = await store.save(report)
        await store.save_onboarding(aid, [_source_patch_file()])
        job_id = await store.create_remediation_job(aid)
        await store.update_remediation_job(
            job_id, "needs_attention", current_step="deliver",
            error="partial",
        )
        pr_url = "https://github.com/org/needs-attention-already-open-app/pull/12"
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered", details={"outcomes": {"source_patch": {"pr_url": pr_url}}},
        )

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": pr_url, "title": "fix", "merged_at": ""},
        ):
            resp = await client.get(f"/assessments/{aid}/onboard-results")

        assert 'data-action="retry-scan-delivery"' not in resp.text
        assert "Opened by Scan" in resp.text
        assert "merge on GitHub" in resp.text

    async def test_legacy_per_agent_prs_no_longer_surfaced(self, ui_client):
        """Per-Agent product path removed — onboarding_results.pr_url alone
        must not resurrect a parallel PR list on Onboard Results."""
        client, store = ui_client
        report = make_report(repo_name="per-agent-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_manifest_at_rest_file()])
        pr_url = "https://github.com/org/per-agent-app/pull/8"
        await store.update_pr_url(aid, pr_url)

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": pr_url, "title": "dependency: manifests", "merged_at": ""},
        ):
            resp = await client.get(f"/assessments/{aid}/onboard-results")

        assert resp.status_code == 200
        assert "Earlier per-agent PRs" not in resp.text
        assert "Per-Agent" not in resp.text

    async def test_older_prs_for_a_re_delivered_category_are_noted_not_dropped(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="redelivered-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_source_patch_file()])
        first_pr = "https://github.com/org/redelivered-app/pull/1"
        second_pr = "https://github.com/org/redelivered-app/pull/2"
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered", details={"outcomes": {"source_patch": {"pr_url": first_pr}}},
        )
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered", details={"outcomes": {"source_patch": {"pr_url": second_pr}}},
        )

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": second_pr, "title": "", "merged_at": ""},
        ):
            resp = await client.get(f"/assessments/{aid}/onboard-results")

        assert "1 earlier PR" in resp.text
        assert "Delivery History" in resp.text


class TestDeEmphasizedRawPlumbing:
    """The complaint this redesign addresses: raw manifest/category/agent
    plumbing must no longer be the page's primary, always-visible framing."""

    async def test_orchestration_heading_renamed_and_generated_files_collapsed(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="deemphasized-app")
        aid = await store.save(report)
        await store.save_onboarding(
            aid, [_cluster_config_file(), _source_patch_file()],
            orchestration={
                "agents": [{"name": "codechange", "category": "codechange", "success": True,
                            "files_count": 1, "error": None}],
                "conflicts": [], "recommendation": "AUTO-APPROVED", "auto_approve": True, "gates": [],
            },
        )

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert "Orchestration (" not in resp.text
        assert "Generation Summary" in resp.text
        assert "Agent Run Details" not in resp.text
        assert "34 manifests across" not in resp.text
        assert "Generated Files" in resp.text
        assert "<details" in resp.text

    async def test_failed_agent_error_shown_under_fail_badge(self, ui_client):
        """orchestration.agents[].error must surface under FAIL for skills /
        optional codechange without digging in JSON."""
        client, store = ui_client
        report = make_report(repo_name="agent-fail-error-app")
        aid = await store.save(report)
        await store.save_onboarding(
            aid, [_cluster_config_file()],
            orchestration={
                "agents": [
                    {"name": "skills", "category": "skills", "success": False,
                     "files_count": 0, "error": "LLM timeout while resolving BOM"},
                    {"name": "codechange", "category": "codechange", "success": True,
                     "files_count": 2, "error": None},
                ],
                "conflicts": [], "recommendation": "GENERATION INCOMPLETE", "auto_approve": False, "gates": [],
            },
        )

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert 'badge-critical">FAIL' in resp.text
        assert "LLM timeout while resolving BOM" in resp.text
        assert 'badge-low">OK' in resp.text
        assert "Primary remediations" in resp.text
        assert "Optional source patches" in resp.text

    async def test_no_inline_styles_in_pull_requests_section(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="no-inline-style-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_cluster_config_file(), _source_patch_file()])

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        for line in resp.text.split("\n"):
            if "style=" in line.lower() and 'style="--pct' not in line:
                assert False, f"Inline style found: {line.strip()}"
