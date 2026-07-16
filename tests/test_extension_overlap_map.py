"""Tests for extension_overlap_map.build_overlap_map."""
import json
import logging
import pytest
from agentit.extension_overlap_map import build_overlap_map


FINDINGS = [{"id": "F1"}, {"id": "F2"}, {"id": "F3"}]
CAPS = [
    {"name": "cap_a", "finding_ids": ["F1", "F2"]},
    {"name": "cap_b", "finding_ids": ["F1"]},
]


def test_two_caps_cover_finding():
    m = build_overlap_map(FINDINGS, CAPS)
    assert set(m["F1"]) == {"cap_a", "cap_b"}
    assert m["F2"] == ["cap_a"]


def test_uncovered_finding_empty_list_and_warning(caplog):
    with caplog.at_level(logging.WARNING):
        m = build_overlap_map(FINDINGS, CAPS)
    assert m["F3"] == []
    assert any("F3" in r.message for r in caplog.records)


def test_result_json_serialisable():
    m = build_overlap_map(FINDINGS, CAPS)
    assert isinstance(json.dumps(m), str)


def test_empty_inputs_return_empty_dict():
    assert build_overlap_map([], []) == {}
    assert build_overlap_map([], CAPS) == {}


def test_no_keyerror_on_unknown_cap_finding_ref():
    caps_with_unknown = [{"name": "cap_x", "finding_ids": ["UNKNOWN"]}]
    m = build_overlap_map(FINDINGS, caps_with_unknown)
    assert "UNKNOWN" not in m
