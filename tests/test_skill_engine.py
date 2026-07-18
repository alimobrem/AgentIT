"""Tests for skill_engine.py: pluralization, platform gating, LLM passthrough."""

from __future__ import annotations

import asyncio

from pathlib import Path

import pytest

from agentit.platform_context import offline_context
from agentit.skill_engine import (
    Skill,
    SkillEngine,
    UnresolvedPlaceholderError,
    _pluralize_kind,
    _render_template,
    load_skill,
    verify_skill,
)
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
    """Stub LLM client recording calls (incl. kwargs like max_tokens, so
    callers can assert on the token budget requested) and returning a
    canned manifest."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []
        self.max_tokens_requested: list[int | None] = []

    def _chat(self, system: str, user: str, max_tokens: int | None = None, **_kwargs) -> str:
        self.calls.append((system, user))
        self.max_tokens_requested.append(max_tokens)
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


_TWO_PLACEHOLDER_TEMPLATE_BODY = """# Two-placeholder template (regression fixture)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{app_name}}
  namespace: {{namespace}}
spec:
  selector:
    matchLabels:
      app: {{app_name}}
  template:
    metadata:
      labels:
        app: {{app_name}}
    spec:
      containers:
        - name: {{app_name}}
          image: "{{image}}"
```
"""

# A response that fails validate_manifest() on every attempt (missing both
# 'kind' and 'metadata.name'/'generateName') -- simulating a real
# `stop_reason=max_tokens` truncation, which _chat() still returns as
# non-None text (just a warning log), not None. _generate_with_llm() then
# exhausts its 2 retry attempts and returns [], forcing generate() to fall
# back to the raw template -- exactly the path the live self-assessment run
# hit twice (app-rollout-patch.yaml, app-compliance-cronjob.yaml).
_TRUNCATED_LLM_RESPONSE = (
    "apiVersion: apps/v1\n"
    "kind: Deployment\n"
    "metadata:\n"
    "  labels:\n"
    "    app: truncated-mid-ob"
)


class TestLLMTruncationFallbackPlaceholderSubstitution:
    """Regression coverage for the live-reproduced bug: when an LLM call
    truncates (stop_reason=max_tokens) and generate() falls back to the raw
    template, the OLD substitution loop only ever replaced `{{app_name}}` --
    every other placeholder (`{{image}}`, `{{namespace}}`, etc.) shipped
    literally in the final manifest. Confirmed live: app-rollout-patch.yaml
    shipped `image: "{{image}}"`; app-compliance-cronjob.yaml shipped
    `"--namespace", "{{namespace}}"` verbatim.
    """

    def _skill(self) -> Skill:
        return Skill(
            name="two-placeholder-deploy",
            domain="security",
            version=1,
            triggers=["network"],
            outputs=["Deployment"],
            property_description="two-placeholder-deploy property",
            body=_TWO_PLACEHOLDER_TEMPLATE_BODY,
            file_path="skills/security/two-placeholder-deploy.md",
            mode="llm",
        )

    def test_llm_truncation_forces_template_fallback(self, tmp_path: Path) -> None:
        """Sanity check that the truncated response really does exhaust
        both LLM attempts and fall through to the template path (i.e. this
        test fixture actually reproduces the reported trigger condition)."""
        engine = SkillEngine(tmp_path, platform=offline_context())
        skill = self._skill()
        report = make_report(repo_name="my-app")
        fake_llm = _FakeLLMClient(_TRUNCATED_LLM_RESPONSE)

        files = engine.generate(skill, report, llm_client=fake_llm)

        assert len(fake_llm.calls) == 2, "expected both LLM retry attempts to be exhausted"
        # Whatever generate() returns here (template content or a hard
        # rejection) must come from the template-fallback path, not the
        # (invalid) LLM response -- verified precisely by the two tests below.
        assert files == [] or "Deployment" in files[0].content

    def test_known_placeholders_are_fully_substituted_not_shipped_literally(self, tmp_path: Path) -> None:
        """`{{namespace}}` has a real, known value (the app's own namespace,
        matching routes/assessments.py's delivery convention) -- it must be
        substituted, never shipped as literal `{{namespace}}` text. This
        alone reproduces the confirmed app-compliance-cronjob.yaml bug
        (`"--namespace", "{{namespace}}"` shipped verbatim)."""
        skill_body = _TWO_PLACEHOLDER_TEMPLATE_BODY.replace('image: "{{image}}"', "restartPolicy: Always")
        skill = Skill(
            name="namespace-only-deploy", domain="security", version=1,
            triggers=["network"], outputs=["Deployment"],
            property_description="namespace-only-deploy property",
            body=skill_body, file_path="skills/security/namespace-only-deploy.md",
            mode="llm",
        )
        engine = SkillEngine(tmp_path, platform=offline_context())
        report = make_report(repo_name="my-app")
        fake_llm = _FakeLLMClient(_TRUNCATED_LLM_RESPONSE)

        files = engine.generate(skill, report, llm_client=fake_llm)

        assert len(files) == 1, "template fallback should fully substitute and ship a file"
        content = files[0].content
        assert "{{" not in content, f"unsubstituted placeholder(s) leaked into output: {content}"
        assert "namespace: my-app" in content
        assert "name: my-app" in content

    def test_unresolvable_placeholder_hard_fails_instead_of_shipping_literal_text(self, tmp_path: Path) -> None:
        """`{{image}}` has no real, known value in this code path -- rather
        than ship `image: "{{image}}"` literally (the confirmed
        app-rollout-patch.yaml bug), generation must hard-fail (produce no
        file) instead of silently shipping broken output."""
        engine = SkillEngine(tmp_path, platform=offline_context())
        skill = self._skill()
        report = make_report(repo_name="my-app")
        fake_llm = _FakeLLMClient(_TRUNCATED_LLM_RESPONSE)

        files = engine.generate(skill, report, llm_client=fake_llm)

        assert files == [], "must hard-fail (no file) rather than ship literal {{image}} text"

    def test_render_template_raises_on_unresolved_placeholder(self) -> None:
        """Unit-level check on the substitution primitive itself."""
        with pytest.raises(UnresolvedPlaceholderError) as exc_info:
            _render_template("image: {{image}}\nname: {{app_name}}", {"app_name": "my-app"})
        assert exc_info.value.placeholders == ["image"]

    def test_render_template_substitutes_every_provided_variable(self) -> None:
        rendered = _render_template(
            "name: {{app_name}}\nnamespace: {{namespace}}\nrepoURL: {{git_url}}",
            {"app_name": "my-app", "namespace": "my-app", "git_url": "https://github.com/org/my-app"},
        )
        assert rendered == "name: my-app\nnamespace: my-app\nrepoURL: https://github.com/org/my-app"

    def test_go_template_alertmanager_syntax_is_never_treated_as_unresolved(self) -> None:
        """Alertmanager/Go-template notification syntax (`{{ .AlertName }}`,
        `{{ range .Alerts }}...{{ end }}`) is legitimate content those
        skills ship verbatim for Alertmanager itself to evaluate at
        alert-fire time -- it must never be misidentified as an AgentIT
        placeholder and must never trigger the hard-fail safety net."""
        text = (
            'description_template: "[CRITICAL] {{app_name}} - {{ .AlertName }}: {{ .Summary }}"\n'
            'title: "[{{ .Status | toUpper }}] {{ .GroupLabels.alertname }}"\n'
            'text: "{{ range .Alerts }}{{ .Annotations.summary }}\\n{{ end }}"\n'
        )
        rendered = _render_template(text, {"app_name": "my-app"})
        assert "{{ .AlertName }}" in rendered
        assert "{{ range .Alerts }}" in rendered
        assert "my-app" in rendered


class TestRunAllStoreWiring:
    """run_all()'s `store` param was accepted but ignored (per the earlier
    self-improvement-loop audit) -- it must now gate generation via
    get_rejection_count() and inform it via get_human_override(), mirroring
    the pattern webhooks.py already uses for auto-fix-after-3-rejections."""

    def _report_and_skill(self):
        skill = _make_skill("network-policy", outputs=["NetworkPolicy"], triggers=["network", "isolation"])
        report = make_report(repo_name="my-app")
        report.scores[0].findings[0].description = "Missing network isolation between pods"
        return report, skill

    async def test_skill_skipped_after_3_rejections_for_this_app_domain(self, tmp_path: Path) -> None:
        from conftest import make_store

        engine = SkillEngine(tmp_path, platform=offline_context())
        report, skill = self._report_and_skill()
        engine.skills = [skill]

        raw_store = await make_store()
        for _ in range(3):
            await raw_store.record_feedback(
                app_name="my-app", agent_name="skill-engine",
                finding_category=skill.domain, action="rejected",
            )
        store = raw_store

        fake_llm = _FakeLLMClient("apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: x\nspec:\n  podSelector: {}\n")
        import asyncio
        files = await asyncio.to_thread(
            engine.run_all, report, store=store, llm_client=fake_llm, loop=asyncio.get_running_loop(),
        )

        assert files == []
        assert len(fake_llm.calls) == 0
        events = await raw_store.list_events_by_agent("skill-engine")
        assert any(e["action"] == "skipped-rejected" for e in events)

    async def test_skill_not_skipped_below_rejection_threshold(self, tmp_path: Path) -> None:
        from conftest import make_store

        engine = SkillEngine(tmp_path, platform=offline_context())
        report, skill = self._report_and_skill()
        engine.skills = [skill]

        raw_store = await make_store()
        for _ in range(2):  # below the 3+ threshold
            await raw_store.record_feedback(
                app_name="my-app", agent_name="skill-engine",
                finding_category=skill.domain, action="rejected",
            )
        store = raw_store

        fake_llm = _FakeLLMClient("apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: x\nspec:\n  podSelector: {}\n")
        import asyncio
        files = await asyncio.to_thread(
            engine.run_all, report, store=store, llm_client=fake_llm, loop=asyncio.get_running_loop(),
        )

        assert len(files) == 1

    async def test_human_override_passed_to_llm_prompt(self, tmp_path: Path) -> None:
        from conftest import make_store

        engine = SkillEngine(tmp_path, platform=offline_context())
        report, skill = self._report_and_skill()
        engine.skills = [skill]

        raw_store = await make_store()
        await raw_store.record_feedback(
            app_name="my-app", agent_name="skill-engine",
            finding_category=skill.domain, action="modified",
            original_value="old-policy", human_value="a stricter deny-all default policy",
        )
        store = raw_store

        fake_llm = _FakeLLMClient("apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: x\nspec:\n  podSelector: {}\n")
        import asyncio
        files = await asyncio.to_thread(
            engine.run_all, report, store=store, llm_client=fake_llm, loop=asyncio.get_running_loop(),
        )

        assert len(files) == 1
        _, user_prompt = fake_llm.calls[0]
        assert "a stricter deny-all default policy" in user_prompt

    def test_no_store_behaves_exactly_as_before(self, tmp_path: Path) -> None:
        engine = SkillEngine(tmp_path, platform=offline_context())
        report, skill = self._report_and_skill()
        engine.skills = [skill]

        fake_llm = _FakeLLMClient("apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: x\nspec:\n  podSelector: {}\n")
        files = engine.run_all(report, store=None, llm_client=fake_llm)

        assert len(files) == 1


# ── Live bug repro: "skill matched the verification fixture but generated
# no output" (resourcequota-contextual, blocked in the live portal
# 2026-07-18T16:41-16:42Z) ──────────────────────────────────────────────
#
# Root cause traced via the live pod's own logs: SkillEngine._generate_with_llm()
# called llm_client._chat(system, user) with no max_tokens override, silently
# inheriting agentit.llm._DEFAULT_MAX_TOKENS (512) -- sized for short,
# fixed-shape classifier JSON, not a full K8s manifest. The live LLM call hit
# stop_reason=max_tokens on every attempt, so validate_manifest() rejected the
# truncated content twice and generate() returned no files at all -- and
# since this skill (mode: llm, no ```yaml block in its body) has no template
# to fall back to, verify_skill() had nothing left to check and reported
# "generated no output". The exact real skill body below is copied verbatim
# from the live pod (`oc exec ... cat skills/infrastructure/
# resourcequota-contextual.md`) -- including its own draft-generation
# truncation (missing Constraints/Verification sections, cut off mid-sentence
# at "avoid spike-driven"), a second live symptom of the identical
# too-small-token-budget bug in learning_agent.py's generate_skill_from_research()
# (see test_llm.py's sibling regression test).
_REAL_RESOURCEQUOTA_CONTEXTUAL_MD = (
    "---\n"
    "name: resourcequota-contextual\n"
    "domain: infrastructure\n"
    "version: 1\n"
    "triggers:\n"
    "  - resourcequota\n"
    "  - resource quota\n"
    "  - namespace quota\n"
    "  - quota\n"
    "  - namespace limits\n"
    "  - cpu limit\n"
    "  - memory limit\n"
    "  - namespace resource limits\n"
    "  - quota policy\n"
    "  - namespace governance\n"
    "  - resource constraints\n"
    "  - limitrange\n"
    "  - limit range\n"
    "  - namespace capacity\n"
    "  - quota tuning\n"
    "outputs:\n"
    "  - ResourceQuota\n"
    "  - LimitRange\n"
    "property: >\n"
    "  Generate contextually grounded ResourceQuota and LimitRange manifests by first\n"
    "  inspecting actual namespace workload declarations and live consumption metrics,\n"
    "  then deriving tiered quota values with explicit headroom buffers and inline\n"
    "  justifications rather than emitting generic placeholder figures.\n"
    "mode: llm\n"
    "status: draft\n"
    "source: learning-agent\n"
    'created_at: "2025-01-30"\n'
    "---\n"
    "\n"
    "## Property\n"
    "\n"
    "Generate contextually grounded ResourceQuota and LimitRange manifests by first "
    "inspecting actual namespace workload declarations and live consumption metrics, "
    "then deriving tiered quota values with explicit headroom buffers and inline "
    "justifications rather than emitting generic placeholder figures.\n"
    "\n"
    "---\n"
    "\n"
    "## Key Decisions for the LLM\n"
    "\n"
    "### 1. Namespace Workload Inspection (Pre-generation Required)\n"
    "\n"
    "Before emitting any manifest, the LLM **must** gather the following signals from "
    "the target namespace. If a signal is unavailable, it must be noted explicitly in "
    "the output and a conservative fallback assumption must be stated.\n"
    "\n"
    "**Declared resource signals:**\n"
    "- List all `Deployment`, `StatefulSet`, and `DaemonSet` objects and extract their "
    "`resources.requests` and `resources.limits` per container.\n"
    "- Check for existing `HorizontalPodAutoscaler` objects and note `maxReplicas` to "
    "understand burst capacity.\n"
    "- Check for any existing `ResourceQuota` or `LimitRange` objects to understand "
    "current policy and avoid regressions.\n"
    "\n"
    "**Live consumption signals (if available):**\n"
    "- Query `metrics-server` via `kubectl top pods -n <namespace>` to obtain current "
    "CPU and memory consumption per pod.\n"
    "- If Prometheus is available, prefer p95 CPU and memory over a 7-day window to "
    "avoid spike-driven"
)

