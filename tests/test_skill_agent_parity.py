"""Skill-engine parity coverage.

The product decision is to remove the hardcoded Python agents
(``src/agentit/agents/*.py``) and rely on skills + the check engine going
forward. That decision depends on skills being able to produce a useful
baseline artifact via template substitution alone -- with no LLM in the
loop -- for every domain a Python agent used to cover.

These tests exercise ``SkillEngine`` directly (no LLM client, no live
platform) against realistic ``AssessmentReport`` fixtures and assert that
real manifests come out the other end: non-empty, valid YAML, with the
resource kinds a human would expect. This is the skill-engine integration
coverage that ``test_orchestrator.py`` does not have today.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    Severity,
    StackInfo,
)
from agentit.skill_engine import SkillEngine, load_skill

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# The 8 skills that used to be mode: llm with no template block at all --
# i.e. they produced NOTHING without an LLM connection. This is the set the
# product owner flagged as a hard blocker for removing the Python fallback.
FORMERLY_LLM_ONLY_SKILLS: dict[str, set[str]] = {
    "skills/security/network-policy.md": {"NetworkPolicy"},
    "skills/security/containerfile.md": {"BuildConfig"},
    "skills/cicd/tekton-pipeline.md": {"Pipeline", "PipelineRun"},
    "skills/retirement/decommission-plan.md": {"ConfigMap"},
    "skills/release/release-runbook.md": {"ConfigMap"},
    "skills/incident/runbook.md": {"ConfigMap"},
    "skills/compliance/image-registry-policy.md": {"Policy"},
    "skills/compliance/compliance-evidence.md": {"ConfigMap"},
}

# Domains that a hardcoded Python agent (src/agentit/agents/*.py) currently
# covers, per agents/capabilities.py's AGENT_CLASSES as of this audit.
# Hardcoded here (rather than imported) so this test never depends on the
# in-flux agents/ package -- it only needs to know what skill-domain
# coverage is expected to replace.
PYTHON_AGENT_DOMAINS = {
    "security", "observability", "cicd", "compliance", "infrastructure",
    "cost", "dependency", "incident", "release", "retirement",
}
# codechange (source-level patches) and chaos (previously unregistered,
# dead code) are deliberately excluded -- see docs/agent-removal-readiness.md
# for why codechange doesn't fit the skill model at all, and why chaos is
# handled separately.


def _lang(name: str = "python") -> Language:
    return Language(name=name, version="3.12", file_count=20, percentage=100.0)


def _finding(category: str, description: str, *, severity: Severity = Severity.high) -> Finding:
    return Finding(
        category=category,
        severity=severity,
        description=description,
        recommendation=f"Address: {description}",
    )


def _make_full_coverage_report(repo_name: str = "parity-app") -> AssessmentReport:
    """A single assessment report whose findings span every skill domain.

    ``Skill.matches()`` checks trigger keywords against the *entire* report
    text (summary + every dimension + every finding's category/description/
    recommendation) -- not per-finding -- so one report with a broad set of
    findings is sufficient to exercise matching across all domains at once.
    """
    scores = [
        DimensionScore(
            dimension="security",
            score=25,
            max_score=100,
            findings=[
                _finding("network security", "No NetworkPolicy restricts ingress or egress"),
                _finding("container image", "No Containerfile / Dockerfile found"),
                _finding("rbac authorization", "Application runs under the default ServiceAccount"),
                _finding("resource limits", "Containers have no CPU/memory limits"),
                _finding("vulnerability scanning", "No image scanning configured"),
                _finding("security context root", "Container runs as root"),
            ],
        ),
        DimensionScore(
            dimension="cicd",
            score=30,
            max_score=100,
            findings=[
                _finding("pipeline cicd", "No automated CI/CD pipeline"),
                _finding("gitops deployment", "No Argo CD Application manages this app"),
                _finding("rollout canary", "No progressive delivery strategy configured"),
            ],
        ),
        DimensionScore(
            dimension="compliance",
            score=35,
            max_score=100,
            findings=[
                _finding("policy compliance label governance", "No Kyverno policy enforcement"),
                _finding("sbom bill of materials", "No SBOM generated for builds"),
                _finding("audit logging", "No Kubernetes audit policy configured"),
                _finding("registry image policy", "Images not restricted to trusted registries"),
                _finding("compliance evidence attestation", "No compliance evidence document exists"),
            ],
        ),
        DimensionScore(
            dimension="infrastructure",
            score=40,
            max_score=100,
            findings=[
                _finding("scaling availability replica", "No HorizontalPodAutoscaler configured"),
                _finding("disruption availability ha", "No PodDisruptionBudget configured"),
                _finding("quota resource limit governance namespace", "No ResourceQuota configured"),
                _finding("namespace project environment", "No dedicated Namespace manifest"),
            ],
        ),
        DimensionScore(
            dimension="cost",
            score=50,
            max_score=100,
            findings=[
                _finding("cost rightsize resources", "No VerticalPodAutoscaler for right-sizing"),
                _finding("cost attribution chargeback label", "No cost-attribution labels configured"),
            ],
        ),
        DimensionScore(
            dimension="dependency",
            score=45,
            max_score=100,
            findings=[
                _finding("dependency update renovate", "No automated dependency update tooling"),
                _finding("dependency scan schedule weekly", "No scheduled dependency vulnerability scan"),
            ],
        ),
        DimensionScore(
            dimension="incident",
            score=30,
            max_score=100,
            findings=[
                _finding("runbook incident operations oncall", "No incident runbook documented"),
                _finding("pagerduty alert oncall notification", "No PagerDuty integration configured"),
                _finding("alertmanager alert routing notification", "No Alertmanager routing configured"),
            ],
        ),
        DimensionScore(
            dimension="release",
            score=40,
            max_score=100,
            findings=[
                _finding("release deploy runbook checklist", "No release runbook documented"),
                _finding("analysis canary verification rollout", "No AnalysisTemplate gates canary promotion"),
            ],
        ),
        DimensionScore(
            dimension="retirement",
            score=20,
            max_score=100,
            findings=[
                _finding("retirement decommission sunset", "Application has no decommission plan"),
            ],
        ),
        DimensionScore(
            dimension="observability",
            score=35,
            max_score=100,
            findings=[
                _finding("metrics monitoring prometheus", "No ServiceMonitor scrapes this app"),
                _finding("dashboard grafana visualization", "No Grafana dashboard exists"),
                _finding("alert monitoring observability prometheus", "No PrometheusRule alerts on this app"),
            ],
        ),
        DimensionScore(
            dimension="ha_dr",
            score=40,
            max_score=100,
            findings=[
                _finding("availability resilience", "No chaos/resiliency testing verifies recovery"),
            ],
        ),
    ]

    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc),
        stack=StackInfo(
            languages=[_lang()],
            frameworks=[],
            databases=[],
            runtimes=[],
            package_managers=["pip"],
        ),
        architecture=ArchitectureInfo(
            service_count=1,
            architecture_style="monolith",
            has_api=True,
            api_style="REST",
            external_dependencies=[],
            auth_mechanism=None,
        ),
        scores=scores,
        criticality="high",
        summary="Comprehensive fixture report exercising every skill domain.",
        remediation_plan=[],
    )


@pytest.fixture()
def engine() -> SkillEngine:
    """A SkillEngine with no platform context, so output-kind gating never
    filters anything out -- these tests are about template generation, not
    platform discovery."""
    return SkillEngine(SKILLS_DIR, platform=None)


@pytest.fixture()
def full_report() -> AssessmentReport:
    return _make_full_coverage_report()


# ---------------------------------------------------------------------------
# 1. The 8 formerly-LLM-only skills must produce output with no LLM at all.
# ---------------------------------------------------------------------------


class TestFormerlyLLMOnlySkillsHaveTemplateFallback:
    """Each of these skills used to have `mode: llm` and no ```yaml``` block
    in its body -- generate() would return [] whenever no LLM was available.
    That is now fixed; verify it stays fixed."""

    @pytest.mark.parametrize("skill_path,expected_kinds", sorted(FORMERLY_LLM_ONLY_SKILLS.items()))
    def test_generates_nonempty_valid_manifest_without_llm(
        self, skill_path: str, expected_kinds: set[str], full_report: AssessmentReport,
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        skill = load_skill(repo_root / skill_path)
        assert skill is not None, f"failed to load {skill_path}"

        engine = SkillEngine(SKILLS_DIR, platform=None)
        files = engine.generate(skill, full_report, llm_client=None)

        assert files, f"{skill_path}: template fallback produced no files"
        for f in files:
            assert f.content.strip(), f"{skill_path}: generated file has empty content"
            docs = [d for d in yaml.safe_load_all(f.content) if d is not None]
            assert docs, f"{skill_path}: generated content has no YAML documents"
            kinds = {d.get("kind") for d in docs if isinstance(d, dict)}
            assert kinds & expected_kinds, (
                f"{skill_path}: expected one of {expected_kinds}, got {kinds}"
            )
            for d in docs:
                assert isinstance(d, dict)
                assert "apiVersion" in d, f"{skill_path}: doc missing apiVersion"
                assert "metadata" in d, f"{skill_path}: doc missing metadata"
                assert d["metadata"].get("name") or d["metadata"].get("generateName")

    def test_no_unsubstituted_app_name_placeholder_remains(self, full_report: AssessmentReport) -> None:
        """`{{app_name}}` must always be substituted -- only other,
        deliberately-manual placeholders (e.g. `{{repo_url}}`) may remain."""
        repo_root = Path(__file__).resolve().parent.parent
        engine = SkillEngine(SKILLS_DIR, platform=None)
        for skill_path in FORMERLY_LLM_ONLY_SKILLS:
            skill = load_skill(repo_root / skill_path)
            files = engine.generate(skill, full_report, llm_client=None)
            for f in files:
                assert "{{app_name}}" not in f.content, f"{skill_path}: app_name not substituted"
                assert "parity-app" in f.content, f"{skill_path}: expected app name in output"


# ---------------------------------------------------------------------------
# 2. Narrative-document skills embed a real markdown baseline in a ConfigMap.
# ---------------------------------------------------------------------------


class TestNarrativeSkillsEmbedUsableMarkdown:
    """decommission-plan, release-runbook, runbook, and compliance-evidence
    generate prose, not native K8s objects. The template fallback wraps that
    prose in a ConfigMap so the skill engine (which only ever writes a
    single .yaml file) still produces something applyable and inspectable."""

    @pytest.mark.parametrize(
        "skill_path,data_key,required_snippets",
        [
            ("skills/retirement/decommission-plan.md", "decommission-plan.md",
             ["Decommission Plan", "Stakeholder Notification", "Resource Reclamation Timeline"]),
            ("skills/release/release-runbook.md", "release-runbook.md",
             ["Release Runbook", "Pre-Deployment Checklist", "Rollback Triggers"]),
            ("skills/incident/runbook.md", "runbook.md",
             ["Incident Response Runbook", "Escalation Matrix", "Recovery Procedures"]),
            ("skills/compliance/compliance-evidence.md", "compliance-evidence.md",
             ["Compliance Evidence Report", "Gap Analysis"]),
        ],
    )
    def test_configmap_contains_real_markdown(
        self, skill_path: str, data_key: str, required_snippets: list[str], full_report: AssessmentReport,
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        skill = load_skill(repo_root / skill_path)
        engine = SkillEngine(SKILLS_DIR, platform=None)
        files = engine.generate(skill, full_report, llm_client=None)
        assert files

        doc = yaml.safe_load(files[0].content)
        assert doc["kind"] == "ConfigMap"
        assert data_key in doc["data"], f"{skill_path}: expected data key {data_key}"
        markdown = doc["data"][data_key]
        for snippet in required_snippets:
            assert snippet in markdown, f"{skill_path}: markdown missing '{snippet}'"


# ---------------------------------------------------------------------------
# 3. Domain-level parity: every domain a Python agent used to own now has
#    skill coverage that actually fires and generates something.
# ---------------------------------------------------------------------------


class TestDomainCoverageParity:
    def test_full_report_covers_every_former_python_agent_domain(
        self, engine: SkillEngine, full_report: AssessmentReport,
    ) -> None:
        files = engine.run_all(full_report, llm_client=None)
        assert files, "no skill files generated at all for the full-coverage report"

        covered = engine.covered_domains(files)
        missing = PYTHON_AGENT_DOMAINS - covered
        assert not missing, (
            f"domains with a Python agent equivalent produced no skill output: {sorted(missing)}"
        )

    def test_every_generated_file_is_nonempty_and_valid_yaml(
        self, engine: SkillEngine, full_report: AssessmentReport,
    ) -> None:
        files = engine.run_all(full_report, llm_client=None)
        assert len(files) >= 10, "expected broad skill coverage from the full-coverage fixture"

        for f in files:
            assert f.content.strip(), f"{f.path}: empty content"
            docs = [d for d in yaml.safe_load_all(f.content) if d is not None]
            assert docs, f"{f.path}: no parseable YAML documents"
            for d in docs:
                assert isinstance(d, dict), f"{f.path}: doc is not a mapping"

    def test_chaos_domain_fires_on_availability_findings(
        self, engine: SkillEngine, full_report: AssessmentReport,
    ) -> None:
        """Chaos experiments have no Python-agent parity requirement (the
        agent was dead/unregistered), but the new chaos skills should still
        fire on resiliency/availability findings and produce ChaosEngines."""
        files = engine.run_all(full_report, llm_client=None)

        def _first_doc(content: str) -> dict:
            return next(d for d in yaml.safe_load_all(content) if d is not None)

        chaos_files = [f for f in files if _first_doc(f.content).get("kind") == "ChaosEngine"]
        assert chaos_files, "expected at least one chaos skill to fire"
        for f in chaos_files:
            doc = _first_doc(f.content)
            assert doc["kind"] == "ChaosEngine"
            assert doc["spec"]["chaosServiceAccount"]


# ---------------------------------------------------------------------------
# 4. Chaos skills use correct Litmus semantics (the Python agent's chaos.py
#    used non-standard experiment names/fields per the 2026-07-12 code
#    review -- guard against reintroducing that bug in the skill).
# ---------------------------------------------------------------------------


class TestChaosSkillsUseCorrectLitmusSemantics:
    def test_pod_delete_uses_real_litmus_experiment_name(self, full_report: AssessmentReport) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        skill = load_skill(repo_root / "skills/chaos/pod-delete.md")
        engine = SkillEngine(SKILLS_DIR, platform=None)
        files = engine.generate(skill, full_report, llm_client=None)
        assert files
        doc = yaml.safe_load(files[0].content)
        exp = doc["spec"]["experiments"][0]
        assert exp["name"] == "pod-delete"
        env_names = {e["name"] for e in exp["spec"]["components"]["env"]}
        assert "PODS_AFFECTED_PERC" in env_names
        probe_cmd = doc["spec"]["experiments"][0]["spec"]["probe"][0]["k8sProbe/inputs"]["command"]
        assert "labelSelector" in probe_cmd
        assert "fieldSelector" not in probe_cmd

    def test_network_latency_uses_real_litmus_experiment_name(self, full_report: AssessmentReport) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        skill = load_skill(repo_root / "skills/chaos/network-latency.md")
        engine = SkillEngine(SKILLS_DIR, platform=None)
        files = engine.generate(skill, full_report, llm_client=None)
        assert files
        doc = yaml.safe_load(files[0].content)
        exp = doc["spec"]["experiments"][0]
        assert exp["name"] == "pod-network-latency"
