"""Tests for the `mode: watcher` registration metadata loader
(`agents/capabilities.py::load_watcher_agents()`) -- Phase 3 of
docs/extension-model-unification-plan-2026-07-18.md.

Mirrors tests/test_agent_registration.py's role for the Phase 2 agent
loader: schema validation for every real `watchers/*.md` file on disk,
plus a parity proof that the file-derived `WATCHER_AGENTS` list is
identical (as a set of entries) to the hardcoded list literal it
replaced.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from agentit.agents.capabilities import WATCHER_AGENTS, load_watcher_agents

WATCHERS_DIR = Path(__file__).resolve().parent.parent / "watchers"
REQUIRED_FIELDS = {"name", "mode", "interval", "description", "code_ref"}

# The exact hardcoded list `WATCHER_AGENTS` used to be (Phase 1 and
# earlier) -- see docs/extension-model-unification-plan-2026-07-18.md's
# Phase 3 section. Re-verified against the real, current
# agents/capabilities.py before this port (the plan doc's own "what
# exists today" table is stale here: it lists only 6 watchers, but
# self-health-check was added after that doc was written, making 7).
# `load_watcher_agents()` must reproduce every one of these fields
# (name/mode/interval/description) exactly -- a pure format migration,
# zero behavior change, mirroring Phase 1's/Phase 2's own parity discipline.
_PRE_PHASE3_WATCHER_AGENTS: list[dict[str, str]] = [
    {"name": "vuln-watcher", "mode": "Kafka consumer + polling", "interval": "6 hours", "description": "Monitors fleet for critical/high findings and raises an alert for each one"},
    {"name": "slo-tracker", "mode": "Polling", "interval": "5 minutes", "description": "Checks SLO status across all assessments, publishes breach alerts, recommends rollbacks"},
    {"name": "drift-detector", "mode": "Argo CD polling", "interval": "10 minutes", "description": "Queries Argo CD apps for OutOfSync state and auto-syncs them back to the Git-declared state"},
    {"name": "skill-learner", "mode": "LLM polling", "interval": "24 hours", "description": "Researches recent CVEs via LLM and drafts new skills (status: draft) for human review — requires an LLM connection"},
    {"name": "capability-scout", "mode": "LLM polling", "interval": "24 hours", "description": "Reads fleet usage/effectiveness data and doc-gap signals, proposes one small change to AgentIT itself as a draft PR for human review — requires an LLM connection and GITHUB_TOKEN"},
    {"name": "reassess-scheduler", "mode": "Polling", "interval": "1 hour", "description": "Checks every app's configured re-assessment cadence (daily/weekly/monthly, set on its Assessment Detail page) and automatically re-Assesses any app that's due, via the same route the manual Scan button uses"},
    {"name": "self-health-check", "mode": "Kube + GitHub API polling", "interval": "15 minutes", "description": "Verifies AgentIT's own critical infrastructure end to end -- GitHub webhook delivery health, CI pipeline stall detection, maintenance CronJob success, and cleanup-CronJob effectiveness -- publishing pass/fail events surfaced on the Health page's Self-Health panel and the sitewide Events badge"},
]

_KNOWN_WATCHER_CODE_REFS: dict[str, str] = {
    "vuln-watcher": "agentit.watchers.vuln_watcher:VulnWatcher",
    "slo-tracker": "agentit.watchers.slo_tracker:SloTracker",
    "drift-detector": "agentit.watchers.drift_detector:DriftDetector",
    "skill-learner": "agentit.watchers.skill_learner:SkillLearner",
    "capability-scout": "agentit.watchers.capability_scout:CapabilityScout",
    "reassess-scheduler": "agentit.watchers.reassess_scheduler:ReassessScheduler",
    "self-health-check": "agentit.watchers.self_health_check:SelfHealthCheck",
}


def _all_watcher_files() -> list[Path]:
    if not WATCHERS_DIR.is_dir():
        return []
    return sorted(WATCHERS_DIR.glob("*.md"))


@pytest.fixture(params=_all_watcher_files(), ids=lambda p: p.name)
def watcher_file(request: pytest.FixtureRequest) -> Path:
    return request.param


def _parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    assert m, f"{path.name}: no YAML frontmatter found"
    meta = yaml.safe_load(m.group(1))
    assert isinstance(meta, dict), f"{path.name}: frontmatter is not a mapping"
    return meta


class TestAllWatcherRegistrations:
    """Schema validation for every `watchers/*.md` file on disk."""

    def test_has_required_fields(self, watcher_file: Path) -> None:
        meta = _parse_frontmatter(watcher_file)
        missing = REQUIRED_FIELDS - set(meta.keys())
        assert not missing, f"{watcher_file.name} missing fields: {missing}"

    def test_code_ref_is_module_colon_classname(self, watcher_file: Path) -> None:
        meta = _parse_frontmatter(watcher_file)
        code_ref = str(meta["code_ref"])
        assert ":" in code_ref, f"{watcher_file.name}: code_ref {code_ref!r} missing ':'"
        module_path, _, class_name = code_ref.rpartition(":")
        assert module_path and class_name, f"{watcher_file.name}: malformed code_ref {code_ref!r}"

    def test_code_ref_class_is_actually_importable(self, watcher_file: Path) -> None:
        import importlib

        meta = _parse_frontmatter(watcher_file)
        module_path, _, class_name = str(meta["code_ref"]).rpartition(":")
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name, None)
        assert cls is not None, f"{watcher_file.name}: {class_name} not found in {module_path}"

    def test_code_ref_matches_known_mapping(self, watcher_file: Path) -> None:
        """Cross-check against an independently maintained mapping (not
        derived from the file itself), catching a typo'd code_ref that
        still happens to import successfully but points at the wrong
        watcher's class."""
        meta = _parse_frontmatter(watcher_file)
        name = str(meta["name"])
        assert name in _KNOWN_WATCHER_CODE_REFS, f"{watcher_file.name}: unknown watcher name {name!r}"
        assert meta["code_ref"] == _KNOWN_WATCHER_CODE_REFS[name]

    def test_filename_matches_name_field(self, watcher_file: Path) -> None:
        meta = _parse_frontmatter(watcher_file)
        assert watcher_file.stem == meta["name"], (
            f"{watcher_file.name}: filename stem does not match name field {meta['name']!r}"
        )


