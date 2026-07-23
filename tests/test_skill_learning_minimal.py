"""Minimal skill-learning ship: record theater refusals, cool down repeated
skills, stop blind redispatch, category-gated run_all skips.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agentit.assessment_diff import finding_key
from agentit.models import DimensionScore, Finding, Severity
from agentit.platform_context import offline_context
from agentit.portal.auto_delivery import auto_validate_and_deliver
from agentit.portal.delivery import handle_confirmed_finding_failure
from agentit.portal.store.skills import (
    SKILL_COOLDOWN_SAME_REASON,
    skill_reject_reason_prefix,
)
from agentit.skill_engine import Skill, SkillEngine
from conftest import make_report, make_store


class TestRejectReasonPrefix:
    def test_collapses_still_present_delivery_ids(self):
        a = skill_reject_reason_prefix(
            "finding still present after merge (delivery abc)",
        )
        b = skill_reject_reason_prefix(
            "finding still present after merge (delivery xyz)",
        )
        assert a == b == "finding still present after merge"

    def test_clear_evidence_prefix(self):
        assert skill_reject_reason_prefix(
            "clear-evidence: audit module at repo root only",
        ) == "clear-evidence"


class TestSkillCooldownStore:
    async def test_cools_after_two_identical_prefixes(self):
        store = await make_store()
        await store.record_skill_outcome(
            "app-audit-logging", "pinky", "rejected",
            "clear-evidence: audit module at repo root only",
        )
        assert not await store.is_skill_cooling_down("pinky", "app-audit-logging")
        await store.record_skill_outcome(
            "app-audit-logging", "pinky", "rejected",
            "clear-evidence: still root-only audit.py",
        )
        assert await store.is_skill_cooling_down("pinky", "app-audit-logging")
        info = await store.get_skill_cooldown("pinky", "app-audit-logging")
        assert info is not None
        assert info["count"] >= SKILL_COOLDOWN_SAME_REASON
        assert info["reason_prefix"] == "clear-evidence"

    async def test_list_cooled_skills_scoped_to_app(self):
        store = await make_store()
        for _ in range(2):
            await store.record_skill_outcome(
                "containerfile", "app-a", "rejected", "clear-evidence: :latest",
            )
        await store.record_skill_outcome(
            "containerfile", "app-b", "rejected", "clear-evidence: :latest",
        )
        cooled_a = await store.list_cooled_skills("app-a")
        assert any(c["skill_name"] == "containerfile" for c in cooled_a)
        cooled_b = await store.list_cooled_skills("app-b")
        assert cooled_b == []

    async def test_identical_reject_fast_path_aligns_with_cooldown(self):
        store = await make_store()
        # Threshold matches SKILL_COOLDOWN_SAME_REASON so a single-app
        # cool-down still flags the skill for skill-learner research.
        for i in range(SKILL_COOLDOWN_SAME_REASON):
            await store.record_skill_outcome(
                "db-migration-tooling", f"app-{i}", "rejected",
                "clear-evidence: target_metadata = None theater",
            )
        flagged = await store.get_skills_with_identical_reject_reasons()
        assert any(f["skill"] == "db-migration-tooling" for f in flagged)
        entry = next(f for f in flagged if f["skill"] == "db-migration-tooling")
        assert entry["identical_reject_count"] >= SKILL_COOLDOWN_SAME_REASON
        assert entry.get("fast_path") is True


class TestMatchAndRunAllCooldown:
    def _skill(self) -> Skill:
        return Skill(
            name="app-audit-logging",
            domain="compliance",
            version=1,
            triggers=["audit"],
            outputs=["ConfigMap"],
            property_description="audit logging",
            body="# audit\n```yaml\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n```\n",
            file_path="skills/compliance/app-audit-logging.md",
            mode="template",
        )

    def _report(self):
        report = make_report(repo_name="pinky")
        report.scores = [
            DimensionScore(
                dimension="compliance", score=40, max_score=100,
                findings=[Finding(
                    category="audit", severity=Severity.high,
                    description="No audit logging", recommendation="Add audit",
                )],
            ),
        ]
        return report

    async def test_match_skips_cooled_skill(self, tmp_path: Path):
        store = await make_store()
        for _ in range(2):
            await store.record_skill_outcome(
                "app-audit-logging", "pinky", "rejected",
                "clear-evidence: root only",
            )
        engine = SkillEngine(tmp_path, platform=offline_context())
        engine.skills = [self._skill()]
        report = self._report()
        cooled = {c["skill_name"] for c in await store.list_cooled_skills("pinky")}
        assert "app-audit-logging" in cooled
        assert engine.match(report, cooled_skills=cooled) == []
        assert engine.skill_for_category("audit", cooled_skills=cooled) is None

    async def test_run_all_skips_cooled_and_logs(self, tmp_path: Path):
        store = await make_store()
        for _ in range(2):
            await store.record_skill_outcome(
                "app-audit-logging", "pinky", "rejected",
                "clear-evidence: root only",
            )
        engine = SkillEngine(tmp_path, platform=offline_context())
        engine.skills = [self._skill()]
        report = self._report()
        files = await asyncio.to_thread(
            engine.run_all, report, store=store, llm_client=None,
            loop=asyncio.get_running_loop(),
        )
        assert files == []
        events = await store.list_events_by_agent("skill-engine")
        assert any(e["action"] == "skipped-cooldown" for e in events)

    async def test_prior_reject_reasons_reach_llm_prompt(self, tmp_path: Path):
        store = await make_store()
        await store.record_skill_outcome(
            "app-audit-logging", "pinky", "rejected",
            "clear-evidence: audit module at repo root only",
        )
        engine = SkillEngine(tmp_path, platform=offline_context())
        skill = self._skill()
        skill.mode = "llm"
        skill.body = "# no template"
        engine.skills = [skill]
        report = self._report()

        class _Fake:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def _chat(self, system: str, user: str, max_tokens: int | None = None, **_k) -> str:
                self.calls.append((system, user))
                return (
                    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\ndata: {}\n"
                )

        fake = _Fake()
        files = await asyncio.to_thread(
            engine.run_all, report, store=store, llm_client=fake,
            loop=asyncio.get_running_loop(),
        )
        assert len(files) == 1
        _, user = fake.calls[0]
        assert "Prior attempts" in user
        assert "repo root" in user


class TestClearEvidenceRecordsSkillOutcome:
    async def test_clear_evidence_refusal_writes_skill_effectiveness(self):
        store = await make_store()
        report = make_report(repo_name="sim-learn-app")
        report.scores = [
            DimensionScore(
                dimension="security", score=50, max_score=100,
                findings=[Finding(
                    category="container", severity=Severity.high,
                    description="using :latest", recommendation="pin",
                )],
            ),
        ]
        aid = await store.save(report)
        files = [{
            "category": "codechange",
            "path": "patch-Dockerfile",
            "target_path": "Dockerfile",
            "content": "FROM ubi9/python-312:latest\nUSER 1001\n",
            "description": "bad pin",
            "skill_name": "containerfile",
        }]

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.auto_delivery.validate_and_fix_manifests",
                   return_value={"files": files, "clean": True, "iterations": []}), \
             patch("agentit.portal.auto_delivery._dry_run_check",
                   return_value=([], [], set(), [])), \
             patch("agentit.portal.auto_delivery._check_properties", return_value=[]), \
             patch("agentit.portal.github_pr.commit_to_infra_repo"), \
             patch("agentit.portal.github_pr.create_source_patch_pr"), \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            result = await auto_validate_and_deliver(
                store=store, report=report, app_name=report.repo_name, namespace="ns",
                assessment_id=aid, actor="auto-delivery",
                files=files,
                orchestration={},
                target_findings=[("container", "using :latest")],
            )

        assert result["status"] == "needs_attention"
        reasons = await store.get_recent_skill_reject_reasons(
            "sim-learn-app", "containerfile", limit=5,
        )
        assert reasons
        assert any("clear-evidence" in (r or "").lower() for r in reasons)


class TestNoBlindRedispatch:
    async def test_identical_rejects_escalate_instead_of_redispatch(self):
        store = await make_store()
        report = make_report(repo_name="esc-same-reason")
        report.scores = [
            DimensionScore(
                dimension="security", score=60, max_score=100,
                findings=[Finding(
                    category="network", severity=Severity.medium,
                    description="Missing NetworkPolicy", recommendation="Add one",
                )],
            ),
        ]
        aid = await store.save(report)
        target = finding_key("network", "Missing NetworkPolicy")
        for _ in range(2):
            await store.record_skill_outcome(
                "network-policy", "esc-same-reason", "rejected",
                "finding still present after merge (delivery x)",
            )

        with patch(
            "agentit.portal.delivery.redispatch_finding_fix",
            new_callable=AsyncMock,
        ) as mock_redispatch:
            result = await handle_confirmed_finding_failure(
                store, report, aid, "esc-same-reason", target,
            )

        assert result["action"] == "escalated"
        assert result.get("reason") == "identical_reject_reason"
        mock_redispatch.assert_not_called()
        events = await store.list_events()
        assert any(e["action"] == "skill-learner-queued" for e in events)


class TestSkillLearnerFastPath:
    async def test_flagged_includes_identical_reject_skills(self, tmp_path: Path):
        from agentit.watchers.skill_learner import SkillLearner

        store = await make_store()
        for i in range(SKILL_COOLDOWN_SAME_REASON):
            await store.record_skill_outcome(
                "containerfile", f"app-{i}", "rejected",
                "clear-evidence: destructive rewrite",
            )
        # Below generic min_count=5 — must still surface via fast path.
        learner = SkillLearner(
            publisher=AsyncMock(),
            store=store,
            skills_dir=tmp_path / "skills",
        )
        flagged = await learner._get_flagged_skills()
        assert any(f["skill"] == "containerfile" for f in flagged)
