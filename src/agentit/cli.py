from __future__ import annotations

import asyncio
import contextlib
import functools
import shutil
import sys
from collections.abc import Callable, Coroutine, Generator
from pathlib import Path

import click

from agentit.cloner import CloneError, clone_repo
from agentit.models import AssessmentReport
from agentit.portal.store_factory import create_store
from agentit.reporter import render_json_report, render_terminal_report
from agentit.runner import run_assessment


def _run_async(coro_func: Callable[..., Coroutine]) -> Callable[..., None]:
    """Let a Click command's callback be ``async def`` while Click itself
    (which has no native async support) still calls it as a plain function.

    Smallest, safest option per docs/postgres-migration-plan.md §9 Phase 3
    (the alternative -- ``anyio``/``asyncclick`` -- would add a new
    dependency for no behavior difference here). Must be the innermost
    decorator, applied directly to the ``async def`` function, so that
    ``@click.option``/``@main.command`` see a normal sync callable.
    """
    @functools.wraps(coro_func)
    def wrapper(*args: object, **kwargs: object) -> None:
        return asyncio.run(coro_func(*args, **kwargs))
    return wrapper


@contextlib.contextmanager
def _resolve_and_assess(
    repo_url: str,
    criticality: str,
    use_llm: bool | None = None,
    llm_model: str | None = None,
) -> Generator[AssessmentReport]:
    clone_dir: Path | None = None
    try:
        if Path(repo_url).is_dir():
            repo_path = Path(repo_url)
        else:
            click.echo(f"Cloning {repo_url}...", err=True)
            repo_path = clone_repo(repo_url)
            clone_dir = repo_path

        llm_client = None
        if use_llm is None:
            import os
            use_llm = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"))
        if use_llm:
            try:
                from agentit.llm import LLMClient
                llm_client = LLMClient(model=llm_model)
            except Exception as exc:
                click.echo(f"LLM init failed (continuing without): {exc}", err=True)

        click.echo("Running assessment...", err=True)
        report = run_assessment(repo_path, repo_url=repo_url, criticality=criticality, llm_client=llm_client)
        yield report
    finally:
        if clone_dir and clone_dir.exists():
            shutil.rmtree(clone_dir, ignore_errors=True)


@click.group()
@click.version_option(version="0.1.0", prog_name="agentit")
def main() -> None:
    """AgentIT -- Enterprise Readiness Assessor.

    \b
    User commands:
      assess       Score a repo across 7 enterprise dimensions
      onboard      Assess + generate hardening manifests
      watch        Continuously re-assess on a schedule
      portal       Launch the web UI
      self-assess  AgentIT assesses itself

    \b
    Internal/daemon commands:
      run-agent      Run a single agent (used by K8s Jobs)
      consume        Start Kafka event consumer
      vuln-watch     Start vulnerability watcher
      slo-track      Start SLO tracker
      drift-detect   Start drift detector
      learn-watch    Start skill learner (periodic CVE research)
      orchestrate    Run full orchestration (low-level)
    """
    from agentit.logging_config import configure_logging
    configure_logging()


async def _rescan_fleet(
    dimension: str | None,
    use_llm: bool | None = None,
    llm_model: str | None = None,
) -> list[dict]:
    """Re-assess every currently-tracked fleet app once and persist the results.

    Used by ``watch --rescan`` / ``assess --rescan``, which are invoked from
    K8s CronJobs that already control periodicity via their own schedule --
    this performs exactly one pass over the fleet (tracked via the shared
    store) rather than looping. If ``dimension`` is given, only findings for
    that dimension (e.g. ``compliance``, ``security``, ``ha_dr``) are counted
    in the per-app summary line.
    """
    store = await create_store()
    fleet = await store.get_fleet_data()
    if not fleet:
        click.echo("[rescan] No tracked apps in the fleet -- nothing to do.", err=True)
        return []

    click.echo(f"[rescan] Re-assessing {len(fleet)} fleet app(s)"
               + (f" (dimension={dimension})" if dimension else "") + "...", err=True)

    results: list[dict] = []
    for app in fleet:
        repo_url = app["repo_url"]
        try:
            with _resolve_and_assess(repo_url, app["criticality"], use_llm, llm_model) as report:
                await store.save(report)
                findings = [
                    f for s in report.scores for f in s.findings
                    if dimension is None or s.dimension == dimension
                ]
                delta = report.overall_score - (app["latest_score"] or report.overall_score)
                click.echo(
                    f"[rescan] {report.repo_name}: {report.overall_score:.0f}/100 "
                    f"({'+' if delta >= 0 else ''}{delta:.0f}) {len(findings)} finding(s)",
                    err=True,
                )
                results.append({
                    "repo_url": repo_url,
                    "repo_name": report.repo_name,
                    "score": report.overall_score,
                    "delta": delta,
                    "findings_count": len(findings),
                })
        except CloneError as exc:
            click.echo(f"[rescan] {repo_url}: error -- {exc}", err=True)

    click.echo(f"[rescan] Done. Re-assessed {len(results)}/{len(fleet)} app(s).", err=True)
    return results