class TestLoadWatcherAgents:
    """`load_watcher_agents()` must reproduce the pre-Phase-3 hardcoded
    list exactly (as a set of name/mode/interval/description entries) --
    the parity proof for this migration."""

    @staticmethod
    def _strip_code_ref(entries: list[dict[str, str]]) -> list[dict[str, str]]:
        return [{k: v for k, v in e.items() if k != "code_ref"} for e in entries]

    def test_matches_pre_phase3_hardcoded_list_as_a_set(self) -> None:
        loaded = self._strip_code_ref(load_watcher_agents())
        assert {e["name"] for e in loaded} == {e["name"] for e in _PRE_PHASE3_WATCHER_AGENTS}
        by_name_loaded = {e["name"]: e for e in loaded}
        by_name_expected = {e["name"]: e for e in _PRE_PHASE3_WATCHER_AGENTS}
        for name, expected in by_name_expected.items():
            assert by_name_loaded[name] == expected, f"watcher '{name}' fields changed"

    def test_module_level_watcher_agents_is_file_derived(self) -> None:
        assert WATCHER_AGENTS == load_watcher_agents()

    def test_every_entry_has_a_code_ref(self) -> None:
        """Additive field beyond the pre-Phase-3 shape -- every consumer
        (schedules.py/capabilities.py) only reads the pre-existing keys,
        so this extra key must not break anything, but it should always
        be present now that the source is Markdown frontmatter."""
        for entry in WATCHER_AGENTS:
            assert entry.get("code_ref"), f"watcher '{entry.get('name')}' missing code_ref"

    def test_missing_directory_returns_empty_list_not_a_crash(self, tmp_path: Path) -> None:
        assert load_watcher_agents(tmp_path / "does-not-exist") == []

    def test_skips_malformed_file_without_crashing(self, tmp_path: Path) -> None:
        (tmp_path / "good.md").write_text(
            "---\nname: good\nmode: Polling\ninterval: 1 hour\n"
            "code_ref: agentit.watchers.slo_tracker:SloTracker\n"
            "description: ok\n---\n\nbody\n",
        )
        (tmp_path / "bad.md").write_text("no frontmatter here at all\n")
        (tmp_path / "bad-code-ref.md").write_text(
            "---\nname: bad\nmode: Polling\ninterval: 1 hour\n"
            "code_ref: not-a-module-colon-class\ndescription: ok\n---\n\nbody\n",
        )
        result = load_watcher_agents(tmp_path)
        assert result == [{
            "name": "good", "mode": "Polling", "interval": "1 hour",
            "description": "ok", "code_ref": "agentit.watchers.slo_tracker:SloTracker",
        }]
