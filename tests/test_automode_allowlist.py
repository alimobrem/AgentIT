"""Tests for the per-(namespace, resource-kind) auto-mode allowlist --
`split_files_by_allowlist()` (pure partitioning) plus its wiring into
`AutoMode.execute()` (split-batch partial-allow, RBAC-shaped hard-deny,
and the "no allowlist configured" no-op default)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.automode import AutoMode, RBAC_SHAPED_KINDS, parse_allowlist, split_files_by_allowlist
from conftest import make_async_store, make_report


def _cm_file(path: str = "cm.yaml", namespace: str = "default", name: str = "test") -> dict:
    return {
        "category": "skills", "path": path,
        "content": f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {name}\n  namespace: {namespace}\n",
        "description": "configmap",
    }


def _crb_file(path: str = "crb.yaml", name: str = "test-crb") -> dict:
    return {
        "category": "skills", "path": path,
        "content": f"apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata:\n  name: {name}\n",
        "description": "cluster role binding",
    }


def _secret_file(path: str = "secret.yaml", namespace: str = "default") -> dict:
    return {
        "category": "skills", "path": path,
        "content": f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: test\n  namespace: {namespace}\n",
        "description": "secret",
    }


class TestParseAllowlist:
    def test_none_returns_empty(self):
        assert parse_allowlist(None) == []

    def test_empty_string_returns_empty(self):
        assert parse_allowlist("") == []

    def test_invalid_json_returns_empty(self):
        assert parse_allowlist("not json") == []

    def test_non_list_json_returns_empty(self):
        assert parse_allowlist('{"a": 1}') == []

    def test_valid_patterns_parsed(self):
        assert parse_allowlist('["*/ConfigMap", "prod/NetworkPolicy"]') == ["*/ConfigMap", "prod/NetworkPolicy"]

    def test_entries_without_slash_are_dropped(self):
        assert parse_allowlist('["ConfigMap", "*/NetworkPolicy"]') == ["*/NetworkPolicy"]


class TestSplitFilesByAllowlistNoOp:
    """No allowlist configured -- must be a pure no-op (purely additive
    requirement: existing deployments that never touch this setting see
    identical whole-batch behavior)."""

    def test_empty_allowlist_allows_everything_including_rbac(self):
        files = [_cm_file(), _crb_file(), _secret_file()]
        allowed, denied, reasons = split_files_by_allowlist(files, "default", [])
        assert allowed == files
        assert denied == []
        assert reasons == {}


class TestSplitFilesByAllowlistPartial:
    """The core split-batch-partial-allow behavior: one allowed ConfigMap
    and one disallowed ClusterRoleBinding in the same batch must split, not
    be treated all-or-nothing."""

    def test_mixed_batch_splits_allowed_and_denied(self):
        cm = _cm_file(path="cm.yaml", namespace="default")
        crb = _crb_file(path="crb.yaml")
        allowed, denied, reasons = split_files_by_allowlist(
            [cm, crb], "default", ["*/ConfigMap"],
        )
        assert allowed == [cm]
        assert denied == [crb]
        assert "crb.yaml" in reasons

    def test_wildcard_namespace_pattern_matches_any_namespace(self):
        cm = _cm_file(namespace="prod-app")
        allowed, denied, _ = split_files_by_allowlist([cm], "prod-app", ["*/ConfigMap"])
        assert allowed == [cm]
        assert denied == []

    def test_exact_namespace_pattern_denies_other_namespaces(self):
        cm = _cm_file(namespace="staging")
        allowed, denied, reasons = split_files_by_allowlist([cm], "staging", ["prod/ConfigMap"])
        assert allowed == []
        assert denied == [cm]
        assert "staging/ConfigMap not in auto-mode allowlist" in reasons["cm.yaml"][0]

    def test_kind_not_matching_any_pattern_is_denied(self):
        np_file = {
            "category": "skills", "path": "np.yaml",
            "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n  namespace: default\n",
            "description": "netpol",
        }
        allowed, denied, _ = split_files_by_allowlist([np_file], "default", ["*/ConfigMap"])
        assert allowed == []
        assert denied == [np_file]

    def test_non_yaml_files_always_allowed(self):
        md_file = {"category": "codechange", "path": "summary.md", "content": "# notes", "description": ""}
        allowed, denied, _ = split_files_by_allowlist([md_file], "default", ["*/ConfigMap"])
        assert allowed == [md_file]
        assert denied == []

    def test_unparseable_yaml_always_allowed(self):
        bogus = {"category": "skills", "path": "bogus.yaml", "content": "not: a: k8s: doc: at: all: [", "description": ""}
        allowed, denied, _ = split_files_by_allowlist([bogus], "default", ["*/ConfigMap"])
        assert allowed == [bogus]
        assert denied == []


class TestSplitFilesByAllowlistRbacHardDeny:
    """RBAC-shaped kinds are hard-denied by default, even if a pattern
    naming them (or a blanket wildcard) is present in the allowlist."""

    def test_rbac_shaped_kinds_all_denied_even_with_wildcard_allowlist(self):
        for kind, content in [
            ("Secret", "apiVersion: v1\nkind: Secret\nmetadata:\n  name: s\n  namespace: default\n"),
            ("Role", "apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\nmetadata:\n  name: r\n  namespace: default\n"),
            ("RoleBinding", "apiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\nmetadata:\n  name: rb\n  namespace: default\n"),
            ("ClusterRole", "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: cr\n"),
            ("ClusterRoleBinding", "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata:\n  name: crb\n"),
        ]:
            f = {"category": "skills", "path": f"{kind}.yaml", "content": content, "description": ""}
            allowed, denied, reasons = split_files_by_allowlist([f], "default", ["*/*"])
            assert denied == [f], f"{kind} should be denied even with a */* allowlist entry"
            assert allowed == []
            assert "RBAC-shaped" in reasons[f"{kind}.yaml"][0]

    def test_explicit_pattern_naming_an_rbac_kind_is_still_ignored(self):
        secret = _secret_file()
        allowed, denied, _ = split_files_by_allowlist([secret], "default", ["*/Secret"])
        assert denied == [secret]
        assert allowed == []

    def test_rbac_shaped_kinds_constant_matches_expected_set(self):
        assert RBAC_SHAPED_KINDS == {"Secret", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}


class TestExecuteAllowlistIntegration:
    """`AutoMode.execute()`'s direct-apply path wired to the allowlist."""

    async def _make_engine_with_safe_llm(self, store):
        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Adds ConfigMap",
        }
        return AutoMode(store=store, llm_client=llm)

    async def test_no_allowlist_configured_applies_everything_unchanged(self):
        """Purely additive: no `auto_mode_allowlist` setting -> identical to
        pre-allowlist behavior (single apply call covering every file)."""
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)
        engine = await self._make_engine_with_safe_llm(s)

        files = [_cm_file(), _crb_file()]
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["cm.yaml", "crb.yaml"], "skipped": [], "errors": []}
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        # Both files passed through to the same single apply call -- no split.
        first_call = mock_apply.call_args_list[0]
        assert len(first_call.args[0]) == 2

    async def test_partial_allowlist_splits_batch_applies_allowed_gates_denied(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        raw.set_setting("auto_mode_allowlist", '["*/ConfigMap"]')
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)
        engine = await self._make_engine_with_safe_llm(s)

        cm = _cm_file(path="cm.yaml")
        crb = _crb_file(path="crb.yaml")
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["cm.yaml"], "skipped": [], "errors": []}
            result = await engine.execute(aid, [cm, crb], "default", "low", True, "test-app")

        assert result["action"] == "split"
        # Only the allowed ConfigMap was ever handed to the real apply pipeline.
        for call in mock_apply.call_args_list:
            paths = {f["path"] for f in call.args[0]}
            assert "crb.yaml" not in paths
        gates = raw.list_gates(status="pending")
        scope_gates = [g for g in gates if g["gate_type"] == "auto-mode-scope-review"]
        assert len(scope_gates) == 1
        assert "crb.yaml" in scope_gates[0]["summary"]

    async def test_all_denied_gates_whole_batch_without_calling_apply(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        raw.set_setting("auto_mode_allowlist", '["*/ConfigMap"]')
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)
        engine = await self._make_engine_with_safe_llm(s)

        crb = _crb_file()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            result = await engine.execute(aid, [crb], "default", "low", True, "test-app")

        assert result["action"] == "gated"
        assert "allowlist scope" in result["reason"]
        mock_apply.assert_not_called()
        gates = raw.list_gates(status="pending")
        assert any(g["gate_type"] == "auto-mode-scope-review" for g in gates)

    async def test_rbac_shaped_kind_gated_even_with_blanket_wildcard_allowlist(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        raw.set_setting("auto_mode_allowlist", '["*/*"]')
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)
        engine = await self._make_engine_with_safe_llm(s)

        secret = _secret_file()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            result = await engine.execute(aid, [secret], "default", "low", True, "test-app")

        assert result["action"] == "gated"
        mock_apply.assert_not_called()
