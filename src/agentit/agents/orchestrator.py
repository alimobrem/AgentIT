from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from agentit.models import AssessmentReport, Severity
from agentit.portal.metrics import agent_runs_total, agent_run_duration_seconds

logger = logging.getLogger(__name__)


def _safe_path(base: Path, relative: str) -> Path:
    """Resolve a relative path under base, rejecting traversal."""
    clean = PurePosixPath(relative).name  # strips directory components and ..
    return base / clean

AGENT_MODE = os.environ.get("AGENTIT_AGENT_MODE", "local")

# Priority matrix from the spec (Section 4). This only supplies the
# "winner" label used to describe how a *real* conflict (see
# KNOWN_KIND_CONFLICTS / path collisions in _detect_conflicts) should be
# resolved -- it is not itself a conflict trigger. Two agents both
# succeeding is normal and expected, not a conflict.
PRIORITY_MATRIX = {
    ("security", "cicd"): "security",
    ("security", "observability"): "security",
    ("security", "compliance"): "security",
    ("compliance", "cicd"): "compliance",
    ("compliance", "observability"): "compliance",
    ("cicd", "release"): "release",
    ("infrastructure", "security"): "security",
    ("infrastructure", "compliance"): "compliance",
}

# Known agent-pair / resource-kind combinations that genuinely conflict
# when both are actually present for the same workload -- e.g. a VPA in
# "Auto" mode fights an HPA for control over replica/resource sizing.
# See the TODO(orchestrator) markers in agents/cost.py / agents/infrastructure.py.
KNOWN_KIND_CONFLICTS: dict[tuple[str, str], tuple[str, str]] = {
    ("cost", "infrastructure"): ("VerticalPodAutoscaler", "HorizontalPodAutoscaler"),
}


@dataclass
class AgentResult:
    agent_name: str
    category: str
    files_generated: list[str]
    success: bool
    error: str | None = None
    findings_count: int = 0


@dataclass
class OrchestrationPlan:
    repo_url: str
    criticality: str
    score: float
    agents_to_run: list[str]
    gates_required: list[str]
    auto_approve: bool = False


@dataclass
class OrchestrationResult:
    plan: OrchestrationPlan
    agent_results: list[AgentResult] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    gates_created: list[str] = field(default_factory=list)
    recommendation: str = ""


