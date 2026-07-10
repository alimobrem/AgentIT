from __future__ import annotations

import shutil
import sys
from pathlib import Path

import click

from agentit.cloner import CloneError, clone_repo
from agentit.reporter import render_json_report, render_terminal_report
from agentit.runner import run_assessment


@click.group()
def main() -> None:
    """AgentIT -- Enterprise Readiness Assessor"""


@main.command()
@click.argument("repo_url")
@click.option("--criticality", type=click.Choice(["low", "medium", "high", "critical"]), default="medium")
@click.option("--format", "output_format", type=click.Choice(["json", "terminal"]), default="json")
@click.option("--output", "output_file", type=click.Path(), default=None)
def assess(repo_url: str, criticality: str, output_format: str, output_file: str | None) -> None:
    """Assess enterprise readiness of a Git repository."""
    clone_dir: Path | None = None
    try:
        if Path(repo_url).is_dir():
            repo_path = Path(repo_url)
        else:
            click.echo(f"Cloning {repo_url}...", err=True)
            repo_path = clone_repo(repo_url)
            clone_dir = repo_path

        click.echo("Running assessment...", err=True)
        report = run_assessment(repo_path, repo_url=repo_url, criticality=criticality)

        if output_format == "json":
            output = render_json_report(report)
        else:
            output = render_terminal_report(report)

        if output_file:
            Path(output_file).write_text(output)
            click.echo(f"Report written to {output_file}", err=True)
        else:
            click.echo(output)

    except CloneError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    finally:
        if clone_dir and clone_dir.exists():
            shutil.rmtree(clone_dir, ignore_errors=True)
