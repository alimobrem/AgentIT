from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from agentit.models import AssessmentReport

logger = logging.getLogger(__name__)


def _safe_path(base: Path, relative: str) -> Path:
    """Resolve a relative path under base, rejecting traversal."""
    clean = PurePosixPath(relative).name  # strips directory components and ..
    return base / clean


_TARGET_PATHS_MANIFEST = "_target_paths.json"


def _write_target_path_manifest(sub_dir: Path, files: list) -> None:
    """Persist ``{path: target_path}`` for any generated file that carries a
    non-empty ``GeneratedFile.target_path`` (currently only ``CodeChangeAgent``
    output). ``AgentResult`` only threads plain relative paths through to
    ``portal/helpers.py::run_onboarding`` -- this sidecar file is how that
    per-file metadata survives the local-agent/K8s-Job -> portal boundary, so
    the unified delivery router (``portal/delivery.py``) can build a real PR
    patch against each file's actual destination in the app's own repo
    instead of a same-named copy under a new directory.
    """
    mapping = {f.path: f.target_path for f in files if getattr(f, "target_path", "")}
    if not mapping:
        return
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / _TARGET_PATHS_MANIFEST).write_text(json.dumps(mapping), encoding="utf-8")


_FILE_METADATA_MANIFEST = "_file_metadata.json"


def _write_file_metadata_manifest(sub_dir: Path, files: list) -> None:
    """Persist ``{path: {"description", "finding_addressed", "skill_name"}}``
    for every generated file -- the real, human-authored "why this file
    exists" ``GeneratedFile`` already carries (see ``agents/base.py``:
    every agent/skill sets a real ``description``, e.g. "VPA for {name}
    (updateMode: ...)" or a codechange's own ``finding.description``; skill
    output additionally sets ``finding_addressed``/``skill_name``).

    Sibling to ``_write_target_path_manifest()`` above: same problem
    (``AgentResult.files_generated`` only threads plain relative paths
    through to ``portal/helpers.py::run_onboarding``, so a ``GeneratedFile``'s
    own fields don't otherwise survive the local-agent/K8s-Job -> portal
    boundary), same fix (a JSON sidecar file next to each agent's/skills'
    generated output). Without this, Onboard Results' PR-intent framing
    would have no real per-file "why" to show and would have to either
    fabricate one or fall back to a bare filename -- this sidecar is what
    lets it show the real thing instead.
    """
    mapping = {
        f.path: {
            "description": f.description,
            "finding_addressed": getattr(f, "finding_addressed", "") or "",
            "skill_name": getattr(f, "skill_name", "") or "",
        }
        for f in files
    }
    if not mapping:
        return
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / _FILE_METADATA_MANIFEST).write_text(json.dumps(mapping), encoding="utf-8")

def _read_agent_mode() -> str:
    """AGENTIT_AGENT_MODE is the documented/canonical name; AGENT_MODE is
    kept as a fallback for anyone already relying on the
    previously-undocumented name."""
    return os.environ.get("AGENTIT_AGENT_MODE") or os.environ.get("AGENT_MODE", "local")


AGENT_MODE = _read_agent_mode()

# Priority matrix from the spec (Section 4). This only supplies the
# "winner" label used to describe how a *real* conflict (see
# KNOWN_KIND_CONFLICTS / path collisions in _detect_conflicts) should be
# resolved -- it is not itself a conflict trigger. Two agents both
# succeeding is normal and expected, not a conflict.
#
# Cost/dependency Python agents are gone — skills own those remediations
# (VPA/HPA/Renovate/etc.). The only remaining Python onboarding agent is
# `codechange` (optional source-repo patches), plus the aggregate
# `skills` AgentResult from Step 1. No cross-agent kind collision remains
# that needs a priority matrix entry today (HPA and VPA both come from
# skills under one AgentResult).
PRIORITY_MATRIX: dict[tuple[str, str], str] = {}

# Reserved for future kind-pair collisions across distinct AgentResults.
# Empty after cost/dependency agent removal (skills emit VPA+HPA together).
KNOWN_KIND_CONFLICTS: dict[tuple[str, str], tuple[str, str]] = {}


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


