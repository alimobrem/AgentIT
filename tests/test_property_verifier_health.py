"""property_verifier._verify_health_probes -- the 5th property verifier,
closing property_verifier.py:143-148's "only 4 categories" scope gap for
`health` (see skills/infrastructure/health-probes-policy.md for the full
design rationale on why this is a Kyverno mutate policy, not a Deployment
patch, and why this check must therefore recognize *both* shapes).
"""
from __future__ import annotations

from pathlib import Path

from agentit.agents.base import GeneratedFile
from agentit.property_verifier import verify_all_properties
from agentit.skill_engine import SkillEngine, load_skill
from conftest import make_report


def _gen(path: str, content: str) -> GeneratedFile:
    return GeneratedFile(path=path, content=content, description="test fixture")


class TestVerifyHealthProbesDirectWorkload:
    def test_deployment_with_both_probes_on_every_container_passes(self):
        content = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n  name: pinky\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: pinky\n"
            "          livenessProbe: {tcpSocket: {port: http}}\n"
            "          readinessProbe: {tcpSocket: {port: http}}\n"
        )
        results = verify_all_properties([_gen("dep.yaml", content)])
        health = next(r for r in results if r.property_name == "Health Probes")
        assert health.passed is True

    def test_deployment_missing_readiness_probe_fails(self):
        content = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n  name: pinky\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: pinky\n"
            "          livenessProbe: {tcpSocket: {port: http}}\n"
        )
        results = verify_all_properties([_gen("dep.yaml", content)])
        health = next(r for r in results if r.property_name == "Health Probes")
        assert health.passed is False

    def test_rollout_with_probes_also_recognized(self):
        content = (
            "apiVersion: argoproj.io/v1alpha1\n"
            "kind: Rollout\n"
            "metadata:\n  name: pinky\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: pinky\n"
            "          livenessProbe: {tcpSocket: {port: http}}\n"
            "          readinessProbe: {tcpSocket: {port: http}}\n"
        )
        results = verify_all_properties([_gen("rollout.yaml", content)])
        health = next(r for r in results if r.property_name == "Health Probes")
        assert health.passed is True

    def test_no_workload_and_no_policy_fails_not_vacuous_pass(self):
        content = (
            "apiVersion: v1\n"
            "kind: Service\n"
            "metadata:\n  name: pinky\n"
            "spec:\n  ports: [{port: 8080}]\n"
        )
        results = verify_all_properties([_gen("svc.yaml", content)])
        health = next(r for r in results if r.property_name == "Health Probes")
        assert health.passed is False


class TestVerifyHealthProbesMutatePolicy:
    """The health-probes-policy.md skill's own real output must pass this
    check directly from its YAML -- otherwise auto_delivery.py's validate/
    fix loop would spuriously retry-loop on its own correct fix."""

    def test_health_probes_policy_skill_output_passes(self):
        engine = SkillEngine(Path("skills"), platform=None)
        skill = load_skill(Path("skills/infrastructure/health-probes-policy.md"))
        assert skill is not None
        report = make_report(repo_name="pinky")
        files = engine.generate(skill, report, llm_client=None)
        assert files
        results = verify_all_properties(files)
        health = next(r for r in results if r.property_name == "Health Probes")
        assert health.passed is True

    def test_policy_without_livenessprobe_and_readinessprobe_text_does_not_count(self):
        content = (
            "apiVersion: kyverno.io/v1\n"
            "kind: Policy\n"
            "metadata:\n  name: pinky-unrelated\n"
            "spec:\n"
            "  rules:\n"
            "    - name: require-labels\n"
            "      mutate:\n"
            "        patchStrategicMerge:\n"
            "          metadata:\n"
            "            labels:\n"
            "              team: platform\n"
        )
        results = verify_all_properties([_gen("policy.yaml", content)])
        health = next(r for r in results if r.property_name == "Health Probes")
        assert health.passed is False