class FleetOrchestrator:
    """Meta-agent that coordinates all other agents.

    Responsibilities:
    - Determine which agents to run based on assessment
    - Resolve conflicts between agent recommendations
    - Decide auto-approve vs. human gate based on risk
    - Track overall onboarding status
    """

    PROFILES = {
        "lightweight": ["security", "cicd"],
        "standard": ["security", "observability", "cicd", "compliance", "infrastructure", "release"],
        "full": None,  # None = all available agents
    }

    def __init__(
        self,
        report: AssessmentReport,
        output_dir: Path,
        store: object | None = None,
        assessment_id: str | None = None,
        profile: str = "standard",
        agent_filter: list[str] | None = None,
    ):
        self.report = report
        self.output_dir = Path(output_dir)
        self._store = store
        self._assessment_id = assessment_id
        self._events: list[dict] = []
        self._profile = profile
        self._agent_filter = agent_filter

    def plan(self) -> OrchestrationPlan:
        """Analyze the assessment and create an orchestration plan."""
        agents = self._select_agents()
        gates = self._determine_gates()
        auto = self._can_auto_approve()

        return OrchestrationPlan(
            repo_url=self.report.repo_url,
            criticality=self.report.criticality,
            score=self.report.overall_score,
            agents_to_run=agents,
            gates_required=gates,
            auto_approve=auto,
        )

    def run(self) -> OrchestrationResult:
        """Execute the full orchestration: plan -> run agents -> resolve conflicts -> create gates."""
        plan = self.plan()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        from agentit.agents.capabilities import AGENT_CAPABILITIES, AGENT_CLASSES, get_agent_class

        # Build agent_map: name -> (category, class)
        agent_map: dict[str, tuple[str, type]] = {}
        for name, (category, _mod, _cls_name, _tier) in AGENT_CLASSES.items():
            try:
                agent_map[name] = (category, get_agent_class(name))
            except (ImportError, ValueError) as exc:
                logger.warning("Failed to import %s agent: %s", name, exc)

        if self._store is not None:
            for name, (cat, _cls) in agent_map.items():
                try:
                    caps = AGENT_CAPABILITIES.get(name, "")
                    self._store.register_agent(name, cat, capabilities=caps)
                except Exception as exc:
                    logger.warning("Failed to register agent '%s': %s", name, exc)

        # --- Step 1: Run skills FIRST (primary generation path) ---
        skill_files: list = []
        skill_covered_domains: set[str] = set()
        try:
            from agentit.skill_engine import SkillEngine
            skills_dir = Path(__file__).parent.parent.parent.parent / "skills"
            if not skills_dir.exists():
                skills_dir = Path("skills")

            try:
                from agentit.platform_context import discover_platform
                platform = discover_platform(os.environ.get("AGENTIT_NAMESPACE", "default"))
            except Exception:
                from agentit.platform_context import offline_context
                platform = offline_context()

            # Match cli.py's _resolve_and_assess: build an LLM client whenever
            # credentials are configured, but never let LLM init failures
            # block skills-first generation (falls back to template mode).
            llm_client = None
            if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"):
                try:
                    from agentit.llm import LLMClient
                    llm_client = LLMClient()
                except Exception as exc:
                    logger.debug("LLM init failed for skills generation (continuing without): %s", exc)

            engine = SkillEngine(skills_dir, platform=platform)
            skill_files = engine.run_all(self.report, store=self._store, llm_client=llm_client)
            skill_covered_domains = engine.covered_domains(skill_files)

            if skill_files:
                self._log_event("skills", "completed",
                                f"Skills generated {len(skill_files)} files covering domains: "
                                f"{', '.join(sorted(skill_covered_domains)) or 'none'}")
                skill_dir = self.output_dir / "skills"
                skill_dir.mkdir(parents=True, exist_ok=True)
                for f in skill_files:
                    safe = _safe_path(skill_dir, f.path)
                    safe.write_text(f.content, encoding="utf-8")
        except Exception as exc:
            logger.debug("Skill engine failed (non-fatal): %s", exc)

        # --- Step 2: Determine which agents to skip (covered by skills) ---
        skip_agents: set[str] = set()
        if skill_covered_domains:
            for agent_name in plan.agents_to_run:
                agent_category = agent_map.get(agent_name, (agent_name,))[0]
                if agent_category in skill_covered_domains:
                    skip_agents.add(agent_name)
            if skip_agents:
                logger.info("Skills covered domains %s — skipping Python agents: %s",
                            ", ".join(sorted(skill_covered_domains)),
                            ", ".join(sorted(skip_agents)))

        # --- Step 3: Run Python agents only for uncovered domains (fallback) ---
        if AGENT_MODE == "kubernetes":
            results = self._run_agents_as_jobs(plan, agent_map, skip_agents=skip_agents)
        else:
            results = self._run_agents_local(plan, agent_map, skip_agents=skip_agents)

        # --- Step 4: Add skill results to the result list ---
        if skill_files:
            results.append(AgentResult(
                agent_name="skills",
                category="skills",
                files_generated=[f.path for f in skill_files],
                success=True,
                findings_count=len(skill_files),
            ))

        # Validate generated output
        validation_issues = self._post_hardening_validation(results)
        if validation_issues:
            for issue in validation_issues:
                logger.warning("Post-hardening validation: %s", issue)
            self._log_event("orchestrator", "validation-issues",
                            f"{len(validation_issues)} manifest validation issue(s) found")

        # Resolve conflicts
        conflicts = self._detect_conflicts(results)

        if validation_issues:
            conflicts.append({
                "type": "validation",
                "agents": list({i.split(":")[0].split("/")[0] for i in validation_issues}),
                "resolution": f"{len(validation_issues)} manifest(s) failed validation — review before deploying",
                "winner": "validation",
            })

        # plan.auto_approve was computed from score/criticality alone at
        # plan() time, before agents ran. A genuine conflict found for THIS
        # actual run must override that and force auto_approve to False --
        # it must never be trusted to auto-deploy when real conflicts exist.
        non_blocker_conflicts = [c for c in conflicts if c["type"] != "blocker"]
        if non_blocker_conflicts:
            plan.auto_approve = False

        # Determine recommendation
        recommendation = self._generate_recommendation(plan, results, conflicts)

        # Write orchestration summary
        self._write_summary(plan, results, conflicts, recommendation)

        return OrchestrationResult(
            plan=plan,
            agent_results=results,
            conflicts=conflicts,
            gates_created=plan.gates_required,
            recommendation=recommendation,
        )

    def _run_agents_local(
        self,
        plan: OrchestrationPlan,
        agent_map: dict[str, tuple[str, type]],
        *,
        skip_agents: set[str] | None = None,
    ) -> list[AgentResult]:
        """Run agents in-process (default mode)."""
        from agentit.agents.capabilities import AGENT_CLASSES

        results: list[AgentResult] = []

        for agent_name in plan.agents_to_run:
            if skip_agents and agent_name in skip_agents:
                logger.info("Skipping agent '%s' — already covered by skills", agent_name)
                continue
            if agent_name not in agent_map:
                logger.warning("Agent '%s' was planned but not available (import failed?)", agent_name)
                results.append(AgentResult(
                    agent_name=agent_name,
                    category=agent_name,
                    files_generated=[],
                    success=False,
                    error=f"Agent '{agent_name}' not available (import failed)",
                ))
                continue
            category, agent_cls = agent_map[agent_name]
            sub_dir = self.output_dir / category
            resource_tier = AGENT_CLASSES.get(agent_name, ("", "", "", "standard"))[3]

            t0 = time.monotonic()
            try:
                agent_instance = agent_cls(report=self.report, output_dir=sub_dir)
                result = agent_instance.run()
                elapsed = time.monotonic() - t0
                agent_run_duration_seconds.labels(agent=agent_name, mode="local").observe(elapsed)
                agent_runs_total.labels(agent=agent_name, mode="local", status="success").inc()
                self._save_agent_run(agent_name, "local", "success", elapsed, resource_tier)
                results.append(AgentResult(
                    agent_name=agent_name,
                    category=category,
                    files_generated=[f.path for f in result.files],
                    success=True,
                    findings_count=len(result.files),
                ))
                self._log_event(agent_name, "completed", f"Generated {len(result.files)} files")
                self._record_remediations(agent_name, result.files)
                if agent_name == "release":
                    self._create_default_slos()
            except Exception as exc:
                elapsed = time.monotonic() - t0
                agent_run_duration_seconds.labels(agent=agent_name, mode="local").observe(elapsed)
                agent_runs_total.labels(agent=agent_name, mode="local", status="error").inc()
                self._save_agent_run(agent_name, "local", "error", elapsed, resource_tier, error=str(exc))
                logger.warning("Agent %s failed: %s", agent_name, exc)
                results.append(AgentResult(
                    agent_name=agent_name,
                    category=category,
                    files_generated=[],
                    success=False,
                    error=str(exc),
                ))
                self._log_event(agent_name, "failed", str(exc))

        return results

    def _save_agent_run(
        self,
        agent_name: str,
        mode: str,
        status: str,
        elapsed_seconds: float,
        resource_tier: str,
        error: str | None = None,
    ) -> None:
        """Persist a structured `agent_runs` row for this execution (best-effort)."""
        if self._store is None:
            return
        try:
            self._store.save_agent_run(
                agent_name, mode, status,
                assessment_id=self._assessment_id,
                duration_ms=int(elapsed_seconds * 1000),
                resource_tier=resource_tier,
                error=error,
            )
        except Exception as exc:
            logger.warning("Failed to record agent_runs row for %s: %s", agent_name, exc)

    @staticmethod
    def _extract_result_json(log_output: str) -> str:
        """Extract JSON from between result markers, ignoring any other log noise."""
        begin = "--- AGENTIT_RESULT_BEGIN ---"
        end = "--- AGENTIT_RESULT_END ---"
        b = log_output.find(begin)
        e = log_output.find(end)
        if b != -1 and e != -1:
            return log_output[b + len(begin):e].strip()
        return log_output.strip()

    def _run_agents_as_jobs(
        self,
        plan: OrchestrationPlan,
        agent_map: dict[str, tuple[str, type]],
        *,
        skip_agents: set[str] | None = None,
    ) -> list[AgentResult]:
        """Run agents as K8s Jobs in parallel."""
        from agentit import kube
        from agentit.agents.capabilities import AGENT_CLASSES, RESOURCE_TIERS

        namespace = os.environ.get("AGENTIT_NAMESPACE", "agentit")
        image = os.environ.get("AGENTIT_IMAGE") or kube.get_current_pod_image() or "quay.io/amobrem/agentit:latest"

        # Serialize report to ConfigMap
        report_json = self.report.model_dump_json()
        cm_name = f"agentit-report-{self._assessment_id or 'manual'}"[:63]
        if not kube.create_config_map(cm_name, namespace, {"report.json": report_json}):
            logger.warning("Failed to create report ConfigMap, falling back to local mode")
            return self._run_agents_local(plan, agent_map, skip_agents=skip_agents)

        # Launch all Jobs
        job_names: dict[str, str] = {}
        job_started_at: dict[str, float] = {}
        job_tiers: dict[str, str] = {}
        for agent_name in plan.agents_to_run:
            if skip_agents and agent_name in skip_agents:
                logger.info("Skipping K8s job for agent '%s' — already covered by skills", agent_name)
                continue
            if agent_name not in agent_map:
                continue
            job_name = f"agentit-{agent_name}-{self._assessment_id or 'manual'}"[:63]
            command = [
                "python", "-m", "agentit", "run-agent", agent_name,
                "--report", "/input/report.json",
            ]
            tier_name = AGENT_CLASSES.get(agent_name, ("", "", "", "standard"))[3]
            tier = RESOURCE_TIERS.get(tier_name, RESOURCE_TIERS["standard"])
            if kube.create_job(
                job_name, namespace, image, command,
                config_map_name=cm_name,
                labels={"agentit/agent": agent_name, "agentit/managed-by": "orchestrator"},
                resources=tier,
            ):
                job_names[agent_name] = job_name
                job_started_at[agent_name] = time.monotonic()
                job_tiers[agent_name] = tier_name
                self._log_event(agent_name, "job-created", f"K8s Job {job_name} created")

        # Poll until all complete (timeout 5 min)
        results: list[AgentResult] = []
        deadline = time.monotonic() + 300
        pending = set(job_names.keys())

        while pending and time.monotonic() < deadline:
            for agent_name in list(pending):
                status = kube.get_job_status(job_names[agent_name], namespace)
                elapsed = time.monotonic() - job_started_at.get(agent_name, time.monotonic())
                tier = job_tiers.get(agent_name, "standard")
                if status == "succeeded":
                    pending.discard(agent_name)
                    log_output = kube.get_job_pod_log(job_names[agent_name], namespace)
                    category = agent_map[agent_name][0]
                    try:
                        from agentit.agents.base import GeneratedFile
                        result_json = self._extract_result_json(log_output)
                        files_data = json.loads(result_json)
                        files = [GeneratedFile(**f) for f in files_data]
                        agent_runs_total.labels(agent=agent_name, mode="kubernetes", status="success").inc()
                        self._save_agent_run(agent_name, "kubernetes", "success", elapsed, tier)
                        results.append(AgentResult(
                            agent_name=agent_name, category=category,
                            files_generated=[f.path for f in files],
                            success=True, findings_count=len(files),
                        ))
                        self._log_event(agent_name, "completed", f"Generated {len(files)} files (K8s Job)")
                        self._record_remediations(agent_name, files)
                        # Write files to output_dir for downstream consumption
                        sub_dir = self.output_dir / category
                        sub_dir.mkdir(parents=True, exist_ok=True)
                        for f in files:
                            safe = _safe_path(sub_dir, f.path)
                            safe.write_text(f.content, encoding="utf-8")
                    except Exception as exc:
                        logger.warning("Failed to parse Job output for %s: %s", agent_name, exc)
                        agent_runs_total.labels(agent=agent_name, mode="kubernetes", status="error").inc()
                        self._save_agent_run(agent_name, "kubernetes", "error", elapsed, tier, error=str(exc))
                        results.append(AgentResult(
                            agent_name=agent_name, category=category,
                            files_generated=[], success=False,
                            error=f"Failed to parse Job output: {exc}",
                        ))
                elif status == "failed":
                    pending.discard(agent_name)
                    category = agent_map[agent_name][0]
                    log_output = kube.get_job_pod_log(job_names[agent_name], namespace)
                    agent_runs_total.labels(agent=agent_name, mode="kubernetes", status="error").inc()
                    self._save_agent_run(agent_name, "kubernetes", "error", elapsed, tier,
                                         error=f"K8s Job failed: {log_output[:200]}")
                    results.append(AgentResult(
                        agent_name=agent_name, category=category,
                        files_generated=[], success=False,
                        error=f"K8s Job failed: {log_output[:200]}",
                    ))
                    self._log_event(agent_name, "failed", "K8s Job failed")
            if pending:
                time.sleep(5)

        # Handle timed-out agents
        for agent_name in pending:
            category = agent_map[agent_name][0]
            elapsed = time.monotonic() - job_started_at.get(agent_name, time.monotonic())
            agent_runs_total.labels(agent=agent_name, mode="kubernetes", status="timeout").inc()
            self._save_agent_run(
                agent_name, "kubernetes", "timeout", elapsed,
                job_tiers.get(agent_name, "standard"), error="K8s Job timed out",
            )
            results.append(AgentResult(
                agent_name=agent_name, category=category,
                files_generated=[], success=False, error="K8s Job timed out",
            ))
            self._log_event(agent_name, "timeout", "K8s Job timed out after 5 minutes")

        # Cleanup
        for job_name in job_names.values():
            kube.delete_job(job_name, namespace)
        kube.delete_config_map(cm_name, namespace)

        return results

    def _select_agents(self) -> list[str]:
        """Select which agents to run based on profile, filter, and assessment."""
        if self._agent_filter:
            return list(self._agent_filter)

        profile_agents = self.PROFILES.get(self._profile)
        if profile_agents is not None:
            agents = list(profile_agents)
        else:
            agents = ["security", "observability", "cicd", "compliance", "infrastructure", "release"]

        if self._profile in ("standard", "full") or profile_agents is None:
            if self.report.criticality in ("high", "critical"):
                agents.extend(["dependency", "incident", "cost"])
            if self.report.overall_score < 30:
                agents.append("retirement")
            if self.report.criticality in ("high", "critical") or self.report.overall_score < 50:
                agents.append("codechange")

        return agents

    def _determine_gates(self) -> list[str]:
        """Determine which human approval gates are needed."""
        gates = []

        critical_findings = sum(
            1 for s in self.report.scores
            for f in s.findings if f.severity == Severity.critical
        )

        if critical_findings > 0:
            gates.append("security-review")

        if self.report.criticality in ("high", "critical"):
            gates.append("deploy-approval")

        gates.append("final-approval")
        return gates

    def _can_auto_approve(self) -> bool:
        """Determine if this onboarding can be auto-approved."""
        if self.report.criticality in ("high", "critical"):
            return False

        critical = sum(
            1 for s in self.report.scores
            for f in s.findings if f.severity == Severity.critical
        )
        if critical > 0:
            return False

        if self.report.overall_score >= 70:
            return True

        return False

    def _manifest_kinds(self, result: AgentResult) -> dict[str, dict]:
        """Best-effort: parse `kind` -> manifest dict for every YAML file an agent generated."""
        kinds: dict[str, dict] = {}
        for fpath in result.files_generated:
            if not fpath.endswith((".yaml", ".yml")):
                continue
            full = self.output_dir / result.category / fpath
            try:
                for doc in yaml.safe_load_all(full.read_text(encoding="utf-8")):
                    if isinstance(doc, dict) and doc.get("kind"):
                        kinds[doc["kind"]] = doc
            except (OSError, yaml.YAMLError):
                continue
        return kinds

    def _priority_winner(self, a: str, b: str) -> tuple[str, str]:
        """Look up (winner, loser) for a pair from PRIORITY_MATRIX, defaulting to `a`."""
        winner = PRIORITY_MATRIX.get((a, b)) or PRIORITY_MATRIX.get((b, a)) or a
        loser = b if winner == a else a
        return winner, loser

    def _detect_conflicts(self, results: list[AgentResult]) -> list[dict]:
        """Detect REAL conflicts between agent outputs.

        Two agents both succeeding is normal, not a conflict -- a conflict
        must be an actual collision: either a known-conflicting resource
        kind pair (e.g. VPA in Auto mode vs. HPA) both being produced for
        this run, or two agents writing a file at the same output path.
        """
        conflicts: list[dict] = []

        failed = {r.agent_name: r for r in results if not r.success}
        succeeded = {r.agent_name: r for r in results if r.success}

        # Security blocker: if security agent failed, block everything
        if "security" in failed:
            conflicts.append({
                "type": "blocker",
                "agents": ["security"],
                "resolution": "Security agent failed -- all deployments blocked until resolved",
                "winner": "security",
            })

        flagged_pairs: set[tuple[str, str]] = set()

        # Known resource-kind conflicts (e.g. VPA vs HPA on the same workload)
        for (a, b), (kind_a, kind_b) in KNOWN_KIND_CONFLICTS.items():
            if a not in succeeded or b not in succeeded:
                continue
            manifests_a = self._manifest_kinds(succeeded[a])
            manifests_b = self._manifest_kinds(succeeded[b])
            if kind_a not in manifests_a or kind_b not in manifests_b:
                continue
            # A VPA in "Off" mode only issues recommendations -- it doesn't
            # actually fight the HPA for control, so it's not a real conflict.
            doc_a = manifests_a[kind_a]
            update_mode = doc_a.get("spec", {}).get("updatePolicy", {}).get("updateMode")
            if kind_a == "VerticalPodAutoscaler" and update_mode == "Off":
                continue
            winner, loser = self._priority_winner(a, b)
            conflicts.append({
                "type": "priority",
                "agents": [a, b],
                "resolution": (
                    f"{a} generated {kind_a} and {b} generated {kind_b} for the same "
                    f"workload -- {winner} output takes precedence over {loser}"
                ),
                "winner": winner,
            })
            flagged_pairs.add((a, b))

        # Generic collision: two agents writing a file at the exact same
        # output-relative path is a real conflict regardless of category.
        owner_of: dict[str, str] = {}
        for r in results:
            if not r.success:
                continue
            for fpath in r.files_generated:
                prior_owner = owner_of.get(fpath)
                if prior_owner is None:
                    owner_of[fpath] = r.agent_name
                    continue
                if prior_owner == r.agent_name:
                    continue
                pair = (prior_owner, r.agent_name) if prior_owner < r.agent_name else (r.agent_name, prior_owner)
                if pair in flagged_pairs:
                    continue
                winner, loser = self._priority_winner(prior_owner, r.agent_name)
                conflicts.append({
                    "type": "priority",
                    "agents": [prior_owner, r.agent_name],
                    "resolution": (
                        f"Both {prior_owner} and {r.agent_name} generated a file at '{fpath}' -- "
                        f"{winner} output takes precedence over {loser}"
                    ),
                    "winner": winner,
                })
                flagged_pairs.add(pair)

        return conflicts

    def _post_hardening_validation(self, results: list[AgentResult]) -> list[str]:
        """Validate generated files exist on disk and YAML parses as valid K8s manifests."""
        from agentit.agents.base import validate_manifest

        issues: list[str] = []
        for r in results:
            if not r.success:
                continue
            for fpath in r.files_generated:
                full = self.output_dir / r.category / fpath
                if not full.exists():
                    issues.append(f"{r.agent_name}: expected file {fpath} missing from disk")
                elif fpath.endswith((".yaml", ".yml")):
                    errors = validate_manifest(full.read_text())
                    if errors:
                        issues.append(f"{r.agent_name}/{fpath}: {'; '.join(errors)}")
        return issues

    def _generate_recommendation(
        self,
        plan: OrchestrationPlan,
        results: list[AgentResult],
        conflicts: list[dict],
    ) -> str:
        success_count = sum(1 for r in results if r.success)
        fail_count = sum(1 for r in results if not r.success)
        total_files = sum(len(r.files_generated) for r in results)

        blockers = [c for c in conflicts if c["type"] == "blocker"]
        warnings = [c for c in conflicts if c["type"] != "blocker"]
        if blockers:
            return f"BLOCKED: {len(blockers)} blocker(s) require resolution before proceeding."

        if fail_count > 0:
            return f"PARTIAL: {success_count}/{success_count + fail_count} agents succeeded, {total_files} files generated. Review failures before deploying."

        warn_suffix = f" ({len(warnings)} non-blocker conflict(s) — review before proceeding)" if warnings else ""

        if plan.auto_approve and not warnings:
            return f"AUTO-APPROVED: All {success_count} agents succeeded, {total_files} files generated. Safe for automated deployment."

        return f"READY FOR REVIEW: All {success_count} agents succeeded, {total_files} files generated. Awaiting human approval.{warn_suffix}"

    _SLO_DEFAULTS = {
        "critical": [
            ("availability", 99.99),
            ("error_rate", 0.01),
            ("latency_p99_ms", 100.0),
        ],
        "high": [
            ("availability", 99.9),
            ("error_rate", 0.05),
            ("latency_p99_ms", 200.0),
        ],
        "medium": [
            ("availability", 99.5),
            ("error_rate", 0.1),
            ("latency_p99_ms", 500.0),
        ],
        "low": [
            ("availability", 99.0),
            ("error_rate", 0.5),
            ("latency_p99_ms", 1000.0),
        ],
    }

    def _create_default_slos(self) -> None:
        """Create default SLOs based on app criticality after release agent runs."""
        if self._store is None or self._assessment_id is None:
            return
        slo_set = self._SLO_DEFAULTS.get(self.report.criticality, self._SLO_DEFAULTS["medium"])
        for metric, target in slo_set:
            try:
                self._store.save_slo(self._assessment_id, metric, target)
            except Exception as exc:
                logger.warning("Failed to create SLO %s: %s", metric, exc)
        self._log_event("release", "slos-created",
                        f"Created {len(slo_set)} default SLOs for {self.report.criticality} criticality")

    def _record_remediations(self, agent_name: str, files: list) -> None:
        """Record each generated file as a remediation in the store."""
        if self._store is None or self._assessment_id is None:
            return
        for f in files:
            try:
                self._store.save_remediation(
                    self._assessment_id,
                    agent_name,
                    f.description,
                )
            except Exception as exc:
                logger.warning("Failed to record remediation for %s/%s: %s",
                               agent_name, f.path, exc)

    def _log_event(self, agent_name: str, action: str, summary: str) -> None:
        self._events.append({
            "agent": agent_name,
            "action": action,
            "summary": summary,
        })
        if self._store is not None:
            self._store.log_event(
                agent_name,
                action,
                self.report.repo_name,
                "info",
                summary,
                correlation_id=self._assessment_id,
            )

    def _write_summary(
        self,
        plan: OrchestrationPlan,
        results: list[AgentResult],
        conflicts: list[dict],
        recommendation: str,
    ) -> None:
        lines = [
            f"# Orchestration Summary: {self.report.repo_name}",
            "",
            f"**Score:** {plan.score:.0f}/100",
            f"**Criticality:** {plan.criticality}",
            f"**Auto-approve:** {'Yes' if plan.auto_approve else 'No'}",
            f"**Recommendation:** {recommendation}",
            "",
            "## Agent Results",
            "",
        ]

        for r in results:
            status = "PASS" if r.success else "FAIL"
            lines.append(f"- [{status}] **{r.agent_name}**: {len(r.files_generated)} files")
            if r.error:
                lines.append(f"  - Error: {r.error}")
            for f in r.files_generated:
                lines.append(f"  - {f}")

        if conflicts:
            lines.extend(["", "## Conflicts", ""])
            for c in conflicts:
                lines.append(f"- **{c['type']}**: {c['resolution']} (winner: {c['winner']})")

        if plan.gates_required:
            lines.extend(["", "## Required Gates", ""])
            for g in plan.gates_required:
                lines.append(f"- [ ] {g}")

        lines.append("")
        summary_path = self.output_dir / "orchestration-summary.md"
        summary_path.write_text("\n".join(lines))
