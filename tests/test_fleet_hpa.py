"""Fleet HPA workload discovery + fail-closed scaleTargetRef gate."""
from __future__ import annotations

from agentit.portal.fleet_hpa import (
    NamespaceWorkloads,
    WorkloadRef,
    filter_fleet_hpa_files,
    fleet_hpa_correctness_reason,
    generate_fleet_hpa_yaml,
)


def _hpa_yaml(
    *,
    kind: str = "Deployment",
    name: str = "pinky",
    api_version: str = "apps/v1",
    hpa_name: str = "pinky-hpa",
) -> str:
    return (
        "apiVersion: autoscaling/v2\n"
        "kind: HorizontalPodAutoscaler\n"
        "metadata:\n"
        f"  name: {hpa_name}\n"
        "spec:\n"
        "  scaleTargetRef:\n"
        f"    apiVersion: {api_version}\n"
        f"    kind: {kind}\n"
        f"    name: {name}\n"
        "  minReplicas: 2\n"
        "  maxReplicas: 10\n"
    )


class TestPreferredScaleTargets:
    def test_prefers_exact_rollout_over_suffixed_deployments(self) -> None:
        wl = NamespaceWorkloads(
            namespace="pinky",
            deployments=("pinky-api", "pinky-web", "pinky-worker", "pinky-temporal"),
            rollouts=("pinky",),
        )
        targets = wl.preferred_scale_targets("pinky")
        assert targets == [
            WorkloadRef(
                kind="Rollout",
                name="pinky",
                api_version="argoproj.io/v1alpha1",
            )
        ]

    def test_prefers_exact_deployment_when_no_rollout(self) -> None:
        wl = NamespaceWorkloads(
            namespace="demo",
            deployments=("demo", "demo-redis"),
            rollouts=(),
        )
        assert wl.preferred_scale_targets("demo") == [
            WorkloadRef(kind="Deployment", name="demo", api_version="apps/v1")
        ]

    def test_multi_service_prefers_api_web_worker_skips_infra(self) -> None:
        wl = NamespaceWorkloads(
            namespace="pinky",
            deployments=(
                "pinky-api",
                "pinky-web",
                "pinky-worker",
                "pinky-temporal",
                "pinky-temporal-ui",
            ),
            rollouts=(),
        )
        names = [t.name for t in wl.preferred_scale_targets("pinky")]
        assert names == ["pinky-api", "pinky-web", "pinky-worker"]

    def test_empty_when_no_matching_workloads(self) -> None:
        wl = NamespaceWorkloads(
            namespace="other",
            deployments=("unrelated",),
            rollouts=(),
        )
        assert wl.preferred_scale_targets("pinky") == []


class TestFleetHpaCorrectness:
    def test_refuses_missing_deployment_pinky_class(self) -> None:
        """gitops #18 shape: Deployment/pinky when only pinky-api/… exist."""
        wl = NamespaceWorkloads(
            namespace="pinky",
            deployments=("pinky-api", "pinky-web", "pinky-worker"),
            rollouts=("pinky",),
        )
        why = fleet_hpa_correctness_reason(
            _hpa_yaml(kind="Deployment", name="pinky"),
            wl,
            app_name="pinky",
        )
        assert why is not None
        assert "not found" in why
        assert "Rollout/pinky" in why or "Rollouts:" in why

    def test_accepts_rollout_target(self) -> None:
        wl = NamespaceWorkloads(
            namespace="pinky",
            deployments=("pinky-api",),
            rollouts=("pinky",),
        )
        why = fleet_hpa_correctness_reason(
            _hpa_yaml(
                kind="Rollout",
                name="pinky",
                api_version="argoproj.io/v1alpha1",
            ),
            wl,
            app_name="pinky",
        )
        assert why is None

    def test_accepts_real_deployment_target(self) -> None:
        wl = NamespaceWorkloads(
            namespace="pinky",
            deployments=("pinky-api",),
            rollouts=(),
        )
        why = fleet_hpa_correctness_reason(
            _hpa_yaml(kind="Deployment", name="pinky-api"),
            wl,
            app_name="pinky",
        )
        assert why is None

    def test_fail_closed_when_discovery_failed(self) -> None:
        wl = NamespaceWorkloads(
            namespace="pinky",
            deployments=(),
            rollouts=(),
            discovery_ok=False,
        )
        why = fleet_hpa_correctness_reason(_hpa_yaml(), wl, app_name="pinky")
        assert why is not None
        assert "discovery failed" in why

    def test_non_hpa_content_passes(self) -> None:
        wl = NamespaceWorkloads(
            namespace="pinky", deployments=(), rollouts=(), discovery_ok=False,
        )
        assert fleet_hpa_correctness_reason(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n",
            wl,
        ) is None


class TestGenerateAndFilter:
    def test_generate_rollout_hpa_for_pinky_shape(self) -> None:
        wl = NamespaceWorkloads(
            namespace="pinky",
            deployments=("pinky-api", "pinky-web", "pinky-worker"),
            rollouts=("pinky",),
        )
        yaml_text = generate_fleet_hpa_yaml("pinky", wl)
        assert yaml_text is not None
        assert "kind: Rollout" in yaml_text
        assert "name: pinky\n" in yaml_text
        assert "kind: Deployment" not in yaml_text.split("scaleTargetRef:")[1][:80]

    def test_generate_multi_service_when_no_exact_match(self) -> None:
        wl = NamespaceWorkloads(
            namespace="pinky",
            deployments=("pinky-api", "pinky-web", "pinky-temporal"),
            rollouts=(),
        )
        yaml_text = generate_fleet_hpa_yaml("pinky", wl)
        assert yaml_text is not None
        assert yaml_text.count("kind: HorizontalPodAutoscaler") == 2
        assert "name: pinky-api" in yaml_text
        assert "name: pinky-web" in yaml_text
        assert "pinky-temporal" not in yaml_text

    def test_filter_drops_bad_keeps_good(self) -> None:
        wl = NamespaceWorkloads(
            namespace="pinky",
            deployments=("pinky-api",),
            rollouts=("pinky",),
        )
        files = [
            {"path": "bad.yaml", "content": _hpa_yaml(kind="Deployment", name="pinky")},
            {
                "path": "good.yaml",
                "content": _hpa_yaml(
                    kind="Rollout",
                    name="pinky",
                    api_version="argoproj.io/v1alpha1",
                ),
            },
            {"path": "cm.yaml", "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"},
        ]
        kept, reasons = filter_fleet_hpa_files(files, wl, app_name="pinky")
        assert [f["path"] for f in kept] == ["good.yaml", "cm.yaml"]
        assert len(reasons) == 1
        assert "bad.yaml" in reasons[0]
