"""Tests for skill_effectiveness_monitor."""
import pytest
from agentit.skill_effectiveness_monitor import check_skill_effectiveness


def test_resourcequota_flagged():
    result = check_skill_effectiveness([{"skill": "resourcequota", "weighted_rate": 0.0, "total": 5}])
    assert len(result) == 1
    assert result[0]["skill"] == "resourcequota"


def test_healthy_skill_not_flagged():
    result = check_skill_effectiveness([{"skill": "ok_skill", "weighted_rate": 0.5, "total": 10}])
    assert result == []


def test_empty_input():
    assert check_skill_effectiveness([]) == []


def test_threshold_boundary():
    result = check_skill_effectiveness([{"skill": "edge", "weighted_rate": 0.0, "total": 3}], threshold=0.0)
    assert len(result) == 0  # 0.0 < 0.0 is False


def test_warning_keys():
    result = check_skill_effectiveness([{"skill": "resourcequota", "weighted_rate": 0.0, "total": 5}])
    assert {"skill", "weighted_rate", "action"} <= set(result[0].keys())
