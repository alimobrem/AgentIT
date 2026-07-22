# Agent Removal Readiness

> **SUPERSEDED outcome (2026-07-21).** Skills-primary simplification landed:
> cost/dependency Python agents and Per-Agent PRs are **gone**; optional
> **CodeChangeAgent** remains for source patches. `checks/` YAML is empty
> (`mode: detect` skills). Treat this audit as historical evidence for *why*
> that cut was safe — not as a live backlog of eight domain agents.
> Current model: [../README.md](../README.md) § The agent fleet.

Status of the skills-side work required before the hardcoded Python agents
(`src/agentit/agents/*.py`) can be deleted in favor of skills
(`skills/**/*.md`, loaded by `src/agentit/skill_engine.py`) plus the
data-driven check engine (`src/agentit/check_engine.py`, `checks/`).

This document is scoped to the skills side of that decision. It does not
revisit *whether* to remove the agents — that's decided — only whether the
skills side is actually ready to stand alone, and if not, exactly what's
missing.

**Audit date:** 2026-07-12/13 (pre–skills-primary). Verify against current
`git log` / README before treating anything here as final.

## 1. The 8 hard-blocker skills now have a working template fallback

These skills had `mode: llm` and **no** `` ```yaml `` template block in their
body at all — `SkillEngine.generate()` would return `[]` for every single
one of them whenever no LLM client was available or the LLM call failed.
That is the literal, cited blocker for removing the deterministic Python
fallback: if the LLM is ever down, these 8 categories of remediation would
silently produce nothing.

All 8 now have a template block, verified to:
- Load via `load_skill()` and pass `agentit test-skill <path>` (frontmatter
  complete; body contains Property/Constraints/Verification sections)
- Parse as valid YAML after the *real* substitution the engine performs
  (only `{{app_name}}` is replaced — verified with a script that mimics
  `SkillEngine.generate()`'s actual behavior, not the more forgiving
  "replace every `{{word}}`" sanitization `test_all_skills.py` uses for its
  own placeholder-agnostic checks)
- Produce the expected K8s `kind`(s) with `apiVersion`/`kind`/`metadata`
- Are covered by a dedicated regression test in
  `tests/test_skill_agent_parity.py`

| Skill | Reference Python logic read | Template now produces |
|---|---|---|
| `skills/security/network-policy.md` | `hardening.py::_generate_network_policy` | `NetworkPolicy` deny-all + allow-common (app port + DB ports 5432/3306/6379/27017 + DNS) |
| `skills/security/containerfile.md` | `hardening.py::_containerfile_for` (default branch) | `BuildConfig` (`build.openshift.io/v1`) with an inline UBI-minimal, non-root, `HEALTHCHECK`-bearing Dockerfile |
| `skills/cicd/tekton-pipeline.md` | `cicd.py::_generate_tekton_pipeline` | `Pipeline` + `PipelineRun` (`tekton.dev/v1`, not the deprecated `v1beta1` cicd.py uses) — clone → image-build → image-scan → sbom-generate → deploy |
| `skills/compliance/image-registry-policy.md` | `compliance.py::_generate_kyverno_policies` (registry rule) | Namespaced Kyverno `Policy`, `validationFailureAction: Audit`, matching the skill's own (pre-existing) constraints |
| `skills/retirement/decommission-plan.md` | `retirement.py::_generate_decommission_plan` | `ConfigMap` embedding a real 6-section markdown decommission checklist |
| `skills/release/release-runbook.md` | `release.py::_generate_release_runbook` | `ConfigMap` embedding a real release runbook (checklist, rollout steps, rollback triggers, escalation) |
| `skills/incident/runbook.md` | `incident.py::_generate_runbook` | `ConfigMap` embedding a real incident runbook (quick reference, triage, escalation matrix, recovery) |
| `skills/compliance/compliance-evidence.md` | `compliance.py::_generate_compliance_evidence` | `ConfigMap` embedding a control-to-evidence mapping table, explicitly marked "Unconfirmed" until LLM-verified against real generated manifests |

### Why a ConfigMap for the 4 narrative-document skills

`SkillEngine.generate()` always writes exactly one file per skill, named
`f"{app_name}-{skill.name}.yaml"` — that's hardcoded in the engine (out of
this workstream's scope; see `skill_engine.py`, not touched here). A skill
whose real output is markdown prose (a runbook, a decommission plan) has no
way to come out of that code path as a bare `.md` file. Wrapping the prose
in a `ConfigMap`'s `data` field turns it into a real, applyable, inspectable
K8s object — `kubectl get configmap ... -o jsonpath='{.data}'` — instead of
YAML-parse-erroring or silently truncating. This mirrors an existing pattern
already used elsewhere in this codebase for structured non-manifest data
(`incident.py::_generate_pagerduty_config`, `release.py::_generate_rollback_policy`
both wrap config data in a `ConfigMap` the same way). The LLM enhancement
path is untouched by this change — it still produces markdown directly when
an LLM is available; only the *template fallback* is ConfigMap-wrapped.

### A note on scope-limited templates

Some of these baselines are deliberately more generic than what the LLM
path (or the Python agent) can produce, because the engine only substitutes
`{{app_name}}` — there's no template-level access to "detected language,"
"detected databases," or "which findings actually fired." For example:
- `containerfile.md`'s template baseline uses a single UBI-minimal image
  regardless of language; the LLM enhancement still does the Go/Python/
  Java/Node-specific multi-stage build described in the skill's own "Key
  decisions" section.
- `compliance-evidence.md`'s template baseline lists what evidence *would*
  exist for each control area and marks every row "Unconfirmed" — it does
  not claim a control is actually met, because the template has no way to
  check that. That's a correctness improvement over just guessing "Met."

## 2. Bonus fix: chaos engineering had a skill-shaped gap with zero coverage

`ChaosAgent` (`agents/chaos.py`) generates pure LitmusChaos `ChaosEngine`
manifests — no narrative text, no source patches, 100% skill-shaped. But
there was **no `skills/chaos/` domain at all**. Per the 2026-07-12 code
review (`docs/code-review-2026-07-12.md`), `ChaosAgent` was also not
registered in `agents/capabilities.py`'s `AGENT_CLASSES`/`AGENT_CAPABILITIES`
at the time of that review — i.e. it was dead code even on the Python side.

**Registration status re-checked live during this audit:** as of this
session, `agents/capabilities.py` **does** register `"chaos"` in both
`AGENT_CLASSES` and `AGENT_CAPABILITIES` — the registration gap from the
code review appears to have already been closed by a concurrent sibling
workstream. Since `capabilities.py` is out of this workstream's scope
(it's under `agents/`), treat this as an observation, not something this
audit fixed or can promise stays true.

Regardless of the Python-side registration status, the skill-side gap was
real and is now closed: added `skills/chaos/pod-delete.md` and
`skills/chaos/network-latency.md`, both `mode: template` (no LLM needed —
chaos experiment definitions are standardized enough not to require one).
Both fix two concrete correctness bugs the code review flagged in
`chaos.py` rather than reproducing them:
- Uses the real LitmusChaos experiment name `pod-delete` (not the
  non-standard `pod-kill`) and the real env var `PODS_AFFECTED_PERC` (not
  the invented `KILL_COUNT`)
- Uses `labelSelector` for probe targeting, not a Kubernetes `fieldSelector`
  (which Litmus's `k8sProbe` schema doesn't accept)

These trigger on `chaos`/`resilience`/`resiliency`/`disruption`/
`availability`/`latency`/`network` — the `availability`/`disruption`
triggers deliberately overlap with the existing `ha_dr` dimension's checks
(`checks/ha_dr/pdb.yaml`, `checks/ha_dr/replicas.yaml`), so a report flagging
"no PodDisruptionBudget" or "no multi-replica deployment" now also
surfaces a chaos experiment to verify the redundancy claim, not just an HPA/
PDB manifest that asserts it.

Both are covered by dedicated tests in `tests/test_skill_agent_parity.py`
(`TestChaosSkillsUseCorrectLitmusSemantics`).

## 3. Coverage gaps that are real and were deliberately NOT forced into skills

Property-based skills generate K8s manifests from findings. Not everything
a Python agent produces fits that model. Forcing a bad fit (e.g. wrapping a
git diff in a ConfigMap) would be worse than admitting the gap. Three
categories don't fit, for different reasons:

### `CodeChangeAgent` — fundamentally not skill-shaped

`agents/codechange.py` generates **source-level patches to the application's
own repository** — a `Dockerfile`, a `.gitignore` append, a `healthz.py`
file, OpenTelemetry instrumentation snippets — and proposes them as a PR
against the app's repo. Skills generate K8s manifests to apply to a
*cluster*; they have no concept of "the application's source tree" at all,
and the skill engine has no PR/patch-application machinery.

**This is not a skill-vs-template problem — it's a different capability
entirely.** Removing `CodeChangeAgent` without a replacement means AgentIT
loses the ability to fix `.gitignore`, add health endpoints, or scaffold
OpenTelemetry instrumentation into an app's own code. If that capability is
still wanted post-removal, it needs to stay as a standalone tool (not
re-integrated as "just another agent" the removal is trying to delete), or
be explicitly accepted as a dropped feature. The code review also flagged
`codechange.py`'s finding-category filter and its deterministic-fix
dispatcher as already disagreeing with each other (dockerfile/container/
health fixes are unreachable dead code today) — so this agent's *current*
value, independent of the removal question, is already in doubt.

### `DependencyAgent` / `CostOptimizationAgent` — **removed (2026-07-21)**

Manifest outputs were already skill-covered (`skills/dependency/*`,
`skills/cost/*`). Narrative reports were dropped (low delivery value —
never PR candidates). Guidance lives in analyzer findings that trigger
those skills (e.g. infrastructure `resources` / security dependency
scanning recommendations). Python agents and Per-Agent PRs are gone;
Scan/`auto_delivery` remains the sole GitOps PR path. Optional
`codechange` stays as a source-patch path only.

## 4. Blocked on other workstreams (do not attempt — tracked here for visibility)

These are Phase 0 plumbing items in other subagents' exclusive scope
(`orchestrator.py`, `skill_engine.py`, `agents/capabilities.py`) that this
skills-side work depends on, but does not itself fix:

- **Orchestrator passing the LLM client to skills.** Observed as *already
  wired* in the current `orchestrator.py` (`OrchestratorAgent` builds an
  `LLMClient` when `ANTHROPIC_API_KEY`/`ANTHROPIC_VERTEX_PROJECT_ID` is set
  and passes it into `SkillEngine.run_all(..., llm_client=llm_client)`).
  Given concurrent edits, re-verify this is still true before relying on it.
- **Platform-gating pluralization fix in `skill_engine.py`.** The
  2026-07-12 code review flagged `has_api(kind.lower() + "s")` as broken for
  irregular plurals (`NetworkPolicy` → `networkpolicys` instead of
  `networkpolicies`; same for `Policy`/`Ingress`). This directly affects two
  of the 8 skills fixed here (`network-policy`, `image-registry-policy`) —
  under the buggy pluralization, both would be silently skipped by
  `generate()`'s output-kind gate on every real (or `offline_context()`)
  platform, because `has_api()` would never find them. **Observed as already
  fixed** in the current working tree (`_pluralize_kind()` with an
  `_IRREGULAR_KIND_PLURALS` map for `policy`/`networkpolicy`/`ingress`,
  verified empirically against `offline_context()` during this audit) — but
  this landed via a concurrent, uncommitted edit to a file outside this
  workstream's scope, so treat it as unconfirmed until it's actually
  committed and re-verify before depending on it.
- **Per-artifact skip logic.** Currently, `orchestrator.py`'s
  `skip_agents` logic is domain-level, not artifact-level: if *any* skill in
  a domain matches and produces output, the *entire* corresponding Python
  agent category is skipped — even artifacts that domain's skills don't yet
  cover. Concretely: if a `compliance` skill matches, `ComplianceAgent` is
  skipped entirely, even for any compliance artifact that has no skill
  equivalent yet. As of this audit, skill coverage across the domains
  audited in section 1 is now broad enough that this is less risky than it
  was, but it's still a coarser skip than "only skip what's actually
  covered," and it's not something this workstream can fix (it's
  `orchestrator.py`).

## 5. Test coverage added

`tests/test_skill_agent_parity.py` (new file, 18 tests) — the skill-engine
integration coverage `test_orchestrator.py` was cited as missing. Runs
`SkillEngine` directly (`platform=None`, `llm_client=None` — hermetic,
no cluster, no network, no LLM credentials) against a comprehensive
`AssessmentReport` fixture with findings spanning every skill domain, and
asserts:

- Each of the 8 formerly-LLM-only skills produces non-empty, valid-YAML,
  correctly-`kind`ed output via template fallback alone (the core fix)
- `{{app_name}}` is always substituted; no unsubstituted placeholder leaks
  into generated output for these 8 skills
- The 4 narrative skills' `ConfigMap` actually contains the expected
  markdown sections, not just "some string"
- Across the full fixture, every domain that used to have a Python-agent
  equivalent (`security`, `observability`, `cicd`, `compliance`,
  `infrastructure`, `cost`, `dependency`, `incident`, `release`,
  `retirement`) produces at least one skill file — i.e. skills now cover
  every domain the removal decision needs them to cover, `codechange`
  and `chaos` deliberately excluded (see sections 2 and 3)
- Every generated file across the full run is non-empty and parses as
  valid YAML
- The two new chaos skills fire on availability/resiliency findings and use
  correct LitmusChaos experiment names/fields

### A real bug this test coverage caught (and fixed)

Building the fixture and asserting real YAML-parseability (not the more
forgiving `test_all_skills.py`-style "replace every `{{word}}` with a
placeholder" sanitization) surfaced a genuine, pre-existing bug:
`SkillEngine.generate()` only ever substitutes `{{app_name}}`. Any other
`{{word}}` placeholder left **unquoted** as a bare YAML scalar value
(`image: {{image}}`) is not "manual placeholder text" — it's a YAML flow
mapping with an unhashable key (`{{...}}` opens a nested flow mapping), and
parsing it raises `yaml.constructor.ConstructorError`. This is not
recoverable at read time; the file this produces cannot be parsed by
anything, including `kubectl apply`.

This affected 5 pre-existing skill files (unrelated to the 8 required by
this task, discovered because the parity test's fixture happens to trigger
them) and was fixed with a minimal one-line quote per occurrence — no
behavior change, no field renamed, just making the literal placeholder text
a valid YAML string instead of invalid YAML syntax:

- `skills/release/rollout-patch.md` — `image: {{image}}` → `image: "{{image}}"`
- `skills/cicd/argo-rollout.md` — `image: {{image}}` → `image: "{{image}}"`
- `skills/dependency/dependency-cronjob.md` — `image: {{scanner_image}}` → quoted
- `skills/cost/cost-cronjob.md` — `image: {{cost_report_image}}` → quoted
- `skills/cicd/argocd-application.md` — `repoURL: {{git_url}}` and `namespace: {{namespace}}` → quoted

A repo-wide scan (mimicking the engine's real `{{app_name}}`-only
substitution against every `` ```yaml `` block in `skills/`) confirms no
other skill file has this problem as of this audit.

## 6. Bottom line

**Ready for removal**, on the skills side, for every domain a Python agent
currently owns except two explicitly-scoped exceptions:

| Domain | Skill coverage | Template-only baseline works without LLM | Parity test coverage |
|---|---|---|---|
| security | Yes (network-policy, containerfile, rbac, resource-limits, security-context, image-scan-task) | Yes | Yes |
| cicd | Yes (tekton-pipeline, argocd-application, argo-rollout) | Yes | Yes |
| compliance | Yes (kyverno-policies, sbom-task, audit-policy, image-registry-policy, compliance-evidence, compliance-cronjob) | Yes | Yes |
| infrastructure | Yes (hpa, pdb, resourcequota, limitrange, namespace) | Yes (pre-existing) | Yes |
| cost | Yes for manifests (vpa, cost-labels, cost-cronjob); **no** for the narrative cost-report | Yes for manifests | Yes for manifests |
| dependency | Yes for manifests (renovate/dependabot config, dependency-cronjob); **no** for the narrative dependency-report | Yes for manifests | Yes for manifests |
| incident | Yes (runbook, pagerduty-config, alertmanager-config) | Yes | Yes |
| release | Yes (release-runbook, analysis-template, rollout-patch, rollback-policy) | Yes | Yes |
| retirement | Yes (decommission-plan, cleanup-task, data-archive-job) | Yes | Yes |
| observability | Yes (service-monitor, grafana-dashboard, alerting-rules, otel-collector) | Yes (pre-existing) | Yes |
| chaos | Yes, newly added (pod-delete, network-latency) — not full parity with `chaos.py`'s cpu-stress/schedule experiments, but the two most common ones | Yes | Yes |
| codechange | **No — not skill-shaped.** See section 3. | N/A | N/A |

Honest caveats before anyone flips the switch:
1. The two "no" cells above (dependency-report, cost-report narrative text)
   are real, accepted feature gaps if `DependencyAgent`/`CostOptimizationAgent`
   are deleted outright — not a skills-side oversight, a genuine mismatch
   between what a template can do and what those reports need.
2. `codechange` has no replacement path at all in the skills model. If
   source-level patch generation is still wanted, it must survive removal
   as a standalone capability, not be deleted along with the other agents.
3. Everything in section 4 is someone else's in-flight work this audit
   observed but does not own. Re-verify platform-gating and LLM-passthrough
   behavior against `git log` immediately before relying on this document.
4. `chaos` skill coverage is new and narrower than `chaos.py` (2 of its 4
   experiment types); it also has no corresponding check-engine finding
   category today, so it only fires when a report happens to contain
   availability/resiliency-flavored finding text — verify that's actually
   true in production traffic patterns, not just in this audit's synthetic
   fixture.
