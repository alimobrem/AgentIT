"""Tests for portal/content_diff.py -- the line-level diff between a
generated file's original and human-edited content that backs the
edit-before-apply flow's diff view.
"""
from __future__ import annotations

from agentit.portal.content_diff import diff_lines


def test_no_change_returns_all_context_rows():
    text = "line one\nline two\nline three"
    rows = diff_lines(text, text)
    assert all(r["type"] == "context" for r in rows)
    assert [r["text"] for r in rows] == ["line one", "line two", "line three"]


def test_added_line_is_tagged_add():
    original = "a\nb\n"
    edited = "a\nb\nc\n"
    rows = diff_lines(original, edited)
    add_rows = [r for r in rows if r["type"] == "add"]
    assert add_rows == [{"type": "add", "text": "c"}]


def test_removed_line_is_tagged_remove():
    original = "a\nb\nc\n"
    edited = "a\nc\n"
    rows = diff_lines(original, edited)
    remove_rows = [r for r in rows if r["type"] == "remove"]
    assert remove_rows == [{"type": "remove", "text": "b"}]


def test_replaced_line_produces_remove_then_add():
    original = "kind: ConfigMap\n"
    edited = "kind: Secret\n"
    rows = diff_lines(original, edited)
    assert rows == [
        {"type": "remove", "text": "kind: ConfigMap"},
        {"type": "add", "text": "kind: Secret"},
    ]


def test_empty_original_is_all_additions():
    rows = diff_lines("", "new content\nmore\n")
    assert rows == [
        {"type": "add", "text": "new content"},
        {"type": "add", "text": "more"},
    ]