@main.command()
@click.argument("repo_url", required=False, default=None)
@click.option("--criticality", type=click.Choice(["low", "medium", "high", "critical"]), default="medium")
@click.option("--format", "output_format", type=click.Choice(["json", "terminal"]), default="json")
@click.option("--output", "output_file", type=click.Path(), default=None)
@click.option("--llm/--no-llm", "use_llm", default=None, help="Enable/disable Claude LLM (auto-detects credentials if omitted).")
@click.option("--llm-model", default=None, help="Claude model to use (default: env AGENTIT_LLM_MODEL).")
@click.option("--rescan", is_flag=True, default=False,
              help="Re-assess every currently-tracked fleet app once and exit, instead of assessing a single REPO_URL.")
@click.option("--dimension", default=None,
              help="With --rescan, only count/report findings for this dimension (e.g. compliance, security, ha_dr).")
def assess(
    repo_url: str | None, criticality: str, output_format: str, output_file: str | None,
    use_llm: bool, llm_model: str | None, rescan: bool, dimension: str | None,
) -> None:
    """Assess enterprise readiness of a Git repository."""
    if rescan:
        asyncio.run(_rescan_fleet(dimension, use_llm, llm_model))
        return
    if not repo_url:
        click.echo("Error: REPO_URL is required unless --rescan is set.", err=True)
        sys.exit(1)
    try:
        with _resolve_and_assess(repo_url, criticality, use_llm, llm_model) as report:
            output = render_json_report(report) if output_format == "json" else render_terminal_report(report)
            if output_file:
                Path(output_file).write_text(output, encoding="utf-8")
                click.echo(f"Report written to {output_file}", err=True)
            elif output_format == "json":
                # Delimit the payload so warning/info logs merged onto the same
                # stream (e.g. by CliRunner, or `2>&1`) can't corrupt JSON parsing.
                click.echo("--- AGENTIT_RESULT_BEGIN ---")
                click.echo(output)
                click.echo("--- AGENTIT_RESULT_END ---")
            else:
                click.echo(output)
    except CloneError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command()
@click.option("--host", default="0.0.0.0")
@click.option("--port", default=8080, type=int)
def portal(host: str, port: int) -> None:
    """Launch the AgentIT portal web UI."""
    import uvicorn

    from agentit.portal.app import app

    uvicorn.run(app, host=host, port=port)


@main.command()
@click.argument("repo_url", required=False, default=None)
@click.option("--interval", default=3600, type=int, help="Re-assessment interval in seconds (default: 1 hour).")
@click.option("--criticality", type=click.Choice(["low", "medium", "high", "critical"]), default="medium")
@click.option("--llm", "use_llm", is_flag=True, default=None)
@click.option("--llm-model", default=None)
@click.option("--webhook", default=None, help="Webhook URL to POST results to.")
@click.option("--rescan", is_flag=True, default=False,
              help="Re-assess every currently-tracked fleet app once and exit, instead of continuously watching a single REPO_URL.")
@click.option("--dimension", default=None,
              help="With --rescan, only count/report findings for this dimension (e.g. compliance, security, ha_dr).")
def watch(
    repo_url: str | None, interval: int, criticality: str, use_llm: bool | None, llm_model: str | None,
    webhook: str | None, rescan: bool, dimension: str | None,
) -> None:
    """Continuously re-assess a repository on a schedule, or with --rescan, re-assess the whole tracked fleet once."""
    import time
    import json
    import httpx

    if rescan:
        asyncio.run(_rescan_fleet(dimension, use_llm, llm_model))
        return
    if not repo_url:
        click.echo("Error: REPO_URL is required unless --rescan is set.", err=True)
        sys.exit(1)

    click.echo(f"Watching {repo_url} every {interval}s...", err=True)
    previous_score: float | None = None

    while True:
        try:
            with _resolve_and_assess(repo_url, criticality, use_llm, llm_model) as report:
                current_score = report.overall_score
                delta = ""
                if previous_score is not None:
                    diff = current_score - previous_score
                    delta = f" ({'+' if diff >= 0 else ''}{diff:.0f})"

                click.echo(
                    f"[{report.assessed_at.strftime('%Y-%m-%d %H:%M')}] "
                    f"{report.repo_name}: {current_score:.0f}/100{delta} "
                    f"({sum(len(s.findings) for s in report.scores)} findings)",
                    err=True,
                )

                if webhook:
                    try:
                        httpx.post(webhook, json={
                            "repo_url": repo_url,
                            "score": current_score,
                            "delta": current_score - (previous_score or current_score),
                            "findings_count": sum(len(s.findings) for s in report.scores),
                        }, timeout=10)
                    except Exception as exc:
                        click.echo(f"Webhook POST failed: {exc}", err=True)

                previous_score = current_score

        except CloneError as exc:
            click.echo(f"Error: {exc}", err=True)
        except KeyboardInterrupt:
            click.echo("Stopped.", err=True)
            break

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("Stopped.", err=True)
            break


