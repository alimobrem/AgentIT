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
                    except Exception:
                        pass

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


@main.command("vuln-watch")
@click.option("--interval", default=21600, type=int, help="Scan interval in seconds (default: 6 hours).")
def vuln_watch(interval: int) -> None:
    """Long-lived vulnerability watcher — monitors for CVE events and rescans."""
    import time
    from agentit.consumer import EventConsumer
    from agentit.events import get_publisher
    from agentit.portal.store import AssessmentStore

    click.echo(f"Starting vulnerability watcher (interval={interval}s)...", err=True)
    consumer = EventConsumer(
        topics=["agentit-events"],
        group_id="agentit-vuln-watcher",
    )
    publisher = get_publisher()
    store = AssessmentStore()

    def handle_event(event: dict) -> None:
        action = event.get("action", "")
        target = event.get("targetApp", "")
        if action == "assessment-complete":
            click.echo(f"[vuln-watch] Assessment completed for {target}, checking for CVEs...", err=True)
            publisher.publish(
                "agentit-events",
                agent_id="vuln-watcher",
                action="cve-check-triggered",
                target_app=target,
                summary=f"CVE check triggered by assessment of {target}",
            )

    while True:
        try:
            events = consumer.poll_once()
            for event in events:
                handle_event(event)

            fleet = store.get_fleet_data()
            click.echo(f"[vuln-watch] Monitoring {len(fleet)} apps", err=True)

            from agentit.automode import AutoMode
            from agentit.remediation_loop import RemediationLoop

            auto = AutoMode(store=store, publisher=publisher, llm_client=None)
            loop = RemediationLoop(store=store, publisher=publisher)

            for app_data in fleet:
                if app_data.get("critical_count", 0) > 0:
                    publisher.publish(
                        "agentit-alerts",
                        agent_id="vuln-watcher",
                        action="critical-findings-detected",
                        target_app=app_data["repo_name"],
                        severity="warning",
                        summary=f"{app_data['critical_count']} critical/high findings in {app_data['repo_name']}",
                    )
                    if auto.enabled:
                        click.echo(f"[vuln-watch] Auto-mode: running remediation loop for {app_data['repo_name']}...", err=True)
                        try:
                            result = loop.trigger(
                                repo_url=app_data["repo_url"],
                                app_name=app_data["repo_name"],
                                criticality=app_data.get("criticality", "medium"),
                                reason=f"critical findings detected ({app_data['critical_count']})",
                            )
                            click.echo(f"[vuln-watch] Loop result: {result['outcome']} at step {result.get('step', '?')}", err=True)
                        except Exception as exc:
                            click.echo(f"[vuln-watch] Remediation loop failed: {exc}", err=True)
        except KeyboardInterrupt:
            click.echo("Vulnerability watcher stopped.", err=True)
            break
        except Exception as exc:
            click.echo(f"[vuln-watch] Error: {exc}", err=True)

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("Vulnerability watcher stopped.", err=True)
            break


