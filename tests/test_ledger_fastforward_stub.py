"""Tests for ledger_fastforward_stub."""
import pytest
from agentit.ledger_fastforward_stub import check_fastforward_gap


def test_warning_raised():
    with pytest.warns(UserWarning, match="fast-forward") as rec:
        result = check_fastforward_gap()
    assert any("not built" in str(w.message).lower() for w in rec)


def test_log_emitted(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="agentit.ledger_fastforward_stub"):
        check_fastforward_gap()
    assert any("fast-forward" in r.message for r in caplog.records)
    assert any("ledger-design-spec" in r.message for r in caplog.records)


def test_return_value():
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = check_fastforward_gap()
    assert result["gap"] == "predictive fast-forward preview"
    assert result["doc"] == "docs/ledger-design-spec.md"
    assert result["line_no"] == 193
