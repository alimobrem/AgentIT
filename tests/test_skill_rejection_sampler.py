"""Tests for skill_rejection_sampler."""
import json, logging, os, tempfile, pytest
from src.agentit.skill_rejection_sampler import record_rejection, summarize_rejections

def tmp(tmp_path):
    return str(tmp_path / "rej.jsonl")

def test_record_writes_valid_json(tmp_path):
    p = tmp(tmp_path)
    record_rejection("resourcequota", {"ns": "default"}, reason="quota_exceeded", path=p)
    lines = open(p).readlines()
    assert len(lines) == 1
    r = json.loads(lines[0])
    assert r["skill"] == "resourcequota"
    assert r["reason"] == "quota_exceeded"
    assert "timestamp" in r

def test_summarize_aggregates(tmp_path):
    p = tmp(tmp_path)
    record_rejection("resourcequota", {}, reason="quota_exceeded", path=p)
    record_rejection("resourcequota", {}, reason="quota_exceeded", path=p)
    record_rejection("resourcequota", {}, reason="limit_missing", path=p)
    counts = summarize_rejections("resourcequota", path=p)
    assert counts["quota_exceeded"] == 2
    assert counts["limit_missing"] == 1

def test_low_approval_warns(tmp_path, caplog):
    p = tmp(tmp_path)
    with caplog.at_level(logging.WARNING, logger="src.agentit.skill_rejection_sampler"):
        record_rejection("resourcequota", {}, reason="quota_exceeded", path=p, approval_rate=0.17, threshold=0.20)
    assert any("low-effectiveness" in m for m in caplog.messages)

def test_file_created_if_missing(tmp_path):
    p = tmp(tmp_path)
    assert not os.path.exists(p)
    record_rejection("resourcequota", {}, path=p)
    assert os.path.exists(p)
