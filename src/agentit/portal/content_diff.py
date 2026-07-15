"""Line-level diff between a generated file's original and human-edited
content -- the concrete "diff between generated and applied content" the
README's Known Gap callout named as missing (see docs/self-improvement-for-
agentit.md and docs/unified-apply-flow.md, both of which cite this exact
gap). There is no pre-existing diff-rendering convention elsewhere in this
codebase to reuse: ``assessment_diff.py``'s ``AssessmentDiff`` and
``skill_inventory.py``'s ``InventoryDiff`` are structural diffs over Pydantic
models (score deltas, skill add/remove sets), not line-level text diffs, so
this is a new, small, dedicated module -- mirroring the pattern those two
files already establish (one small dedicated module per diff *shape*,
consistent with `_sanitize_name`/`validate_manifest` living in `agents/base.py`
rather than being duplicated per caller).
"""
from __future__ import annotations

import difflib


def diff_lines(original: str, edited: str) -> list[dict]:
    """Line-level diff, tagged for template rendering.

    Each row is ``{"type": "context" | "add" | "remove", "text": <line>}``
    with no trailing newline. Deliberately returns tagged rows instead of a
    unified-diff text blob -- so ``onboard_results.html`` can color add/
    remove/context lines via CSS classes (this repo's own "never use inline
    styles" convention) instead of the template re-parsing +/-/space prefixes
    out of monospace text.

    Uses ``difflib.SequenceMatcher``'s opcodes -- the same algorithm
    ``difflib.unified_diff`` builds on internally -- rather than hand-rolling
    a line-matching algorithm.
    """
    original_lines = original.splitlines()
    edited_lines = edited.splitlines()
    matcher = difflib.SequenceMatcher(a=original_lines, b=edited_lines, autojunk=False)
    rows: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            rows.extend({"type": "context", "text": line} for line in original_lines[i1:i2])
        elif tag == "delete":
            rows.extend({"type": "remove", "text": line} for line in original_lines[i1:i2])
        elif tag == "insert":
            rows.extend({"type": "add", "text": line} for line in edited_lines[j1:j2])
        elif tag == "replace":
            rows.extend({"type": "remove", "text": line} for line in original_lines[i1:i2])
            rows.extend({"type": "add", "text": line} for line in edited_lines[j1:j2])
    return rows
