"""Tests for doc_gap_exporter.scan_docs."""
import pytest
from src.agentit.doc_gap_exporter import scan_docs


def test_single_not_built(tmp_path):
    (tmp_path / "spec.md").write_text("# Title\n**Explicitly not built:** foo bar\n")
    results = scan_docs(docs_dir=tmp_path)
    assert len(results) == 1
    r = results[0]
    assert r["file"] == "spec.md"
    assert r["line_no"] == 2
    assert r["anchor"] == "not built"
    assert r["text"] == "foo bar"


def test_no_match_returns_empty(tmp_path):
    (tmp_path / "empty.md").write_text("# Nothing here\nJust prose.\n")
    assert scan_docs(docs_dir=tmp_path) == []


def test_multiple_entries_ordered(tmp_path):
    content = "line1\n**Explicitly not built:** alpha\nline3\n**Explicitly not built:** beta\n"
    (tmp_path / "multi.md").write_text(content)
    results = scan_docs(docs_dir=tmp_path)
    assert len(results) == 2
    assert results[0]["line_no"] < results[1]["line_no"]
    assert results[0]["text"] == "alpha"
    assert results[1]["text"] == "beta"
