import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from agentit.cli import main


def _truncate_fleet(dsn: str) -> None:
    """Reset the shared database to empty before a test that needs a
    genuinely clean fleet (e.g. asserting "no tracked apps"). Uses its own
    throwaway pool against `dsn` rather than conftest's shared
    session-wide store/pool -- that singleton is bound to the
    pytest-asyncio session loop, and this helper runs inside its own,
    separate `asyncio.run()`-created loop (see `_seed_and_get_history`'s
    docstring for why these tests must stay plain `def`); reusing an
    asyncpg pool across two different event loops raises, not just
    reconnects."""
    from agentit.portal.store import AssessmentStore, _ALL_TABLES

    async def _run():
        store = await AssessmentStore.create(dsn, min_size=1, max_size=2)
        try:
            await store._pool.execute(f"TRUNCATE {', '.join(_ALL_TABLES)} CASCADE")
        finally:
            await store.close()

    asyncio.run(_run())


def _seed_and_get_history(dsn: str, repo_url: str, **report_kwargs) -> list[dict]:
    """Truncate the fleet, then seed one report into a fresh pool against
    `dsn` and read back history -- see `_truncate_fleet`'s docstring for
    why this uses its own independent pool/loop rather than the shared
    session store."""
    from agentit.portal.store import AssessmentStore
    from conftest import make_report

    _truncate_fleet(dsn)

    async def _run():
        store = await AssessmentStore.create(dsn, min_size=1, max_size=2)
        try:
            await store.save(make_report(repo_url=repo_url, **report_kwargs))
            return await store.list_history(repo_url)
        finally:
            await store.close()

    return asyncio.run(_run())


def _history(dsn: str, repo_url: str) -> list[dict]:
    from agentit.portal.store import AssessmentStore

    async def _run():
        store = await AssessmentStore.create(dsn, min_size=1, max_size=2)
        try:
            return await store.list_history(repo_url)
        finally:
            await store.close()

    return asyncio.run(_run())


def _make_local_repo(tmp_path: Path) -> str:
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()
    (repo_dir / "main.go").write_text("package main\nfunc main() {}\n")
    (repo_dir / "go.mod").write_text("module github.com/test/app\n\ngo 1.22\n")
    (repo_dir / "Dockerfile").write_text("FROM golang:1.22\nCMD ['app']\n")
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@t.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "T"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, capture_output=True)
    return str(repo_dir)


def test_watch_runs_one_iteration(tmp_path: Path):
    repo_url = _make_local_repo(tmp_path)
    runner = CliRunner()

    with patch("time.sleep", side_effect=KeyboardInterrupt):
        result = runner.invoke(main, ["watch", repo_url, "--interval", "10"])

    assert result.exit_code == 0, result.output
    assert "Watching" in result.output
    assert "/100" in result.output
    assert "Stopped." in result.output


def test_watch_requires_repo_url_unless_rescan():
    """Regression: repo_url must still be required for normal (non-rescan)
    invocations, even though it's no longer a hard Click-required argument."""
    runner = CliRunner()
    result = runner.invoke(main, ["watch"])
    assert result.exit_code != 0
    assert "REPO_URL is required" in result.output


def test_assess_requires_repo_url_unless_rescan():
    runner = CliRunner()
    result = runner.invoke(main, ["assess"])
    assert result.exit_code != 0
    assert "REPO_URL is required" in result.output


def test_watch_rescan_iterates_the_fleet(tmp_path: Path, monkeypatch, postgres_dsn):
    """Regression: `agentit watch --rescan` (used by CronJobs, see
    chart/templates/workflows/cve-scan-cronworkflow.yaml) previously failed
    with a Click usage error because --rescan wasn't a real flag. It must
    now iterate every tracked fleet app via the store and re-assess each."""
    monkeypatch.setenv("AGENTIT_DB_DSN", postgres_dsn)

    repo_url = _make_local_repo(tmp_path)
    _seed_and_get_history(postgres_dsn, repo_url, repo_name="tracked-app", criticality="low")

    runner = CliRunner()
    result = runner.invoke(main, ["watch", "--rescan"])

    assert result.exit_code == 0, result.output
    assert "[rescan]" in result.output
    assert "Re-assessing" in result.output

    # A fresh assessment must have been persisted for the tracked app.
    history = _history(postgres_dsn, repo_url)
    assert len(history) == 2  # the seeded one + the rescan


def test_assess_rescan_with_no_fleet_apps_is_a_noop(tmp_path: Path, monkeypatch, postgres_dsn):
    monkeypatch.setenv("AGENTIT_DB_DSN", postgres_dsn)
    _truncate_fleet(postgres_dsn)

    runner = CliRunner()
    result = runner.invoke(main, ["assess", "--rescan"])

    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output.lower()


def test_watch_cost_report_flag_does_not_exist():
    """Regression test: chart/templates/workflows/cost-report-cronjob.yaml
    used to call `watch --cost-report`, a flag that was never implemented on
    this command -- confirm it's rejected by Click, not silently ignored."""
    runner = CliRunner()
    result = runner.invoke(main, ["watch", "--cost-report"])
    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_watch_rescan_with_cost_dimension(tmp_path: Path, monkeypatch, postgres_dsn):
    """Regression: the cost-report CronJob's fixed command
    (`watch --rescan --dimension cost`) must actually be accepted and behave
    the same as its siblings (compliance-rescan, dependency-update)."""
    monkeypatch.setenv("AGENTIT_DB_DSN", postgres_dsn)

    repo_url = _make_local_repo(tmp_path)
    _seed_and_get_history(postgres_dsn, repo_url, repo_name="tracked-app-cost", criticality="low")

    runner = CliRunner()
    result = runner.invoke(main, ["watch", "--rescan", "--dimension", "cost"])

    assert result.exit_code == 0, result.output
    assert "[rescan]" in result.output
    assert "dimension=cost" in result.output


def test_assess_rescan_filters_dimension_count(tmp_path: Path, monkeypatch, postgres_dsn):
    monkeypatch.setenv("AGENTIT_DB_DSN", postgres_dsn)

    repo_url = _make_local_repo(tmp_path)
    _seed_and_get_history(postgres_dsn, repo_url, repo_name="tracked-app2", criticality="low")

    runner = CliRunner()
    result = runner.invoke(main, ["assess", "--rescan", "--dimension", "compliance"])

    assert result.exit_code == 0, result.output
    assert "dimension=compliance" in result.output
