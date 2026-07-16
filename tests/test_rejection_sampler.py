"""Tests for RejectionSampler."""
import pytest
from unittest.mock import patch
from src.agentit.rejection_sampler import RejectionSampler


def test_record_and_get_samples():
    rs = RejectionSampler()
    for _ in range(5):
        rs.record("resourcequota", "quota_exceeded", {})
    samples = rs.get_samples("resourcequota")
    assert len(samples) == 5
    for s in samples:
        assert {"skill", "reason", "metadata", "timestamp"} <= s.keys()
        assert s["skill"] == "resourcequota"
        assert s["reason"] == "quota_exceeded"


def test_warning_emitted_on_high_rejection_rate():
    rs = RejectionSampler()
    with patch("src.agentit.rejection_sampler.log") as mock_log:
        for _ in range(5):
            rs.record("resourcequota", "quota_exceeded", {})
        assert mock_log.warning.called
        call_kwargs = mock_log.warning.call_args
        assert call_kwargs[0][0] == "high_rejection_rate"


def test_deque_cap_evicts_oldest():
    rs = RejectionSampler(cap=200)
    for i in range(201):
        rs.record("resourcequota", f"reason_{i}", {})
    samples = rs.get_samples("resourcequota")
    assert len(samples) == 200
    # oldest entry (reason_0) should be evicted
    assert all(s["reason"] != "reason_0" for s in samples)
