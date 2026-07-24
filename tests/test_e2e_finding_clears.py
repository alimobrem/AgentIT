"""End-to-end proof that a delivered fix actually clears the finding it
claims to -- generate -> deliver -> re-Assess, start to finish -- using
pulse-agent's own real, live findings as the first fixtures (``replicas``:
a Helm-templated chart; ``container``: a Dockerfile ``:latest`` pin).

Every other test in this suite proves staged content / routing / refuse
reasons in isolation (``test_workload_patches.py``, ``test_quality_prs.py``,
``test_clear_evidence.py``, ...) -- none of them run the real loop this
product's own contract depends on ("a good AgentIT PR clears a real
finding on next re-Assess"). That gap is exactly why the ``#199``-``#204``
class of bug (a verifier too weak to notice a "fix" that would not
actually clear the finding) was only ever caught by live dogfooding, one
incident at a time, not CI -- see docs/plan-quality-helpful-prs.md.

Uses a real temp-directory "repo" and the real analyzer + skill-generation
+ enrichment functions; only the GitHub API boundary (``read_file`` /
``tree_paths``) is backed by that same temp directory instead of a live
network call, so the whole loop runs hermetically and fast.

Covers the first two of pulse-agent's real, live findings (``replicas``,
``container``); ``sbom`` (``enrich_sbom_from_repo`` -- CycloneDX inventory
enrichment) is a natural next fixture to add here, not yet built out.
"""
from __future__ import annotations

from pathlib import Path

from agentit.analyzers.ha_dr import HADRAnalyzer
from agentit.analyzers.security import SecurityAnalyzer
from agentit.models import AssessmentReport, DimensionScore, Finding, Severity
from agentit.remediation.source_patches import (
    apply_containerfile_pin_only,
    enrich_workload_files_from_repo,
    generate_source_patch_for_skill,
)
from agentit.skill_engine import load_all_skills
from conftest import make_report

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _load_skill(name: str):
    for skill in load_all_skills(_SKILLS_DIR):
        if skill.name == name:
            return skill
    raise AssertionError(f"skill {name!r} not found under {_SKILLS_DIR}")


def _repo_tree(root: Path) -> list[str]:
    return sorted(
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file()
    )


def _read_from(root: Path):
    def _read(path: str) -> str | None:
        p = root / path
        return p.read_text() if p.is_file() else None
    return _read


class TestReplicasFindingActuallyClearsAfterMerge:
    """pulse-agent's real shape: a Helm chart whose Deployment templates
    ``replicas:`` via ``{{ .Values.replicaCount }}`` and a values.yaml
    declaring ``replicaCount: 1`` -- the exact finding
    pulse-agent#5/#6 (a fabricated ``deploy/deployment.yaml``) never
    actually cleared."""

    def _write_repo(self, root: Path) -> None:
        chart = root / "chart" / "templates"
        chart.mkdir(parents=True)
        (chart / "deployment.yaml").write_text(
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: pulse-agent\n"
            "spec:\n  replicas: {{ .Values.replicaCount }}\n"
            "  template:\n    spec:\n      containers:\n"
            "        - name: pulse-agent\n          image: pulse-agent:1\n"
        )
        (root / "chart" / "values.yaml").write_text("replicaCount: 1\n")

    def test_generate_deliver_reassess_clears_the_finding(self, tmp_path: Path) -> None:
        self._write_repo(tmp_path)

        # 1. Baseline: the real analyzer flags the real, unfixed repo.
        before = HADRAnalyzer().analyze(tmp_path)
        assert any(f.category == "replicas" for f in before.findings), (
            "fixture repo did not reproduce the baseline finding"
        )

        # 2. Generate: the real workload-replicas skill's output with no
        #    repo context yet -- exactly what generation sees today, since
        #    the analyzers.snapshot ContextVar is only ever populated
        #    during run_assessment()'s own analyzer pass, long gone by the
        #    time a separate onboarding job calls this generator.
        report = make_report(
            repo_name="pulse-agent",
            scores=[DimensionScore(
                dimension="ha_dr", score=40, max_score=100,
                findings=[Finding(
                    category="replicas", severity=Severity.high,
                    description="Single replica or no replica count defined -- no redundancy",
                    recommendation="Set replicas >= 2 for high availability",
                )],
            )],
        )
        skill = _load_skill("workload-replicas")
        stub_files = [
            f.model_dump() for f in generate_source_patch_for_skill(skill, report, "pulse-agent")
        ]
        assert stub_files, "workload-replicas skill produced no output"
        # Confirms the root-cause bug this whole test guards against:
        # without enrichment, generation alone still fabricates a
        # disconnected stand-in, exactly like the real incident.
        assert stub_files[0]["target_path"] == "deploy/deployment.yaml"

        # 3. Enrich: the real fix (source_patches.enrich_workload_files_
        #    from_repo), using read_file/tree_paths backed by the temp repo
        #    instead of a live GitHub call.
        enriched_files, drop_reasons = enrich_workload_files_from_repo(
            stub_files, read_file=_read_from(tmp_path), tree_paths=_repo_tree(tmp_path),
        )
        assert not drop_reasons, drop_reasons
        assert len(enriched_files) == 1
        assert enriched_files[0]["target_path"] == "chart/values.yaml"
        assert "replicaCount: 2" in enriched_files[0]["content"]
        # The chart template itself is never rewritten -- no second,
        # conflicting literal replicas: key next to the templated one.
        template_text = (tmp_path / "chart/templates/deployment.yaml").read_text()
        assert "{{ .Values.replicaCount }}" in template_text

        # 4. Deliver: simulate a human merging the PR + Argo sync -- write
        #    the enriched content to its real target path.
        (tmp_path / enriched_files[0]["target_path"]).write_text(enriched_files[0]["content"])

        # 5. Re-Assess: the real analyzer, against the now-patched repo.
        after = HADRAnalyzer().analyze(tmp_path)
        assert not any(f.category == "replicas" for f in after.findings), (
            "replicas finding did not clear after merge -- the generated "
            "fix does not actually attach to the real workload"
        )

    def test_known_gap_the_disconnected_stub_alone_also_fools_the_analyzer(
        self, tmp_path: Path,
    ) -> None:
        """Known limitation, surfaced by writing this e2e test -- not fixed
        here, flagged rather than silently left undiscovered.

        The intended "negative control" for the test above was: deliver the
        un-enriched, fabricated ``deploy/deployment.yaml`` stub as-is (the
        real pulse-agent#5/#6 shape) and confirm the finding stays open,
        proving enrichment is what actually matters. It does not stay
        open: ``HADRAnalyzer`` scans every YAML file in the whole repo
        tree for *any* ``replicas: >=2`` line (``iter_yaml_files`` is
        repo-wide, not scoped to "the workload that is actually deployed")
        -- so a completely disconnected stub sitting next to the real,
        still-unfixed chart also clears the finding, exactly as
        dishonestly as the real theater PR would have. The *detection*
        side has the same "is this the real workload" blind spot the
        *generation* side just had; this test documents it precisely
        rather than asserting something false.
        """
        self._write_repo(tmp_path)
        report = make_report(
            repo_name="pulse-agent",
            scores=[DimensionScore(
                dimension="ha_dr", score=40, max_score=100,
                findings=[Finding(
                    category="replicas", severity=Severity.high,
                    description="Single replica or no replica count defined -- no redundancy",
                    recommendation="Set replicas >= 2 for high availability",
                )],
            )],
        )
        skill = _load_skill("workload-replicas")
        stub_files = [
            f.model_dump() for f in generate_source_patch_for_skill(skill, report, "pulse-agent")
        ]
        # Deliver the stub unmodified, to its own (fabricated) target path
        # -- the real chart/values.yaml is left at replicaCount: 1.
        stub_path = tmp_path / stub_files[0]["target_path"]
        stub_path.parent.mkdir(parents=True, exist_ok=True)
        stub_path.write_text(stub_files[0]["content"])
        assert (tmp_path / "chart/values.yaml").read_text() == "replicaCount: 1\n"

        after = HADRAnalyzer().analyze(tmp_path)
        assert not any(f.category == "replicas" for f in after.findings), (
            "if this now fails, HADRAnalyzer has been scoped to the real "
            "workload and this known-gap test (and its docstring) should "
            "be updated to a real positive assertion instead"
        )


