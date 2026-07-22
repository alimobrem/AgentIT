"""Interfaces layer: import direction and score aggregate (ADR 0006)."""
from __future__ import annotations

from types import SimpleNamespace

from agentit.interfaces.breakers import CircuitBreaker, kube_breaker, llm_breaker
from agentit.interfaces.score_aggregate import weighted_overall_score
from agentit.portal import helpers as portal_helpers
from conftest import make_report


def test_breakers_live_in_interfaces_not_only_portal():
    assert llm_breaker is portal_helpers.llm_breaker
    assert kube_breaker is portal_helpers.kube_breaker
    assert isinstance(llm_breaker, CircuitBreaker)


def test_weighted_overall_score_criticality_shifts_overall():
    dims = [
        SimpleNamespace(dimension="security", score=80),
        SimpleNamespace(dimension="compliance", score=60),
    ]
    critical = weighted_overall_score(dims, "critical")
    low = weighted_overall_score(dims, "low")
    assert critical != low
    assert 0 < critical <= 100


def test_assessment_report_overall_uses_interfaces_aggregate():
    report = make_report(repo_name="iface-app", criticality="critical")
    assert report.score_version >= 2
    assert report.overall_score > 0
