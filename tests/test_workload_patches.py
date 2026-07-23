"""Workload replicas / health-probe source patches + clear-evidence."""
from __future__ import annotations

from agentit.remediation.clear_evidence import (
    WORKLOAD_PROBES,
    WORKLOAD_REPLICAS,
    verify_evidence,
)
from agentit.remediation.source_patches import (
    enrich_workload_files_from_repo,
    harden_dockerfile_content,
)
from agentit.remediation.workload_patches import (
    chart_root_for_template_path,
    has_health_probes,
    helm_templated_replicas_key,
    patch_health_probes,
    patch_replicas,
    patch_values_numeric_key,
    replicas_at_least,
    values_yaml_path_for_chart,
    values_yaml_replicas_at_least,
    verify_workload_probes,
    verify_workload_replicas,
)


class TestWorkloadReplicas:
    def test_bumps_replicas_1_to_2(self) -> None:
        src = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n  name: app\n"
            "spec:\n  replicas: 1\n  selector:\n    matchLabels:\n      app: app\n"
        )
        out = patch_replicas(src, replicas=2)
        assert replicas_at_least(out, 2)
        assert "replicas: 2" in out

    def test_clear_evidence_ok(self) -> None:
        ok, reason = verify_workload_replicas([{
            "target_path": "deploy/deployment.yaml",
            "content": (
                "kind: Deployment\nspec:\n  replicas: 2\n"
            ),
        }])
        assert ok, reason
        ok2, _ = verify_evidence(WORKLOAD_REPLICAS, [{
            "target_path": "deploy/deployment.yaml",
            "content": "kind: Rollout\nspec:\n  replicas: 3\n",
        }])
        assert ok2


class TestWorkloadProbes:
    def test_injects_probes(self) -> None:
        src = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n  name: app\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: app\n"
            "          image: app:1\n"
            "          ports:\n"
            "            - containerPort: 8080\n"
        )
        out = patch_health_probes(src)
        assert has_health_probes(out)
        assert "livenessProbe" in out and "readinessProbe" in out

    def test_clear_evidence_ok(self) -> None:
        content = (
            "kind: Deployment\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: app\n"
            "          livenessProbe:\n"
            "            tcpSocket:\n"
            "              port: 8080\n"
            "          readinessProbe:\n"
            "            tcpSocket:\n"
            "              port: 8080\n"
        )
        ok, reason = verify_workload_probes([{
            "target_path": "deploy/deployment.yaml",
            "content": content,
        }])
        assert ok, reason
        ok2, _ = verify_evidence(WORKLOAD_PROBES, [{
            "target_path": "chart/templates/deploy.yaml",
            "content": content,
        }])
        assert ok2


class TestHelmChartAwareness:
    """Regression coverage for the pulse-agent#5/#6 class of bug: a Helm
    chart's ``replicas:`` line is never a literal digit, so the plain-text
    helpers above can never find or safely patch it — these prove the
    Helm-detection/values.yaml-patch path used by
    ``source_patches.enrich_workload_files_from_repo`` instead."""

    def test_helm_templated_replicas_key_detected(self) -> None:
        content = (
            "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
            "  replicas: {{ .Values.replicaCount }}\n"
        )
        assert helm_templated_replicas_key(content) == "replicaCount"

    def test_plain_literal_replicas_is_not_helm_templated(self) -> None:
        content = "kind: Deployment\nspec:\n  replicas: 1\n"
        assert helm_templated_replicas_key(content) is None

    def test_chart_root_for_template_path(self) -> None:
        assert chart_root_for_template_path("chart/templates/deployment.yaml") == "chart"
        assert chart_root_for_template_path("templates/deployment.yaml") == ""
        assert chart_root_for_template_path("deploy/deployment.yaml") is None

    def test_values_yaml_path_for_chart(self) -> None:
        assert values_yaml_path_for_chart("chart") == "chart/values.yaml"
        assert values_yaml_path_for_chart("") == "values.yaml"

    def test_patch_values_numeric_key_bumps_below_minimum(self) -> None:
        out = patch_values_numeric_key("replicaCount: 1\nimage:\n  tag: x\n", "replicaCount")
        assert out is not None
        assert "replicaCount: 2" in out
        assert values_yaml_replicas_at_least(out)

    def test_patch_values_numeric_key_noop_when_already_satisfied(self) -> None:
        content = "replicaCount: 3\n"
        assert patch_values_numeric_key(content, "replicaCount") == content

    def test_patch_values_numeric_key_refuses_nested_key(self) -> None:
        assert patch_values_numeric_key("x: 1\n", "deployment.replicaCount") is None

    def test_patch_values_numeric_key_refuses_missing_key(self) -> None:
        assert patch_values_numeric_key("image:\n  tag: x\n", "replicaCount") is None

    def test_verify_workload_replicas_accepts_values_yaml_evidence(self) -> None:
        ok, reason = verify_workload_replicas([{
            "target_path": "chart/values.yaml",
            "content": "replicaCount: 2\n",
        }])
        assert ok, reason