# A complete, valid multi-document manifest of the shape a real (untruncated)
# LLM response would produce for this skill's declared outputs
# (ResourceQuota + LimitRange) -- used to prove verify_skill() now passes
# once generation is actually given room to finish.
_REAL_RESOURCEQUOTA_CONTEXTUAL_MANIFEST = (
    "apiVersion: v1\n"
    "kind: ResourceQuota\n"
    "metadata:\n"
    "  name: skill-verify-tier1-quota\n"
    "  namespace: skill-verify\n"
    "spec:\n"
    "  hard:\n"
    "    requests.cpu: \"4\"\n"
    "    requests.memory: 8Gi\n"
    "    limits.cpu: \"8\"\n"
    "    limits.memory: 16Gi\n"
    "---\n"
    "apiVersion: v1\n"
    "kind: LimitRange\n"
    "metadata:\n"
    "  name: skill-verify-default-limits\n"
    "  namespace: skill-verify\n"
    "spec:\n"
    "  limits:\n"
    "    - type: Container\n"
    "      defaultRequest:\n"
    "        cpu: 100m\n"
    "        memory: 128Mi\n"
    "      default:\n"
    "        cpu: 500m\n"
    "        memory: 512Mi\n"
)


class TestResourceQuotaContextualLiveBugRepro:
    """Regression coverage for the exact live activation-blocked toast:
    'Activation blocked — skill failed verification: skill matched the
    verification fixture but generated no output', reported against the
    real 'resourcequota-contextual' draft skill."""

    def _write_real_skill(self, tmp_path: Path) -> Path:
        skills_dir = tmp_path / "skills" / "infrastructure"
        skills_dir.mkdir(parents=True)
        path = skills_dir / "resourcequota-contextual.md"
        path.write_text(_REAL_RESOURCEQUOTA_CONTEXTUAL_MD, encoding="utf-8")
        return path

    def test_real_skill_parses_as_llm_mode_draft_with_no_template_fallback(self, tmp_path: Path) -> None:
        """Sanity check that this fixture really does reproduce the live
        skill's shape: mode: llm, status: draft, and -- because its body has
        no ```yaml block -- no template to fall back to if LLM generation
        fails."""
        from agentit.skill_engine import _extract_template

        skill = load_skill(self._write_real_skill(tmp_path))
        assert skill is not None
        assert skill.name == "resourcequota-contextual"
        assert skill.mode == "llm"
        assert skill.status == "draft"
        assert skill.outputs == ["ResourceQuota", "LimitRange"]
        assert _extract_template(skill.body) is None

    def test_verify_skill_blocked_when_llm_output_is_truncated(self, tmp_path: Path) -> None:
        """Negative control: confirms this fixture genuinely reproduces the
        live failure mode when the LLM response is truncated (the pre-fix
        512-token behavior observed live: stop_reason=max_tokens on every
        attempt) -- i.e. this is a real repro, not a fixture that would have
        passed regardless of the fix."""
        skill = load_skill(self._write_real_skill(tmp_path))
        assert skill is not None
        truncated_response = "apiVersion: v1\nkind: ResourceQuota\nmetadata:\n  labels:\n    tier: mid-sen"
        fake_llm = _FakeLLMClient(truncated_response)

        passed, issues, warnings = verify_skill(skill, llm_client=fake_llm)

        assert passed is False
        assert any("generated no output" in i for i in issues)

    def test_verify_skill_passes_with_a_complete_llm_response(self, tmp_path: Path) -> None:
        """The actual fix: once generation is given a real manifest-sized
        token budget instead of the 512-token classifier default, a complete
        LLM response for this exact real skill produces real, valid,
        non-empty output and verify_skill() passes -- no more
        'generated no output', no more blocked activation."""
        skill = load_skill(self._write_real_skill(tmp_path))
        assert skill is not None
        fake_llm = _FakeLLMClient(_REAL_RESOURCEQUOTA_CONTEXTUAL_MANIFEST)

        passed, issues, warnings = verify_skill(skill, llm_client=fake_llm)

        assert passed is True, f"verify_skill still blocked: {issues}"
        assert issues == []
        assert "generated no output" not in "; ".join(issues)

    def test_generation_requests_manifest_sized_token_budget_not_classifier_default(self, tmp_path: Path) -> None:
        """The actual mechanism of the fix: generation for this skill must
        request a real manifest-sized token budget, not silently inherit the
        512-token default sized for short classifier JSON responses."""
        import dataclasses

        from agentit.llm import _SKILL_GENERATION_MAX_TOKENS

        skill = load_skill(self._write_real_skill(tmp_path))
        assert skill is not None
        skill_active = dataclasses.replace(skill, status="active")
        engine = SkillEngine(tmp_path, platform=None)
        fake_llm = _FakeLLMClient(_REAL_RESOURCEQUOTA_CONTEXTUAL_MANIFEST)
        report = make_report(repo_name="skill-verify")

        files = engine.generate(skill_active, report, llm_client=fake_llm)

        assert len(files) == 1
        assert fake_llm.max_tokens_requested, "LLM was never called"
        assert fake_llm.max_tokens_requested[0] == _SKILL_GENERATION_MAX_TOKENS
        assert fake_llm.max_tokens_requested[0] > 512
