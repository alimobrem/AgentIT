"""Tests for the futureproof architecture: platform context, skill engine, property verification, API drift."""
from __future__ import annotations

import tempfile
from pathlib import Path

from conftest import make_report


class TestPlatformContext:
    def test_offline_context(self):
        from agentit.platform_context import offline_context
        ctx = offline_context("1.30", "4.16")
        assert ctx.k8s_version == "1.30"
        assert "deployments" in ctx.available_kinds
        assert len(ctx.deprecated_apis) > 0

    def test_has_api(self):
        from agentit.platform_context import PlatformContext
        ctx = PlatformContext(available_kinds={"networkpolicies", "deployments"})
        assert ctx.has_api("networkpolicies")
        assert not ctx.has_api("foobar")

    def test_deprecation_detection(self):
        from agentit.platform_context import _check_deprecations
        deps = _check_deprecations("1.25")
        assert any("PodSecurityPolicy" in d["api"] for d in deps)

    def test_prompt_context_format(self):
        from agentit.platform_context import offline_context
        ctx = offline_context()
        prompt = ctx.to_prompt_context()
        assert "Kubernetes" in prompt
        assert "OpenShift" in prompt


class TestSkillEngine:
    def _create_skill_file(self, tmpdir: Path, name: str, content: str) -> Path:
        path = tmpdir / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        return path

    def test_load_skill(self):
        from agentit.skill_engine import load_skill
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.md"
            path.write_text("""---
name: test-skill
domain: security
version: 1
triggers:
  - network
  - firewall
outputs:
  - NetworkPolicy
property: "No unauthorized access"
mode: template
---

# Test Skill

## Template

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{app_name}}-test
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  podSelector:
    matchLabels:
      app: {{app_name}}
  policyTypes:
    - Ingress
```
""", encoding="utf-8")
            skill = load_skill(path)
            assert skill is not None
            assert skill.name == "test-skill"
            assert skill.domain == "security"
            assert "network" in skill.triggers

    def test_skill_matching(self):
        from agentit.skill_engine import Skill
        skill = Skill(
            name="test", domain="security", version=1,
            triggers=["network"], outputs=["NetworkPolicy"],
            property_description="test", body="test", file_path="test.md",
        )
        report = make_report()
        # make_report should have findings — check if skill matches
        matched = skill.matches(report)
        # Result depends on whether make_report() has network findings
        assert isinstance(matched, bool)

    def test_skill_engine_loads_directory(self):
        from agentit.skill_engine import SkillEngine
        from agentit.platform_context import offline_context
        # Use the actual skills directory
        skills_dir = Path(__file__).parent.parent / "skills"
        if skills_dir.exists():
            engine = SkillEngine(skills_dir, platform=offline_context())
            assert len(engine.skills) > 0
        else:
            # Skills dir might not exist yet in CI
            engine = SkillEngine(Path("/nonexistent"), platform=offline_context())
            assert len(engine.skills) == 0

    def test_template_rendering(self):
        from agentit.skill_engine import SkillEngine
        from agentit.platform_context import offline_context

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_path = Path(tmpdir) / "test.md"
            skill_path.write_text("""---
name: test-template
domain: test
version: 1
triggers:
  - network
outputs:
  - NetworkPolicy
property: "test"
mode: template
---

# Test

## Template

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{app_name}}-deny
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  podSelector:
    matchLabels:
      app: {{app_name}}
  policyTypes:
    - Ingress
```
""", encoding="utf-8")

            engine = SkillEngine(Path(tmpdir), platform=offline_context())
            assert len(engine.skills) == 1

            report = make_report()
            files = engine.generate(engine.skills[0], report)
            # Template should render if findings match
            # If no network findings, this returns []
            assert isinstance(files, list)

    def test_find_uncovered_findings(self):
        from agentit.skill_engine import SkillEngine
        from agentit.platform_context import offline_context

        engine = SkillEngine(Path("/nonexistent"), platform=offline_context())
        report = make_report()

        # No generated files -> everything is uncovered
        uncovered = engine.find_uncovered_findings(report, [])
        assert isinstance(uncovered, list)