class TestContainerFindingActuallyClearsAfterMerge:
    """pulse-agent's real ``:latest`` Dockerfile finding -- the container
    pin-only mechanism that already worked (unlike replicas) because it
    reads the finding's own persisted ``file_path``, not a live re-scan."""

    def _write_repo(self, root: Path) -> None:
        (root / "Dockerfile").write_text(
            "FROM python:latest\nWORKDIR /app\nCOPY . .\nCMD [\"python\", \"app.py\"]\n"
        )

    def test_generate_deliver_reassess_clears_the_finding(self, tmp_path: Path) -> None:
        self._write_repo(tmp_path)

        # 1. Baseline: the real analyzer flags the real, unfixed repo.
        before = SecurityAnalyzer().analyze(tmp_path)
        latest_before = [
            f for f in before.findings
            if f.category == "container" and "latest" in f.description.lower()
        ]
        assert latest_before, "fixture repo did not reproduce the baseline finding"
        target_findings = [(f.category, f.description) for f in before.findings if f.category == "container"]

        # 2. Generate: the real containerfile skill's pin-only placeholder
        #    for the existing (real, finding-recorded) file_path.
        report: AssessmentReport = make_report(
            repo_name="pulse-agent",
            scores=[DimensionScore(dimension="security", score=40, max_score=100, findings=before.findings)],
        )
        skill = _load_skill("containerfile")
        stub_files = [
            f.model_dump() for f in generate_source_patch_for_skill(skill, report, "pulse-agent")
        ]
        assert stub_files
        assert stub_files[0]["target_path"] == "Dockerfile"

        # 3. Enrich: the real fix (apply_containerfile_pin_only), reading
        #    the existing Dockerfile from the temp repo instead of a live
        #    GitHub call.
        enriched_files = apply_containerfile_pin_only(
            stub_files, read_file=_read_from(tmp_path),
            target_findings=target_findings, language="python",
        )
        assert enriched_files
        assert ":latest" not in enriched_files[0]["content"]

        # 4. Deliver: simulate a human merging the PR.
        (tmp_path / enriched_files[0]["target_path"]).write_text(enriched_files[0]["content"])

        # 5. Re-Assess: the real analyzer, against the now-patched repo.
        after = SecurityAnalyzer().analyze(tmp_path)
        latest_after = [
            f for f in after.findings
            if f.category == "container" and "latest" in f.description.lower()
        ]
        assert not latest_after, (
            "container :latest finding did not clear after merge -- the "
            "generated fix does not actually pin the real Dockerfile"
        )
