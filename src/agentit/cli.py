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
    use_llm: bool = False,
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
        if use_llm:
            from agentit.llm import LLMClient
            llm_client = LLMClient(model=llm_model or "claude-sonnet-4-5-20250514")

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
@click.option("--llm", "use_llm", is_flag=True, default=False, help="Enable Claude LLM for improved analysis (requires ANTHROPIC_API_KEY or ANTHROPIC_VERTEX_PROJECT_ID).")
@click.option("--llm-model", default=None, help="Claude model to use (default: claude-sonnet-4-5-20250514).")
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
@click.option("--llm", "use_llm", is_flag=True, default=False, help="Enable Claude LLM for improved analysis.")
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
@click.option("--output-dir", default="./onboarding-output", type=click.Path())
@click.option("--criticality", type=click.Choice(["low", "medium", "high", "critical"]), default="medium")
@click.option("--llm", "use_llm", is_flag=True, default=False, help="Enable Claude LLM for improved analysis.")
@click.option("--llm-model", default=None, help="Claude model to use.")
def onboard(repo_url: str, output_dir: str, criticality: str, use_llm: bool, llm_model: str | None) -> None:
    """Run full enterprise onboarding: assess -> harden -> observe -> cicd -> comply."""
    import json

    out = Path(output_dir)

    try:
        with _resolve_and_assess(repo_url, criticality, use_llm, llm_model) as report:
            # Write assessment report
            out.mkdir(parents=True, exist_ok=True)
            assessment_path = out / "assessment.json"
            assessment_path.write_text(render_json_report(report), encoding="utf-8")

            # Run agents into subdirectories
            agents: list[tuple[str, type]] = [
                ("security", HardeningAgent),
                ("observability", ObservabilityAgent),
                ("cicd", CICDAgent),
                ("compliance", ComplianceAgent),
            ]

            all_files: dict[str, list[str]] = {}
            for subdir, agent_cls in agents:
                sub_path = out / subdir
                click.echo(f"Running {agent_cls.__name__}...", err=True)
                result = agent_cls(report=report, output_dir=sub_path).run()
                all_files[subdir] = [gf.path for gf in result.files]

            # Summary
            click.echo(f"\nAssessment score: {report.overall_score:.1f}", err=True)
            click.echo(f"Assessment report: {assessment_path}", err=True)
            for category, files in all_files.items():
                click.echo(f"\n[{category}]", err=True)
                for f in files:
                    click.echo(f"  {out / category / f}", err=True)
    except CloneError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
