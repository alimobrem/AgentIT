from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentit.models import AssessmentReport, Severity
from agentit.portal.metrics import agent_runs_total, agent_run_duration_seconds

logger = logging.getLogger(__name__)

AGENT_MODE = os.environ.get("AGENTIT_AGENT_MODE", "local")

# Priority matrix from the spec (Section 4)
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

    def __init__(
        self,
        report: AssessmentReport,
        output_dir: Path,
        store: object | None = None,
        assessment_id: str | None = None,
    ):
        self.report = report
        self.output_dir = Path(output_dir)
        self._store = store
        self._assessment_id = assessment_id
        self._events: list[dict] = []

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

        if AGENT_MODE == "kubernetes":
            results = self._run_agents_as_jobs(plan, agent_map)
        else:
            results = self._run_agents_local(plan, agent_map)

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
    ) -> list[AgentResult]:
        """Run agents in-process (default mode)."""
        results: list[AgentResult] = []

        for agent_name in plan.agents_to_run:
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

            t0 = time.monotonic()
            try:
                agent_instance = agent_cls(report=self.report, output_dir=sub_dir)
                result = agent_instance.run()
                elapsed = time.monotonic() - t0
                agent_run_duration_seconds.labels(agent=agent_name, mode="local").observe(elapsed)
                agent_runs_total.labels(agent=agent_name, mode="local", status="success").inc()
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
            return self._run_agents_local(plan, agent_map)

        # Launch all Jobs
        job_names: dict[str, str] = {}
        for agent_name in plan.agents_to_run:
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
                self._log_event(agent_name, "job-created", f"K8s Job {job_name} created")

        # Poll until all complete (timeout 5 min)
        results: list[AgentResult] = []
        deadline = time.monotonic() + 300
        pending = set(job_names.keys())

        while pending and time.monotonic() < deadline:
            for agent_name in list(pending):
                status = kube.get_job_status(job_names[agent_name], namespace)
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
                            (sub_dir / f.path).write_text(f.content, encoding="utf-8")
                    except Exception as exc:
                        logger.warning("Failed to parse Job output for %s: %s", agent_name, exc)
                        agent_runs_total.labels(agent=agent_name, mode="kubernetes", status="error").inc()
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
            agent_runs_total.labels(agent=agent_name, mode="kubernetes", status="timeout").inc()
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
        """Select which agents to run based on assessment findings."""
        agents = ["security", "observability", "cicd", "compliance", "infrastructure", "release"]

        # Always run these core 5, then add based on findings/criticality
        if self.report.criticality in ("high", "critical"):
            agents.extend(["dependency", "incident", "cost"])

        if self.report.overall_score < 30:
            agents.append("retirement")  # Consider if app is worth hardening

        # Code change agent runs for high/critical or when score is low
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

    def _detect_conflicts(self, results: list[AgentResult]) -> list[dict]:
        """Detect conflicts between agent outputs using the priority matrix."""
        conflicts: list[dict] = []

        failed = {r.agent_name: r for r in results if not r.success}
        succeeded = {r.agent_name for r in results if r.success}

        # Security blocker: if security agent failed, block everything
        if "security" in failed:
            conflicts.append({
                "type": "blocker",
                "agents": ["security"],
                "resolution": "Security agent failed -- all deployments blocked until resolved",
                "winner": "security",
            })

        # Apply priority matrix for overlapping successful agents
        for (a, b), winner in PRIORITY_MATRIX.items():
            if a in succeeded and b in succeeded:
                loser = b if winner == a else a
                conflicts.append({
                    "type": "priority",
                    "agents": [a, b],
                    "resolution": f"{winner} output takes precedence over {loser} for overlapping concerns",
                    "winner": winner,
                })

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
