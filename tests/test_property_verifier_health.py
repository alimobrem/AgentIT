"""property_verifier._verify_health_probes -- the 5th property verifier,
closing property_verifier.py:143-148's "only 4 categories" scope gap for
`health` (see skills/infrastructure/health-probes-policy.md for the full
design rationale on why this is a Kyverno mutate policy, not a Deployment
patch, and why this check must therefore recognize *both* shapes).
"""
from __future__ import annotations

from pathlib import Path

import yaml

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

    def test_policy_gates_each_probe_independently_and_requires_a_real_port(self):
        """Regression guard for two real safety gaps found in review, before
        this policy could ever reach a live cluster:

        1. A single shared precondition (only checking `livenessProbe` is
           empty) gated *both* patches -- a container with an existing
           `readinessProbe` but no `livenessProbe` would have its real probe
           silently replaced (JSON Patch `op: add` on an existing path
           overwrites it, RFC 6902), violating this skill's own stated
           "never overwrite an existing probe" constraint.
        2. `tcpSocket.port` referenced `element.ports[0].containerPort` with
           no check that the container actually declares a port. A
           background-worker container with zero ports would evaluate to a
           null/empty port, which the API server's schema validation for
           `Probe.tcpSocket.port` (a required field) rejects at admission --
           breaking *every* future update to that Deployment, not just this
           one, until the policy is fixed or removed.

        Verified structurally (no Kyverno CLI available in this sandbox):
        the generated policy must have two independent `foreach` entries,
        each gated on its own probe field being empty AND on a real port
        being present, with the liveness entry's patch never touching
        `readinessProbe` and vice versa.
        """
        engine = SkillEngine(Path("skills"), platform=None)
        skill = load_skill(Path("skills/infrastructure/health-probes-policy.md"))
        assert skill is not None
        files = engine.generate(skill, make_report(repo_name="pinky"), llm_client=None)
        assert files

        policy = yaml.safe_load(files[0].content)
        rule = policy["spec"]["rules"][0]
        foreach_entries = rule["mutate"]["foreach"]
        assert len(foreach_entries) == 2, (
            "expected one independent foreach entry per probe, not a single "
            "entry patching both"
        )

        by_probe: dict[str, dict] = {}
        for entry in foreach_entries:
            patch_docs = list(yaml.safe_load_all(entry["patchesJson6902"]))
            assert len(patch_docs) == 1, "each entry must patch exactly one probe field"
            patch = patch_docs[0][0]
            probe_field = patch["path"].rsplit("/", 1)[-1]
            assert probe_field in ("livenessProbe", "readinessProbe")
            by_probe[probe_field] = entry

        assert set(by_probe) == {"livenessProbe", "readinessProbe"}

        for probe_field, entry in by_probe.items():
            preconditions = entry["preconditions"]["all"]
            precondition_keys = {p["key"] for p in preconditions}
            # Gated on its OWN field, never the other probe's.
            assert any(probe_field in k for k in precondition_keys), (
                f"{probe_field} entry's precondition must check {probe_field} "
                f"itself, not the other probe -- got {precondition_keys}"
            )
            other_field = "readinessProbe" if probe_field == "livenessProbe" else "livenessProbe"
            assert not any(other_field in k for k in precondition_keys), (
                f"{probe_field} entry must not be gated on {other_field} -- "
                f"got {precondition_keys}"
            )
            # Must also require a real, non-empty port before patching.
            assert any("ports[0].containerPort" in k for k in precondition_keys), (
                f"{probe_field} entry has no port-existence precondition -- "
                f"a portless container would get an invalid tcpSocket.port"
            )

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
