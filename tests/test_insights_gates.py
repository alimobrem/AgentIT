"""Insights' "Pending Gates" stat card must reflect real, current gate
activity -- not a blind total that can be inflated by a leftover pending
row of a gate type no code path can create anymore (Direct Apply's
`cluster-conflict-review`, per routes/gates.py's own comment on it), and
not something that needs a follow-up edit here every time a gate type is
retired elsewhere (e.g. if `cluster-admin-review` is removed next).

See routes/insights.py's `_is_live_gate_type` / `_LIVE_GATE_TYPES`.
"""
from __future__ import annotations

from conftest import make_report


def _pending_gates_value(html: str) -> str:
    """Pull the "Pending Gates" stat card's rendered number out of Insights'
    raw HTML -- the same split-on-marker style test_portal.py's other
    stat/row assertions already use, rather than pulling in an HTML parser
    dependency just for this."""
    after_label = html.split("Pending Gates</div>", 1)[1]
    after_open_tag = after_label.split('<div class="stat-value', 1)[1].split(">", 1)[1]
    return after_open_tag.split("<", 1)[0].strip()


async def test_pending_gates_excludes_a_stale_dead_gate_type(portal_client):
    """A leftover pending `cluster-conflict-review` gate (Direct Apply was
    removed entirely; this gate type can no longer be created by any code
    path -- routes/gates.py) must not inflate the fleet-wide count, even
    though the underlying `gates` table row still literally exists."""
    client, store, assessment_id = portal_client
    await store.create_gate(assessment_id, "cluster-conflict-review", "Stale gate from before Direct Apply was removed.")

    resp = await client.get("/insights")
    assert resp.status_code == 200
    assert _pending_gates_value(resp.text) == "0"


async def test_pending_gates_counts_every_currently_live_gate_type(portal_client):
    """Seeded active-gate scenario: one pending gate of each gate type a
    real code path can still create today -- the cross-app elevated-RBAC
    gate (`cluster-admin-review`), the literal GitHub-PR-merge gate
    (`gitops-pr-pending`), AutoMode's own gate, an SLO-breach rollback
    gate, the dispatcher's per-category fix-review gate, and the Phase 4
    escalation gate -- must all still be counted. This is genuine, current
    gate activity; none of it is stale, so none of it should be dropped."""
    client, store, assessment_id = portal_client
    live_gate_types = [
        "cluster-admin-review",
        "gitops-pr-pending",
        "auto-mode-review",
        "rollback-review",
        "finding-security",
        "finding-unresolved-escalation",
    ]
    apps = []
    for i, gate_type in enumerate(live_gate_types):
        aid = assessment_id if i == 0 else await store.save(make_report(repo_name=f"live-gate-app-{i}"))
        apps.append(aid)
        await store.create_gate(aid, gate_type, f"Pending {gate_type} gate")

    resp = await client.get("/insights")
    assert resp.status_code == 200
    assert _pending_gates_value(resp.text) == str(len(live_gate_types))


async def test_pending_gates_mixes_live_and_dead_gate_types_correctly(portal_client):
    """The count is neither "all gates" nor "zero" -- it's specifically the
    live ones, alongside a genuinely dead one in the same fleet."""
    client, store, assessment_id = portal_client
    aid2 = await store.save(make_report(repo_name="mixed-gate-app-2"))
    await store.create_gate(assessment_id, "gitops-pr-pending", "Live: PR merge pending")
    await store.create_gate(aid2, "cluster-conflict-review", "Dead: stale conflict gate")

    resp = await client.get("/insights")
    assert resp.status_code == 200
    assert _pending_gates_value(resp.text) == "1"
    assert 'href="/ledger?needs_you=1"' in resp.text


async def test_pending_gates_zero_when_no_gates_pending(portal_client):
    client, store, assessment_id = portal_client
    resp = await client.get("/insights")
    assert resp.status_code == 200
    assert _pending_gates_value(resp.text) == "0"
    assert "text-success" in resp.text.split("Pending Gates</div>", 1)[1].split("</a>", 1)[0]


async def test_resolved_gate_does_not_count_as_pending(portal_client):
    """Only `status = 'pending'` gates count -- an approved/rejected one,
    live type or not, must not still show up in the fleet-wide total."""
    client, store, assessment_id = portal_client
    gate_id = await store.create_gate(assessment_id, "auto-mode-review", "needs review")
    await store.resolve_gate(gate_id, "approved", "tester")

    resp = await client.get("/insights")
    assert resp.status_code == 200
    assert _pending_gates_value(resp.text) == "0"
