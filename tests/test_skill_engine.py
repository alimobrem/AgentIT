"""Tests for skill_engine.py: pluralization, platform gating, LLM passthrough."""

from __future__ import annotations

from pathlib import Path

from agentit.platform_context import offline_context
from agentit.skill_engine import Skill, SkillEngine, _pluralize_kind
from conftest import make_report


class TestPluralizeKind:
    """Regression: naive `+s` mis-pluralizes irregular K8s kinds."""

    def test_network_policy_pluralizes_correctly(self) -> None:
        assert _pluralize_kind("NetworkPolicy") == "networkpolicies"

    def test_policy_pluralizes_correctly(self) -> None:
        assert _pluralize_kind("Policy") == "policies"

    def test_ingress_pluralizes_correctly(self) -> None:
        assert _pluralize_kind("Ingress") == "ingresses"

    def test_regular_kinds_fall_back_to_naive_plus_s(self) -> None:
        assert _pluralize_kind("Deployment") == "deployments"
        assert _pluralize_kind("ConfigMap") == "configmaps"
        assert _pluralize_kind("ServiceAccount") == "serviceaccounts"


def _make_skill(name: str, outputs: list[str], mode: str = "llm", triggers: list[str] | None = None) -> Skill:
    return Skill(
        name=name,
        domain="security",
        version=1,
        triggers=triggers or ["network"],
        outputs=outputs,
        property_description=f"{name} property",
        body="# no template block, LLM-only skill",
        file_path=f"skills/security/{name}.md",
        mode=mode,
    )


_NETPOL_TEMPLATE_BODY = """# Network Policy (template)

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{app_name}}-netpol
spec:
  podSelector: {}
  policyTypes:
    - Ingress
```
"""


class TestPlatformGating:
    """Regression: the platform-kind-gating check in generate() must recognize
    the real (plural) API resource name for irregular kinds like NetworkPolicy.

    Before the fix, `output_kind.lower() + "s"` produced "networkpolicys",
    which never matches offline_context's "networkpolicies" -- so the skill
    was always wrongly skipped, even for a template-mode skill with no LLM
    involved at all.
    """

    def test_network_policy_kind_not_wrongly_gated_out(self, tmp_path: Path) -> None:
        engine = SkillEngine(tmp_path, platform=offline_context())
        skill = _make_skill(
            "network-policy-template", outputs=["NetworkPolicy"], mode="template",
        )
        skill.body = _NETPOL_TEMPLATE_BODY
        report = make_report()

        files = engine.generate(skill, report, llm_client=None)

        assert len(files) == 1, "NetworkPolicy skill was wrongly gated out by platform-kind check"
        assert "NetworkPolicy" in files[0].content

    def test_ingress_kind_recognized_on_platform(self, tmp_path: Path) -> None:
        engine = SkillEngine(tmp_path, platform=offline_context())
        assert engine.platform.has_api(_pluralize_kind("Ingress"))


class _FakeLLMClient:
    """Stub LLM client recording calls and returning a canned manifest."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def _chat(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.response


class TestLLMPassthrough:
    """Regression: LLM-only skills (mode: llm, no template block) must
    actually use the LLM client when one is supplied to generate()/run_all()."""

    def test_llm_only_skill_uses_llm_client_when_provided(self, tmp_path: Path) -> None:
        engine = SkillEngine(tmp_path, platform=offline_context())
        skill = _make_skill("network-policy", outputs=["NetworkPolicy"])
        report = make_report()

        manifest = (
            "apiVersion: networking.k8s.io/v1\n"
            "kind: NetworkPolicy\n"
            "metadata:\n"
            "  name: test-app-netpol\n"
            "spec:\n"
            "  podSelector: {}\n"
            "  policyTypes:\n"
            "    - Ingress\n"
            "    - Egress\n"
        )
        fake_llm = _FakeLLMClient(manifest)

        files = engine.generate(skill, report, llm_client=fake_llm)

        assert len(fake_llm.calls) == 1, "LLM-only skill never invoked the LLM client"
        assert len(files) == 1
        assert "NetworkPolicy" in files[0].content

    def test_llm_only_skill_produces_nothing_without_llm_client(self, tmp_path: Path) -> None:
        """LLM-only skills have no template fallback -- without an LLM client
        they legitimately produce no file (this is expected, not a bug)."""
        engine = SkillEngine(tmp_path, platform=offline_context())
        skill = _make_skill("network-policy", outputs=["NetworkPolicy"])
        report = make_report()

        files = engine.generate(skill, report, llm_client=None)
        assert files == []

    def test_run_all_forwards_llm_client_to_generate(self, tmp_path: Path) -> None:
        """run_all() must forward its llm_client kwarg down to generate()."""
        engine = SkillEngine(tmp_path, platform=offline_context())
        skill = _make_skill("network-policy", outputs=["NetworkPolicy"], triggers=["network", "isolation"])
        engine.skills = [skill]

        report = make_report()
        report.scores[0].findings[0].description = "Missing network isolation between pods"

        fake_llm = _FakeLLMClient(
            "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: x\nspec:\n  podSelector: {}\n"
        )
        files = engine.run_all(report, llm_client=fake_llm)

        assert len(fake_llm.calls) == 1
        assert len(files) == 1
