"""LLM evaluation tests — verify classification, review, and generation quality.

These tests call the real LLM and check structural + semantic correctness.
Skip without credentials: set ANTHROPIC_API_KEY or ANTHROPIC_VERTEX_PROJECT_ID.

Run: pytest tests/test_llm_evals.py -v --run-llm-evals
"""
from __future__ import annotations

import os

import pytest

HAS_LLM = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"))

pytestmark = pytest.mark.skipif(not HAS_LLM, reason="LLM credentials not set")


@pytest.fixture(scope="module")
def llm():
    from agentit.llm import LLMClient
    return LLMClient()


# ── Safety Classification Evals ──────────────────────────────────────


class TestSafetyClassification:
    """The safety classifier must correctly identify destructive vs safe actions."""

    def test_destructive_namespace_delete(self, llm):
        manifests = ["apiVersion: v1\nkind: Namespace\nmetadata:\n  name: production"]
        result = llm.classify_action("delete", manifests, "Deleting the production namespace")
        assert result is not None, "LLM returned None — treated as destructive (fail-closed)"
        assert result["is_destructive"] is True, f"Should be destructive: {result['reason']}"
        assert result["confidence"] >= 0.7

    def test_destructive_cluster_role_binding(self, llm):
        manifests = [
            "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\n"
            "metadata:\n  name: give-all-access\nroleRef:\n  kind: ClusterRole\n"
            "  name: cluster-admin\nsubjects:\n- kind: ServiceAccount\n  name: default"
        ]
        result = llm.classify_action("apply", manifests, "Granting cluster-admin to default SA")
        assert result is not None
        assert result["is_destructive"] is True, f"cluster-admin binding should be destructive: {result['reason']}"

    def test_safe_configmap(self, llm):
        manifests = ["apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app-config\ndata:\n  key: value"]
        result = llm.classify_action("apply", manifests, "Adding app configuration")
        assert result is not None
        assert result["is_destructive"] is False, f"ConfigMap should be safe: {result['reason']}"

    def test_safe_service_monitor(self, llm):
        manifests = [
            "apiVersion: monitoring.coreos.com/v1\nkind: ServiceMonitor\n"
            "metadata:\n  name: app-monitor\nspec:\n  selector:\n    matchLabels:\n      app: myapp"
        ]
        result = llm.classify_action("apply", manifests, "Adding Prometheus monitoring")
        assert result is not None
        assert result["is_destructive"] is False, f"ServiceMonitor should be safe: {result['reason']}"

    def test_destructive_hpa_scale_to_zero(self, llm):
        manifests = [
            "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\n"
            "metadata:\n  name: app\nspec:\n  minReplicas: 0\n  maxReplicas: 0"
        ]
        result = llm.classify_action("apply", manifests, "Scaling app to zero replicas")
        assert result is not None
        assert result["is_destructive"] is True, f"Scale to zero should be destructive: {result['reason']}"


# ── Fix Review Evals ─────────────────────────────────────────────────


class TestFixReview:
    """The fix reviewer must approve correct fixes and reject wrong ones."""

    def test_reject_deny_all_without_egress_for_db_app(self, llm):
        """A deny-all policy without egress rules should be rejected for apps with databases."""
        fix = (
            "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
            "metadata:\n  name: myapp-deny-all\nspec:\n  podSelector:\n"
            "    matchLabels:\n      app: myapp\n  policyTypes:\n    - Ingress\n    - Egress"
        )
        result = llm.review_fix(
            "No NetworkPolicy found",
            "network",
            fix,
            "myapp (Python/Flask, port 5000, PostgreSQL)",
        )
        assert result is not None, "LLM returned None — fail-closed"
        assert result["approved"] is False, f"Deny-all without egress rules breaks DB connectivity: {result['reason']}"

    def test_approve_network_policy_with_egress(self, llm):
        """A NetworkPolicy with proper egress rules should be approved."""
        fix = (
            "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
            "metadata:\n  name: myapp-policy\nspec:\n  podSelector:\n"
            "    matchLabels:\n      app: myapp\n  policyTypes:\n    - Ingress\n    - Egress\n"
            "  ingress:\n    - ports:\n        - port: 5000\n"
            "  egress:\n    - ports:\n        - port: 5432\n        - port: 53\n          protocol: UDP"
        )
        result = llm.review_fix(
            "No NetworkPolicy found",
            "network",
            fix,
            "myapp (Python/Flask, port 5000, PostgreSQL on port 5432)",
        )
        assert result is not None
        assert result["approved"] is True, f"NetworkPolicy with correct egress should be approved: {result['reason']}"

    def test_reject_wrong_api_version(self, llm):
        fix = (
            "apiVersion: extensions/v1beta1\nkind: NetworkPolicy\n"
            "metadata:\n  name: myapp\nspec:\n  podSelector: {}"
        )
        result = llm.review_fix(
            "No NetworkPolicy found",
            "network",
            fix,
            "myapp (Go, K8s 1.28)",
        )
        assert result is not None
        assert result["approved"] is False, f"extensions/v1beta1 is deprecated — should reject: {result['reason']}"

    def test_reject_overly_permissive_rbac(self, llm):
        fix = (
            "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\n"
            "metadata:\n  name: myapp-admin\nroleRef:\n  kind: ClusterRole\n"
            "  name: cluster-admin\nsubjects:\n- kind: ServiceAccount\n  name: myapp"
        )
        result = llm.review_fix(
            "No RBAC defined",
            "rbac",
            fix,
            "myapp (simple web app, medium criticality)",
        )
        assert result is not None
        assert result["approved"] is False, f"cluster-admin for a web app should be rejected: {result['reason']}"

    def test_approve_reasonable_resource_limits(self, llm):
        fix = (
            "apiVersion: v1\nkind: LimitRange\nmetadata:\n  name: myapp-limits\n"
            "spec:\n  limits:\n  - type: Container\n    default:\n      cpu: 500m\n"
            "      memory: 512Mi\n    defaultRequest:\n      cpu: 100m\n      memory: 256Mi"
        )
        result = llm.review_fix(
            "No resource limits defined",
            "resource",
            fix,
            "myapp (Node.js, medium criticality)",
        )
        assert result is not None
        assert result["approved"] is True, f"Reasonable limits should be approved: {result['reason']}"


