"""Tests for platform context discovery."""
from __future__ import annotations

from agentit.platform_context import PlatformContext, _check_deprecations, offline_context


class TestPlatformContext:
    def test_has_api(self):
        ctx = PlatformContext(available_kinds={"deployments", "services", "networkpolicies"})
        assert ctx.has_api("deployments")
        assert ctx.has_api("Deployments")  # case-insensitive
        assert not ctx.has_api("foobar")

    def test_has_operator(self):
        ctx = PlatformContext(installed_operators=["openshift-pipelines-operator-rh.v1.14"])
        assert ctx.has_operator("pipelines")
        assert not ctx.has_operator("nonexistent")

    def test_has_crd(self):
        ctx = PlatformContext(installed_crds=["pipelines.tekton.dev", "applications.argoproj.io"])
        assert ctx.has_crd("tekton.dev")
        assert not ctx.has_crd("litmus")

    def test_summary(self):
        ctx = offline_context()
        s = ctx.summary()
        assert "1.28" in s
        assert "OpenShift" in s

    def test_to_prompt_context(self):
        ctx = offline_context()
        prompt = ctx.to_prompt_context()
        assert "Kubernetes 1.28" in prompt
        assert "OpenShift 4.15" in prompt

    def test_deprecation_check(self):
        deps = _check_deprecations("1.25")
        apis = [d["api"] for d in deps]
        assert any("PodSecurityPolicy" in a for a in apis)
        assert any("v1beta1 Ingress" in a for a in apis)

    def test_no_deprecations_old_cluster(self):
        deps = _check_deprecations("1.10")
        # Only Tekton deprecations (0.44) match — no K8s-native deprecations at 1.10
        k8s_apis = [d for d in deps if not d["api"].startswith("tekton")]
        assert len(k8s_apis) == 0

    def test_offline_context(self):
        ctx = offline_context("1.30", "4.16")
        assert ctx.k8s_version == "1.30"
        assert ctx.ocp_version == "4.16"
        assert "deployments" in ctx.available_kinds
        assert len(ctx.deprecated_apis) > 0