@main.command()
@click.argument("repo_url")
@click.option("--output-dir", default="./orchestration-output", type=click.Path())
@click.option("--criticality", type=click.Choice(["low", "medium", "high", "critical"]), default="medium")
@click.option("--llm", "use_llm", is_flag=True, default=None, help="Enable Claude LLM (auto-detects credentials if omitted).")
@click.option("--llm-model", default=None, help="Claude model to use.")
@_run_async
async def orchestrate(repo_url: str, output_dir: str, criticality: str, use_llm: bool, llm_model: str | None) -> None:
    """Run full orchestrated onboarding with Fleet Orchestrator."""
    from agentit.agents.orchestrator import FleetOrchestrator

    try:
        with _resolve_and_assess(repo_url, criticality, use_llm, llm_model) as report:
            click.echo("Running Fleet Orchestrator...", err=True)
            orch = FleetOrchestrator(report=report, output_dir=Path(output_dir))
            result = await orch.run()

            # Print plan
            plan = result.plan
            click.echo(f"\n=== Orchestration Plan ===", err=True)
            click.echo(f"Agents: {', '.join(plan.agents_to_run)}", err=True)
            click.echo(f"Auto-approve: {plan.auto_approve}", err=True)

            # Print results
            click.echo(f"\n=== Agent Results ===", err=True)
            for ar in result.agent_results:
                status = "PASS" if ar.success else "FAIL"
                click.echo(f"  [{status}] {ar.agent_name}: {len(ar.files_generated)} files", err=True)
                if ar.error:
                    click.echo(f"         error: {ar.error}", err=True)

            # Print conflicts
            if result.conflicts:
                click.echo(f"\n=== Conflicts ===", err=True)
                for c in result.conflicts:
                    click.echo(f"  {c['type']}: {c['resolution']}", err=True)

            # Print recommendation and gates
            click.echo(f"\n=== Recommendation ===", err=True)
            click.echo(f"  {result.recommendation}", err=True)
            click.echo(f"\n=== Gates ===", err=True)
            for g in result.gates_created:
                click.echo(f"  [ ] {g}", err=True)
    except CloneError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command()
@click.argument("repo_url")
@click.option("--output-dir", default="./onboarding-output", type=click.Path())
@click.option("--criticality", type=click.Choice(["low", "medium", "high", "critical"]), default="medium")
@click.option("--llm", "use_llm", is_flag=True, default=None, help="Enable Claude LLM (auto-detects credentials if omitted).")
@click.option("--llm-model", default=None, help="Claude model to use.")
@click.option("--profile", type=click.Choice(["lightweight", "standard", "full"]), default="standard",
              help="Agent profile: lightweight (security+cicd), standard (core 6), full (all agents).")
@click.option("--agents", default=None, help="Comma-separated agent list (overrides --profile).")
@_run_async
async def onboard(repo_url: str, output_dir: str, criticality: str, use_llm: bool, llm_model: str | None,
                   profile: str, agents: str | None) -> None:
    """Run enterprise onboarding: assess + generate hardening manifests."""
    import json

    out = Path(output_dir)

    from agentit.agents.orchestrator import FleetOrchestrator

    try:
        with _resolve_and_assess(repo_url, criticality, use_llm, llm_model) as report:
            # Write assessment report
            out.mkdir(parents=True, exist_ok=True)
            assessment_path = out / "assessment.json"
            assessment_path.write_text(render_json_report(report), encoding="utf-8")

            # Run full orchestration
            agent_filter = agents.split(",") if agents else None
            click.echo(f"Running Fleet Orchestrator (profile={profile})...", err=True)
            orch = FleetOrchestrator(report=report, output_dir=out,
                                    profile=profile, agent_filter=agent_filter)
            result = await orch.run()

            # Summary
            click.echo(f"\nAssessment score: {report.overall_score:.1f}", err=True)
            click.echo(f"Assessment report: {assessment_path}", err=True)
            for ar in result.agent_results:
                status = "PASS" if ar.success else "FAIL"
                click.echo(f"\n[{status}] {ar.agent_name}", err=True)
                for f in ar.files_generated:
                    click.echo(f"  {out / ar.category / f}", err=True)
                if ar.error:
                    click.echo(f"  error: {ar.error}", err=True)

            click.echo(f"\n{result.recommendation}", err=True)
    except CloneError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command()
@click.option("--topics", default="agentit-events", help="Comma-separated Kafka topics.")
@click.option("--group-id", default="agentit-consumers", help="Kafka consumer group ID.")
@_run_async
async def consume(topics: str, group_id: str) -> None:
    """Start a blocking Kafka consumer that dispatches events to watchers."""
    from agentit.consumer import EventConsumer
    from agentit.events import get_publisher

    topic_list = [t.strip() for t in topics.split(",") if t.strip()]
    store = await create_store()
    # EventConsumer accepts the async store directly -- its constructor
    # captures the current running loop (this coroutine's) so its
    # dead-letter persistence can schedule store writes back onto it via
    # `run_coroutine_threadsafe`, safe even for an asyncpg-backed store.
    # `consume()` itself is a genuinely synchronous, blocking Kafka loop
    # (kafka-python has no async client), so it's dispatched to a worker
    # thread below -- narrow `to_thread` at this one blocking call site,
    # not a bridge on the store.
    consumer = EventConsumer(topics=topic_list, group_id=group_id, store=store)

    if not consumer.connected:
        click.echo("Kafka unavailable — cannot start consumer.", err=True)
        sys.exit(1)

    publisher = get_publisher()

    def handler(event: dict) -> None:
        action = event.get("action", "")
        target = event.get("targetApp", "unknown")
        click.echo(f"[consume] {action} -> {target}", err=True)

        if action == "assessment-complete":
            publisher.publish(
                "agentit-events",
                agent_id="consumer-dispatch",
                action="cve-check-triggered",
                target_app=target,
                summary=f"CVE check triggered by assessment of {target}",
            )
        elif action == "drift-detected":
            click.echo(f"[consume] Drift detected for {target}", err=True)
        elif action == "slo-breach":
            click.echo(f"[consume] SLO breach for {target}", err=True)

    click.echo(f"Consuming topics={topic_list} group={group_id}...", err=True)
    await asyncio.to_thread(consumer.consume, handler)