class TestEnrichWorkloadFilesFromRepo:
    """``enrich_workload_files_from_repo`` — the read_file/tree_paths
    enrichment that replaces a fabricated ``deploy/deployment.yaml`` stub
    with the app's real workload (or its chart's values.yaml)."""

    _STUB = {
        "path": "patch-workload-replicas",
        "content": "kind: Deployment\nspec:\n  replicas: 2\n",
        "description": "Generated by skill workload-replicas",
        "skill_name": "workload-replicas",
        "target_path": "deploy/deployment.yaml",
    }

    def test_helm_chart_patches_values_yaml_not_the_template(self) -> None:
        files = {
            "chart/templates/deployment.yaml": (
                "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
                "  replicas: {{ .Values.replicaCount }}\n"
            ),
            "chart/values.yaml": "replicaCount: 1\n",
        }
        out, drops = enrich_workload_files_from_repo(
            [dict(self._STUB)],
            read_file=lambda p: files.get(p),
            tree_paths=list(files),
        )
        assert not drops
        assert len(out) == 1
        assert out[0]["target_path"] == "chart/values.yaml"
        assert "replicaCount: 2" in out[0]["content"]
        # Never a second, conflicting literal ``replicas:`` key alongside
        # the chart's own templated one — the delivered file is values.yaml
        # only, the template itself is not part of the output at all.
        assert all(f["target_path"] != "chart/templates/deployment.yaml" for f in out)

    def test_plain_yaml_workload_is_patched_directly(self) -> None:
        files = {
            "deploy/app.yaml": "kind: Deployment\nspec:\n  replicas: 1\n",
        }
        out, drops = enrich_workload_files_from_repo(
            [dict(self._STUB)],
            read_file=lambda p: files.get(p),
            tree_paths=list(files),
        )
        assert not drops
        assert len(out) == 1
        assert out[0]["target_path"] == "deploy/app.yaml"
        assert replicas_at_least(out[0]["content"], minimum=2)

    def test_no_real_workload_found_drops_the_stub(self) -> None:
        files = {"README.md": "hello"}
        out, drops = enrich_workload_files_from_repo(
            [dict(self._STUB)],
            read_file=lambda p: None,
            tree_paths=list(files),
        )
        assert out == []
        assert drops and "no real Deployment/Rollout" in drops[0]

    def test_already_satisfied_workload_drops_stub_silently(self) -> None:
        files = {"deploy/app.yaml": "kind: Deployment\nspec:\n  replicas: 3\n"}
        out, drops = enrich_workload_files_from_repo(
            [dict(self._STUB)],
            read_file=lambda p: files.get(p),
            tree_paths=list(files),
        )
        assert out == []
        assert drops == []

    def test_health_probes_stub_is_retargeted_to_real_workload(self) -> None:
        stub = dict(self._STUB)
        stub["skill_name"] = "workload-health-probes"
        files = {
            "deploy/app.yaml": (
                "kind: Deployment\nspec:\n  template:\n    spec:\n      "
                "containers:\n        - name: app\n          image: app:1\n"
                "          ports:\n            - containerPort: 8080\n"
            ),
        }
        out, drops = enrich_workload_files_from_repo(
            [stub],
            read_file=lambda p: files.get(p),
            tree_paths=list(files),
        )
        assert not drops
        assert len(out) == 1
        assert out[0]["target_path"] == "deploy/app.yaml"
        assert has_health_probes(out[0]["content"])

    def test_non_workload_files_pass_through_unchanged(self) -> None:
        other = {"path": "x", "content": "y", "skill_name": "containerfile", "target_path": "Dockerfile"}
        out, drops = enrich_workload_files_from_repo(
            [dict(other)], read_file=lambda p: None, tree_paths=["README.md"],
        )
        assert out == [other]
        assert not drops

    def test_no_read_file_leaves_stub_untouched(self) -> None:
        out, drops = enrich_workload_files_from_repo(
            [dict(self._STUB)], read_file=None, tree_paths=["deploy/app.yaml"],
        )
        assert out == [self._STUB]
        assert drops == []


class TestDockerfileHarden:
    def test_adds_user_and_healthcheck(self) -> None:
        existing = "FROM python:latest\nWORKDIR /app\nCOPY . .\n"
        out = harden_dockerfile_content(
            existing,
            add_user=True,
            add_healthcheck=True,
            force_ubi=True,
            language="python",
        )
        assert "USER 1001" in out
        assert "HEALTHCHECK" in out
        assert "ubi" in out.lower()
        assert ":latest" not in out.split("FROM", 1)[-1].splitlines()[0]
