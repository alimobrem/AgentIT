"""Tests for the edit-before-apply flow: editing a generated file's raw
content on the onboard-results page before delivery, persisting both the
original and edited content for diffing, re-validating YAML edits via the
existing ``validate_manifest()`` path, and making sure the SAVED (possibly
edited) content -- not the original -- is what ``route_and_deliver()``
actually delivers and records in the ``deliveries`` table.

This is the concrete build-out of the gap README's "Known gap" callout
named (see docs/self-improvement-for-agentit.md and
docs/unified-apply-flow.md, both of which cite it): "the portal has no
edit-before-apply flow ... so there's no 'diff between generated and
applied content' to capture."
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from conftest import make_report, make_store, prime_csrf


def _configmap_file(path: str = "app-config.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\ndata:\n  key: value\n",
        "description": "config",
    }


@pytest.fixture
async def edit_client():
    store = await make_store()
    async_store = store
    report = make_report(repo_name="test-app")
    assessment_id = await store.save(report)
    await store.save_onboarding(assessment_id, [_configmap_file()])

    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            await prime_csrf(client)
            yield client, store, assessment_id


@pytest.fixture(autouse=True)
def _mock_kube():
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": True, "error": None}
        yield mock_kube


# ── Store-level round trip ──────────────────────────────────────────────


class TestStoreUpdateOnboardingFile:
    async def test_persists_content_and_captures_original(self):
        store = await make_store()
        report = make_report()
        aid = await store.save(report)
        await store.save_onboarding(aid, [_configmap_file()])

        updated = await store.update_onboarding_file(aid, "skills", "app-config.yaml", "edited: true\n")
        assert updated is not None
        assert updated["content"] == "edited: true\n"
        assert updated["original_content"] == _configmap_file()["content"]
        assert updated["edited"] is True
        assert "edited_at" in updated

        files = await store.get_onboarding(aid)
        saved = next(f for f in files if f["path"] == "app-config.yaml")
        assert saved["content"] == "edited: true\n"
        assert saved["original_content"] == _configmap_file()["content"]

    async def test_original_content_preserved_across_multiple_edits(self):
        store = await make_store()
        report = make_report()
        aid = await store.save(report)
        await store.save_onboarding(aid, [_configmap_file()])

        await store.update_onboarding_file(aid, "skills", "app-config.yaml", "first edit\n")
        second = await store.update_onboarding_file(aid, "skills", "app-config.yaml", "second edit\n")

        assert second["content"] == "second edit\n"
        # original_content must still be the FIRST-ever generated content,
        # not "first edit" -- otherwise a second edit would silently lose
        # the real diff-against-original.
        assert second["original_content"] == _configmap_file()["content"]

    async def test_returns_none_for_unknown_path(self):
        store = await make_store()
        report = make_report()
        aid = await store.save(report)
        await store.save_onboarding(aid, [_configmap_file()])
        assert await store.update_onboarding_file(aid, "skills", "nonexistent.yaml", "x") is None

    async def test_returns_none_when_no_onboarding_exists(self):
        store = await make_store()
        report = make_report()
        aid = await store.save(report)
        assert await store.update_onboarding_file(aid, "skills", "app-config.yaml", "x") is None


# ── Edit route ───────────────────────────────────────────────────────────


class TestEditOnboardingFileRoute:
    async def test_valid_edit_saves_and_redirects_with_success(self, edit_client):
        client, store, aid = edit_client
        new_content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\ndata:\n  key: newvalue\n"
        resp = await client.post(
            f"/assessments/{aid}/onboard-results/edit-file",
            data={"category": "skills", "path": "app-config.yaml", "content": new_content},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "edited=app-config.yaml" in resp.headers["location"]

        files = await store.get_onboarding(aid)
        saved = next(f for f in files if f["path"] == "app-config.yaml")
        assert saved["content"] == new_content
        assert saved["edited"] is True

    async def test_edit_logs_an_event_with_correlation_id(self, edit_client):
        client, store, aid = edit_client
        await client.post(
            f"/assessments/{aid}/onboard-results/edit-file",
            data={
                "category": "skills", "path": "app-config.yaml",
                "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n",
            },
            follow_redirects=False,
        )
        events = await store.list_events(limit=50)
        edit_events = [e for e in events if e["action"] == "onboarding-file-edited"]
        assert len(edit_events) == 1
        assert edit_events[0]["correlation_id"] == aid

    async def test_invalid_yaml_edit_is_rejected_and_not_persisted(self, edit_client):
        """A human's raw edit that breaks YAML syntax must be re-validated
        via the existing validate_manifest() path (agents/base.py) and
        rejected outright -- the original content must remain untouched."""
        client, store, aid = edit_client
        broken_yaml = "apiVersion: v1\nkind: ConfigMap\nmetadata: [unterminated\n"
        resp = await client.post(
            f"/assessments/{aid}/onboard-results/edit-file",
            data={"category": "skills", "path": "app-config.yaml", "content": broken_yaml},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

        files = await store.get_onboarding(aid)
        saved = next(f for f in files if f["path"] == "app-config.yaml")
        assert saved["content"] == _configmap_file()["content"]
        assert not saved.get("edited")

    async def test_structurally_invalid_manifest_missing_metadata_is_rejected(self, edit_client):
        client, store, aid = edit_client
        missing_metadata = "apiVersion: v1\nkind: ConfigMap\n"
        resp = await client.post(
            f"/assessments/{aid}/onboard-results/edit-file",
            data={"category": "skills", "path": "app-config.yaml", "content": missing_metadata},
            follow_redirects=False,
        )
        assert "error=" in resp.headers["location"]
        files = await store.get_onboarding(aid)
        saved = next(f for f in files if f["path"] == "app-config.yaml")
        assert not saved.get("edited")

    async def test_unknown_file_path_returns_404(self, edit_client):
        client, _store, aid = edit_client
        resp = await client.post(
            f"/assessments/{aid}/onboard-results/edit-file",
            data={
                "category": "skills", "path": "does-not-exist.yaml",
                "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 404

    async def test_unknown_assessment_returns_404(self, edit_client):
        client, _store, _aid = edit_client
        resp = await client.post(
            "/assessments/nonexistent/onboard-results/edit-file",
            data={"category": "skills", "path": "app-config.yaml", "content": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 404


class TestOnboardResultsPageRendersEditAndDiff:
    async def test_page_shows_editor_controls(self, edit_client):
        client, _store, aid = edit_client
        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "editor-textarea" in resp.text
        assert "Save Edit" in resp.text

    async def test_page_shows_diff_after_edit(self, edit_client):
        client, _store, aid = edit_client
        new_content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\ndata:\n  key: changed\n"
        await client.post(
            f"/assessments/{aid}/onboard-results/edit-file",
            data={"category": "skills", "path": "app-config.yaml", "content": new_content},
            follow_redirects=False,
        )
        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "diff-block" in resp.text
        assert "diff-add" in resp.text or "diff-remove" in resp.text
        assert ">edited<" in resp.text.replace("\n", "")

    async def test_no_inline_styles_introduced_by_editor_or_diff(self, edit_client):
        client, _store, aid = edit_client
        await client.post(
            f"/assessments/{aid}/onboard-results/edit-file",
            data={
                "category": "skills", "path": "app-config.yaml",
                "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\ndata:\n  foo: bar\n",
            },
            follow_redirects=False,
        )
        resp = await client.get(f"/assessments/{aid}/onboard-results")
        for line in resp.text.split("\n"):
            if "style=" in line.lower() and 'style="--pct' not in line:
                assert False, f"Inline style found: {line.strip()}"


# ── Genuine round trip: edited content is what actually gets delivered ──


class TestDeliverUsesEditedContent:
    async def test_deliver_applies_edited_content_not_original(self, edit_client, _mock_kube):
        client, store, aid = edit_client
        edited_content = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\ndata:\n  key: EDITED_VALUE\n"
        )
        edit_resp = await client.post(
            f"/assessments/{aid}/onboard-results/edit-file",
            data={"category": "skills", "path": "app-config.yaml", "content": edited_content},
            follow_redirects=False,
        )
        assert edit_resp.status_code == 303

        deliver_resp = await client.post(
            f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False,
        )
        assert deliver_resp.status_code == 303
        assert "applied=1" in deliver_resp.headers["location"]

        _mock_kube.apply_yaml.assert_called_once()
        delivered_content = _mock_kube.apply_yaml.call_args[0][0]
        assert "EDITED_VALUE" in delivered_content
        assert "EDITED_VALUE" not in _configmap_file()["content"]

    async def test_delivery_row_records_edited_files(self, edit_client, _mock_kube):
        client, store, aid = edit_client
        await client.post(
            f"/assessments/{aid}/onboard-results/edit-file",
            data={
                "category": "skills", "path": "app-config.yaml",
                "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\ndata:\n  key: edited\n",
            },
            follow_redirects=False,
        )
        await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)

        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) == 1
        assert deliveries[0]["details"]["edited_files"] == ["app-config.yaml"]

    async def test_delivery_row_has_no_edited_files_when_nothing_was_edited(self, edit_client, _mock_kube):
        client, store, aid = edit_client
        await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)
        deliveries = await store.list_deliveries(aid)
        assert deliveries[0]["details"]["edited_files"] == []


# ── Safety model: editing must never bypass routing/taxonomy ─────────────


class TestEditCannotBypassSafetyRouting:
    async def test_editing_configmap_into_secret_blocks_delivery(self, edit_client, _mock_kube):
        """The routing/safety logic must react to the ACTUAL edited content,
        not the original classification -- editing a ConfigMap into a
        Secret must hit the exact same permanent deny-rule
        (CATEGORY_SECRET_BLOCKED) any originally-generated Secret would,
        never routed to any delivery mechanism."""
        client, store, aid = edit_client
        secret_content = (
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: test\ndata:\n  password: c2VjcmV0\n"
        )
        edit_resp = await client.post(
            f"/assessments/{aid}/onboard-results/edit-file",
            data={"category": "skills", "path": "app-config.yaml", "content": secret_content},
            follow_redirects=False,
        )
        assert edit_resp.status_code == 303

        await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)

        _mock_kube.apply_yaml.assert_not_called()
        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) == 1
        # Blocked-only delivery: no mechanism was assigned to any category --
        # the file is recorded as `secret_blocked`, never delivered.
        assert deliveries[0]["mechanism"] == "none"
        assert deliveries[0]["categories"] == {"secret_blocked": 1}
        assert deliveries[0]["details"]["edited_files"] == ["app-config.yaml"]

    async def test_editing_manifest_still_goes_through_route_and_deliver(self, edit_client, _mock_kube):
        """A non-adversarial edit (still a ConfigMap) must still be routed
        through the exact same route_and_deliver()/apply_with_verification()
        path as an unedited file -- no separate, edit-only code path."""
        client, store, aid = edit_client
        with patch("agentit.portal.delivery.route_and_deliver") as mock_route:
            mock_route.return_value = {
                "delivery_id": "d1", "registered": False, "infra_repo_url": None,
                "mechanisms": {}, "outcomes": {}, "blocked": [], "excluded": [],
            }
            edited_content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\ndata:\n  x: y\n"
            await client.post(
                f"/assessments/{aid}/onboard-results/edit-file",
                data={"category": "skills", "path": "app-config.yaml", "content": edited_content},
                follow_redirects=False,
            )
            resp = await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)
        assert resp.status_code == 303
        mock_route.assert_called_once()
        delivered_files = mock_route.call_args[0][0]
        assert delivered_files[0]["content"] == edited_content
        assert delivered_files[0]["edited"] is True
