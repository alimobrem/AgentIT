from __future__ import annotations

from datetime import datetime, timezone

from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    RemediationItem,
    Severity,
    StackInfo,
)
from agentit.portal.store import AssessmentStore


def _make_store() -> AssessmentStore:
    return AssessmentStore(db_path=":memory:")


def _make_report(
    repo_name: str = "test-repo",
    score: int = 50,
    assessed_at: datetime | None = None,
) -> AssessmentReport:
    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=assessed_at or datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        stack=StackInfo(
            languages=[Language(name="python", version="3.12", file_count=10, percentage=100.0)],
            frameworks=[],
            databases=[],
            runtimes=[],
            package_managers=["pip"],
        ),
        architecture=ArchitectureInfo(
            service_count=1,
            architecture_style="monolith",
            has_api=True,
            api_style="REST",
            external_dependencies=[],
            auth_mechanism=None,
        ),
        scores=[
            DimensionScore(
                dimension="security",
                score=score,
                max_score=100,
                findings=[
                    Finding(
                        category="test",
                        severity=Severity.info,
                        description="placeholder",
                        recommendation="n/a",
                    )
                ],
            ),
        ],
        criticality="low",
        summary="test",
        remediation_plan=[
            RemediationItem(
                priority=1,
                dimension="security",
                description="fix it",
                estimated_effort="1h",
                agent_responsible="human",
            )
        ],
    )


# ── Events ──────────────────────────────────────────────────────────────


def test_log_and_list_events():
    store = _make_store()
    eid = store.log_event("bot", "deploy", "my-app", "info", "deployed v1")
    assert eid

    events = store.list_events()
    assert len(events) == 1
    assert events[0]["agent_id"] == "bot"
    assert events[0]["action"] == "deploy"
    assert events[0]["target_app"] == "my-app"
    assert events[0]["summary"] == "deployed v1"

    # filter by target_app
    store.log_event("bot", "scan", "other-app", "warning", "drift detected")
    filtered = store.list_events(target_app="other-app")
    assert len(filtered) == 1
    assert filtered[0]["target_app"] == "other-app"

    # all events
    assert len(store.list_events()) == 2


# ── Assessment history ──────────────────────────────────────────────────


def test_list_history_returns_multiple_assessments():
    store = _make_store()
    r1 = _make_report(score=40, assessed_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    r2 = _make_report(score=60, assessed_at=datetime(2025, 2, 1, tzinfo=timezone.utc))
    store.save(r1)
    store.save(r2)

    history = store.list_history("https://github.com/org/test-repo")
    assert len(history) == 2
    # ordered ascending by date
    assert history[0]["overall_score"] == 40.0
    assert history[1]["overall_score"] == 60.0


def test_get_trend_shows_delta():
    store = _make_store()
    r1 = _make_report(score=40, assessed_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    r2 = _make_report(score=70, assessed_at=datetime(2025, 2, 1, tzinfo=timezone.utc))
    store.save(r1)
    store.save(r2)

    trend = store.get_trend("https://github.com/org/test-repo")
    assert trend["current_score"] == 70.0
    assert trend["previous_score"] == 40.0
    assert trend["delta"] == 30.0
    assert trend["assessments_count"] == 2

    # empty repo
    empty = store.get_trend("https://github.com/org/nonexistent")
    assert empty["assessments_count"] == 0
    assert empty["delta"] is None


# ── Gates ────────────────────────────────────────────────────────────────


def test_create_and_resolve_gate():
    store = _make_store()
    aid = store.save(_make_report())
    gid = store.create_gate(aid, "security", "Critical vuln found")
    assert gid

    gates = store.list_gates()
    assert len(gates) == 1
    assert gates[0]["gate_type"] == "security"
    assert gates[0]["status"] == "pending"

    ok = store.resolve_gate(gid, "approved", "alice")
    assert ok is True

    # pending list is now empty
    assert store.list_gates(status="pending") == []
    approved = store.list_gates(status="approved")
    assert len(approved) == 1
    assert approved[0]["resolved_by"] == "alice"

    # resolving again returns False (already resolved)
    assert store.resolve_gate(gid, "rejected", "bob") is False


def test_list_gates_filters_by_status():
    store = _make_store()
    aid = store.save(_make_report())
    g1 = store.create_gate(aid, "compliance", "Missing SBOM")
    g2 = store.create_gate(aid, "security", "No network policy")

    store.resolve_gate(g1, "approved", "carol")

    assert len(store.list_gates(status="pending")) == 1
    assert len(store.list_gates(status="approved")) == 1
    assert store.list_gates(status="pending")[0]["id"] == g2