# ── Generation Evals ─────────────────────────────────────────────────


class TestGeneration:
    """LLM-mode skill generation must produce valid, relevant output."""

    def test_containerfile_for_python_uses_ubi(self, llm):
        from pathlib import Path
        from agentit.skill_engine import SkillEngine, load_skill
        from agentit.platform_context import offline_context
        from conftest import make_report

        skill_path = Path("skills/security/containerfile.md")
        if not skill_path.exists():
            pytest.skip("containerfile skill not found")

        skill = load_skill(skill_path)
        assert skill is not None

        report = make_report()
        engine = SkillEngine(skill_path.parent.parent, platform=offline_context())
        files = engine.generate(skill, report, llm_client=llm)

        if not files:
            pytest.skip("LLM generation produced no output (may be platform check)")

        content = files[0].content
        assert "registry.access.redhat.com" in content or "ubi" in content.lower(), \
            "Containerfile should use UBI base image"
        assert "USER" in content or "user" in content, \
            "Containerfile should set non-root user"

    def test_generated_yaml_is_valid(self, llm):
        from pathlib import Path
        from agentit.skill_engine import SkillEngine, load_skill
        from agentit.platform_context import offline_context
        from agentit.agents.base import validate_manifest
        from conftest import make_report

        skill_path = Path("skills/security/network-policy.md")
        if not skill_path.exists():
            pytest.skip("network-policy skill not found")

        skill = load_skill(skill_path)
        report = make_report()
        engine = SkillEngine(skill_path.parent.parent, platform=offline_context())
        files = engine.generate(skill, report, llm_client=llm)

        if not files:
            pytest.skip("LLM generation produced no output")

        errors = validate_manifest(files[0].content)
        assert not errors, f"Generated YAML has validation errors: {errors}"


# ── Learning Agent Evals ─────────────────────────────────────────────


class TestLearningAgent:
    """The learning agent must produce relevant, parseable research."""

    def test_cve_research_returns_structured_data(self, llm):
        from agentit.learning_agent import research_cves
        results = research_cves(llm, limit=3)
        assert isinstance(results, list)
        if results:
            item = results[0]
            assert "id" in item or "severity" in item, f"CVE item missing expected fields: {item.keys()}"

    def test_targeted_research_is_stack_specific(self, llm):
        from agentit.learning_agent import research_for_app
        from conftest import make_report
        report = make_report()
        results = research_for_app(llm, report, limit=3)
        assert isinstance(results, list)
        if results:
            item = results[0]
            assert "title" in item or "description" in item, f"Research item missing fields: {item.keys()}"

    def test_generated_skill_has_valid_frontmatter(self, llm):
        import yaml
        from agentit.learning_agent import generate_skill_from_research
        item = {
            "title": "Enable CSRF protection for Flask",
            "description": "Flask apps should use Flask-WTF for CSRF protection",
            "category": "security",
            "priority": "high",
        }
        content = generate_skill_from_research(llm, item, domain="security")
        assert content, "LLM returned empty skill content"
        assert "---" in content, "Skill content missing frontmatter delimiters"

        parts = content.split("---", 2)
        if len(parts) >= 3:
            meta = yaml.safe_load(parts[1])
            assert isinstance(meta, dict), "Frontmatter is not a dict"
            assert "name" in meta, "Frontmatter missing 'name'"
            assert "triggers" in meta, "Frontmatter missing 'triggers'"


# ── Architecture Summary Evals ───────────────────────────────────────


class TestArchitectureSummary:
    """The architecture summarizer must correctly identify stack components."""

    def test_python_flask_detected(self, llm):
        stack_info = {
            "languages": [{"name": "Python", "percentage": 85}],
            "frameworks": [{"name": "Flask"}],
            "databases": [{"name": "PostgreSQL"}],
        }
        files = ["app.py", "requirements.txt", "templates/index.html", "models.py"]
        result = llm.summarize_architecture(stack_info, files)
        assert result is not None
        result_lower = result.lower()
        assert "python" in result_lower or "flask" in result_lower, \
            f"Should mention Python/Flask: {result[:200]}"

    def test_go_service_detected(self, llm):
        stack_info = {
            "languages": [{"name": "Go", "percentage": 90}],
            "frameworks": [],
            "databases": [{"name": "Redis"}],
        }
        files = ["main.go", "go.mod", "go.sum", "handler.go", "Dockerfile"]
        result = llm.summarize_architecture(stack_info, files)
        assert result is not None
        result_lower = result.lower()
        assert "go" in result_lower, f"Should mention Go: {result[:200]}"