@main.command("vuln-watch")
@click.option("--interval", default=21600, type=int, help="Scan interval in seconds (default: 6 hours).")
@_run_async
async def vuln_watch(interval: int) -> None:
    """Long-lived vulnerability watcher — monitors for CVE events and rescans."""
    from agentit.consumer import EventConsumer
    from agentit.events import get_publisher
    from agentit.watchers.vuln_watcher import VulnWatcher

    store = await create_store()
    # EventConsumer's `poll_once()` (the only method this watcher's run()
    # loop calls) never touches its `store` param at all -- only
    # `consume()` (used by the standalone `consume` command, not here)
    # does. Passed through here purely for API-shape consistency; the
    # async store is safe to hand it regardless. VulnWatcher itself
    # genuinely supports the async store directly now -- see
    # watchers/vuln_watcher.py's check_fleet().
    consumer = EventConsumer(topics=["agentit-events"], group_id="agentit-vuln-watcher", store=store)
    watcher = VulnWatcher(
        publisher=get_publisher(),
        store=store,
        consumer=consumer,
        interval=interval,
    )
    await watcher.run()


@main.command("slo-track")
@click.option("--interval", default=300, type=int, help="Update interval in seconds (default: 5 minutes).")
@_run_async
async def slo_track(interval: int) -> None:
    """Long-lived SLO tracker — updates SLO current values and alerts on breaches."""
    from agentit.consumer import EventConsumer
    from agentit.events import get_publisher
    from agentit.watchers.slo_tracker import SloTracker

    store = await create_store()
    consumer = EventConsumer(topics=["agentit-events"], group_id="agentit-slo-tracker", store=store)
    tracker = SloTracker(
        publisher=get_publisher(),
        store=store,
        consumer=consumer,
        interval=interval,
    )
    await tracker.run()


@main.command("drift-detect")
@click.option("--interval", default=600, type=int, help="Poll interval in seconds (default: 10 minutes).")
@_run_async
async def drift_detect(interval: int) -> None:
    """Long-lived drift detector — checks Argo CD apps for out-of-sync state."""
    from agentit.events import get_publisher
    from agentit.watchers.drift_detector import DriftDetector

    store = await create_store()
    detector = DriftDetector(publisher=get_publisher(), interval=interval, store=store)
    await detector.run()


@main.command("learn-watch")
@click.option("--interval", default=86400, type=int, help="Research interval in seconds (default: 24 hours).")
@click.option("--limit", default=3, type=int, help="Max CVEs to research per cycle.")
@click.option("--llm-model", default=None, help="Claude model to use.")
@_run_async
async def learn_watch(interval: int, limit: int, llm_model: str | None) -> None:
    """Long-lived skill learner — periodically researches CVEs and drafts new skills."""
    import os

    from agentit.events import get_publisher
    from agentit.watchers.skill_learner import SkillLearner

    store = await create_store()
    # AGENTIT_SKILLS_DIR, when set (chart/templates/agents/skill-learner.yaml
    # sets it to a dedicated mounted PVC), is only the *fallback* location
    # now -- drafts are primarily pushed to the portal via an internal API
    # call (AGENTIT_PORTAL_URL) so they're visible on the Capabilities page
    # immediately; this PVC only matters if the portal is unreachable. See
    # SkillLearner's module docstring for the full mechanism.
    skills_dir_env = os.environ.get("AGENTIT_SKILLS_DIR")
    learner = SkillLearner(
        publisher=get_publisher(), llm_model=llm_model, interval=interval, limit=limit,
        store=store,
        skills_dir=Path(skills_dir_env) if skills_dir_env else None,
    )
    await learner.run()


@main.command("run-agent")
@click.argument("agent_name")
@click.option("--report", "report_path", required=True, type=click.Path(exists=True))
def run_agent(agent_name: str, report_path: str) -> None:
    """Run a single agent from a serialized AssessmentReport JSON. Used by K8s Jobs."""
    import json
    from agentit.agents.capabilities import get_agent_class, AGENT_CLASSES

    if agent_name not in AGENT_CLASSES:
        click.echo(f"Unknown agent: {agent_name}. Available: {', '.join(sorted(AGENT_CLASSES))}", err=True)
        sys.exit(1)

    report_json = Path(report_path).read_text(encoding="utf-8")
    report = AssessmentReport.model_validate_json(report_json)

    category = AGENT_CLASSES[agent_name][0]
    output_dir = Path(f"/tmp/agent-output/{category}")
    output_dir.mkdir(parents=True, exist_ok=True)

    agent_cls = get_agent_class(agent_name)
    agent_instance = agent_cls(report=report, output_dir=output_dir)
    result = agent_instance.run()

    files_data = [f.model_dump() for f in result.files]
    click.echo("--- AGENTIT_RESULT_BEGIN ---")
    click.echo(json.dumps(files_data))
    click.echo("--- AGENTIT_RESULT_END ---")


