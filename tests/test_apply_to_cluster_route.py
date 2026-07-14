"""Integration tests for POST /assessments/{id}/apply -- the manual "Apply to
Cluster" route, now built on the shared ``cluster_apply.apply_with_verification()``.

Confirms the route's pre-refactor behavior is unchanged: it respects its own
``dry_run`` form flag with no automatic dry-run-first sequencing, and its
side effects (skill outcome recording, audit logging) still fire the same
way they did before the shared function existed.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentit.portal.app import app
from agentit.portal.store_factory import AsyncSQLiteStore
from conftest import make_report, make_store, prime_csrf


def _skill_file(path: str = "test-app-network-policy.yaml") -> dict:
    """Path follows SkillEngine.generate()'s ``{app_name}-{skill.name}.yaml``
    naming convention (app_name sanitized from make_report()'s default
    repo_name "test-app") so record_skill_outcomes() can recover the real
    skill name ("network-policy") from it."""
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": "network policy",
    }


@pytest.fixture
def apply_client():
    store = make_store()
    async_store = AsyncSQLiteStore.wrap(store)
    report = make_report(repo_name="test-app")
    assessment_id = store.save(report)
    store.save_onboarding(assessment_id, [_skill_file()])

    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store):
        client = TestClient(app)
        prime_csrf(client)
        yield client, store, assessment_id


@pytest.fixture(autouse=True)
def _mock_kube():
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": True, "error": None}
        yield mock_kube


class TestDryRunFlagRespectedNoForcedSequencing:
    def test_dry_run_true_stops_after_one_dry_run_no_real_apply(self, apply_client, _mock_kube):
        client, store, aid = apply_client
        resp = client.post(f"/assessments/{aid}/apply", data={"namespace": "ns", "dry_run": "true"},
                            follow_redirects=False)
        assert resp.status_code == 303
        assert "dry_run=true" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_not_called()

    def test_dry_run_false_applies_directly_single_call_no_dry_run_first(self, apply_client, _mock_kube):
        """The manual route's real distinction from AutoMode: dry_run=false
        does not trigger an automatic dry-run-first safety check -- exactly
        one real apply call."""
        client, store, aid = apply_client
        resp = client.post(f"/assessments/{aid}/apply", data={"namespace": "ns", "dry_run": "false"},
                            follow_redirects=False)
        assert resp.status_code == 303
        assert "dry_run=false" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_called_once()

    def test_default_dry_run_is_false_when_omitted(self, apply_client, _mock_kube):
        client, store, aid = apply_client
        resp = client.post(f"/assessments/{aid}/apply", data={"namespace": "ns"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "dry_run=false" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_called_once()


class TestSkillOutcomeRecording:
    def test_real_apply_records_skill_outcome(self, apply_client):
        client, store, aid = apply_client
        client.post(f"/assessments/{aid}/apply", data={"namespace": "ns", "dry_run": "false"},
                    follow_redirects=False)
        eff = store.get_skill_effectiveness("network-policy", min_count=1)
        assert eff["network-policy"]["total"] == 1
        assert eff["network-policy"]["approved"] == 1

    def test_dry_run_never_records_skill_outcome(self, apply_client):
        client, store, aid = apply_client
        client.post(f"/assessments/{aid}/apply", data={"namespace": "ns", "dry_run": "true"},
                    follow_redirects=False)
        eff = store.get_skill_effectiveness("network-policy", min_count=1)
        assert eff == {}


class TestAuditLogGapAlreadyCoveredForManualRoute:
    """The manual route already called audit_log() before this refactor --
    these confirm the shared function preserves that (not a gap, just a
    regression check)."""

    def test_audit_log_fires_on_real_apply(self, apply_client, caplog):
        client, store, aid = apply_client
        with caplog.at_level(logging.INFO, logger="agentit.audit"):
            client.post(f"/assessments/{aid}/apply", data={"namespace": "ns", "dry_run": "false"},
                        follow_redirects=False)
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].action == "apply-to-cluster"
        assert audit_records[0].resource == f"assessment:{aid}"
        assert audit_records[0].outcome == "success"

    def test_audit_log_fires_on_dry_run(self, apply_client, caplog):
        client, store, aid = apply_client
        with caplog.at_level(logging.INFO, logger="agentit.audit"):
            client.post(f"/assessments/{aid}/apply", data={"namespace": "ns", "dry_run": "true"},
                        follow_redirects=False)
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].outcome == "success"

    def test_audit_log_fires_with_error_outcome_on_exception(self, apply_client, caplog):
        client, store, aid = apply_client
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.side_effect = RuntimeError("cluster unreachable")
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                resp = client.post(f"/assessments/{aid}/apply", data={"namespace": "ns", "dry_run": "false"},
                                    follow_redirects=False)
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].outcome == "error"
