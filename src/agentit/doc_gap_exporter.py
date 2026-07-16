"""Scan docs/*.md for explicit 'not built' anchors and emit structured audit events."""
import re
from pathlib import Path
from typing import Iterator

ANCHORS = [
    (r"\*\*Explicitly not built:\*\*\s*(.*)", "not built"),
    (r"Known gap[:\s]+\s*(.*)", "known gap"),
    (r"Deliberately deferred[:\s]+\s*(.*)", "deferred"),
]


def scan_docs(docs_dir="docs") -> list:
    """Return list of gap dicts found in docs_dir/*.md."""
    results = []
    for md in sorted(Path(docs_dir).glob("*.md")):
        with md.open(encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                for pattern, anchor in ANCHORS:
                    m = re.search(pattern, line)
                    if m:
                        results.append({
                            "file": md.name,
                            "line_no": line_no,
                            "anchor": anchor,
                            "text": m.group(1).strip(),
                        })
                        break
    return results
