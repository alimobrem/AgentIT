import logging
import pytest
from agentit.skill_effectiveness_alerter import check_skill_effectiveness


def test_zero_rate_triggers_warning(caplog):
    stats = [{"skill": "resourcequota", "approval_rate": 0.0, "total": 5}]
    with caplog.at_level(logging.WARNING):
        result = check_skill_effectiveness(stats)
    assert result == ["resourcequota"]
    assert "resourcequota" in caplog.text


def test_positive_rate_no_warning(caplog):
    stats = [{"skill": "foo", "approval_rate": 0.8, "total": 5}]
    with caplog.at_level(logging.WARNING):
        result = check_skill_effectiveness(stats)
    assert result == []
    assert "foo" not in caplog.text


def test_below_threshold_skipped(caplog):
    stats = [{"skill": "bar", "approval_rate": 0.0, "total": 2}]
    with caplog.at_level(logging.WARNING):
        result = check_skill_effectiveness(stats, threshold=3)
    assert result == []
