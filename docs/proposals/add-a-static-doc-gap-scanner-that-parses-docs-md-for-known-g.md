# Proposal: Add a static doc-gap scanner that parses docs/*.md for 'Known gap'/'Deliberately deferred'/'Documented future idea' markers and surfaces them as structured candidate items

> Proposed by AgentIT's capability-scout — see docs/self-improvement-for-agentit.md

**Risk:** low

## Gap

AgentIT's self-improvement loop reads only structured store data; it never parses its own prose documentation. The docs explicitly admit this: 'nothing today parses this repo's own docs as a data source; skill_learner.py only ever reads structured store data, never prose.' The highest-precision signal source — human-written gap admissions in docs/*.md — is therefore completely unused by the loop.

## Evidence

docs/self-improvement-for-agentit.md line 169: 'The doc-gap scanner (grepping docs/*.md for "Known gap"/"Deliberately deferred"/"Documented future idea" sections and turning them into structured candidate items) — nothing today parses this repo's own docs as a data source; skill_learner.py only ever reads structured store data, never prose.' Also line 77: 'docs/*.md "Known gap" / "Deliberately deferred" / "Documented future idea" sections | New: a small static scan, not a store query | Explicit, human-written admissions of missing functionality — this is the single highest-precision signal available and should be weighted first'

## Suggested target files

- `src/agentit/doc_gap_scanner.py`
- `tests/test_doc_gap_scanner.py`

## Suggested change

Create src/agentit/doc_gap_scanner.py with a single small function scan_doc_gaps(docs_dir) that walks docs/*.md files, finds lines containing the marker phrases ('Known gap', 'Deliberately deferred', 'Documented future idea'), and returns a list of dicts with keys {file, line_no, anchor, text}. No changes to any existing file; this is a net-new, self-contained module. A separate caller (e.g. the improvement loop) can import and use it when ready — this PR only adds the scanner and its tests.

## Test plan

tests/test_doc_gap_scanner.py will: (1) create a temporary directory with two synthetic .md files — one containing each of the three marker phrases on known line numbers, one containing no markers — then assert scan_doc_gaps() returns exactly the expected list of dicts with correct file, line_no, anchor, and text fields; (2) assert that a file with no markers contributes zero results; (3) assert that the function returns an empty list when the docs directory is empty; (4) assert that the 'anchor' field is set to whichever marker phrase was matched.
