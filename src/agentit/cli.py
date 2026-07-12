from __future__ import annotations

import contextlib
import shutil
import sys
from collections.abc import Generator
from pathlib import Path

import click

from agentit.cloner import CloneError, clone_repo
from agentit.models import AssessmentReport
from agentit.reporter import render_json_report, render_terminal_report
from agentit.runner import run_assessment

# Agent imports used by the ``onboard`` command (lazy-imported inline for other
# commands, but we keep them at module level for ``onboard`` readability).
from agentit.agents.hardening import HardeningAgent
from agentit.agents.observability import ObservabilityAgent
from agentit.agents.cicd import CICDAgent
from agentit.agents.compliance import ComplianceAgent


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
      harden       Generate security manifests only
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
      orchestrate    Run full orchestration (low-level)
    """
    from agentit.logging_config import configure_logging
    configure_logging()


@main.command()
@click.argument("repo_url")
@click.option("--criticality", type=click.Choice(["low", "medium", "high", "critical"]), default="medium")
@click.option("--format", "output_format", type=click.Choice(["json", "terminal"]), default="json")
@click.option("--output", "output_file", type=click.Path(), default=None)
@click.option("--llm", "use_llm", is_flag=True, default=None, help="Enable Claude LLM (auto-detects credentials if omitted).")
@click.option("--llm-model", default=None, help="Claude model to use (default: env AGENTIT_LLM_MODEL).")
def assess(repo_url: str, criticality: str, output_format: str, output_file: str | None, use_llm: bool, llm_model: str | None) -> None:
    """Assess enterprise readiness of a Git repository."""
    try:
        with _resolve_and_assess(repo_url, criticality, use_llm, llm_model) as report:
            output = render_json_report(report) if output_format == "json" else render_terminal_report(report)
            if output_file:
                Path(output_file).write_text(output, encoding="utf-8")
                click.echo(f"Report written to {output_file}", err=True)
            else:
                click.echo(output)
    except CloneError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command()
@click.argument("repo_url")
@click.option("--output-dir", default="./hardening-output", type=click.Path())
@click.option("--criticality", type=click.Choice(["low", "medium", "high", "critical"]), default="medium")
@click.option("--llm", "use_llm", is_flag=True, default=None, help="Enable Claude LLM (auto-detects credentials if omitted).")
@click.option("--llm-model", default=None, help="Claude model to use.")
def harden(repo_url: str, output_dir: str, criticality: str, use_llm: bool, llm_model: str | None) -> None:
    """Generate enterprise hardening manifests for a repository."""
    from agentit.agents.hardening import HardeningAgent

    try:
        with _resolve_and_assess(repo_url, criticality, use_llm, llm_model) as report:
            click.echo("Generating hardening manifests...", err=True)
            agent = HardeningAgent(report=report, output_dir=Path(output_dir))
            result = agent.run()
            click.echo(result.summary, err=True)
            for gf in result.files:
                click.echo(f"  {gf.path}: {gf.description}", err=True)
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
@click.argument("repo_url")
@click.option("--interval", default=3600, type=int, help="Re-assessment interval in seconds (default: 1 hour).")
@click.option("--criticality", type=click.Choice(["low", "medium", "high", "critical"]), default="medium")
@click.option("--llm", "use_llm", is_flag=True, default=None)
@click.option("--llm-model", default=None)
@click.option("--webhook", default=None, help="Webhook URL to POST results to.")
def watch(repo_url: str, interval: int, criticality: str, use_llm: bool | None, llm_model: str | None, webhook: str | None) -> None:
    """Continuously re-assess a repository on a schedule."""
    import time
    import json
    import httpx

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
def orchestrate(repo_url: str, output_dir: str, criticality: str, use_llm: bool, llm_model: str | None) -> None:
    """Run full orchestrated onboarding with Fleet Orchestrator."""
    from agentit.agents.orchestrator import FleetOrchestrator

    try:
        with _resolve_and_assess(repo_url, criticality, use_llm, llm_model) as report:
            click.echo("Running Fleet Orchestrator...", err=True)
            orch = FleetOrchestrator(report=report, output_dir=Path(output_dir))
            result = orch.run()

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
def onboard(repo_url: str, output_dir: str, criticality: str, use_llm: bool, llm_model: str | None,
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
            result = orch.run()

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
def consume(topics: str, group_id: str) -> None:
    """Start a blocking Kafka consumer that dispatches events to watchers."""
    from agentit.consumer import EventConsumer
    from agentit.events import get_publisher
    from agentit.portal.store import AssessmentStore

    topic_list = [t.strip() for t in topics.split(",") if t.strip()]
    consumer = EventConsumer(topics=topic_list, group_id=group_id)

    if not consumer.connected:
        click.echo("Kafka unavailable — cannot start consumer.", err=True)
        sys.exit(1)

    publisher = get_publisher()
    store = AssessmentStore()

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
    consumer.consume(handler)


@main.command("vuln-watch")
@click.option("--interval", default=21600, type=int, help="Scan interval in seconds (default: 6 hours).")
def vuln_watch(interval: int) -> None:
    """Long-lived vulnerability watcher — monitors for CVE events and rescans."""
    from agentit.consumer import EventConsumer
    from agentit.events import get_publisher
    from agentit.portal.store import AssessmentStore
    from agentit.watchers.vuln_watcher import VulnWatcher

    consumer = EventConsumer(topics=["agentit-events"], group_id="agentit-vuln-watcher")
    watcher = VulnWatcher(
        publisher=get_publisher(),
        store=AssessmentStore(),
        consumer=consumer,
        interval=interval,
    )
    watcher.run()


@main.command("slo-track")
@click.option("--interval", default=300, type=int, help="Update interval in seconds (default: 5 minutes).")
def slo_track(interval: int) -> None:
    """Long-lived SLO tracker — updates SLO current values and alerts on breaches."""
    from agentit.consumer import EventConsumer
    from agentit.events import get_publisher
    from agentit.portal.store import AssessmentStore
    from agentit.watchers.slo_tracker import SloTracker

    consumer = EventConsumer(topics=["agentit-events"], group_id="agentit-slo-tracker")
    tracker = SloTracker(
        publisher=get_publisher(),
        store=AssessmentStore(),
        consumer=consumer,
        interval=interval,
    )
    tracker.run()


@main.command("drift-detect")
@click.option("--interval", default=600, type=int, help="Poll interval in seconds (default: 10 minutes).")
def drift_detect(interval: int) -> None:
    """Long-lived drift detector — checks Argo CD apps for out-of-sync state."""
    from agentit.events import get_publisher
    from agentit.watchers.drift_detector import DriftDetector

    detector = DriftDetector(publisher=get_publisher(), interval=interval)
    detector.run()


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
def self_assess(repo_url: str, criticality: str, auto_apply: bool, use_llm: bool, llm_model: str | None) -> None:
    """Assess AgentIT itself — dogfooding the platform on its own repo."""
    from agentit.agents.orchestrator import FleetOrchestrator
    from agentit.portal.store import AssessmentStore

    store = AssessmentStore()

    try:
        with _resolve_and_assess(repo_url, criticality, use_llm, llm_model) as report:
            assessment_id = store.save(report)
            click.echo(f"Self-assessment score: {report.overall_score:.0f}/100", err=True)
            click.echo(f"Assessment ID: {assessment_id}", err=True)

            out = Path("./self-assess-output")
            out.mkdir(parents=True, exist_ok=True)

            click.echo("Running Fleet Orchestrator on AgentIT...", err=True)
            orch = FleetOrchestrator(
                report=report, output_dir=out,
                store=store, assessment_id=assessment_id,
            )
            result = orch.run()

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
                apply_result = engine.execute(
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
def self_fix(repo_url: str, criticality: str, dry_run: bool, create_pr: bool) -> None:
    """Autonomous self-healing: assess, fix findings, verify, commit.

    The closed loop: AgentIT finds its own problems and fixes them.
    """
    from agentit.remediation.dispatcher import RemediationDispatcher
    from agentit.remediation.registry import lookup
    from agentit.portal.store import AssessmentStore

    store = AssessmentStore(":memory:")

    click.echo("Step 1: Assessing...", err=True)
    try:
        with _resolve_and_assess(repo_url, criticality) as report:
            before_score = report.overall_score
            assessment_id = store.save(report)

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
                result = dispatcher.dispatch(assessment_id, finding.category, report.repo_name)
                if result.get("files"):
                    for fix_file in result["files"]:
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
                    elif review["approved"] and review["confidence"] >= 0.7:
                        click.echo(f"  ✓ {fix_file.path}: approved ({review['confidence']:.0%}) — {review['reason']}", err=True)
                        approved_files.append(fix_file)
                    else:
                        click.echo(f"  ✗ {fix_file.path}: rejected ({review['confidence']:.0%}) — {review['reason']}", err=True)
                        rejected.append(fix_file)
                else:
                    approved_files.append(fix_file)

            click.echo(f"\n  Approved: {len(approved_files)}, Rejected: {len(rejected)}", err=True)

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