@main.command("self-assess")
@click.option("--repo-url", default="https://github.com/alimobrem/AgentIT", help="AgentIT repo URL.")
@click.option("--criticality", type=click.Choice(["low", "medium", "high", "critical"]), default="high")
@click.option("--auto-apply", is_flag=True, default=False, help="Run auto-mode pipeline after onboarding.")
@click.option("--llm", "use_llm", is_flag=True, default=None)
@click.option("--llm-model", default=None)
@_run_async
async def self_assess(repo_url: str, criticality: str, auto_apply: bool, use_llm: bool, llm_model: str | None) -> None:
    """Assess AgentIT itself — dogfooding the platform on its own repo."""
    from agentit.agents.orchestrator import FleetOrchestrator

    store = await create_store()

    try:
        with _resolve_and_assess(repo_url, criticality, use_llm, llm_model) as report:
            assessment_id = await store.save(report)
            click.echo(f"Self-assessment score: {report.overall_score:.0f}/100", err=True)
            click.echo(f"Assessment ID: {assessment_id}", err=True)

            out = Path("./self-assess-output")
            out.mkdir(parents=True, exist_ok=True)

            click.echo("Running Fleet Orchestrator on AgentIT...", err=True)
            # FleetOrchestrator is now genuinely async -- hand it the
            # async store directly and await it.
            orch = FleetOrchestrator(
                report=report, output_dir=out,
                store=store, assessment_id=assessment_id,
            )
            result = await orch.run()

            for ar in result.agent_results:
                status = "PASS" if ar.success else "FAIL"
                click.echo(f"  [{status}] {ar.agent_name}: {len(ar.files_generated)} files", err=True)

            click.echo(f"\n{result.recommendation}", err=True)

            if auto_apply and result.plan.auto_approve:
                from agentit.automode import AutoMode
                llm_client = None
                if use_llm:
                    try:
                        from agentit.llm import LLMClient
                        llm_client = LLMClient(model=llm_model)
                    except Exception:
                        click.echo("LLM unavailable — continuing without safety classification.", err=True)

                files = []
                for ar in result.agent_results:
                    if not ar.success:
                        continue
                    for fpath in ar.files_generated:
                        full = out / ar.category / fpath
                        if full.is_file():
                            files.append({
                                "category": ar.category,
                                "path": fpath,
                                "content": full.read_text(),
                                "description": fpath,
                            })

                engine = AutoMode(store=store, llm_client=llm_client)
                apply_result = await engine.execute(
                    assessment_id, files, "agentit",
                    criticality, result.plan.auto_approve, "agentit",
                )
                click.echo(f"\nAuto-apply: {apply_result['action']} — {apply_result['reason']}", err=True)
            elif auto_apply:
                click.echo("\nAuto-apply skipped: orchestrator did not auto-approve.", err=True)

    except CloneError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command("self-fix")
