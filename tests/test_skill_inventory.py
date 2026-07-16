"""Tests for skill_inventory.py: snapshotting and diffing the skills/checks catalog."""

from __future__ import annotations

from pathlib import Path

from agentit.skill_inventory import (
    InventorySnapshot,
    diff_and_log_inventory_changes,
    diff_snapshots,
    take_snapshot,
)
from conftest import make_store

_SKILL_TEMPLATE = """---
name: {name}
domain: {domain}
version: 1
triggers: [test]
outputs: [NetworkPolicy]
---
body
"""

_CHECK_TEMPLATE = """name: {name}
dimension: {dimension}
severity: medium
category: test
type: file_exists
pattern: "*.yaml"
description: test check
recommendation: do something
"""


def _write_skill(skills_dir: Path, name: str, domain: str = "security") -> None:
    domain_dir = skills_dir / domain
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / f"{name}.md").write_text(_SKILL_TEMPLATE.format(name=name, domain=domain))


def _write_check(checks_dir: Path, name: str, dimension: str = "security") -> None:
    checks_dir.mkdir(parents=True, exist_ok=True)
    (checks_dir / f"{name}.yaml").write_text(_CHECK_TEMPLATE.format(name=name, dimension=dimension))


class TestTakeSnapshot:
    def test_empty_dirs_produce_empty_snapshot(self, tmp_path: Path) -> None:
        snap = take_snapshot(tmp_path / "skills", tmp_path / "checks")
        assert snap.skills == frozenset()
        assert snap.checks == frozenset()

    def test_snapshot_captures_domain_and_name(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        checks_dir = tmp_path / "checks"
        _write_skill(skills_dir, "netpol-basic", domain="security")
        _write_check(checks_dir, "has-readiness-probe", dimension="reliability")

        snap = take_snapshot(skills_dir, checks_dir)
        assert snap.skills == frozenset({("security", "netpol-basic")})
        assert snap.checks == frozenset({("reliability", "has-readiness-probe")})

    def test_snapshot_round_trips_through_dict(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        checks_dir = tmp_path / "checks"
        _write_skill(skills_dir, "netpol-basic", domain="security")
        _write_check(checks_dir, "has-readiness-probe", dimension="reliability")

        snap = take_snapshot(skills_dir, checks_dir)
        restored = InventorySnapshot.from_dict(snap.to_dict())
        assert restored == snap


class TestDiffSnapshots:
    def test_no_previous_snapshot_reports_everything_as_added(self) -> None:
        current = InventorySnapshot(
            skills=frozenset({("security", "netpol-basic")}),
            checks=frozenset({("reliability", "has-readiness-probe")}),
        )
        diff = diff_snapshots(None, current)
        assert diff.skills_added == [("security", "netpol-basic")]
        assert diff.checks_added == [("reliability", "has-readiness-probe")]
        assert diff.skills_removed == []
        assert diff.checks_removed == []
        assert diff.has_changes

    def test_no_changes_between_identical_snapshots(self) -> None:
        snap = InventorySnapshot(
            skills=frozenset({("security", "netpol-basic")}),
            checks=frozenset({("reliability", "has-readiness-probe")}),
        )
        diff = diff_snapshots(snap, snap)
        assert not diff.has_changes
        assert diff.skills_added == []
        assert diff.skills_removed == []
        assert diff.checks_added == []
        assert diff.checks_removed == []

    def test_added_skill_and_check_detected(self) -> None:
        previous = InventorySnapshot(
            skills=frozenset({("security", "netpol-basic")}),
            checks=frozenset(),
        )
        current = InventorySnapshot(
            skills=frozenset({("security", "netpol-basic"), ("security", "cve-2099-1")}),
            checks=frozenset({("reliability", "has-readiness-probe")}),
        )
        diff = diff_snapshots(previous, current)
        assert diff.skills_added == [("security", "cve-2099-1")]
        assert diff.skills_removed == []
        assert diff.checks_added == [("reliability", "has-readiness-probe")]
        assert diff.checks_removed == []
        assert diff.has_changes

    def test_removed_skill_and_check_detected(self) -> None:
        previous = InventorySnapshot(
            skills=frozenset({("security", "netpol-basic"), ("security", "cve-2099-1")}),
            checks=frozenset({("reliability", "has-readiness-probe")}),
        )
        current = InventorySnapshot(
            skills=frozenset({("security", "netpol-basic")}),
            checks=frozenset(),
        )
        diff = diff_snapshots(previous, current)
        assert diff.skills_added == []
        assert diff.skills_removed == [("security", "cve-2099-1")]
        assert diff.checks_added == []
        assert diff.checks_removed == [("reliability", "has-readiness-probe")]
        assert diff.has_changes

    def test_status_only_change_is_not_reported(self) -> None:
        """Status transitions (active/deprecated/draft) are covered by the
        existing 'skill-activated' event, so identity-only diffing (domain,
        name) must not flag them as added/removed."""
        previous = InventorySnapshot(skills=frozenset({("security", "netpol-basic")}))
        current = InventorySnapshot(skills=frozenset({("security", "netpol-basic")}))
        diff = diff_snapshots(previous, current)
        assert not diff.has_changes


class TestStorePersistence:
    """save_skill_inventory_snapshot() / get_last_skill_inventory_snapshot()."""

    async def test_no_snapshot_saved_yet_returns_none(self) -> None:
        store = await make_store()
        assert await store.get_last_skill_inventory_snapshot() is None

    async def test_save_and_retrieve_snapshot(self) -> None:
        store = await make_store()
        snap = InventorySnapshot(skills=frozenset({("security", "netpol-basic")}))
        await store.save_skill_inventory_snapshot(snap.to_dict())

        loaded = await store.get_last_skill_inventory_snapshot()
        assert loaded is not None
        restored = InventorySnapshot.from_dict(loaded)
        assert restored == snap
        assert "created_at" in loaded

    async def test_get_last_returns_most_recent_of_several(self) -> None:
        store = await make_store()
        first = InventorySnapshot(skills=frozenset({("security", "a")}))
        second = InventorySnapshot(skills=frozenset({("security", "a"), ("security", "b")}))
        await store.save_skill_inventory_snapshot(first.to_dict())
        await store.save_skill_inventory_snapshot(second.to_dict())

        loaded = await store.get_last_skill_inventory_snapshot()
        assert InventorySnapshot.from_dict(loaded) == second


class TestDiffAndLogInventoryChanges:
    """The end-to-end helper wired into `_background_maintenance()`."""

    async def test_first_run_seeds_baseline_without_logging_events(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        checks_dir = tmp_path / "checks"
        _write_skill(skills_dir, "netpol-basic", domain="security")
        store = await make_store()

        diff = await diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)

        assert not diff.has_changes
        assert await store.list_events_by_agent("skill-inventory") == []
        assert await store.get_last_skill_inventory_snapshot() is not None

    async def test_added_skill_logs_skill_added_event(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        checks_dir = tmp_path / "checks"
        _write_skill(skills_dir, "netpol-basic", domain="security")
        store = await make_store()

        await diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)

        _write_skill(skills_dir, "cve-2099-1", domain="security")
        diff = await diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)

        assert diff.skills_added == [("security", "cve-2099-1")]
        events = await store.list_events_by_agent("skill-inventory")
        assert len(events) == 1
        assert events[0]["action"] == "skill-added"
        assert "cve-2099-1" in events[0]["summary"]

    async def test_removed_check_logs_check_removed_event(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        checks_dir = tmp_path / "checks"
        _write_check(checks_dir, "has-readiness-probe", dimension="reliability")
        store = await make_store()

        await diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)

        (checks_dir / "has-readiness-probe.yaml").unlink()
        diff = await diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)

        assert diff.checks_removed == [("reliability", "has-readiness-probe")]
        events = await store.list_events_by_agent("skill-inventory")
        assert len(events) == 1
        assert events[0]["action"] == "check-removed"
        assert events[0]["severity"] == "warning"

    async def test_no_changes_logs_no_events(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        checks_dir = tmp_path / "checks"
        _write_skill(skills_dir, "netpol-basic", domain="security")
        store = await make_store()

        await diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)
        diff = await diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)

        assert not diff.has_changes
        assert await store.list_events_by_agent("skill-inventory") == []


