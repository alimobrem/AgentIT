# Proposal: Add cross-onboarding stack-signature detection to auto-trigger `agentit learn-for`

> Proposed by AgentIT's capability-scout — see docs/self-improvement-for-agentit.md

**Risk:** low

## Gap

README.md's Self-improvement loop section explicitly documents a 'Documented future idea (not built)': auto-triggering `agentit learn-for` when a new/uncommon stack pattern is detected 3+ times across onboardings. The detection logic for cross-onboarding stack signatures does not exist. The doc_gaps evidence confirms this is a real, human-written admission of missing functionality, not an invented gap.

## Evidence

From docs/self-improvement-for-agentit.md line 178 (anchor: 'Documented future idea'): "README's own 'Documented future idea (not built)' (README.md's Self-improvement loop section, tier 2 bullet): 'auto-triggering `agentit learn-for` when a new/uncommon stack pattern is detected 3+ times across onboardings would need new cross-onboarding stack-signature detection logic — flagged here as a real idea, deliberately not built.'" Further corroborated at line 182: "a query over `assessments` (via a small new read, or reusing `get_score_history`-style grouping) for `stack` field values shows, say, 4 onboarded apps in the last 30 days all detected `Go` + `PostgreSQL` + no matching existing skill triggered a `learn-for` call for that combination."

## Suggested target files

- `src/agentit/capability_scout.py`
- `tests/test_stack_signature_detection.py`

## Suggested change

In `capability_scout.py`, add a small function `detect_repeated_stack_patterns(assessments, threshold=3)` that: (1) reads the `stack` field from each assessment record, (2) counts occurrences of each unique stack signature (e.g. frozenset of stack components) across all assessments within the last 30 days, (3) returns a list of stack signatures whose count meets or exceeds `threshold` and for which no existing skill already covers that signature. This function can then be called from `gather_evidence()` to surface these signatures as actionable signals, enabling the caller to trigger `agentit learn-for` for each. No other code is modified; no refactoring of unrelated logic.

## Test plan

In `tests/test_stack_signature_detection.py`: (1) Assert that `detect_repeated_stack_patterns` returns an empty list when fewer than `threshold` assessments share a stack signature. (2) Assert that it returns the correct stack signature when exactly `threshold` assessments share the same stack components within the 30-day window. (3) Assert that stack signatures already covered by an existing skill are excluded from the returned list. (4) Assert that assessments older than 30 days are not counted toward the threshold. (5) Assert that the function handles an empty assessments list without error.
