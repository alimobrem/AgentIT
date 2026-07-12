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
                llm_client = LLMClient(model=llm_model or "claude-sonnet-4-6")
            except Exception as exc:
                click.echo(f"LLM init failed (continuing without): {exc}", err=True)

        click.echo("Running assessment...", err=True)
        report = run_assessment(repo_path, repo_url=repo_url, criticality=criticality, llm_client=llm_client)
        yield report
    finally:
        if clone_dir and clone_dir.exists():
            shutil.rmtree(clone_dir, ignore_errors=True)


@click.group()
def main() -> None:
    """AgentIT -- Enterprise Readiness Assessor"""
    from agentit.logging_config import configure_logging
    configure_logging()


@main.command()
@click.argument("repo_url")
@click.option("--criticality", type=click.Choice(["low", "medium", "high", "critical"]), default="medium")
@click.option("--format", "output_format", type=click.Choice(["json", "terminal"]), default="json")
@click.option("--output", "output_file", type=click.Path(), default=None)
@click.option("--llm", "use_llm", is_flag=True, default=None, help="Enable Claude LLM (auto-detects credentials if omitted).")
@click.option("--llm-model", default=None, help="Claude model to use (default: claude-sonnet-4-6).")
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
def onboard(repo_url: str, output_dir: str, criticality: str, use_llm: bool, llm_model: str | None) -> None:
    """Run full enterprise onboarding: assess -> harden -> observe -> cicd -> comply."""
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
            click.echo("Running Fleet Orchestrator...", err=True)
            orch = FleetOrchestrator(report=report, output_dir=out)
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
                        llm_client = LLMClient(model=llm_model or "claude-sonnet-4-6")
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
