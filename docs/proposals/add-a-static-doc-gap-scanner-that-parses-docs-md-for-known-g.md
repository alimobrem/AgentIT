# Proposal: Add a static doc-gap scanner that parses docs/*.md for 'Known gap'/'Deliberately deferred'/'Documented future idea' markers and surfaces them as structured candidate items

> Proposed by AgentIT's capability-scout — see docs/self-improvement-for-agentit.md

**Risk:** low

## Gap

AgentIT's self-improvement loop reads only structured store data; it never parses its own prose documentation. The docs explicitly admit this: 'nothing today parses this repo's own docs as a data source; skill_learner.py only ever reads structured store data, never prose.' The highest-precision signal source — human-written admissions of missing functionality tagged with known sentinel phrases — is therefore never consumed by the loop.

## Evidence

docs/self-improvement-for-agentit.md line 169: 'The doc-gap scanner (grepping docs/*.md for "Known gap"/"Deliberately deferred"/"Documented future idea" sections and turning them into structured candidate items) — nothing today parses this repo's own docs as a data source; skill_learner.py only ever reads structured store data, never prose.' docs/self-improvement-for-agentit.md line 77: 'New: a small static scan, not a store query | Explicit, human-written admissions of missing functionality — see the worked example below; this is the single highest-precision signal available and should be weighted first'

## Suggested target files

- `src/agentit/doc_gap_scanner.py`
- `tests/test_doc_gap_scanner.py`

## Suggested change

Create src/agentit/doc_gap_scanner.py with a single public function scan_doc_gaps(docs_dir: str) -> list[dict] that walks docs/*.md files, finds lines containing any of the three sentinel phrases ('Known gap', 'Deliberately deferred', 'Documented future idea'), and returns a list of dicts with keys {file, line_no, anchor, text} — the same shape already used in the doc_gaps signal shown in this cycle's evidence. No existing files are modified; this is a pure addition. The function is intentionally small: open each .md file, iterate lines, match sentinels, collect hits, return list.

## Test plan

tests/test_doc_gap_scanner.py creates a temporary directory with two synthetic .md files: one containing a 'Known gap' line and a 'Deliberately deferred' line, one containing no sentinel phrases. It asserts: (1) scan_doc_gaps returns exactly 2 items for that temp dir; (2) each item has keys file, line_no, anchor, text; (3) the anchor field matches the sentinel phrase found; (4) a docs dir with no .md files returns an empty list; (5) a line containing 'Documented future idea' is also captured correctly.