@dataclass
class OrchestrationResult:
    plan: OrchestrationPlan
    agent_results: list[AgentResult] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    recommendation: str = ""


class FleetOrchestrator:
    """Meta-agent that coordinates all other agents.

    Responsibilities:
    - Determine which agents to run based on assessment
    - Resolve conflicts between agent recommendations
    - Decide auto-approve vs. human gate based on risk
    - Track overall onboarding status

    ``store`` is an ``AssessmentStore`` -- every store call is ``await``ed. The
    3 surviving Python agents' ``.run()`` methods stay synchronous (per
    ``agents/base.py``'s existing contract) and are invoked via
    ``asyncio.to_thread`` at their one call site in ``_run_agents_local``;
    K8s Job dispatch (``kube.py``, sync ``kubernetes`` client) is likewise
    wrapped narrowly at each blocking call site in ``_run_agents_as_jobs``
    rather than the whole method running in a worker thread.
    """

    # security, observability, cicd, compliance, infrastructure, and release
    # are now skill-only domains (see docs/agent-removal-readiness.md) --
    # skills run unconditionally in Step 1 of run(), independent of this
    # profile list, so there's no Python agent left to plan for them here.
    PROFILES = {
        "lightweight": [],
        "standard": [],
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
        """Analyze the assessment and create an orchestration plan.

        Pure computation, no I/O -- stays synchronous."""
        agents = self._select_agents()

        return OrchestrationPlan(
            repo_url=self.report.repo_url,
            criticality=self.report.criticality,
            score=self.report.overall_score,
            agents_to_run=agents,
        )

    async def run(self) -> OrchestrationResult:
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
                    await self._store.register_agent(name, cat, capabilities=caps)
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
                # Blocking, synchronous kubernetes-client discovery calls --
                # narrowly wrapped in to_thread rather than the whole run()
                # fighting a sync K8s client.
                platform = await asyncio.to_thread(
                    discover_platform, os.environ.get("AGENTIT_NAMESPACE", "default"),
                )
                # discover_platform() degrades gracefully by design -- every
                # sub-probe (version, API groups, kinds, CRDs) catches its
                # own exceptions, so it never actually raises when the
                # cluster is unreachable or the caller's RBAC is
                # restricted; it just returns a context with whatever it
                # could see. `SkillEngine.generate()` gates every skill on
                # `platform.has_api(...)` for that skill's output kind(s)
                # -- if `available_kinds` is empty, that gate rejects
                # *every* skill, unconditionally, regardless of
                # `k8s_version`. `k8s_version` alone is not a reliable
                # signal that discovery "really connected": K8s/OpenShift
                # expose `/version` even to identities with no other RBAC
                # (e.g. a least-privilege ServiceAccount that legitimately
                # can't list API resources or CRDs cluster-wide), so
                # `k8s_version` can resolve fine while `available_kinds`
                # stays empty -- previously that combination bypassed this
                # fallback entirely and silently collapsed every skill's
                # output to zero files. `available_kinds` being empty is
                # by itself both necessary and sufficient for the has_api()
                # gate to reject everything, so that alone -- independent
                # of k8s_version -- is what must trigger the fallback:
                # skip the API-availability gate entirely (platform=None),
                # matching the removed agents' own platform-independent
                # behavior.
                if not platform.available_kinds:
                    logger.warning(
                        "Platform discovery found no available API kinds "
                        "(unreachable cluster or restricted RBAC) -- generating "
                        "skills without API-availability gating")
                    platform = None
            except Exception as exc:
                logger.warning("Platform discovery failed, falling back to offline context: %s", exc)
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

            # App name agentit (AppSet-excluded) is always self-managed —
            # Application agentit syncs Helm chart/ from AgentIT.git. Pass
            # that into SkillEngine so generation skips fleet-only kinds and
            # emits Helm-shaped chart patches (or nothing), not raw K8s dumps.
            from agentit.portal.delivery import is_appset_excluded_app
            self_managed = is_appset_excluded_app(self.report.repo_name)
            engine = SkillEngine(
                skills_dir, platform=platform, self_managed=self_managed,
            )
            # run_all() is a synchronous, potentially slow call (it may make
            # several sequential LLM requests, one per matched skill) --
            # narrowly wrapped in to_thread so it doesn't block the event
            # loop for however long that takes. It still needs to reach the
            # (genuinely async) store's rejection-count/human-override
            # lookups from inside that worker thread, so it's handed this
            # coroutine's own event loop to bridge those calls back onto
            # (see SkillEngine.run_all's docstring).
            skill_files = await asyncio.to_thread(
                engine.run_all, self.report, store=self._store, llm_client=llm_client,
                loop=asyncio.get_running_loop(),
            )
            skill_covered_domains = engine.covered_domains(skill_files)

            if skill_files:
                await self._log_event("skills", "completed",
                                f"Skills generated {len(skill_files)} files covering domains: "
                                f"{', '.join(sorted(skill_covered_domains)) or 'none'}")
                skill_dir = self.output_dir / "skills"
                skill_dir.mkdir(parents=True, exist_ok=True)
                for f in skill_files:
                    safe = _safe_path(skill_dir, f.path)
                    safe.write_text(f.content, encoding="utf-8")
                _write_file_metadata_manifest(skill_dir, skill_files)
        except Exception as exc:
            logger.debug("Skill engine failed (non-fatal): %s", exc)

        # --- Step 2: Skip Python agents whose domain skills already covered ---
        # Only `codechange` remains as a Python onboarding agent (source
        # patches, not K8s manifests). Skills own cost/dependency/etc.
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
            results = await self._run_agents_as_jobs(plan, agent_map, skip_agents=skip_agents)
        else:
            results = await self._run_agents_local(plan, agent_map, skip_agents=skip_agents)

        # --- Step 4: Add skill results to the result list ---
        if skill_files:
            results.append(AgentResult(
                agent_name="skills",
                category="skills",
                files_generated=[f.path for f in skill_files],
                success=True,
                findings_count=len(skill_files),
            ))

        # Default SLOs are based on the report's own criticality, not on
        # which mechanism (Python agent vs. skill) produced release-domain
        # manifests -- release is now a skill-only domain (see
        # docs/agent-removal-readiness.md), so this used to be tied to
        # ReleaseCoordinatorAgent's agent_name and is now unconditional.
        await self._create_default_slos()

        # Validate generated output
        validation_issues = self._post_hardening_validation(results)
        if validation_issues:
            for issue in validation_issues:
                logger.warning("Post-hardening validation: %s", issue)
            await self._log_event("orchestrator", "validation-issues",
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
        recommendation = self._generate_recommendation(results, conflicts)

        # Write orchestration summary
        self._write_summary(plan, results, conflicts, recommendation)

        return OrchestrationResult(
            plan=plan,
            agent_results=results,
            conflicts=conflicts,
            recommendation=recommendation,
        )

    async def _run_agents_local(
        self,
        plan: OrchestrationPlan,
        agent_map: dict[str, tuple[str, type]],
        *,
        skip_agents: set[str] | None = None,
    ) -> list[AgentResult]:
        """Run agents in-process (default mode)."""
        from agentit.agents.capabilities import AGENT_CLASSES
        # Lazily imported (not at module level) so importing this module
        # standalone -- the CLI's onboard/orchestrate commands -- never
        # transitively pulls in prometheus_client just to run an agent.
        from agentit.metrics import agent_runs_total, agent_run_duration_seconds

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
                # Agent.run() is synchronous per agents/base.py's existing
                # contract -- narrowly wrapped in to_thread right at this
                # one call site rather than making the Agent contract
                # itself async (a much broader ripple than this task needs).
                result = await asyncio.to_thread(agent_instance.run)
                elapsed = time.monotonic() - t0
                agent_run_duration_seconds.labels(agent=agent_name, mode="local").observe(elapsed)
                agent_runs_total.labels(agent=agent_name, mode="local", status="success").inc()
                await self._save_agent_run(agent_name, "local", "success", elapsed, resource_tier)
                _write_target_path_manifest(sub_dir, result.files)
                _write_file_metadata_manifest(sub_dir, result.files)
                results.append(AgentResult(
                    agent_name=agent_name,
                    category=category,
                    files_generated=[f.path for f in result.files],
                    success=True,
                    findings_count=len(result.files),
                ))
                await self._log_event(agent_name, "completed", f"Generated {len(result.files)} files")
            except Exception as exc:
                elapsed = time.monotonic() - t0
                agent_run_duration_seconds.labels(agent=agent_name, mode="local").observe(elapsed)
                agent_runs_total.labels(agent=agent_name, mode="local", status="error").inc()
                await self._save_agent_run(agent_name, "local", "error", elapsed, resource_tier, error=str(exc))
                logger.warning("Agent %s failed: %s", agent_name, exc)
                results.append(AgentResult(
                    agent_name=agent_name,
                    category=category,
                    files_generated=[],
                    success=False,
                    error=str(exc),
                ))
                await self._log_event(agent_name, "failed", str(exc))

        return results

    async def _save_agent_run(
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
            await self._store.save_agent_run(
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

    async def _run_agents_as_jobs(
        self,
        plan: OrchestrationPlan,
        agent_map: dict[str, tuple[str, type]],
        *,
        skip_agents: set[str] | None = None,
    ) -> list[AgentResult]:
        """Run agents as K8s Jobs in parallel.

        ``kube.py``'s ``kubernetes`` client calls are all synchronous --
        each one is wrapped individually in ``asyncio.to_thread`` right at
        its call site below, rather than dispatching this whole method (or
        the whole class) to a worker thread. The polling loop's
        ``time.sleep(5)`` becomes ``await asyncio.sleep(5)`` so it no longer
        blocks the event loop while waiting for Jobs to finish.
        """
        from agentit import kube
        from agentit.agents.capabilities import AGENT_CLASSES, RESOURCE_TIERS
        # Lazily imported (not at module level) so importing this module
        # standalone -- the CLI's onboard/orchestrate commands -- never
        # transitively pulls in prometheus_client just to run an agent.
        from agentit.metrics import agent_runs_total

        namespace = os.environ.get("AGENTIT_NAMESPACE", "agentit")
        pod_image = await asyncio.to_thread(kube.get_current_pod_image)
        image = os.environ.get("AGENTIT_IMAGE") or pod_image or "quay.io/amobrem/agentit:latest"

        # Serialize report to ConfigMap
        report_json = self.report.model_dump_json()
        cm_name = f"agentit-report-{self._assessment_id or 'manual'}"[:63]
        cm_created = await asyncio.to_thread(
            kube.create_config_map, cm_name, namespace, {"report.json": report_json},
        )
        if not cm_created:
            logger.warning("Failed to create report ConfigMap, falling back to local mode")
            return await self._run_agents_local(plan, agent_map, skip_agents=skip_agents)

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
            job_created = await asyncio.to_thread(
                kube.create_job,
                job_name, namespace, image, command,
                config_map_name=cm_name,
                labels={"agentit/agent": agent_name, "agentit/managed-by": "orchestrator"},
                resources=tier,
            )
            if job_created:
                job_names[agent_name] = job_name
                job_started_at[agent_name] = time.monotonic()
                job_tiers[agent_name] = tier_name
                await self._log_event(agent_name, "job-created", f"K8s Job {job_name} created")

        # Poll until all complete (timeout 5 min)
        results: list[AgentResult] = []
        deadline = time.monotonic() + 300
        pending = set(job_names.keys())

        while pending and time.monotonic() < deadline:
            for agent_name in list(pending):
                status = await asyncio.to_thread(kube.get_job_status, job_names[agent_name], namespace)
                elapsed = time.monotonic() - job_started_at.get(agent_name, time.monotonic())
                tier = job_tiers.get(agent_name, "standard")
                if status == "succeeded":
                    pending.discard(agent_name)
                    log_output = await asyncio.to_thread(kube.get_job_pod_log, job_names[agent_name], namespace)
                    category = agent_map[agent_name][0]
                    try:
                        from agentit.agents.base import GeneratedFile
                        result_json = self._extract_result_json(log_output)
                        files_data = json.loads(result_json)
                        files = [GeneratedFile(**f) for f in files_data]
                        agent_runs_total.labels(agent=agent_name, mode="kubernetes", status="success").inc()
                        await self._save_agent_run(agent_name, "kubernetes", "success", elapsed, tier)
                        results.append(AgentResult(
                            agent_name=agent_name, category=category,
                            files_generated=[f.path for f in files],
                            success=True, findings_count=len(files),
                        ))
                        await self._log_event(agent_name, "completed", f"Generated {len(files)} files (K8s Job)")
                        # Write files to output_dir for downstream consumption
                        sub_dir = self.output_dir / category
                        sub_dir.mkdir(parents=True, exist_ok=True)
                        for f in files:
                            safe = _safe_path(sub_dir, f.path)
                            safe.write_text(f.content, encoding="utf-8")
                        _write_target_path_manifest(sub_dir, files)
                        _write_file_metadata_manifest(sub_dir, files)
                    except Exception as exc:
                        logger.warning("Failed to parse Job output for %s: %s", agent_name, exc)
                        agent_runs_total.labels(agent=agent_name, mode="kubernetes", status="error").inc()
                        await self._save_agent_run(agent_name, "kubernetes", "error", elapsed, tier, error=str(exc))
                        results.append(AgentResult(
                            agent_name=agent_name, category=category,
                            files_generated=[], success=False,
                            error=f"Failed to parse Job output: {exc}",
                        ))
                elif status == "failed":
                    pending.discard(agent_name)
                    category = agent_map[agent_name][0]
                    log_output = await asyncio.to_thread(kube.get_job_pod_log, job_names[agent_name], namespace)
                    agent_runs_total.labels(agent=agent_name, mode="kubernetes", status="error").inc()
                    await self._save_agent_run(agent_name, "kubernetes", "error", elapsed, tier,
                                         error=f"K8s Job failed: {log_output[:200]}")
                    results.append(AgentResult(
                        agent_name=agent_name, category=category,
                        files_generated=[], success=False,
                        error=f"K8s Job failed: {log_output[:200]}",
                    ))
                    await self._log_event(agent_name, "failed", "K8s Job failed")
            if pending:
                await asyncio.sleep(5)

        # Handle timed-out agents
        for agent_name in pending:
            category = agent_map[agent_name][0]
            elapsed = time.monotonic() - job_started_at.get(agent_name, time.monotonic())
            agent_runs_total.labels(agent=agent_name, mode="kubernetes", status="timeout").inc()
            await self._save_agent_run(
                agent_name, "kubernetes", "timeout", elapsed,
                job_tiers.get(agent_name, "standard"), error="K8s Job timed out",
            )
            results.append(AgentResult(
                agent_name=agent_name, category=category,
                files_generated=[], success=False, error="K8s Job timed out",
            ))
            await self._log_event(agent_name, "timeout", "K8s Job timed out after 5 minutes")

        # Cleanup
        for job_name in job_names.values():
            await asyncio.to_thread(kube.delete_job, job_name, namespace)
        await asyncio.to_thread(kube.delete_config_map, cm_name, namespace)

        return results

    def _select_agents(self) -> list[str]:
        """Select which agents to run based on profile, filter, and assessment."""
        if self._agent_filter:
            return list(self._agent_filter)

        profile_agents = self.PROFILES.get(self._profile)
        if profile_agents is not None:
            agents = list(profile_agents)
        else:
            agents = []

        # Skills own cluster remediations. Optionally run codechange for
        # source-repo patches when the app is high-criticality or low-score.
        if self._profile in ("standard", "full") or profile_agents is None:
            if self.report.criticality in ("high", "critical") or self.report.overall_score < 50:
                agents.append("codechange")

        return agents

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

        This used to also check for a failed ``"security"`` agent result
        and append a ``"type": "blocker"`` conflict for it, but no
        ``AgentResult`` with ``agent_name="security"`` has been
        structurally possible since the security/observability/cicd/
        compliance/infrastructure/incident/release/retirement/chaos Python
        agents were removed once skills covered their domains (see
        ``AGENT_CLASSES`` in ``agents/capabilities.py`` -- only
        ``codechange`` remains, plus a synthetic ``"skills"`` result).
        Removed 2026-07-20 as dead code, along with the now-equally-dead
        ``"BLOCKED: ..."`` branch of ``_generate_recommendation()`` below,
        since this was the only producer of a ``"type": "blocker"``
        conflict anywhere.
        """
        conflicts: list[dict] = []

        succeeded = {r.agent_name: r for r in results if r.success}

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
        results: list[AgentResult],
        conflicts: list[dict],
    ) -> str:
        success_count = sum(1 for r in results if r.success)
        fail_count = sum(1 for r in results if not r.success)
        total_files = sum(len(r.files_generated) for r in results)

        # A "BLOCKED: ..." branch used to fire on a "type": "blocker"
        # conflict here -- removed 2026-07-20 as dead code alongside
        # _detect_conflicts()'s own removed producer of that conflict type
        # (see its docstring); every conflict `_detect_conflicts()` can
        # produce today is a "priority"/"validation" warning, never a
        # blocker, so `warnings` below is simply every conflict.
        warnings = conflicts

        skill_ok = any(r.agent_name == "skills" and r.success for r in results)
        codechange = next((r for r in results if r.agent_name == "codechange"), None)

        if fail_count > 0:
            failed = ", ".join(r.agent_name for r in results if not r.success)
            return (
                f"GENERATION INCOMPLETE: {total_files} file(s) produced; "
                f"failed: {failed}. Review before Scan delivery."
            )

        # Skills-primary summary — codechange is an optional source-patch
        # path, not a peer "domain agent" in a 3-agent fleet.
        parts = [f"{total_files} remediation file(s)"]
        if skill_ok:
            skill_n = next(len(r.files_generated) for r in results if r.agent_name == "skills")
            parts.append(f"skills generated {skill_n}")
        if codechange and codechange.success and codechange.files_generated:
            parts.append(f"codechange proposed {len(codechange.files_generated)} source patch(es)")
        warn_suffix = f" ({len(warnings)} conflict(s) — review before proceeding)" if warnings else ""
        return f"READY FOR REVIEW: {'; '.join(parts)}. Scan opens PRs; merge on GitHub.{warn_suffix}"

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

    async def _create_default_slos(self) -> None:
        """Create default SLOs based on app criticality after release agent runs.

        Skips any metric that already has an SLO for this assessment --
        `run()` calls this unconditionally every time, so a re-onboard
        (re-assess + onboard again) re-ran this with no uniqueness check
        and inserted a second full set of the same 3 default metrics on
        top of the existing ones. Confirmed live: apps onboarded more than
        once ended up with 6 SLO rows instead of 3 (each metric listed
        twice with identical targets), inflating the Fleet-Wide SLOs
        page's "Total SLOs" stat. `save_slo()` itself stays a plain
        insert -- manually-added SLOs (the Add SLO form) may legitimately
        track more than one threshold for the same metric_name.
        """
        if self._store is None or self._assessment_id is None:
            return
        slo_set = self._SLO_DEFAULTS.get(self.report.criticality, self._SLO_DEFAULTS["medium"])
        try:
            existing_metrics = {s["metric_name"] for s in await self._store.list_slos(self._assessment_id)}
        except Exception as exc:
            logger.warning("Failed to list existing SLOs before seeding defaults: %s", exc)
            existing_metrics = set()
        created = 0
        for metric, target in slo_set:
            if metric in existing_metrics:
                continue
            try:
                await self._store.save_slo(self._assessment_id, metric, target)
                created += 1
            except Exception as exc:
                logger.warning("Failed to create SLO %s: %s", metric, exc)
        if created:
            # "release" was never a real agent/category (skill-only now,
            # like security/observability/cicd/compliance/infrastructure --
            # see docs/agent-removal-readiness.md); "orchestrator" is the
            # same non-agent-specific agent_id this class already uses for
            # its own system events (see the "validation-issues" call
            # above), so this event is actually findable when filtering
            # the Events page by agent.
            await self._log_event("orchestrator", "slos-created",
                            f"Created {created} default SLO(s) for {self.report.criticality} criticality")

    async def _log_event(self, agent_name: str, action: str, summary: str) -> None:
        self._events.append({
            "agent": agent_name,
            "action": action,
            "summary": summary,
        })
        if self._store is not None:
            await self._store.log_event(
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

        lines.append("")
        summary_path = self.output_dir / "orchestration-summary.md"
        summary_path.write_text("\n".join(lines))
