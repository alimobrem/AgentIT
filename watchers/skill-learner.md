---
name: skill-learner
mode: LLM polling
interval: 24 hours
code_ref: agentit.watchers.skill_learner:SkillLearner
description: "Researches recent CVEs via LLM and drafts new skills (status: draft) for human review — requires an LLM connection"
---

# Skill Learner

## What This Watcher Does
Researches recent CVEs via LLM and drafts new skills (`status: draft`)
for human review — see `watchers/skill_learner.py`. Requires an LLM
connection (`ANTHROPIC_API_KEY`/`ANTHROPIC_VERTEX_PROJECT_ID`).

## Code Reference
`code_ref` (`agentit.watchers.skill_learner:SkillLearner`) is a
`module:ClassName` string recorded here for registration purposes only —
`watchers/__init__.py`'s registration/heartbeat wiring (`record_tick`,
`sleep_with_heartbeat`) is unchanged by this file's existence; only the
*listing* of which watchers exist moved from a Python list literal
(`agents/capabilities.py`'s old `WATCHER_AGENTS`) to this Markdown file.

## Verification
`agentit.agents.capabilities.WATCHER_AGENTS` includes an entry with
`name == "skill-learner"` whose `mode`/`interval`/`description` match
this file's frontmatter exactly — see `tests/test_watcher_registration.py`.