@click.option("--repo-url", default="https://github.com/alimobrem/AgentIT", help="AgentIT repo URL.")
@click.option("--criticality", default="high")
@click.option("--dry-run", is_flag=True, help="Show what would be fixed without applying.")
@click.option("--create-pr", is_flag=True, help="Create a PR with the fixes.")
@_run_async
async def self_fix(repo_url: str, criticality: str, dry_run: bool, create_pr: bool) -> None:
    """Autonomous self-healing: assess, fix findings, verify, commit.

    The closed loop: AgentIT finds its own problems and fixes them.
    """
    from agentit.remediation.dispatcher import RemediationDispatcher
    from agentit.remediation.registry import lookup

    store = await create_store(":memory:")

    click.echo("Step 1: Assessing...", err=True)
    try:
        with _resolve_and_assess(repo_url, criticality) as report:
            before_score = report.overall_score
            assessment_id = await store.save(report)

            fixable = []
            for s in report.scores:
                for f in s.findings:
                    if lookup(f.category) is not None:
                        fixable.append(f)

            click.echo(f"  Score: {before_score:.0f}/100", err=True)
            click.echo(f"  Total findings: {sum(len(s.findings) for s in report.scores)}", err=True)
            click.echo(f"  Auto-fixable: {len(fixable)}", err=True)

            if not fixable:
                click.echo(f"\nNothing to fix automatically. Score: {before_score:.0f}/100", err=True)
                return

            click.echo(f"\nStep 2: Generating fixes for {len(fixable)} finding(s)...", err=True)

            # Try skill engine first (LLM-powered, tailored), fall back to dispatcher (templates)
            from agentit.skill_engine import SkillEngine
            from agentit.platform_context import offline_context
            try:
                from agentit.platform_context import discover_platform
                platform = discover_platform()
            except Exception:
                platform = offline_context()

            skills_dir = Path(__file__).parent.parent.parent / "skills"
            if not skills_dir.exists():
                skills_dir = Path("skills")
            engine = SkillEngine(skills_dir, platform=platform)

            llm_for_gen = None
            try:
                from agentit.llm import LLMClient
                llm_for_gen = LLMClient()
                click.echo("  LLM available — generating tailored fixes.", err=True)
            except Exception:
                click.echo("  LLM unavailable — using template fallback.", err=True)

            # RemediationDispatcher is now genuinely async -- hand it the
            # async store directly and await dispatch() below.
            dispatcher = RemediationDispatcher(store)
            generated = []

            for finding in fixable:
                # Try skill engine first
                skill_files = engine.generate_for_finding(
                    finding.category, finding.description, report, llm_client=llm_for_gen,
                )
                if skill_files:
                    for fix_file in skill_files:
                        source = "skill+LLM" if llm_for_gen else "skill+template"
                        click.echo(f"  Generated: {fix_file.path} ({source})", err=True)
                        generated.append((finding, fix_file))
                    continue

                # Fall back to dispatcher (Python agent templates)
                result = await dispatcher.dispatch(assessment_id, finding.category, report.repo_name)
                if result.get("files"):
                    from agentit.agents.base import GeneratedFile
                    for ff_raw in result["files"]:
                        if isinstance(ff_raw, GeneratedFile):
                            fix_file = ff_raw
                        else:
                            fix_file = GeneratedFile(
                                path=ff_raw.get("path", "fix.yaml"),
                                content=ff_raw.get("content", ""),
                                description=ff_raw.get("description", ""),
                                finding_addressed=ff_raw.get("finding_addressed", finding.category),
                            )
                        click.echo(f"  Generated: {fix_file.path} (agent/{result['agent']})", err=True)
                        generated.append((finding, fix_file))
                elif result.get("error"):
                    click.echo(f"  Skip {finding.category}: {result['error']}", err=True)

            if not generated:
                click.echo("\nNo fixes generated.", err=True)
                return

            click.echo(f"\nStep 3: LLM review — first approver gate...", err=True)
            llm_client = None
            try:
                from agentit.llm import LLMClient
                llm_client = LLMClient()
                click.echo("  LLM available — reviewing each fix.", err=True)
            except Exception:
                click.echo("  LLM unavailable — skipping review (all fixes gated).", err=True)

            app_summary = f"{report.repo_name} ({', '.join(l.name for l in report.stack.languages)}, score {before_score:.0f}/100)"
            approved_files = []
            rejected = []
            # LLM reasoning per fix_file, keyed by identity — the review dict itself
            # isn't otherwise retained past this loop, so without this the LLM's actual
            # reasoning text is only ever shown here (click.echo) and lost afterward.
            review_reasons: dict[int, str] = {}

            for finding, fix_file in generated:
                if llm_client:
                    review = llm_client.review_fix(
                        finding_description=finding.description,
                        finding_category=finding.category,
                        fix_content=fix_file.content[:3000],
                        app_summary=app_summary,
                    )
                    if review is None:
                        click.echo(f"  ⚠ {fix_file.path}: LLM unavailable — rejected (fail-closed)", err=True)
                        rejected.append(fix_file)
                        review_reasons[id(fix_file)] = "LLM unavailable — rejected (fail-closed)"
                    elif review["approved"] and review["confidence"] >= 0.7:
                        click.echo(f"  ✓ {fix_file.path}: approved ({review['confidence']:.0%}) — {review['reason']}", err=True)
                        approved_files.append(fix_file)
                        review_reasons[id(fix_file)] = review["reason"]
                    else:
                        click.echo(f"  ✗ {fix_file.path}: rejected ({review['confidence']:.0%}) — {review['reason']}", err=True)
                        rejected.append(fix_file)
                        review_reasons[id(fix_file)] = review["reason"]
                else:
                    approved_files.append(fix_file)
                    review_reasons[id(fix_file)] = "LLM unavailable — auto-approved (no review performed)"

            click.echo(f"\n  Approved: {len(approved_files)}, Rejected: {len(rejected)}", err=True)

            import os
            db_path = os.environ.get('AGENTIT_DB_PATH')
            if db_path:
                eff_store = await create_store(db_path)
                for finding, fix_file in generated:
                    outcome = 'approved' if fix_file in approved_files else 'rejected'
                    # Prefer the exact skill name SkillEngine.generate() sets
                    # (fix_file.skill_name) -- deriving it from the path
                    # (stripping just ".yaml") was wrong for skill-generated
                    # files, since the path is "{app_name}-{skill.name}.yaml"
                    # and this recorded "app_name-skill_name" as the skill
                    # name, never aggregating across apps. Only Python-agent
                    # (dispatcher) files fall back to the old heuristics --
                    # they carry no skill_name.
                    skill_name = fix_file.skill_name or fix_file.path.replace('.yaml', '')
                    if not fix_file.skill_name and fix_file.description and "'" in fix_file.description:
                        parts = fix_file.description.split("'")
                        if len(parts) >= 2:
                            skill_name = parts[1]
                    reason = review_reasons.get(id(fix_file), '')
                    await eff_store.record_skill_outcome(skill_name, report.repo_name, outcome, reason)
                    await eff_store.log_event(
                        skill_name, f"fix-{outcome}", report.repo_name,
                        "info" if outcome == "approved" else "warning",
                        reason or f"Fix {outcome} (no reason captured)",
                    )
                click.echo(f'  Effectiveness recorded to {db_path}', err=True)

            if not approved_files:
                click.echo("\nNo fixes approved by LLM.", err=True)
                return

            if dry_run:
                click.echo("\n[DRY RUN] Would apply:", err=True)
                for ff in approved_files:
                    click.echo(f"  {ff.path}: {ff.description}", err=True)
                return

            click.echo(f"\nStep 4: Writing {len(approved_files)} approved fix(es) to disk...", err=True)
            for ff in approved_files:
                target = Path(ff.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(ff.content, encoding="utf-8")
                click.echo(f"  Wrote: {ff.path}", err=True)

            click.echo("\nStep 5: Re-assessing to verify improvement...", err=True)
            with _resolve_and_assess(repo_url, criticality) as report2:
                after_score = report2.overall_score
                delta = after_score - before_score
                click.echo(f"  Before: {before_score:.0f}/100", err=True)
                click.echo(f"  After:  {after_score:.0f}/100", err=True)
                click.echo(f"  Delta:  {'+' if delta >= 0 else ''}{delta:.0f}", err=True)

                if delta < 0:
                    click.echo("\nScore decreased — reverting fixes.", err=True)
                    sys.exit(1)

                if create_pr:
                    click.echo("\nStep 6: Creating PR...", err=True)
                    import subprocess
                    import time as _t
                    branch = f"agentit-self-fix-{int(_t.time()) % 100000}"
                    try:
                        subprocess.run(["git", "checkout", "-b", branch], check=True, capture_output=True)
                        subprocess.run(["git", "add"] + [ff.path for ff in approved_files],
                                       check=True, capture_output=True)
                        msg = (
                            f"fix: self-heal {len(approved_files)} finding(s) — "
                            f"score {before_score:.0f}→{after_score:.0f}\n\n"
                            f"Auto-generated by agentit self-fix.\n"
                            f"Findings fixed: {', '.join(f.category for f in fixable[:10])}"
                        )
                        subprocess.run(["git", "commit", "-m", msg], check=True, capture_output=True)
                        subprocess.run(["git", "push", "-u", "origin", branch],
                                       check=True, capture_output=True)
                        click.echo(f"  Pushed branch: {branch}", err=True)
                        click.echo(f"  Create PR at: {repo_url}/compare/{branch}", err=True)
                    except subprocess.CalledProcessError as exc:
                        click.echo(f"  Git/push failed: {exc}", err=True)

                click.echo(f"\nSelf-fix complete. Score: {before_score:.0f} → {after_score:.0f}/100", err=True)

    except CloneError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command()
@click.option("--source", type=click.Choice(["cves", "best-practices", "topic"]), default="cves",
              help="Research source: cves, best-practices, or a custom topic.")
@click.option("--topic", default=None, help="Topic string (required when --source=topic or best-practices).")
@click.option("--limit", default=5, type=int, help="Max items to research (default: 5).")
@click.option("--dry-run", is_flag=True, help="Show generated skills without saving.")
@click.option("--llm-model", default=None, help="Claude model to use.")
def learn(source: str, topic: str | None, limit: int, dry_run: bool, llm_model: str | None) -> None:
    """Research CVEs or best practices via LLM and generate new skills."""
    from agentit.learning_agent import (
        check_skill_exists,
        generate_skill_from_research,
        research_best_practices,
        research_cves,
        save_skill,
    )

    try:
        from agentit.llm import LLMClient
        llm_client = LLMClient(model=llm_model)
    except Exception as exc:
        click.echo(f"LLM required for learn command: {exc}", err=True)
        sys.exit(1)

    # Research phase
    if source == "cves":
        click.echo(f"Researching up to {limit} CVEs...", err=True)
        items = research_cves(llm_client, limit=limit)
        domain = "security"
    elif source in ("best-practices", "topic"):
        if not topic:
            click.echo("--topic is required for best-practices/topic source.", err=True)
            sys.exit(1)
        click.echo(f"Researching best practices for: {topic}", err=True)
        items = research_best_practices(llm_client, topic)
        domain = "custom"
    else:
        click.echo(f"Unknown source: {source}", err=True)
        sys.exit(1)

    if not items:
        click.echo("No research results returned.", err=True)
        return

    click.echo(f"Found {len(items)} item(s). Generating skills...", err=True)

    skills_dir = Path(__file__).parent.parent.parent / "skills"
    if not skills_dir.exists():
        skills_dir = Path("skills")

    for i, item in enumerate(items, 1):
        click.echo(f"\n[{i}/{len(items)}] Generating skill...", err=True)
        item_name = item.get("title") or item.get("id") or item.get("name", "")
        if item_name and check_skill_exists(skills_dir, item_name, domain):
            click.echo(f"  Skipped (duplicate): {item_name}", err=True)
            continue
        content = generate_skill_from_research(llm_client, item, domain=domain)
        if not content:
            click.echo("  Failed to generate skill content.", err=True)
            continue

        if dry_run:
            click.echo(f"  [DRY RUN] Would save skill:\n{content[:200]}...", err=True)
        else:
            path = save_skill(content, skills_dir, domain=domain)
            if path:
                click.echo(f"  Saved: {path}", err=True)
            else:
                click.echo("  Failed to save skill.", err=True)

    click.echo(f"\nDone. Generated {len(items)} skill(s).", err=True)


@main.command("test-skill")
@click.argument("skill_path", type=click.Path(exists=True))
@click.option("--repo", default=None, type=click.Path(exists=True), help="Repository path to test against.")
def test_skill(skill_path: str, repo: str | None) -> None:
    """Test a skill definition: load, validate, optionally match against a repo."""
    from agentit.skill_engine import SkillEngine, load_skill

    skill_file = Path(skill_path)
    click.echo(f"Loading skill: {skill_file}", err=True)
    skill = load_skill(skill_file)

    if skill is None:
        click.echo("FAIL: Could not parse skill file.", err=True)
        sys.exit(1)

    click.echo(f"  name: {skill.name}", err=True)
    click.echo(f"  domain: {skill.domain}", err=True)
    click.echo(f"  version: {skill.version}", err=True)
    click.echo(f"  status: {skill.status}", err=True)
    click.echo(f"  triggers: {skill.triggers}", err=True)
    click.echo(f"  outputs: {skill.outputs}", err=True)
    click.echo(f"  mode: {skill.mode}", err=True)

    # Validate frontmatter completeness
    issues: list[str] = []
    if not skill.triggers:
        issues.append("no triggers defined")
    if not skill.outputs:
        issues.append("no outputs defined")
    if not skill.body.strip():
        issues.append("empty body")
    if skill.status not in ("active", "deprecated", "retired", "draft"):
        issues.append(f"invalid status: {skill.status}")

    # Check body for expected sections
    body_lower = skill.body.lower()
    for section in ["property", "constraint", "verification"]:
        if section not in body_lower:
            issues.append(f"missing '{section}' section in body")

    # If a repo is provided, run assessment and check match
    if repo:
        click.echo(f"\nAssessing repo: {repo}", err=True)
        try:
            report = run_assessment(Path(repo), repo_url=repo, criticality="medium")
            matched = skill.matches(report)
            click.echo(f"  Skill matches repo: {matched}", err=True)

            # Try template generation
            if matched:
                from agentit.skill_engine import _extract_template
                template = _extract_template(skill.body)
                if template:
                    import re as _re
                    import yaml as _yaml
                    # Replace {{placeholder}} with dummy values for validation
                    _sanitized = _re.sub(r"\{\{(\w+)\}\}", r"placeholder_\1", template)
                    try:
                        list(_yaml.safe_load_all(_sanitized))
                        click.echo("  Template YAML: valid", err=True)
                    except _yaml.YAMLError as exc:
                        issues.append(f"template YAML invalid: {exc}")
                        click.echo(f"  Template YAML: INVALID -- {exc}", err=True)
        except Exception as exc:
            click.echo(f"  Assessment failed: {exc}", err=True)
            issues.append(f"assessment error: {exc}")

    # Report
    if issues:
        click.echo(f"\nFAIL: {len(issues)} issue(s):", err=True)
        for issue in issues:
            click.echo(f"  - {issue}", err=True)
        sys.exit(1)
    else:
        click.echo("\nPASS: Skill is valid.", err=True)


@main.command("learn-for")
@click.argument("repo_url")
@click.option("--criticality", default="medium")
@click.option("--limit", default=5, help="Number of improvements to research.")
@click.option("--dry-run", is_flag=True, help="Show what would be created without saving.")
def learn_for(repo_url: str, criticality: str, limit: int, dry_run: bool) -> None:
    """Targeted learning: assess an app, then research improvements for its specific stack.

    \b
    Unlike 'learn' (generic research), this command:
    1. Assesses the repo to understand its stack
    2. Asks the LLM specifically about THAT stack's risks and best practices
    3. Generates skills tailored to the app's technology choices
    """
    from agentit.learning_agent import (
        check_skill_exists, research_for_app, generate_skill_from_research, save_skill,
    )

    try:
        from agentit.llm import LLMClient
        llm = LLMClient()
    except Exception as exc:
        click.echo(f"LLM required for learning. Error: {exc}", err=True)
        sys.exit(1)

    click.echo("Step 1: Assessing app to understand its stack...", err=True)
    try:
        with _resolve_and_assess(repo_url, criticality) as report:
            stack_parts = []
            for lang in report.stack.languages:
                stack_parts.append(lang.name)
            for fw in report.stack.frameworks:
                stack_parts.append(fw.name)
            for db in report.stack.databases:
                stack_parts.append(db.name)
            click.echo(f"  Stack: {', '.join(stack_parts) or 'unknown'}", err=True)
            click.echo(f"  Score: {report.overall_score:.0f}/100", err=True)
            click.echo(f"  Findings: {sum(len(s.findings) for s in report.scores)}", err=True)

            click.echo(f"\nStep 2: Researching improvements for this stack...", err=True)
            findings = research_for_app(llm, report, limit)

            if not findings:
                click.echo("No research findings.", err=True)
                return

            click.echo(f"  Found {len(findings)} improvement(s).", err=True)

            click.echo(f"\nStep 3: Generating skills...", err=True)
            skills_dir = Path(__file__).parent.parent.parent / "skills"
            if not skills_dir.exists():
                skills_dir = Path("skills")

            created = []
            for item in findings:
                title = item.get("title", "unknown")
                category = item.get("category", "security")
                priority = item.get("priority", "medium")
                click.echo(f"\n  [{priority.upper()}] {title}", err=True)
                click.echo(f"    {item.get('description', '')[:100]}", err=True)

                if check_skill_exists(skills_dir, title, category):
                    click.echo(f"    Skipped (duplicate): {title}", err=True)
                    continue

                content = generate_skill_from_research(llm, item, domain=category)
                if not content:
                    click.echo(f"    Failed to generate skill.", err=True)
                    continue

                if dry_run:
                    click.echo(f"    [DRY RUN] Would save to skills/{category}/", err=True)
                else:
                    path = save_skill(content, skills_dir, domain=category)
                    if path:
                        click.echo(f"    Saved: {path}", err=True)
                        created.append(path)

            click.echo(f"\nLearning complete. {len(created)} skill(s) created for {report.repo_name}.", err=True)
            if created:
                click.echo("Review the new skills, then commit and merge.", err=True)

    except CloneError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command("activate-skill")
@click.argument("skill_path", type=click.Path(exists=True))
def activate_skill(skill_path: str) -> None:
    """Activate a draft skill after testing."""
    path = Path(skill_path)
    content = path.read_text(encoding="utf-8")
    if "status: draft" not in content:
        click.echo("Skill is not in draft status.", err=True)
        return
    updated = content.replace("status: draft", "status: active", 1)
    path.write_text(updated, encoding="utf-8")
    click.echo(f"Activated: {skill_path}", err=True)
