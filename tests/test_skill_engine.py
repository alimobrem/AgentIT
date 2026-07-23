"""Tests for skill_engine.py: pluralization, platform gating, LLM passthrough."""

from __future__ import annotations

import asyncio

from pathlib import Path

import pytest

from agentit.check_engine import run_checks
from agentit.models import Severity
from agentit.platform_context import offline_context
from agentit.skill_engine import (
    Skill,
    SkillEngine,
    UnresolvedPlaceholderError,
    _pluralize_kind,
    _render_template,
    _skill_to_check_definition,
    detect_check_definitions,
    load_all_skills,
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

    def test_run_all_generates_multiple_skills_concurrently(self, tmp_path: Path) -> None:
        """Matched skills must overlap in wall-clock time (ThreadPoolExecutor),
        not run strictly sequentially — live AgentIT onboard hit the old 300s
        ceiling on sequential LLM calls for many skills."""
        import threading
        import time
        from unittest.mock import patch

        (tmp_path / "infrastructure").mkdir()
        # Avoid HPA: fleet generate() skips it offline without a live target.
        for name, kind in (("pdb", "PodDisruptionBudget"), ("resourcequota", "ResourceQuota")):
            (tmp_path / "infrastructure" / f"{name}.md").write_text(
                "---\n"
                f"name: {name}\n"
                "domain: infrastructure\n"
                "version: 1\n"
                "triggers:\n"
                "  - zzznomatch\n"
                "outputs:\n"
                f"  - {kind}\n"
                f"property: {name}\n"
                "mode: template\n"
                "---\n\n"
                f"# {name}\n\n"
                "```yaml\n"
                "apiVersion: policy/v1\n"
                f"kind: {kind}\n"
                "metadata:\n"
                "  name: {{app_name}}-" + name + "\n"
                "```\n",
                encoding="utf-8",
            )
        engine = SkillEngine(tmp_path, platform=None)
        from agentit.models import DimensionScore, Finding, Severity

        report = make_report()
        report.scores = [
            DimensionScore(
                dimension="ha_dr", score=40, max_score=100,
                findings=[
                    Finding(
                        category="availability", severity=Severity.medium,
                        description="No PodDisruptionBudget", recommendation="Add PDB",
                    ),
                    Finding(
                        category="quota", severity=Severity.medium,
                        description="No ResourceQuota", recommendation="Add quota",
                    ),
                ],
            ),
        ]
        assert {s.name for s in engine.match(report)} == {"pdb", "resourcequota"}

        inflight = 0
        max_inflight = 0
        lock = threading.Lock()
        orig = SkillEngine.generate

        def _slow_generate(self, skill, report, **kwargs):
            nonlocal inflight, max_inflight
            with lock:
                inflight += 1
                max_inflight = max(max_inflight, inflight)
            time.sleep(0.08)
            try:
                return orig(self, skill, report, **kwargs)
            finally:
                with lock:
                    inflight -= 1

        with patch.object(SkillEngine, "generate", _slow_generate):
            files = engine.run_all(report, llm_client=None)

        assert len(files) == 2
        assert max_inflight >= 2, (
            f"expected concurrent generate(); max_inflight={max_inflight}"
        )


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
        # Use finding.category (not skill.domain) — run_all gates on the
        # same category key webhooks.py's get_rejection_count uses.
        report.scores[0].findings[0].category = "network"
        report.scores[0].findings[0].description = "Missing network isolation between pods"
        return report, skill

    async def test_skill_skipped_after_3_rejections_for_this_app_category(self, tmp_path: Path) -> None:
        from conftest import make_store

        engine = SkillEngine(tmp_path, platform=offline_context())
        report, skill = self._report_and_skill()
        engine.skills = [skill]

        raw_store = await make_store()
        for _ in range(3):
            await raw_store.record_feedback(
                app_name="my-app", agent_name="skill-engine",
                finding_category="network", action="rejected",
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
        assert any("category 'network'" in (e.get("summary") or "") for e in events)

    async def test_skill_not_skipped_when_only_domain_was_rejected(self, tmp_path: Path) -> None:
        """Regression: run_all used skill.domain for get_rejection_count."""
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
        assert len(files) == 1

    async def test_skill_not_skipped_below_rejection_threshold(self, tmp_path: Path) -> None:
        from conftest import make_store

        engine = SkillEngine(tmp_path, platform=offline_context())
        report, skill = self._report_and_skill()
        engine.skills = [skill]

        raw_store = await make_store()
        for _ in range(2):  # below the 3+ threshold
            await raw_store.record_feedback(
                app_name="my-app", agent_name="skill-engine",
                finding_category="network", action="rejected",
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
            finding_category="network", action="modified",
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


# ---------------------------------------------------------------------------
# mode: detect -- the detection-shaped half of the unified extension model
# (docs/extension-model-unification-plan-2026-07-18.md, Phase 1)
# ---------------------------------------------------------------------------

_DETECT_SKILL_MD = """\
---
name: {name}
domain: observability
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: health
description: No liveness/readiness probes detected in manifests
recommendation: Add livenessProbe and readinessProbe to all containers
rule:
  type: file_contains
  pattern: livenessProbe
status: {status}
---

# Health Probes Check (test fixture)
"""


def _write_detect_skill(tmp_path: Path, name: str = "health-probes-check", status: str = "active") -> Path:
    p = tmp_path / f"{name}.md"
    p.write_text(_DETECT_SKILL_MD.format(name=name, status=status))
    return p


class TestDetectModeLoading:
    """load_skill() must parse the new mode: detect fields (rule/severity/
    category/description/recommendation) alongside every pre-existing
    field, with zero behavior change for template/llm-mode skills that
    never set them."""

    def test_load_skill_parses_detect_fields(self, tmp_path: Path) -> None:
        skill = load_skill(_write_detect_skill(tmp_path))
        assert skill is not None
        assert skill.mode == "detect"
        assert skill.rule == {"type": "file_contains", "pattern": "livenessProbe"}
        assert skill.severity == "high"
        assert skill.category == "health"
        assert skill.description == "No liveness/readiness probes detected in manifests"
        assert skill.recommendation == "Add livenessProbe and readinessProbe to all containers"

    def test_template_mode_skill_has_empty_detect_fields_by_default(self, tmp_path: Path) -> None:
        """Regression guard: a pre-existing template/llm-mode skill file
        (no rule/severity/category/description/recommendation keys at all)
        must still load exactly as before -- these new fields default to
        empty, never required."""
        skill = _make_skill("plain-skill", outputs=["NetworkPolicy"])
        assert skill.rule == {}
        assert skill.severity == ""
        assert skill.category == ""
        assert skill.description == ""
        assert skill.recommendation == ""


class TestDetectModeNeverRemediates:
    """A mode: detect skill must never participate in remediation matching
    or generation -- it only ever contributes Findings via
    detect_check_definitions(), never a GeneratedFile."""

    def test_matches_always_false_regardless_of_triggers(self, tmp_path: Path) -> None:
        skill = load_skill(_write_detect_skill(tmp_path))
        assert skill is not None
        report = make_report()
        assert skill.matches(report) is False

    def test_generate_returns_empty_list(self, tmp_path: Path) -> None:
        skill = load_skill(_write_detect_skill(tmp_path))
        assert skill is not None
        engine = SkillEngine(tmp_path, platform=offline_context())
        report = make_report()
        assert engine.generate(skill, report, llm_client=None) == []

    def test_skill_engine_match_never_returns_a_detect_mode_skill(self, tmp_path: Path) -> None:
        _write_detect_skill(tmp_path)
        engine = SkillEngine(tmp_path, platform=offline_context())
        report = make_report()
        assert engine.match(report) == []

    def test_match_includes_fix_registry_skill_for_open_finding(
        self, tmp_path: Path,
    ) -> None:
        """Open finding categories must attempt their FIX_REGISTRY skill even
        when that skill's trigger keywords do not appear in report prose."""
        (tmp_path / "infrastructure").mkdir()
        (tmp_path / "infrastructure" / "hpa.md").write_text(
            "---\n"
            "name: hpa\n"
            "domain: infrastructure\n"
            "version: 1\n"
            "triggers:\n"
            "  - zzznomatch-trigger\n"
            "outputs:\n"
            "  - HorizontalPodAutoscaler\n"
            "property: scales\n"
            "mode: template\n"
            "---\n\n"
            "# HPA\n\n"
            "```yaml\n"
            "apiVersion: autoscaling/v2\n"
            "kind: HorizontalPodAutoscaler\n"
            "metadata:\n"
            "  name: {{app_name}}-hpa\n"
            "```\n",
            encoding="utf-8",
        )
        engine = SkillEngine(tmp_path, platform=None)
        from agentit.models import DimensionScore, Finding, Severity

        report = make_report()
        report.scores = [
            DimensionScore(
                dimension="ha_dr",
                score=50,
                max_score=100,
                findings=[
                    Finding(
                        category="scaling",
                        severity=Severity.medium,
                        description="No HorizontalPodAutoscaler defined",
                        recommendation="Add HPA for automatic scaling under load",
                    ),
                ],
            ),
        ]
        # Trigger keyword absent from prose; category alone must pull hpa in.
        assert "zzznomatch-trigger" not in " ".join(
            f"{f.category} {f.description} {f.recommendation}"
            for s in report.scores for f in s.findings
        )
        matched = engine.match(report)
        assert [s.name for s in matched] == ["hpa"]

    def test_match_with_open_findings_skips_unrelated_trigger_skills(
        self, tmp_path: Path,
    ) -> None:
        """Open findings must not pull in every trigger-matched catalog skill.

        Live AgentIT self-managed Onboards at score ~96 timed out at the
        generation ceiling after sequential LLM calls for 10+ unrelated
        skills that auto_delivery's finding gate would have dropped anyway.
        """
        (tmp_path / "infrastructure").mkdir()
        (tmp_path / "compliance").mkdir()
        (tmp_path / "infrastructure" / "hpa.md").write_text(
            "---\n"
            "name: hpa\n"
            "domain: infrastructure\n"
            "version: 1\n"
            "triggers:\n"
            "  - zzznomatch-trigger\n"
            "outputs:\n"
            "  - HorizontalPodAutoscaler\n"
            "property: scales\n"
            "mode: template\n"
            "---\n\n"
            "# HPA\n\n"
            "```yaml\n"
            "apiVersion: autoscaling/v2\n"
            "kind: HorizontalPodAutoscaler\n"
            "metadata:\n"
            "  name: {{app_name}}-hpa\n"
            "```\n",
            encoding="utf-8",
        )
        (tmp_path / "compliance" / "kyverno-require-labels.md").write_text(
            "---\n"
            "name: kyverno-require-labels\n"
            "domain: compliance\n"
            "version: 1\n"
            "triggers:\n"
            "  - python\n"
            "  - fastapi\n"
            "  - kubernetes\n"
            "outputs:\n"
            "  - ClusterPolicy\n"
            "property: labeled\n"
            "mode: template\n"
            "---\n\n"
            "# Labels\n\n"
            "```yaml\n"
            "apiVersion: kyverno.io/v1\n"
            "kind: ClusterPolicy\n"
            "metadata:\n"
            "  name: {{app_name}}-labels\n"
            "```\n",
            encoding="utf-8",
        )
        engine = SkillEngine(tmp_path, platform=None)
        from agentit.models import DimensionScore, Finding, Severity

        report = make_report()
        # Summary would match kyverno's python/fastapi/kubernetes triggers —
        # findings-only haystack must not.
        report.summary = (
            "This is a Python FastAPI application on Kubernetes with extensive docs."
        )
        report.scores = [
            DimensionScore(
                dimension="ha_dr",
                score=50,
                max_score=100,
                findings=[
                    Finding(
                        category="scaling",
                        severity=Severity.medium,
                        description="No HorizontalPodAutoscaler defined",
                        recommendation="Add HPA for automatic scaling under load",
                    ),
                ],
            ),
        ]
        matched = engine.match(report)
        assert [s.name for s in matched] == ["hpa"]

    def test_match_open_findings_no_trigger_companions_for_container(
        self, tmp_path: Path,
    ) -> None:
        """container finding must not pull Kyverno/LimitRange via trigger words.

        Pinky gitops #23 attached image-registry-policy + limitrange because
        finding prose contains "container"/"image"/"dockerfile".
        """
        (tmp_path / "security").mkdir()
        (tmp_path / "compliance").mkdir()
        (tmp_path / "infrastructure").mkdir()
        (tmp_path / "security" / "containerfile.md").write_text(
            "---\n"
            "name: containerfile\n"
            "domain: security\n"
            "version: 1\n"
            "triggers:\n"
            "  - container\n"
            "  - dockerfile\n"
            "outputs:\n"
            "  - Dockerfile\n"
            "delivery: source\n"
            "property: secure image\n"
            "mode: template\n"
            "---\n\n"
            "# Containerfile\n\n"
            "```dockerfile\n"
            "FROM registry.access.redhat.com/ubi9/ubi-minimal:1\n"
            "```\n",
            encoding="utf-8",
        )
        (tmp_path / "compliance" / "image-registry-policy.md").write_text(
            "---\n"
            "name: image-registry-policy\n"
            "domain: compliance\n"
            "version: 1\n"
            "triggers:\n"
            "  - registry\n"
            "  - image\n"
            "  - container\n"
            "  - policy\n"
            "outputs:\n"
            "  - Policy\n"
            "property: trusted registries\n"
            "mode: template\n"
            "---\n\n"
            "# Registry\n\n"
            "```yaml\n"
            "apiVersion: kyverno.io/v1\n"
            "kind: Policy\n"
            "metadata:\n"
            "  name: {{app_name}}-registry\n"
            "```\n",
            encoding="utf-8",
        )
        (tmp_path / "infrastructure" / "limitrange.md").write_text(
            "---\n"
            "name: limitrange\n"
            "domain: infrastructure\n"
            "version: 1\n"
            "triggers:\n"
            "  - limit\n"
            "  - container\n"
            "  - resources\n"
            "outputs:\n"
            "  - LimitRange\n"
            "property: defaults\n"
            "mode: template\n"
            "---\n\n"
            "# LimitRange\n\n"
            "```yaml\n"
            "apiVersion: v1\n"
            "kind: LimitRange\n"
            "metadata:\n"
            "  name: {{app_name}}-limits\n"
            "```\n",
            encoding="utf-8",
        )
        # FIX_REGISTRY lookup needs the real registry mapping — monkeypatch
        # skill_for_category via loading skills that match registry names.
        engine = SkillEngine(tmp_path, platform=None)
        from agentit.models import DimensionScore, Finding, Severity

        report = make_report()
        report.scores = [
            DimensionScore(
                dimension="security",
                score=40,
                max_score=100,
                findings=[
                    Finding(
                        category="container",
                        severity=Severity.high,
                        description="using :latest tag in base image in dockerfile",
                        recommendation="pin the container image tag",
                    ),
                ],
            ),
        ]
        matched = engine.match(report)
        assert [s.name for s in matched] == ["containerfile"]


class TestNonApiOutputGating:
    """outputs like Containerfile must not skip generation when platform is set."""

    def test_non_api_output_kind_does_not_skip_generate(self, tmp_path: Path) -> None:
        skill = _make_skill(
            "containerfile",
            outputs=["Containerfile"],
            mode="template",
            triggers=["container"],
        )
        skill.body = (
            "# Container\n\n"
            "```yaml\n"
            "apiVersion: build.openshift.io/v1\n"
            "kind: BuildConfig\n"
            "metadata:\n"
            "  name: {{app_name}}\n"
            "```\n"
        )
        from agentit.platform_context import PlatformContext

        platform = PlatformContext(available_kinds={"buildconfig", "configmap"})
        engine = SkillEngine(tmp_path, platform=platform)
        files = engine.generate(skill, make_report(repo_name="pinky"), llm_client=None)
        assert len(files) == 1
        assert "kind: BuildConfig" in files[0].content


class TestSelfManagedGenerationConstraints:
    """P1: self-managed AgentIT skips fleet-only kinds and non-Helm templates."""

    def test_skips_forbidden_output_kinds(self, tmp_path: Path) -> None:
        skill = _make_skill("tekton-run", outputs=["PipelineRun"], mode="template")
        engine = SkillEngine(tmp_path, platform=None, self_managed=True)
        report = make_report(repo_name="agentit")
        assert engine.generate(skill, report, llm_client=None) == []

    def test_skips_non_helm_template_fallback(self, tmp_path: Path) -> None:
        skill = Skill(
            name="netpol",
            domain="security",
            version=1,
            triggers=["network"],
            outputs=["NetworkPolicy"],
            property_description="netpol property",
            body=_NETPOL_TEMPLATE_BODY,
            file_path="skills/security/netpol.md",
            mode="template",
        )
        engine = SkillEngine(tmp_path, platform=None, self_managed=True)
        report = make_report(repo_name="agentit")
        # Template has {{app_name}} AgentIT placeholders, not Helm {{ .Release }}
        assert engine.generate(skill, report, llm_client=None) == []

    def test_fleet_mode_still_emits_template(self, tmp_path: Path) -> None:
        skill = Skill(
            name="netpol",
            domain="security",
            version=1,
            triggers=["network"],
            outputs=["NetworkPolicy"],
            property_description="netpol property",
            body=_NETPOL_TEMPLATE_BODY,
            file_path="skills/security/netpol.md",
            mode="template",
        )
        engine = SkillEngine(tmp_path, platform=None, self_managed=False)
        report = make_report(repo_name="pinky")
        files = engine.generate(skill, report, llm_client=None)
        assert len(files) == 1
        assert "NetworkPolicy" in files[0].content

    def test_llm_prompt_includes_self_managed_constraints(self, tmp_path: Path) -> None:
        skill = _make_skill("netpol", outputs=["NetworkPolicy"], mode="llm")
        engine = SkillEngine(tmp_path, platform=None, self_managed=True)
        report = make_report(repo_name="agentit")
        captured: dict[str, str] = {}

        class _FakeLLM:
            def _chat(self, system, user, max_tokens=None):
                captured["system"] = system
                captured["user"] = user
                return (
                    "apiVersion: networking.k8s.io/v1\n"
                    "kind: NetworkPolicy\n"
                    "metadata:\n"
                    "  name: \"{{ .Release.Name }}-netpol\"\n"
                    "  namespace: \"{{ .Release.Namespace }}\"\n"
                    "spec:\n"
                    "  podSelector: {}\n"
                )

        files = engine.generate(skill, report, llm_client=_FakeLLM())
        assert len(files) == 1
        assert "SELF-MANAGED AGENTIT" in captured["user"]
        assert "{{ .Release.Namespace }}" in files[0].content

    def test_self_managed_hpa_skips_template_without_llm(self, tmp_path: Path) -> None:
        """Deployment-shaped HPA template must not ship for AgentIT chart."""
        skill = Skill(
            name="hpa",
            domain="infrastructure",
            version=1,
            triggers=["scaling"],
            outputs=["HorizontalPodAutoscaler"],
            property_description="scales",
            body=(
                "# HPA\n\n```yaml\n"
                "apiVersion: autoscaling/v2\n"
                "kind: HorizontalPodAutoscaler\n"
                "metadata:\n  name: {{app_name}}\n"
                "spec:\n"
                "  scaleTargetRef:\n"
                "    apiVersion: apps/v1\n"
                "    kind: Deployment\n"
                "    name: {{app_name}}\n"
                "  minReplicas: 2\n  maxReplicas: 10\n"
                "```\n"
            ),
            file_path="skills/infrastructure/hpa.md",
            mode="template",
        )
        engine = SkillEngine(tmp_path, platform=None, self_managed=True)
        assert engine.generate(skill, make_report(repo_name="agentit"), llm_client=None) == []

    def test_fleet_hpa_uses_discovered_rollout_not_invented_deployment(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """pinky-class: Rollout/pinky exists — do not emit Deployment/pinky."""
        from agentit.portal.fleet_hpa import NamespaceWorkloads

        skill = Skill(
            name="hpa",
            domain="infrastructure",
            version=2,
            triggers=["scaling"],
            outputs=["HorizontalPodAutoscaler"],
            property_description="scales",
            body="# HPA\n",
            file_path="skills/infrastructure/hpa.md",
            mode="template",
        )
        engine = SkillEngine(tmp_path, platform=None, self_managed=False)
        monkeypatch.setattr(
            "agentit.portal.fleet_hpa.discover_namespace_workloads",
            lambda ns: NamespaceWorkloads(
                namespace=ns,
                deployments=("pinky-api", "pinky-web", "pinky-worker"),
                rollouts=("pinky",),
            ),
        )
        files = engine.generate(skill, make_report(repo_name="pinky"), llm_client=None)
        assert len(files) == 1
        assert "kind: Rollout" in files[0].content
        assert "name: pinky\n" in files[0].content
        # Must not invent Deployment/pinky
        assert "kind: Deployment" not in files[0].content

    def test_fleet_hpa_skips_when_no_live_targets(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from agentit.portal.fleet_hpa import NamespaceWorkloads

        skill = Skill(
            name="hpa",
            domain="infrastructure",
            version=2,
            triggers=["scaling"],
            outputs=["HorizontalPodAutoscaler"],
            property_description="scales",
            body="# HPA\n```yaml\napiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\n```\n",
            file_path="skills/infrastructure/hpa.md",
            mode="template",
        )
        engine = SkillEngine(tmp_path, platform=None, self_managed=False)
        monkeypatch.setattr(
            "agentit.portal.fleet_hpa.discover_namespace_workloads",
            lambda ns: NamespaceWorkloads(
                namespace=ns, deployments=(), rollouts=(), discovery_ok=True,
            ),
        )
        assert engine.generate(skill, make_report(repo_name="ghost"), llm_client=None) == []

    def test_self_managed_llm_rejects_wrong_hpa_accepts_rollout(self, tmp_path: Path) -> None:
        skill = _make_skill(
            "hpa", outputs=["HorizontalPodAutoscaler"], mode="llm", triggers=["scaling"],
        )
        engine = SkillEngine(tmp_path, platform=None, self_managed=True)
        report = make_report(repo_name="agentit")
        captured: dict[str, str] = {}
        calls = {"n": 0}

        class _FakeLLM:
            def _chat(self, system, user, max_tokens=None):
                captured["user"] = user
                calls["n"] += 1
                if calls["n"] == 1:
                    # #134-shaped junk
                    return (
                        "apiVersion: autoscaling/v2\n"
                        "kind: HorizontalPodAutoscaler\n"
                        "metadata:\n"
                        "  name: \"{{ .Release.Name }}-agentit\"\n"
                        "  namespace: \"{{ .Release.Namespace }}\"\n"
                        "spec:\n"
                        "  scaleTargetRef:\n"
                        "    apiVersion: apps/v1\n"
                        "    kind: Deployment\n"
                        "    name: \"{{ .Release.Name }}-agentit\"\n"
                        "  minReplicas: 2\n"
                        "  maxReplicas: 10\n"
                        "  metrics:\n"
                        "    - type: Resource\n"
                        "      resource:\n"
                        "        name: cpu\n"
                        "        target:\n"
                        "          type: Utilization\n"
                        "          averageUtilization: 80\n"
                    )
                return (
                    "apiVersion: autoscaling/v2\n"
                    "kind: HorizontalPodAutoscaler\n"
                    "metadata:\n"
                    "  name: \"{{ .Release.Name }}\"\n"
                    "  namespace: \"{{ .Release.Namespace }}\"\n"
                    "spec:\n"
                    "  scaleTargetRef:\n"
                    "    apiVersion: argoproj.io/v1alpha1\n"
                    "    kind: Rollout\n"
                    "    name: \"{{ .Release.Name }}\"\n"
                    "  minReplicas: 1\n"
                    "  maxReplicas: 1\n"
                    "  metrics:\n"
                    "    - type: Resource\n"
                    "      resource:\n"
                    "        name: cpu\n"
                    "        target:\n"
                    "          type: Utilization\n"
                    "          averageUtilization: 80\n"
                )

        files = engine.generate(skill, report, llm_client=_FakeLLM())
        assert "HORIZONTALPODAUTOSCALER" in captured["user"]
        assert len(files) == 1
        assert "kind: Rollout" in files[0].content
        assert "maxReplicas: 1" in files[0].content
        assert calls["n"] == 2


class TestSkillToCheckDefinition:
    """_skill_to_check_definition()/detect_check_definitions() -- the bridge
    that runs a mode: detect skill's rule through check_engine's own
    runners, so a Markdown-defined rule and a legacy checks/*.yaml file
    behave identically."""

    def test_compiles_a_valid_detect_skill(self, tmp_path: Path) -> None:
        skill = load_skill(_write_detect_skill(tmp_path))
        assert skill is not None
        defn = _skill_to_check_definition(skill)
        assert defn is not None
        assert defn.name == "health-probes-check"
        assert defn.dimension == "observability"
        assert defn.category == "health"
        assert defn.check_type == "file_contains"
        assert defn.pattern == "livenessProbe"

    def test_returns_none_for_invalid_rule_type(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.md"
        p.write_text(_DETECT_SKILL_MD.format(name="bad", status="active").replace(
            "type: file_contains", "type: not_a_real_type",
        ))
        skill = load_skill(p)
        assert skill is not None
        assert _skill_to_check_definition(skill) is None

    def test_returns_none_for_invalid_severity(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.md"
        p.write_text(_DETECT_SKILL_MD.format(name="bad", status="active").replace(
            "severity: high", "severity: extreme",
        ))
        skill = load_skill(p)
        assert skill is not None
        assert _skill_to_check_definition(skill) is None

    def test_category_falls_back_to_domain_when_unset(self, tmp_path: Path) -> None:
        p = tmp_path / "nocat.md"
        p.write_text(_DETECT_SKILL_MD.format(name="nocat", status="active").replace(
            "category: health\n", "",
        ))
        skill = load_skill(p)
        assert skill is not None
        defn = _skill_to_check_definition(skill)
        assert defn is not None
        assert defn.category == "observability"

    def test_detect_check_definitions_includes_active_skill(self, tmp_path: Path) -> None:
        _write_detect_skill(tmp_path, status="active")
        skills = load_all_skills(tmp_path)
        defs = detect_check_definitions(skills)
        assert len(defs) == 1
        assert defs[0].name == "health-probes-check"

    def test_detect_check_definitions_excludes_draft(self, tmp_path: Path) -> None:
        _write_detect_skill(tmp_path, status="draft")
        skills = load_all_skills(tmp_path)
        assert detect_check_definitions(skills) == []

    def test_detect_check_definitions_excludes_retired(self, tmp_path: Path) -> None:
        _write_detect_skill(tmp_path, status="retired")
        skills = load_all_skills(tmp_path)
        assert detect_check_definitions(skills) == []

    def test_detect_check_definitions_includes_deprecated(self, tmp_path: Path) -> None:
        """Deprecated is a "still runs, but flagged" state -- mirrors
        Skill.matches()'s own "deprecated matches but warns" behavior for
        template-mode skills."""
        _write_detect_skill(tmp_path, status="deprecated")
        skills = load_all_skills(tmp_path)
        defs = detect_check_definitions(skills)
        assert len(defs) == 1

    def test_detect_check_definitions_ignores_template_mode_skills(self, tmp_path: Path) -> None:
        """A directory with only ordinary template-mode skills produces no
        CheckDefinitions at all -- zero impact on the existing skill catalog."""
        skill_path = tmp_path / "netpol.md"
        skill_path.write_text(
            "---\nname: netpol\ndomain: security\nversion: 1\n"
            "triggers: [network]\noutputs: [NetworkPolicy]\nmode: template\n---\n\nbody\n"
        )
        skills = load_all_skills(tmp_path)
        assert detect_check_definitions(skills) == []


class TestDetectModeParity:
    """Proves the ported skills/observability/health-probes-check.md
    produces exactly the same Finding the deleted
    checks/observability/health-check.yaml used to, before that YAML file
    was removed in the same commit as this test -- the Phase 1 cutover
    proof, mirroring the discipline
    docs/extension-model-unification-plan.md's own (rescued, superseded)
    Task 3 established for the checks-vs-analyzers migration: prove
    identical output, then delete the old artifact in the same commit."""

    REAL_SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "observability" / "health-probes-check.md"

    def test_real_ported_skill_loads_and_compiles(self) -> None:
        skill = load_skill(self.REAL_SKILL_PATH)
        assert skill is not None
        assert skill.mode == "detect"
        defn = _skill_to_check_definition(skill)
        assert defn is not None

    def test_fires_identically_to_the_deleted_yaml_check_when_probe_absent(self, create_mock_repo) -> None:
        repo = create_mock_repo({"deploy/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\n"})
        skill = load_skill(self.REAL_SKILL_PATH)
        assert skill is not None
        defs = detect_check_definitions([skill])
        findings = run_checks(defs, repo)
        assert len(findings) == 1
        assert findings[0].category == "health"
        assert findings[0].description == "No liveness/readiness probes detected in manifests"
        assert findings[0].recommendation == "Add livenessProbe and readinessProbe to all containers"
        assert findings[0].severity == Severity.high

    def test_passes_identically_to_the_deleted_yaml_check_when_probe_present(self, create_mock_repo) -> None:
        repo = create_mock_repo({
            "deploy/deployment.yaml": "livenessProbe:\n  httpGet:\n    path: /health\n",
        })
        skill = load_skill(self.REAL_SKILL_PATH)
        assert skill is not None
        defs = detect_check_definitions([skill])
        findings = run_checks(defs, repo)
        assert findings == []


class TestVerifyDetectSkill:
    """verify_skill() must branch to detect-shaped verification for mode:
    detect skills instead of running the remediation-shaped
    triggers/outputs/generation checks, which are meaningless here."""

    def test_valid_detect_skill_passes(self, tmp_path: Path) -> None:
        skill = load_skill(_write_detect_skill(tmp_path))
        assert skill is not None
        passed, issues, warnings = verify_skill(skill)
        assert passed is True
        assert issues == []

    def test_missing_rule_blocks_activation(self, tmp_path: Path) -> None:
        p = tmp_path / "norule.md"
        p.write_text(_DETECT_SKILL_MD.format(name="norule", status="active").replace(
            "rule:\n  type: file_contains\n  pattern: livenessProbe\n", "",
        ))
        skill = load_skill(p)
        assert skill is not None
        passed, issues, _warnings = verify_skill(skill)
        assert passed is False
        assert any("rule" in i for i in issues)

    def test_missing_severity_blocks_activation(self, tmp_path: Path) -> None:
        p = tmp_path / "nosev.md"
        p.write_text(_DETECT_SKILL_MD.format(name="nosev", status="active").replace("severity: high\n", ""))
        skill = load_skill(p)
        assert skill is not None
        passed, issues, _warnings = verify_skill(skill)
        assert passed is False
        assert any("severity" in i for i in issues)

    def test_invalid_rule_type_blocks_activation_even_with_all_fields_present(self, tmp_path: Path) -> None:
        p = tmp_path / "badtype.md"
        p.write_text(_DETECT_SKILL_MD.format(name="badtype", status="active").replace(
            "type: file_contains", "type: not_a_real_type",
        ))
        skill = load_skill(p)
        assert skill is not None
        passed, issues, _warnings = verify_skill(skill)
        assert passed is False
        assert any("compile" in i for i in issues)


class TestRunAssessmentPicksUpDetectModeSkills:
    """End-to-end: runner.run_assessment() must merge findings from mode:
    detect skills exactly like it already does for legacy checks/*.yaml
    files -- the actual integration point a caller (portal/CLI) depends on."""

    def test_detect_skill_finding_appears_in_report(self, tmp_path: Path, create_mock_repo) -> None:
        from agentit.runner import run_assessment

        repo = create_mock_repo({"main.py": "print('hi')\n"})
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        obs_dir = skills_dir / "observability"
        obs_dir.mkdir()
        _write_detect_skill(obs_dir)
        empty_checks_dir = tmp_path / "empty_checks"
        empty_checks_dir.mkdir()

        report = run_assessment(
            repo, repo_url="https://github.com/test/app",
            checks_dir=empty_checks_dir, skills_dir=skills_dir,
        )
        obs = next(s for s in report.scores if s.dimension == "observability")
        assert any(f.category == "health" for f in obs.findings)

    def test_checks_only_dimension_appears_with_clean_score_when_skill_check_passes(
        self, tmp_path: Path, create_mock_repo,
    ) -> None:
        """A dimension whose only producer is a mode: detect skill (no
        analyzer at all -- true today for every skill-only domain, e.g.
        chaos/cost/incident) must still appear in report.scores with a
        clean 100/100 score when its check passes -- not silently vanish.
        This is the runner._merge_check_findings fix
        (docs/extension-model-unification-plan-2026-07-18.md, Phase 1)."""
        from agentit.runner import run_assessment

        repo = create_mock_repo({"main.py": "print('hi')\n"})
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        chaos_dir = skills_dir / "chaos"
        chaos_dir.mkdir()
        (chaos_dir / "synthetic-check.md").write_text(
            "---\n"
            "name: synthetic-check\n"
            "domain: totally_synthetic_dimension\n"
            "version: 1\n"
            "mode: detect\n"
            "triggers: []\n"
            "outputs: []\n"
            "severity: low\n"
            "category: synthetic\n"
            "type: file_missing\n"
            "description: synthetic finding that should never fire\n"
            "recommendation: n/a\n"
            "rule:\n"
            "  type: file_missing\n"
            '  pattern: "this-file-should-never-exist.xyz"\n'
            "status: active\n"
            "---\n\nbody\n"
        )
        empty_checks_dir = tmp_path / "empty_checks"
        empty_checks_dir.mkdir()

        report = run_assessment(
            repo, repo_url="https://github.com/test/app",
            checks_dir=empty_checks_dir, skills_dir=skills_dir,
        )
        synthetic = next(
            (s for s in report.scores if s.dimension == "totally_synthetic_dimension"), None,
        )
        assert synthetic is not None, "checks-only dimension vanished when its only check passed"
        assert synthetic.score == 100
        assert synthetic.findings == []