@main.command("slo-track")
@click.option("--interval", default=300, type=int, help="Update interval in seconds (default: 5 minutes).")
def slo_track(interval: int) -> None:
    """Long-lived SLO tracker — updates SLO current values and alerts on breaches."""
    import time
    from agentit.consumer import EventConsumer
    from agentit.events import get_publisher
    from agentit.portal.store import AssessmentStore

    click.echo(f"Starting SLO tracker (interval={interval}s)...", err=True)
    consumer = EventConsumer(
        topics=["agentit-events"],
        group_id="agentit-slo-tracker",
    )
    publisher = get_publisher()
    store = AssessmentStore()

    while True:
        try:
            consumer.poll_once()

            assessments = store.list_all()
            breached_apps = 0
            for a in assessments:
                slos = store.list_slos(a["id"])
                app_breaches = [s for s in slos if s["status"] == "breached"]
                for slo in app_breaches:
                    publisher.publish(
                        "agentit-alerts",
                        agent_id="slo-tracker",
                        action="slo-breach",
                        target_app=a["repo_name"],
                        severity="critical",
                        summary=f"SLO breached: {slo['metric_name']} (target={slo['target_value']}, current={slo['current_value']})",
                    )

                if app_breaches:
                    breached_apps += 1
                    # Check if there was a recent apply — if so, recommend rollback
                    apply_result = store.get_apply_results(a["id"])
                    if apply_result and apply_result.get("applied"):
                        publisher.publish(
                            "agentit-alerts",
                            agent_id="slo-tracker",
                            action="rollback-recommended",
                            target_app=a["repo_name"],
                            severity="critical",
                            summary=f"SLO breach after recent apply — consider rollback: kubectl argo rollouts undo {a['repo_name']}",
                        )
                        store.create_gate(
                            a["id"], "rollback-review",
                            f"SLO breach detected for {a['repo_name']} after recent apply. "
                            f"Breached: {', '.join(s['metric_name'] for s in app_breaches)}. "
                            f"Review and decide: rollback or investigate.",
                        )
                        click.echo(f"[slo-track] ROLLBACK RECOMMENDED: {a['repo_name']}", err=True)

            click.echo(f"[slo-track] Checked {len(assessments)} apps, {breached_apps} with breaches", err=True)
        except KeyboardInterrupt:
            click.echo("SLO tracker stopped.", err=True)
            break
        except Exception as exc:
            click.echo(f"[slo-track] Error: {exc}", err=True)

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("SLO tracker stopped.", err=True)
            break


@main.command("drift-detect")
@click.option("--interval", default=600, type=int, help="Poll interval in seconds (default: 10 minutes).")
def drift_detect(interval: int) -> None:
    """Long-lived drift detector — checks Argo CD apps for out-of-sync state."""
    import subprocess
    import time
    from agentit.events import get_publisher

    click.echo(f"Starting drift detector (interval={interval}s)...", err=True)
    publisher = get_publisher()

    while True:
        try:
            result = subprocess.run(
                ["oc", "get", "applications.argoproj.io", "-A", "-o", "json"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["kubectl", "get", "applications.argoproj.io", "-A", "-o", "json"],
                    capture_output=True, text=True, timeout=30,
                )

            if result.returncode == 0:
                import json
                apps = json.loads(result.stdout)
                items = apps.get("items", [])
                for app in items:
                    name = app.get("metadata", {}).get("name", "unknown")
                    status = app.get("status", {})
                    sync_status = status.get("sync", {}).get("status", "Unknown")
                    health = status.get("health", {}).get("status", "Unknown")

                    if sync_status == "OutOfSync":
                        publisher.publish(
                            "agentit-events",
                            agent_id="drift-detector",
                            action="drift-detected",
                            target_app=name,
                            severity="warning",
                            summary=f"Argo CD app '{name}' is OutOfSync (health: {health})",
                        )
                        click.echo(f"[drift-detect] DRIFT: {name} is OutOfSync", err=True)

                        # Auto-sync if auto-mode is on
                        from agentit.automode import AutoMode
                        from agentit.portal.store import AssessmentStore
                        _store = AssessmentStore()
                        _auto = AutoMode(store=_store, publisher=publisher)
                        if _auto.enabled:
                            click.echo(f"[drift-detect] Auto-syncing {name}...", err=True)
                            sync_result = subprocess.run(
                                ["oc", "app", "sync", name, "--async"],
                                capture_output=True, text=True, timeout=30,
                            )
                            if sync_result.returncode != 0:
                                click.echo(f"[drift-detect] Sync failed, trying argocd CLI...", err=True)
                                subprocess.run(
                                    ["argocd", "app", "sync", name, "--async"],
                                    capture_output=True, text=True, timeout=30,
                                )

                click.echo(f"[drift-detect] Checked {len(items)} Argo CD apps", err=True)
            else:
                click.echo("[drift-detect] No Argo CD access — skipping", err=True)

        except KeyboardInterrupt:
            click.echo("Drift detector stopped.", err=True)
            break
        except Exception as exc:
            click.echo(f"[drift-detect] Error: {exc}", err=True)

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("Drift detector stopped.", err=True)
            break


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
                        pass

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
