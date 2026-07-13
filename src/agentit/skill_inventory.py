"""Skill/check inventory snapshotting and diffing.

Answers: "how does a user know if a new skill was added, or an old skill/check
was removed?" There's no in-app tracking of catalog changes today — the only
history is `git log` on `skills/`/`checks/`, which isn't surfaced anywhere in
the portal. This module takes a point-in-time snapshot of what's on disk and
diffs it against the previously saved snapshot so callers can log events for
anything added or removed.

Deliberately only tracks (domain, name) / (dimension, name) identity — status
transitions (active/deprecated/draft) are already covered by the existing
"skill-activated" event, so we don't want to double-report those here.

Read-only imports from skill_engine/check_engine — snapshot/diff logic lives
here so it doesn't collide with concurrent edits to those modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agentit.check_engine import load_checks
from agentit.skill_engine import load_all_skills


@dataclass(frozen=True)
class InventorySnapshot:
    """A point-in-time set of skill and check identities."""

    skills: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    checks: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    def to_dict(self) -> dict:
        return {
            "skills": sorted(list(item) for item in self.skills),
            "checks": sorted(list(item) for item in self.checks),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InventorySnapshot":
        return cls(
            skills=frozenset(tuple(item) for item in data.get("skills", [])),
            checks=frozenset(tuple(item) for item in data.get("checks", [])),
        )


@dataclass(frozen=True)
class InventoryDiff:
    """The delta between two InventorySnapshots."""

    skills_added: list[tuple[str, str]] = field(default_factory=list)
    skills_removed: list[tuple[str, str]] = field(default_factory=list)
    checks_added: list[tuple[str, str]] = field(default_factory=list)
    checks_removed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.skills_added or self.skills_removed
            or self.checks_added or self.checks_removed
        )


def take_snapshot(skills_dir: Path, checks_dir: Path) -> InventorySnapshot:
    """Load the current skills/checks catalogs and reduce them to identity sets."""
    skills = load_all_skills(skills_dir)
    checks = load_checks(checks_dir)
    return InventorySnapshot(
        skills=frozenset((s.domain, s.name) for s in skills),
        checks=frozenset((c.dimension, c.name) for c in checks),
    )


def diff_snapshots(previous: InventorySnapshot | None, current: InventorySnapshot) -> InventoryDiff:
    """Compare *current* against *previous* (``None`` means "no prior snapshot").

    With no prior snapshot, everything currently present is reported as
    "added" so the very first run seeds a baseline visible to the user
    rather than silently producing no diff.
    """
    prev_skills = previous.skills if previous is not None else frozenset()
    prev_checks = previous.checks if previous is not None else frozenset()

    return InventoryDiff(
        skills_added=sorted(current.skills - prev_skills),
        skills_removed=sorted(prev_skills - current.skills),
        checks_added=sorted(current.checks - prev_checks),
        checks_removed=sorted(prev_checks - current.checks),
    )


def diff_and_log_inventory_changes(
    store,
    skills_dir: Path = Path("skills"),
    checks_dir: Path = Path("checks"),
) -> InventoryDiff:
    """Snapshot the current catalog, diff it against the last saved one, and
    log a `store` event for every skill/check added or removed.

    This is what makes catalog changes show up on the existing Events page
    for free — no new UI plumbing needed there, just new event actions
    (`skill-added`, `skill-removed`, `check-added`, `check-removed`) that the
    generic event feed already knows how to render.

    On the very first call ever (no prior snapshot persisted), the current
    catalog is saved as a baseline without logging events — otherwise every
    pre-existing skill/check would be reported as "added" the first time
    this runs after the feature ships.
    """
    current = take_snapshot(skills_dir, checks_dir)
    last = store.get_last_skill_inventory_snapshot()
    previous = InventorySnapshot.from_dict(last) if last is not None else None

    diff = diff_snapshots(previous, current)

    if previous is None:
        store.save_skill_inventory_snapshot(current.to_dict())
        return InventoryDiff()

    if not diff.has_changes:
        return diff

    for domain, name in diff.skills_added:
        store.log_event(
            agent_id="skill-inventory", action="skill-added", target_app=None,
            severity="info", summary=f"New skill added: {domain}/{name}",
        )
    for domain, name in diff.skills_removed:
        store.log_event(
            agent_id="skill-inventory", action="skill-removed", target_app=None,
            severity="warning", summary=f"Skill removed: {domain}/{name}",
        )
    for dimension, name in diff.checks_added:
        store.log_event(
            agent_id="skill-inventory", action="check-added", target_app=None,
            severity="info", summary=f"New check added: {dimension}/{name}",
        )
    for dimension, name in diff.checks_removed:
        store.log_event(
            agent_id="skill-inventory", action="check-removed", target_app=None,
            severity="warning", summary=f"Check removed: {dimension}/{name}",
        )

    store.save_skill_inventory_snapshot(current.to_dict())
    return diff