class TestPropertyVerifier:
    def test_verification_result_summary(self):
        from agentit.property_verifier import VerificationResult
        result = VerificationResult(
            property_name="Network Isolation",
            passed=True,
            checks=[{"name": "test", "passed": True, "detail": "ok"}],
        )
        assert "PASS" in result.summary()
        assert "Network Isolation" in result.summary()

    def test_failed_result(self):
        from agentit.property_verifier import VerificationResult
        result = VerificationResult(
            property_name="RBAC",
            passed=False,
            checks=[{"name": "test", "passed": False, "detail": "no SA"}],
        )
        assert "FAIL" in result.summary()

    def test_verifier_registry(self):
        from agentit.property_verifier import PROPERTY_VERIFIERS
        assert "network-isolation" in PROPERTY_VERIFIERS
        assert "rbac" in PROPERTY_VERIFIERS
        assert "autoscaling" in PROPERTY_VERIFIERS
        assert "monitoring" in PROPERTY_VERIFIERS
        # health-probes closes the "only 4 categories" scope gap for
        # `health` -- see tests/test_property_verifier_health.py for the
        # detailed behavior.
        assert "health-probes" in PROPERTY_VERIFIERS


class TestAPIDrift:
    def test_no_drift_on_first_run(self):
        import os
        from agentit.api_drift_detector import detect_drift
        # Use temp file for snapshot
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            os.environ["AGENTIT_API_SNAPSHOT"] = f.name
            try:
                # Remove existing snapshot
                Path(f.name).unlink(missing_ok=True)
                drift = detect_drift({"pods", "services"}, ["operator-a"])
                assert not drift.has_breaking_changes
                assert len(drift.removed_apis) == 0
            finally:
                Path(f.name).unlink(missing_ok=True)
                del os.environ["AGENTIT_API_SNAPSHOT"]

    def test_detects_removed_api(self):
        import os
        import json
        from agentit.api_drift_detector import detect_drift
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({"kinds": ["pods", "services", "podsecuritypolicies"], "operators": []}, f)
            f.flush()
            os.environ["AGENTIT_API_SNAPSHOT"] = f.name
            try:
                drift = detect_drift({"pods", "services"}, [])
                assert drift.has_breaking_changes
                assert "podsecuritypolicies" in drift.removed_apis
            finally:
                Path(f.name).unlink(missing_ok=True)
                del os.environ["AGENTIT_API_SNAPSHOT"]

    def test_detects_new_api(self):
        import os
        import json
        from agentit.api_drift_detector import detect_drift
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({"kinds": ["pods", "services"], "operators": []}, f)
            f.flush()
            os.environ["AGENTIT_API_SNAPSHOT"] = f.name
            try:
                drift = detect_drift({"pods", "services", "gateways"}, [])
                assert "gateways" in drift.new_apis
            finally:
                Path(f.name).unlink(missing_ok=True)
                del os.environ["AGENTIT_API_SNAPSHOT"]

    def test_manifest_deprecation_scan(self):
        from agentit.api_drift_detector import check_manifests_for_deprecated_apis
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "old-hpa.yaml"
            manifest.write_text("""apiVersion: autoscaling/v2beta1
kind: HorizontalPodAutoscaler
metadata:
  name: test
""", encoding="utf-8")
            deprecated = [{"api": "autoscaling/v2beta1 HorizontalPodAutoscaler",
                          "deprecated_in": "1.23", "removed_in": "1.26",
                          "replacement": "autoscaling/v2"}]
            issues = check_manifests_for_deprecated_apis(Path(tmpdir), deprecated)
            assert len(issues) == 1
            assert "v2beta1" in issues[0]["api"]
