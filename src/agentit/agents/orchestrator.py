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

    def __init__(self, report: AssessmentReport, output_dir: Path, store: object | None = None):
        self.report = report
        self.output_dir = Path(output_dir)
        self._store = store
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

        # Try importing optional agents
        try:
            from agentit.agents.dependency import DependencyAgent
            agent_map["dependency"] = ("dependency", DependencyAgent)
        except ImportError:
            pass
        try:
            from agentit.agents.incident import IncidentAgent
            agent_map["incident"] = ("incident", IncidentAgent)
        except ImportError:
            pass
        try:
            from agentit.agents.cost import CostAgent
            agent_map["cost"] = ("cost", CostAgent)
        except ImportError:
            pass
        try:
            from agentit.agents.chaos import ChaosAgent
            agent_map["chaos"] = ("chaos", ChaosAgent)
        except ImportError:
            pass
        try:
            from agentit.agents.retirement import RetirementAgent
            agent_map["retirement"] = ("retirement", RetirementAgent)
        except ImportError:
            pass

        results: list[AgentResult] = []

        for agent_name in plan.agents_to_run:
            if agent_name not in agent_map:
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

        # Resolve conflicts
        conflicts = self._detect_conflicts(results)

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
        agents = ["security", "observability", "cicd", "compliance"]

        # Always run these core 4, then add based on findings/criticality
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
        """Detect conflicts between agent outputs."""
        conflicts: list[dict] = []

        failed = {r.agent_name: r for r in results if not r.success}

        # Security blocker: if security agent failed, block everything
        if "security" in failed:
            conflicts.append({
                "type": "blocker",
                "agents": ["security"],
                "resolution": "Security agent failed -- all deployments blocked until resolved",
                "winner": "security",
            })

        return conflicts

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
