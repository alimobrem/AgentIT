from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from agentit.models import AssessmentReport, Severity

logger = logging.getLogger(__name__)

# Priority matrix from the spec (Section 4)
PRIORITY_MATRIX = {
    ("security", "cicd"): "security",
    ("security", "observability"): "security",
    ("security", "compliance"): "security",
    ("compliance", "cicd"): "compliance",
    ("compliance", "observability"): "compliance",
    ("cicd", "release"): "release",
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

        # Import agents
        from agentit.agents.hardening import HardeningAgent
        from agentit.agents.observability import ObservabilityAgent
        from agentit.agents.cicd import CICDAgent
        from agentit.agents.compliance import ComplianceAgent

        agent_map: dict[str, tuple[str, type]] = {
            "security": ("security", HardeningAgent),
            "observability": ("observability", ObservabilityAgent),
            "cicd": ("cicd", CICDAgent),
            "compliance": ("compliance", ComplianceAgent),
        }

        # Import optional agents — log failures instead of silently swallowing
        try:
            from agentit.agents.dependency import DependencyAgent
            agent_map["dependency"] = ("dependency", DependencyAgent)
        except ImportError:
            logger.warning("Failed to import DependencyAgent — agent will be skipped")
        try:
            from agentit.agents.incident import IncidentAgent
            agent_map["incident"] = ("incident", IncidentAgent)
        except ImportError:
            logger.warning("Failed to import IncidentAgent — agent will be skipped")
        try:
            from agentit.agents.cost import CostOptimizationAgent
            agent_map["cost"] = ("cost", CostOptimizationAgent)
        except ImportError:
            logger.warning("Failed to import CostOptimizationAgent — agent will be skipped")
        try:
            from agentit.agents.chaos import ChaosAgent
            agent_map["chaos"] = ("chaos", ChaosAgent)
        except ImportError:
            logger.warning("Failed to import ChaosAgent — agent will be skipped")
        try:
            from agentit.agents.retirement import RetirementAgent
            agent_map["retirement"] = ("retirement", RetirementAgent)
        except ImportError:
            logger.warning("Failed to import RetirementAgent — agent will be skipped")
        try:
            from agentit.agents.release import ReleaseCoordinatorAgent
            agent_map["release"] = ("release", ReleaseCoordinatorAgent)
        except ImportError:
            logger.warning("Failed to import ReleaseCoordinatorAgent — agent will be skipped")

        # Register all available agents in the store
        if self._store is not None:
            for name, (cat, _cls) in agent_map.items():
                try:
                    self._store.register_agent(name, cat)
                except Exception as exc:
                    logger.warning("Failed to register agent '%s': %s", name, exc)

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

            try:
                agent_instance = agent_cls(report=self.report, output_dir=sub_dir)
                result = agent_instance.run()
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
                logger.warning("Agent %s failed: %s", agent_name, exc)
                results.append(AgentResult(
                    agent_name=agent_name,
                    category=category,
                    files_generated=[],
                    success=False,
                    error=str(exc),
                ))
                self._log_event(agent_name, "failed", str(exc))

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

    def _select_agents(self) -> list[str]:
        """Select which agents to run based on assessment findings."""
        agents = ["security", "observability", "cicd", "compliance", "release"]

        # Always run these core 5, then add based on findings/criticality
        if self.report.criticality in ("high", "critical"):
            agents.extend(["dependency", "incident", "cost"])

        if self.report.criticality != "critical":
            agents.append("chaos")

        if self.report.overall_score < 30:
            agents.append("retirement")  # Consider if app is worth hardening

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

        if conflicts:
            return f"BLOCKED: {len(conflicts)} conflict(s) require resolution before proceeding."

        if fail_count > 0:
            return f"PARTIAL: {success_count}/{success_count + fail_count} agents succeeded, {total_files} files generated. Review failures before deploying."

        if plan.auto_approve:
            return f"AUTO-APPROVED: All {success_count} agents succeeded, {total_files} files generated. Safe for automated deployment."

        return f"READY FOR REVIEW: All {success_count} agents succeeded, {total_files} files generated. Awaiting human approval."

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
