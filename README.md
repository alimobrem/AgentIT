<p align="center">
  <img src="docs/assets/banner.png" alt="AgentIT" width="100%">
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-blue">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Built for" src="https://img.shields.io/badge/built%20for-OpenShift-EE0000">
  <img alt="GitOps" src="https://img.shields.io/badge/deploy-Argo%20CD-orange">
</p>

<p align="center"><b>An agent-powered platform that assesses, hardens, and continuously operates applications on Red Hat OpenShift — turning an MVP repo into an enterprise-ready, self-healing workload.</b></p>

---

Point AgentIT at a Git repository and it will:

1. **Assess** the repo across 7 enterprise-readiness dimensions and produce a scored report.
2. **Generate** Kubernetes/Helm/Tekton/Argo manifests to close the gaps — via property-based skills (LLM-tailored) backed by a fleet of specialized agents.
3. **Onboard** the app onto the cluster through a human-gated or (optionally) fully autonomous apply pipeline, with an LLM safety gate that fails closed.
4. **Operate** it going forward — watching for CVEs, SLO breaches, API drift, and GitOps drift, then closing the loop by re-assessing, re-generating, and re-applying fixes.
5. **Learn** from outcomes — the learning agent researches CVEs and best practices, generates new skills, and deprecates ineffective ones.

## Table of Contents

- [Why AgentIT](#why-agentit)
- [Architecture, at a glance](#architecture-at-a-glance)
- [Skills & check engine](#skills--check-engine)
- [The agent fleet](#the-agent-fleet)
- [Self-improvement loop](#self-improvement-loop)
- [Self-improvement of AgentIT itself (capability-scout)](#self-improvement-of-agentit-itself-capability-scout)
- [Web portal](#web-portal)
- [Getting started](#getting-started)
  - [CLI](#cli)
  - [Portal (local)](#portal-local)
- [Configuration](#configuration)
- [Deploying to OpenShift](#deploying-to-openshift)
- [Testing](#testing)
- [Security notes](#security-notes)
- [Repository layout](#repository-layout)
- [License](#license)

## Why AgentIT

AI makes building software trivial — a team can go from idea to working MVP in days. But that MVP is a liability: no security posture, no observability, no compliance evidence, no CI/CD. The gap between "it works" and "it's enterprise-ready" takes 10x longer than building the app itself and requires specialized expertise most organizations can't scale.

AgentIT treats that work as something a fleet of specialized agents and property-based skills can plan and generate, with a human (or an LLM safety gate) approving anything destructive before it touches a live cluster.

It is built to run **on** OpenShift, **for** OpenShift: Argo CD for GitOps, Argo Rollouts for canary delivery, Tekton for CI, Argo Events + Kafka for the event-driven loop, and OLM Subscriptions for any operator dependencies the generated manifests need.

## Architecture, at a glance

```mermaid
flowchart LR
    A["Git repo"] --> B["Assess\n7 dimensions + data-driven checks"]
    B --> C["Skill Engine\nmatches property-based skills"]
    C --> D["LLM generates\ntailored manifests"]
    D --> E{"LLM safety gate\n(first approver)"}
    E -->|"safe + confident"| F["Apply to cluster"]
    E -->|"destructive or unsure"| G["Human gate\n(second approver)"]
    G --> F
    F --> H["Watchers\nCVE / SLO / API drift"]
    H -.->|"re-trigger"| B
    B -.->|"first assessment"| I["Learning agent\nresearch stack-specific gaps"]
    I -.->|"new skills"| C
```

The real system has more moving parts — an event-driven path via Kafka + Argo Events, platform context discovery, canary delivery via Argo Rollouts, and conflict resolution. **See [`docs/architecture.md`](docs/architecture.md) for full diagrams.**

## Skills & check engine

AgentIT uses two complementary systems for assessment and remediation:

### Property-based skills (45 skills across 12 domains)

Skills are Markdown files with YAML frontmatter that define **what must be true** (properties), not how to generate manifests. The skill engine matches skills to assessment findings; the LLM generates tailored fixes using the skill's constraints and the app's platform context. `FleetOrchestrator` builds and passes an LLM client into the skill engine on every run (CLI, portal, and webhook onboarding alike) whenever `ANTHROPIC_API_KEY`/`ANTHROPIC_VERTEX_PROJECT_ID` is configured, so LLM-only skills (no template block — e.g. `network-policy`, `containerfile`, `tekton-pipeline`) actually produce tailored output in production, not just template substitution.

```
skills/
├── security/       # network-policy, rbac, containerfile, security-context, resource-limits, image-scan-task
├── observability/   # service-monitor, grafana-dashboard, alerting-rules, otel-collector
├── cicd/            # tekton-pipeline, argocd-application, argo-rollout
├── compliance/      # kyverno-policies, audit-policy, sbom-task, compliance-evidence, image-registry-policy, compliance-cronjob
├── infrastructure/  # hpa, pdb, resourcequota, limitrange, namespace
├── cost/            # vpa, cost-labels, cost-cronjob
├── dependency/      # renovate-config, dependabot-config, dependency-cronjob
├── incident/        # runbook, pagerduty-config, alertmanager-config
├── release/         # analysis-template, rollout-patch, rollback-policy, release-runbook
├── retirement/      # decommission-plan, cleanup-task, data-archive-job
├── chaos/           # pod-delete, network-latency (LitmusChaos, template-only, no LLM needed)
└── custom/          # learning-agent-generated skills (created on first draft; not present until then)
```

Skills have lifecycle management: `draft` → `active` → `deprecated` → `retired`. The API drift detector auto-deprecates skills when their target APIs are removed from the cluster. Low-effectiveness skills (< 30% human approval rate) are flagged for review.

### Data-driven checks (20 checks across 7 dimensions)

YAML check files in `checks/` define declarative rules that supplement the Python analyzers. Check types: `file_exists`, `file_contains`, `file_missing`, `yaml_kind_exists`, `yaml_kind_missing`. The learning agent can create new checks without touching Python code.

```
checks/
├── security/         # containerfile, network-policy, secrets-scanning
├── observability/    # health-check, metrics-endpoint, structured-logging
├── cicd/             # ci-pipeline, dockerfile, gitops
├── compliance/       # admission-policies, license, sbom
├── infrastructure/   # helm-chart, k8s-manifests, resource-quota
├── ha_dr/            # hpa, pdb, replicas
└── data_governance/  # backup-config, retention-policy
```

### Catalog change tracking

Additions and removals to the `skills/`/`checks/` catalog are no longer only visible via `git log` — `skill_inventory.py` snapshots the catalog (by `(domain, name)` / `(dimension, name)` identity, so status-only transitions like `active → deprecated` aren't double-counted alongside the existing `skill-activated` event) and diffs it against the last saved snapshot once an hour from the portal's background maintenance loop. Every skill/check added or removed is logged as a `skill-added` / `skill-removed` / `check-added` / `check-removed` event, which shows up automatically on the **Events** feed (`/events`) and in a "Recent Catalog Changes" section on the **Capabilities** page.

### EOL / end-of-life detection

The `infrastructure` dimension's analyzer now flags base images and language runtimes that are past or approaching end-of-life (`analyzers/eol.py`). A deterministic baseline (always on, no LLM required) matches `Dockerfile`/`Containerfile` `FROM` lines, `.python-version`/`runtime.txt`/`pyproject.toml`, and `package.json`'s `engines.node` against real, cited support-lifecycle dates for Python, Node.js, Ubuntu, Debian, CentOS, and Alpine. When an LLM client is configured, `LLMClient.detect_eol_risks()` additionally reasons over the repo's detected stack and key files to flag EOL/near-EOL components the fixed table doesn't cover — purely additive on top of the baseline, and it degrades to nothing (never fabricates a date) on any LLM failure or low-confidence result.

## The agent fleet

**3 one-shot onboarding agents and 3 long-lived watchers.** Skills run **first**, unconditionally, as the primary generation path for every domain; the 3 remaining Python agents supplement for capabilities skills can't (or don't yet fully) replace. This is a smaller fleet than earlier versions of this doc described — `security`, `observability`, `cicd`, `compliance`, `infrastructure`, `incident`, `release`, `retirement`, and `chaos` used to each have a dedicated hardcoded Python agent; all nine were removed once skills (`skills/**/*.md`, template-fallback verified with no LLM required) reached full parity for every artifact those agents used to generate. See [`docs/agent-removal-readiness.md`](docs/agent-removal-readiness.md) for the domain-by-domain readiness audit this removal was executed against.

| Agent | Category | Always runs? | Generates | Why it's still a Python agent |
|---|---|---|---|---|
| **DependencyAgent** | `dependency` | high/critical | Renovate/Dependabot config, CVE-scan CronWorkflow, **plus** a narrative `dependency-report.md` | Its *manifest* outputs are also skill-covered, but `dependency-report.md` needs real runtime-computed data (detected ecosystems, CVE package matches against `report.architecture.external_dependencies`) that a static skill template has no access to — faking it would violate this project's "no mock data" rule. `FleetOrchestrator` never skips this agent for skill coverage of its manifest outputs, specifically so the report keeps generating. |
| **CostOptimizationAgent** | `cost` | high/critical | VPA, cost labels, cost CronWorkflow, **plus** a narrative `cost-report.md` | Same reasoning as `dependency` — `cost-report.md` needs a computed deployment tier (`_tier()`, from service/language count) and cost-lookup-table result a template can't produce. |
| **CodeChangeAgent** | `codechange` | high/critical or score < 50 | `.gitignore`, health endpoints, OTel/structured-logging instrumentation — as source patches to the **app's own repo** | Fundamentally not skill-shaped: skills generate K8s manifests to apply to a cluster; they have no concept of "the application's source tree" and no PR/patch-application machinery. |

Conflict detection only flags *real* collisions between agent outputs — a known-conflicting resource-kind pair actually being generated for the same workload (e.g. an actively-resizing VPA alongside an HPA), or two agents writing a file at the same output path — not merely "both agents succeeded". `plan.auto_approve` (computed from score/criticality at plan time) is downgraded to `False` if a real conflict is found during the actual run, so it can be trusted end-to-end.

Long-lived watchers (deployed as separate pods):

| Watcher | Loop | Role |
|---|---|---|
| **vuln-watcher** | 6h | Fleet CVE monitoring, triggers remediation |
| **slo-tracker** | 5m | Collects fresh `availability`/`error_rate`/`latency_p99_ms` metrics for every tracked SLO (via `slo_collector`: `availability`/`error_rate` from pod status via the kubernetes client, `latency_p99_ms` from Prometheus — `histogram_quantile(0.99, ...)` over `http_request_duration_seconds_bucket`, scoped to the app's namespace, same `AGENTIT_PROMETHEUS_URL` connection `resource_tuner` uses; apps with no data yet are skipped/logged, not silently ignored), checks breaches with the correct per-metric direction (`availability` = higher is better; `error_rate`/`latency_p99_ms` = lower is better), publishes breach alerts, and opens rollback gates |
| **drift-detector** | 10m | Argo CD sync monitoring, API drift detection, auto-deprecation of affected skills, reports still-in-use deprecated APIs (`PlatformContext.deprecated_apis`) |
| **skill-learner** | 24h | Researches CVEs via LLM, drafts new skills for human review — opt-in via `agents.skillLearner.enabled` (chart default: disabled; enabled on the live deployment via `argocd/application.yaml`), requires an LLM connection |

Every watcher records real tick telemetry after each loop iteration — a `tick-complete`/`tick-failed` event plus an `AssessmentStore.agent_heartbeat()` call (`agentit/watchers/__init__.py::record_tick`) — so "last seen" on the **Agents** and **Schedules** pages reflects an actual heartbeat instead of a static "—". A Prometheus gauge, `agentit_watcher_last_success_timestamp{watcher=...}`, backs an `AgentITWatcherStale` alert (one rule per watcher, threshold = 2x its expected interval) in the chart's `PrometheusRule`. The liveness probe's own `/tmp/heartbeat` file is kept fresh independently of `record_tick` — `vuln-watcher`/`skill-learner`'s tick intervals (6h/24h) far exceed the probe's 900s staleness window, so `run()` sleeps between ticks via a shared `agentit.watchers.sleep_with_heartbeat` helper that touches the file every 300s instead of only once per full tick, avoiding a restart loop.

## Self-improvement loop

AgentIT improves itself through three tiers of learning. The loop is now closed end-to-end — `record_skill_outcome()` fires from every real production path, not just the CLI `self-fix` command, and the learning agent actually reads that data back before deciding what to research next:

1. **Feedback loop, wired into every real apply path.** `record_skill_outcome()` — previously only called from `self-fix` — now fires from the unified Deliver action (`routes/assessments.py::deliver`), gate resolve (`routes/gates.py::resolve_gate`, both approve and reject), and auto-mode's successful auto-apply (`automode.py::AutoMode.execute`), via the shared `skill_engine.record_skill_outcomes()` helper. The manual apply route and `AutoMode.execute()` themselves share one orchestration function, `cluster_apply.apply_with_verification()` — it preserves the one real difference between them (the manual route makes a single call honoring its own `dry_run` form flag with no automatic follow-up; auto-mode always dry-runs first via `force_dry_run_first=True` and only proceeds to a real apply if that dry-run is clean) while consolidating `record_skill_outcomes()` and `audit_log()` so neither call site can drift out of sync — closing a real gap where auto-mode's own auto-applies previously left no audit trail at all. Each generated file's producing skill is recovered from `SkillEngine.generate()`'s `{app_name}-{skill.name}.yaml` naming convention (`skill_engine.skill_name_from_path()`) since neither `AgentResult` nor `onboarding_results.files_json` carry a structured skill-name field; `GeneratedFile.skill_name` (set directly by `SkillEngine.generate()`) gives the CLI's `self-fix` path exact attribution instead of the previous "app-name-skill-name" bug. Effectiveness is a **recency-weighted** rate (`AssessmentStore.get_skill_effectiveness()`/`get_low_effectiveness_skills()`, half-life ~90 days) so a skill that was bad months ago and has since improved can recover off the "Skills Needing Review" list on Insights, rather than being stuck flagged forever by outcomes that no longer reflect its current behavior. `SkillEngine.run_all()` now actually uses its `store` parameter: a skill whose domain has been rejected 3+ times for an app is skipped outright (mirroring the same threshold `webhooks.py` already used for auto-fix), and a human's most recent correction for that app+domain is passed to LLM generation as extra guidance (`get_human_override()`). Skill activation (`capabilities.py`'s "Activate" button) now runs a real functional check (`skill_engine.verify_skill()` — frontmatter completeness plus an actual generation smoke test against a synthetic fixture, validated with `agents/base.py::validate_manifest()`) before flipping `status: draft` → `active`, instead of a blind string replace; a skill that can't produce valid output is blocked, not silently activated.
   - **Edit-before-apply flow, now built** (previously a "Known gap" — manifests were approve-as-is or reject-and-regenerate only, with no "diff between generated and applied content" to capture). The onboard-results page (`onboard_results.html`) now lets a human edit any generated file's raw content in place before delivery, via a real textarea editor, not a mockup. Saving an edit (`POST /assessments/{id}/onboard-results/edit-file` → `AssessmentStore.update_onboarding_file()`) rewrites the *same* `onboarding_results` row's file entry — the exact row `get_onboarding()`/`route_and_deliver()` already read — so the SAVED (possibly-edited) content, not the original LLM/template output, is what a subsequent "Deliver" click actually delivers: a genuine round trip, not a preview. The first edit captures `original_content` (never overwritten by later edits of the same file) alongside the live `content`, so a real line-level diff — `portal/content_diff.py::diff_lines()`, tagged add/remove/context rows rendered via CSS classes per this repo's "no inline styles" rule — is always reconstructable and shown inline on the results page. YAML/YAML-adjacent edits are re-validated through the existing `agents/base.py::validate_manifest()` path before being persisted (a human's raw edit could introduce a syntax or structural error the original generation wouldn't have had); an invalid edit is rejected outright with a visible error, never partially saved. Because `route_and_deliver()`'s `classify_file()` always classifies off whatever `content` is currently persisted, editing a file's `kind` in a way that changes its taxonomy category (e.g. a ConfigMap edited into a Secret) is classified — and blocked or rerouted — off the real edited content; there is no separate edit-only code path that could bypass the router's existing taxonomy/safety gates. `route_and_deliver()` now also records `edited_files` in each `deliveries` row's `details_json`, so "was this delivery's content edited from what was originally generated" is a permanent, queryable fact, not a transient UI detail — surfaced today on the onboard-results page's Delivery History table, and available to any future Ledger/Decisions view over the same `deliveries` table.

2. **Learning agent, now reading its own effectiveness data.** The research cycle (`SkillLearner.research_once()`, and the portal's "Research CVEs & Generate Skills" button) checks `get_low_effectiveness_skills()` **first**: if any skill is flagged, the LLM is asked specifically to propose a replacement (`learning_agent.research_skill_improvement()`) for each flagged skill (up to the configured limit), and only falls back to the generic CVE sweep when nothing's flagged. This is the wiring that actually closes the loop — before it, the learning agent was blind to which of its own already-shipped skills humans kept rejecting. Runs automatically every 24h via the `skill-learner` watcher (chart default: disabled — enable with `agents.skillLearner.enabled=true`; currently enabled on the live deployment via `argocd/application.yaml`), and can also be triggered on demand from the Capabilities page or via `agentit learn` / `agentit learn-for` on the CLI. Draft skills get an "Activate" button right next to them on the Capabilities page — the full research → draft → human-review → active loop runs end-to-end in the portal, no CLI required.
   - **Persistence and cross-pod visibility.** The `skill-learner` watcher runs in its own pod, separate from the portal, with no shared filesystem between them — this cluster has no ReadWriteMany storage class available (confirmed via `oc get storageclass`: only `gp2-csi`/`gp3-csi`, both EBS-backed and `ReadWriteOnce`-only), which ruled out a shared/RWX PVC as the fix. Instead, every draft the watcher generates is pushed straight to the portal via an internal-token-authenticated API call (`POST /api/webhook/skill-draft`, `routes/webhooks.py`) — the same `AGENTIT_PORTAL_URL` + `AGENTIT_INTERNAL_WEBHOOK_TOKEN` pattern `RemediationLoop` already uses to call back into the portal from a separate watcher pod. That endpoint calls the exact same `save_skill()` the portal's own in-process "Research CVEs & Generate Skills" button uses, into the portal's own `skills/` tree, and busts its 60s skills cache, so a watcher-drafted skill is visible on the Capabilities page on the very next page load — no restart or manual sync step needed. If the portal can't be reached that cycle, the draft falls back to the watcher's own dedicated, single-consumer PVC (`agents.skillLearner.persistence`, default on, mounted at `/data/skills` via `AGENTIT_SKILLS_DIR`) so nothing is lost, and `SkillLearner._save_draft()` logs a loud warning in that (expected to be rare) case, since a draft that only exists on that PVC stays invisible until a human recovers it. **Real 2026-07-15 incident:** `AGENTIT_PORTAL_URL` points at the Argo Rollouts *stable* Service (`http://agentit.agentit.svc:8080`), whose selector only flips over to a canary's new ReplicaSet once that rollout fully promotes — mid-rollout, the still-serving old pod genuinely 404s a route that's correctly wired in the code about to become stable (confirmed live: `oc exec`-curling the exact path returned 404 from the stable-hash pod and 401 — i.e. route present — from the canary-hash pod behind the same Service). `_submit_draft_to_portal()` now retries specifically on a 404 (a few attempts with a short delay) before falling back to the PVC, so a routine rollout window doesn't cost a draft.
   - ~~**Documented future idea (not built):** cross-onboarding stack-signature detection~~ — **shipped**: `src/agentit/stack_signature_detector.py` detects repeated uncommon stacks (auto-trigger `learn-for` remains a separate follow-up). Do not re-propose the detector. `tick_failure_classifier` classifies permission-denied (and similar) tick failures into remediation hints.

3. **Platform awareness** — `PlatformContext` discovers the cluster's K8s version, available APIs, CRDs, and operators. Every skill generation includes this context. The API drift detector auto-deprecates a skill specifically when the API kind it generates has been removed from the cluster (a narrower guarantee than the effectiveness-based flagging in tier 1). `FleetOrchestrator.run()` treats an empty `available_kinds` result from `discover_platform()` as "the has_api() gate has no signal to work with" and skips gating entirely (`platform=None`) — this is checked independently of whether `k8s_version` resolved, since K8s/OpenShift expose the version endpoint even to identities with no other RBAC (e.g. a least-privilege ServiceAccount), which previously let a real-but-empty discovery result silently gate every skill's output to zero instead of triggering the fallback.

**Loop visibility.** The Capabilities page's skill table links each skill to a per-skill lifecycle page (`/capabilities/skills/{name}/history`) showing its full effectiveness trend (every recorded outcome, most recent first) and its activation/deprecation history (matched from the `events` table by skill name — `skill-added`/`skill-removed`/`skill-activated`/`skill-deprecated`/`skipped-rejected`/`skill-improvement-drafted`). The Insights page adds one loop-health meta-metric: of the skills currently flagged low-effectiveness, what percentage have had an improvement actually drafted for them in the last 30 days (`AssessmentStore.get_loop_health()`) — a live snapshot of whether the loop is actually turning, not just theoretically closed, using data that's only non-trivially populated now that tier 1's wiring exists.

**Learn-button transparency.** The "Research CVEs & Generate Skills" button previously left no trace beyond a transient toast — `learning_agent.describe_learning_run()` now backs a `learning-run` event that both entry points (the button and the `skill-learner` watcher's own tick) log for **every** outcome, not just the ones that generated a skill: success, a no-op skip, or an outright failure (LLM unavailable, exception mid-run) all leave a durable, queryable row. A "Learning Agent Runs" table on the Capabilities page surfaces the last 15 of these (timestamp, manual vs. automatic trigger, what was researched, outcome), and the button's own description now dynamically previews what it's *about* to do — naming the currently-flagged low-effectiveness skill(s) it will try to improve, or stating that it'll fall back to a generic CVE sweep — plus the `skill-learner` watcher's real last-heartbeat status (not the chart's default, which the live deployment overrides), so "is automatic research even on, and did it already run today" no longer requires guessing.

**Auditing LLM decisions.** Every place the LLM's output directly gates an outcome (not just generates content) persists a real, attributed record, surfaced on the **Decisions** page: `self-fix`'s Step 3 first-approver gate (`LLMClient.review_fix`) — attributed by real skill name via `skill_effectiveness` — auto-mode's safety classification (`LLMClient.classify_action`, `AutoMode.execute`) — attributed by the real originating agent when the caller knows it (e.g. the dispatcher's `result["agent"]`), otherwise the generic `auto-mode` component name, which is still the common case since most callers apply a whole bundle of manifests spanning several agents at once — the security analyzer's LLM-based secret false-positive filter (`classify_secret`, `SecurityAnalyzer._check_secrets`), which decides per match whether to keep or drop a potential-secret finding, attributed to the generic `security-analyzer` component and persisted via `llm_decisions.build_secret_classify_events()` + `store.log_event()` (action `secret-classify`) — and capability-scout's self-improvement proposal cycle (`LLMClient.propose_capability_improvement`), attributed to the generic `capability-scout` component. See `llm_decisions.py`'s module docstring for the full attribution details of each.

## Self-improvement of AgentIT itself (capability-scout)

The loop above (`skill-learner`) improves what AgentIT *generates for other apps* — the skills catalog. It has no counterpart that improves AgentIT's *own* codebase, its portal routes, its watchers, its CLI. `capability-scout` is that counterpart: a separate, opt-in, 24h watcher (`agents.capabilityScout.enabled`, default **off** — this is a live-deployment decision for the repo owner to make explicitly, not something enabled as a side effect of shipping it) that mirrors `skill-learner`'s shape exactly — **research → propose → verify → human review → merge** — but aimed at AgentIT's own repo. See [`docs/self-improvement-for-agentit.md`](docs/self-improvement-for-agentit.md) for the full design; this is deliberately named distinctly from `self-assess`/`self-fix` (which run AgentIT's existing hardening pipeline *against* AgentIT's own repo, generating K8s manifests for it — not proposing new Python features for AgentIT's product surface).

**The loop, end to end, every 24h:**

1. **Real signal, never fabricated.** `capability_scout.gather_evidence()` reads fleet-wide rejection rates by finding category (`AssessmentStore.get_fleet_wide_rejection_stats()`, a new `GROUP BY` aggregate over `agent_feedback`), agent run health (`get_agent_stats()`), check compliance (`get_check_compliance()`), skill effectiveness (`get_skill_effectiveness()`/`get_low_effectiveness_skills()`/`get_loop_health()`), recent watcher tick failures, and — the highest-precision signal — a static grep of this repo's own `docs/*.md` for "Known gap" / "Deliberately deferred" / "Documented future idea" / "not built" text (`capability_scout.scan_doc_gaps()`; the image `COPY`s `docs/` into `/opt/app-root/src` so that path resolves in the capability-scout pod, same as `tests/`/`chart/`). Fewer than 5 real data points anywhere → the cycle logs an honest no-op, never an invented proposal.
2. **One LLM proposal, or none.** `LLMClient.propose_capability_improvement()` (mirrors `detect_eol_risks()`'s `_chat()`/graceful-failure/JSON-parsing convention) is given only that real evidence and asked to propose **at most one** small, evidence-cited change — title, gap description, the exact evidence that grounded it, suggested target files, risk, and a test plan — or to explicitly propose nothing. It's instructed to prefer a documented doc-gap over inventing one, and to never suggest touching `chart/`, `argocd/`, `.github/workflows/`, or anything secret/RBAC-related. `_chat()` takes an explicit `max_tokens` per caller rather than one global default: this call's 7-field, multi-paragraph response gets a 2048-token budget (`detect_eol_risks()`'s open-ended risk list gets 1024) while the simple safe/unsafe-style classifiers keep the 512-token default — a real proposal was previously getting cut off mid-JSON under that same 512 budget and correctly logging a `no-proposal` outcome for a genuinely-too-small budget rather than a bad proposal.
3. **Real, executable safety gates — not stubs.** `capability_scout.run_safety_gates()` runs, in order, fail-closed: diff-size cap (≤3 files, ≤150 lines), scope allowlist (`src/agentit/`, `skills/`, `checks/`, `tests/`, `docs/` only), a secret-pattern regex scan, a test-plan-required check, `py_compile` on every touched `.py` file, a `gh pr list`-backed check that no `agentit/self-improve/*` PR is already open (configurable via `agents.capabilityScout.maxOpenPRs`, default 1), and finally the **exact** `pytest tests/ -q --ignore=...` invocation `.github/workflows/tests.yml` uses. Any failing gate discards the cycle — no PR opens, but the attempt (and exactly which gate blocked it) is still logged.
4. **A real draft PR, never a direct commit.** When every gate passes, `git_pr.py` (extracted from `self-fix --create-pr`'s existing branch/commit/push mechanics, not reimplemented) creates a new `agentit/self-improve/<slug>-<date>` branch, commits the one artifact this cycle produced (a reviewable `docs/proposals/<slug>.md` write-up citing the evidence verbatim — see the module docstring in `capability_scout.py` for why v1 documents a proposed change rather than mechanically applying a source diff to files the LLM has never seen the contents of), pushes it, and opens it via `gh pr create --draft`. Existing CI (`tests.yml`/`security.yml`) runs on it like any other PR. Nothing here ever auto-merges.
5. **Every outcome is logged, every cycle.** One `capability-run` event (`capability_scout.CAPABILITY_RUN_ACTION`) is logged whether the cycle proposed something, got gate-blocked, or found no signal — mirroring `learning-run`'s "every run leaves a trace" convention exactly.

**Fully transparent from inside the portal, without needing to already know a PR exists:**
- A **Self-Improvement** tab on the Capabilities page (`/capabilities/self-improvement`) — a "Self-Improvement Runs" table mirroring "Learning Agent Runs" (timestamp, trigger, evidence considered, outcome, live PR status badge).
- A per-run drill-through (`/capabilities/self-improvement/runs/{event_id}`) mirroring `/capabilities/skills/{name}/history`'s layout: evidence, a per-gate pass/fail table, and the resulting PR's live status — polled via the same `github_pr.get_pr_status()` call `onboarding_history()` already uses, no `gh` needed inside the portal process itself.
- A `capability-proposal` entry on the **Decisions** page (`llm_decisions.py`), filterable alongside every other real LLM decision, attributed to the `capability-scout` component.
- A one-line addition to the **Schedules** page's watcher table (`WATCHER_AGENTS`) — real heartbeat-derived status, zero new route/template code, the same mechanism every other watcher already gets for free.

GitHub's own PR UI stays the surface for reviewing the actual code diff; the portal is where a human sees what the loop considered and why.

```bash
# Long-lived watcher (24h default; dogfood may use 1h via Helm) -- mirrors `learn-watch`
uv run agentit propose-watch --interval 86400 --max-open-prs 1

# One-shot cycle for dogfood / debugging (no startup grace, no loop)
uv run agentit propose-once --mode auto --max-open-prs 1
```

**Build modes:** `docs` (proposal markdown only), `source` (edit `skills/`/`checks/`/`tests/`/`src/agentit/` when every target is in that allowlist), `auto` (source when eligible, else docs). Prefer **new small modules** over rewriting large files — full-file generation of big modules fails and (in `source`/`auto`) skips the cycle rather than opening a docs-only PR. When a proposal targets an existing file already over the 150-line size cap, scout rewrites that target to a new `src/agentit/<feature>.py` sibling before calling the LLM (so L3 cycles are not stuck gate-blocked on `diff-size`). File generation uses a higher token budget and a compact JSON retry when the first reply truncates. Dogfood sets `agents.capabilityScout.mode=auto` via Helm.

See [`docs/superpowers/plans/2026-07-15-autonomous-self-improve-dogfood.md`](docs/superpowers/plans/2026-07-15-autonomous-self-improve-dogfood.md) for the L0→L5 dogfood milestone plan (substrate → source PRs → outcome loop).

## Unified apply flow

Every path that gets a generated change into a cluster or a repo — the manual "Deliver" action, gate-approve, `AutoMode`, and `DriftDetector` — funnels through one router, `portal/delivery.py::route_and_deliver()`, instead of each independently deciding "apply now" with no shared audit trail, no shared verification, and (before this) no idea whether an app is already GitOps-registered. See [`docs/unified-apply-flow.md`](docs/unified-apply-flow.md) for the full design this implements.

**Why this exists.** Before this, gate-approve called a raw, unaudited `apply_manifests_to_cluster()` while "Create PR" — when it committed to a GitOps infra repo — also unconditionally ensured a live, `selfHeal: true`/`prune: true` Argo CD `ApplicationSet` watching that same app's namespace. Nothing stopped a subsequent gate-approve or "Apply to Cluster" click from applying directly into that same namespace, one Argo reconcile away from Argo silently deleting whatever it just applied. This design closes that hole structurally.

**The router's taxonomy → mechanism table, as implemented** (`classify_file()`):

| Category | Detected by | Mechanism |
|---|---|---|
| Cluster/app config | Valid K8s YAML, not `codechange`, not a shared-operator-namespace target | GitOps-registered → commit to infra repo + open PR (never auto-merged). Not registered → direct apply. |
| CI/CD → shared operator namespace | Valid K8s YAML whose `metadata.namespace` is one of `openshift-gitops`/`openshift-pipelines`/`openshift-operators`/`openshift-monitoring`/`openshift-logging` | A dedicated `cluster-admin-review` gate — never a silent skip. Approving it applies directly into that namespace (`allow_operator_namespaces=True`), the one case where the "never apply into a shared namespace" rule is bypassed, by explicit elevated-RBAC human approval. |
| Source-repo patch | `category == "codechange"` (excluding its own summary doc) | PR against the app's own repo, as a real patch against each file's `target_path` (e.g. `Dockerfile`) — not a same-named copy under `.agentit/codechange/`. |
| Narrative documentation / manifests-at-rest | Any other YAML (e.g. `ConfigMap`-wrapped runbook prose) / non-YAML config (Renovate/Dependabot) | Routed exactly like cluster/app config, or (non-YAML) as an informational PR against the app's own repo. |
| Narrative reports | `category in ("dependency", "cost")` and filename is `dependency-report.md`/`cost-report.md` | Excluded from delivery entirely — read-only artifacts, downloadable from the assessment page, never a delivery candidate. |
| Secrets | Any manifest with `kind: Secret` | Hard-blocked from every mechanism — logged loudly, never delivered. |

**GitOps registration** is a real, current query — `kube.get_custom_resource("argoproj.io", "v1alpha1", "applications", f"managed-{app}", namespace="openshift-gitops")` — not just "was an infra repo URL ever set." It only falls back to `report.infra_repo_url is not None` when the cluster call itself fails (offline/unreachable, e.g. tests); a successful call that simply finds no `Application` is **not** registered regardless of `infra_repo_url`.

**AutoMode's terminal action** (`should_auto_apply()`'s safety classification is completely unchanged) now depends on that registration check: not registered → the exact same direct-apply behavior as before. Registered → AutoMode still autonomously commits and opens a PR (it already trusts its own safety classification enough to skip human review for a direct apply), but creates a `gitops-pr-pending` gate instead of finishing — a human must merge the PR; **AgentIT never auto-merges**, matching this project's own "Argo CD is the sole deployer" stance for its own deployment.

**Every delivery is tracked** in a new `deliveries` table (`store.py`, same as every other table) — what was routed, which mechanism was chosen, delivery status, and verification outcome. Direct applies and GitOps commits with an actual SLO already being tracked for that assessment get the same 60-second SLO-watch tail (generalized out of `RemediationLoop` into `remediation_loop.verify_slos()`/`rollback_action()`) run in the background; `DriftDetector`'s existing ~10-minute Argo poll closes the loop for GitOps deliveries specifically once it observes the committed revision synced (Argo sync isn't synchronous, so this can't happen inline).

**Point-of-no-return confirmation.** A dynamically-labeled single "Deliver" button (`onboard_results.html`) replaces the old independent "Apply to Cluster"/"Create PR" buttons — the mechanism is no longer a human's choice for cluster/app config. Both that button and every gate's "Approve & Deliver" button (on Assessment Detail's Actions tab, Fleet, or Admin Review — see "Web portal" below) show the exact same "AgentIT will: ..." statement (`delivery.confirmation_text()`) twice: once as page-level preview text, and again inside the un-skippable confirm modal a human must actively acknowledge immediately before the real delivery fires — never only on an earlier, easy-to-click-through dry-run screen.

**Real per-field-manager server-side-apply.** `kube.apply_yaml()` no longer shells out to `oc apply --server-side --force-conflicts` — it applies each document in a manifest individually via the Kubernetes Python client's dynamic-client server-side-apply support (`content_type="application/apply-patch+yaml"`, `field_manager="agentit"`), with `force` defaulting to `False`. On a genuine field-manager conflict (HTTP 409), it returns a structured, distinguishable result (`conflict: True`, `conflict_details: [...]`) instead of either failing silently or forcing through — every apply path (`AutoMode`, the unified delivery router, gate resolution) routes a real conflict to a dedicated `cluster-conflict-review` gate. Approving that gate is the **only** place in the app that ever passes `force=True`, seizing ownership from the other manager after explicit human review.

**Per-(namespace, resource-kind) auto-mode allowlist.** The `auto_mode` setting is still a single global on/off toggle by default (see Settings below) — but an operator can additionally scope it to specific `namespace/kind` patterns (`*` wildcards allowed on either side, e.g. `*/ConfigMap`, `prod/NetworkPolicy`) via the same Settings page. `AutoMode.execute()` splits each apply batch per file against this allowlist rather than treating it all-or-nothing: an allowed `ConfigMap` and a disallowed `ClusterRoleBinding` in the same batch apply and gate independently (`auto-mode-scope-review` gate for the denied portion). `Secret`/`Role`/`RoleBinding`/`ClusterRole`/`ClusterRoleBinding` can never be allowlisted, even by an explicit or wildcard pattern naming them. With no allowlist configured (the default), this is a pure no-op — identical whole-batch behavior to before it existed.

## Web portal

`agentit portal` launches a FastAPI + Jinja2 app (htmx + Alpine.js for interactivity, no frontend framework). 98 routes.

Key pages:

| Page | Purpose |
|---|---|
| **Fleet** | Dashboard of all managed apps with scores, lifecycle stage, a GitOps-registration badge ("GitOps" vs. "Direct apply") next to the existing sync-status badge, and a "Needs Action" pending-gate count per app (own nav-bar badge for the fleet-wide total) — counted by the app's `repo_url`, not its current `assessment_id`, so a gate created against an older assessment of the same app still counts here after a re-assessment (`fleet.py::_attach_pending_actions`) |
| **Assessment Detail** | 7-dimension scores, lifecycle stepper, score trend + a rendered score-history table with deltas, a GitOps-registration badge + a dismissible "Register for GitOps" nudge for unregistered apps, a 4th **Actions** tab showing that app's own pending gates (Approve & Deliver / Reject / Dismiss) across **every** historical assessment of this app, not just the current one (`AssessmentStore.list_gates_for_assessment()` joins by `repo_url`), timeline, remediation items |
| **Admin Review** | Narrow, cross-app queue for the one gate type that's genuinely a different audience than an app owner: `cluster-admin-review` (CI/CD manifests targeting a shared operator namespace this service account can't apply to without elevated RBAC). Every other gate type (`auto-mode-review`, `dry-run-failed`, `gitops-pr-pending`, `auto-mode-scope-review`, `rollback-review`, `cluster-conflict-review`, and per-finding `finding-{category}` gates) is per-app and lives on that app's own Assessment Detail Actions tab + a Fleet row badge instead — see "Unified apply flow" above for what each gate type means and how approving it resolves. Approving funnels through the unified delivery router (`route_and_deliver()`) for ordinary gates; `gitops-pr-pending` gates merge a PR instead of re-delivering, `cluster-admin-review` gates apply directly into a shared operator namespace, `cluster-conflict-review` gates force-reapply after a server-side-apply field-manager conflict. `/gates` redirects here for any stale bookmark. |
| **Insights** | Fleet stats, agent performance (from real `agent_runs` records), low-effectiveness skills, fleet-wide check compliance (pass rate per data-driven check across every recorded assessment), and fleet-wide learning feedback |
| **Decisions** | Audit of every real LLM *decision* point (fix-review, auto-mode classify — not just LLM-generated content), attributed by the agent or skill that triggered it, with the LLM's actual reasoning and a per-agent/skill approve/reject/gate breakdown. See `llm_decisions.py` for exactly what's covered and what isn't. |
| **Capabilities** | Skills/checks catalog, onboarding agents, watchers, and the "Research CVEs & Generate Skills" trigger. Its **Self-Improvement** tab has the matching "Run Self-Improvement Scan" trigger for `capability-scout` (previously only reachable via its 24h watcher tick). Tabbed with **Agents** (live registry of who's actually run, their real success rate, and a per-agent run-history table with duration/resource tier/error) |
| **Events** | Activity feed with a `correlation_id` "Chain" column (click through to trace a single assess → onboard → apply run end to end) and a DLQ for failed events that now actually populates from Kafka dead-letters and republishes to the original topic on retry |
| **Health** | Rollout/pod/pipeline status, Kafka topic/consumer-group stats, live circuit breaker (LLM/Kubernetes) open/closed state, and a "Deployment Status" section (stage-by-stage PipelineRun progress, the commit being deployed, and the resolved outcome once it's done) |
| **SLOs** | SLO definitions and error budgets |
| **Settings** | Auto-mode toggle, per-(namespace, resource-kind) auto-mode allowlist, decision matrix, configuration. Tabbed with **Schedules** (watcher status — now backed by real heartbeats — and cron jobs) |

Webhook endpoints power the event-driven loop: `/api/webhook/assess`, `/api/webhook/github-push`, `/api/webhook/onboard`, `/api/webhook/auto-apply`, `/api/webhook/remediate`, plus three self-monitoring endpoints described below (`/api/webhook/synthetic-probe`, `/api/webhook/backup-status`, `/api/webhook/secret-check`). All but `github-push` require the shared-secret `X-Internal-Webhook-Token` header (see [Security notes](#security-notes)).

### Operating AgentIT on itself

AgentIT is a platform that assesses and hardens *other* apps — the gap was that it never fully ran that same playbook on itself. AgentIT is now registered in its own fleet (`chart/templates/tekton/pipeline.yaml`'s `register-self-in-fleet` task calls `/api/webhook/assess` with its own repo URL on every CI build), which is what lets the fleet-wide `vuln-watcher` and the `cost-report` CronJob cover AgentIT the same way they cover every app it onboards. Alongside that, six new opt-in chart features close the gaps kubelet-level health checks and the existing watchers structurally can't reach:

| Capability | Chart flag | What it catches |
|---|---|---|
| External synthetic uptime probe | `syntheticProbe.enabled` | Route/router-layer failures — kubelet's `/healthz`/`/readyz` probes hit the container directly and can't see these |
| TLS certificate-expiry watch | (same CronJob) | Alerts at <30d/<14d/<7d via `agentit_route_cert_expiry_days` |
| Backup success/failure reporting | (always active once `backup.enabled` / `postgres.bundled.backup.enabled`) | A silently-failing backup job now sets `agentit_backup_last_status`/`agentit_backup_last_success_timestamp` instead of only leaving a line in a CronJob pod's logs |
| Secret rotation + drift detection | `secretRotation.enabled` | Monthly rotation of `agentit-internal-webhook-token` (+ automatic restart of every consumer); a lighter existence check for `github-webhook-secret` (can't be safely auto-rotated — see the CronJob's own comment) that would have caught the 2026-07-13 incident below in minutes instead of ~8.5 hours |
| Rate limiting on the portal's own routes | `rateLimit.enabled` | A runaway/replayed webhook loop hitting the routes auto-mode can use to apply changes to a live cluster — see `rate_limit.py` for exactly what this is (and isn't) a substitute for |
| Slack alert routing | `monitoring.slack.enabled` (needs an `agentit-slack-webhook` Secret you create yourself — never generated or hardcoded) | The alerts in `prometheusrule.yaml` previously fired with no confirmed destination |
| Own-repo dependency/base-image bumps | `.github/dependabot.yml` | `pip` + `github-actions` + `docker` (Containerfile *and* the two pinned images in `chart/values.yaml`) — the same `dependabot-config` skill AgentIT generates for apps it onboards, now pointed at itself. Native Dependabot PR bodies already carry changelog/CVE reasoning; see that file's own comments for one open dependabot-core limitation (`Containerfile`-named PR creation) and one structural one (Helm-templated `image:` refs in `chart/templates/**` aren't reachable by any static scanner) |

See [`docs/deployment.md`](docs/deployment.md) for the full incident writeup these were prioritized against, and this repo's own commit history for the "what's worth adding now vs. later" reasoning behind which checklist items got picked first.

### Self-observability

AgentIT's own Postgres store and `/metrics` endpoint are the source of truth for "what is the platform actually doing", not just what it can do:

- **`agent_runs` table** — `FleetOrchestrator` writes one structured row (agent, mode, status, duration, resource tier, error, assessment_id) per agent execution, local or Kubernetes-Job. `AssessmentStore.get_agent_stats()` and the new `list_agent_runs()` read from this table instead of pattern-matching event `action` strings, so the Agents/Insights pages reflect real run history.
- **`check_results` table** — every data-driven check run during an assessment (`check_engine.run_checks_by_dimension_with_status`) is snapshotted pass/fail, keyed by assessment. `get_check_compliance()` aggregates this into a fleet-wide pass-rate view on the Insights page.
- **`correlation_id` on events** — `AssessmentStore.log_event()` accepts a `correlation_id` (populated with the `assessment_id` by `save()`, `save_onboarding()`, and `FleetOrchestrator`), matching the same id already used for Kafka's `correlationId`. The Events page's new "Chain" column links straight to `/events?correlation_id=...` to trace an assess → onboard → apply run end to end.
- **DLQ end-to-end** — `EventConsumer._dead_letter()` now persists to the `events` table (not just the Kafka `agentit-dlq` topic) so `/events/dlq` actually shows failures, and `retry_dlq_message()` republishes the original message to its original topic via the Kafka producer instead of only relabelling the row.
- **Circuit breaker visibility** — `portal/helpers.py::get_circuit_breaker_states()` exposes live LLM/Kubernetes breaker state, shown on the Health page and set on the `agentit_circuit_breaker_open{name=...}` gauge every time `/health` is polled.
- **Ambient deploy-status indicator** — a compact badge in `base.html`'s nav, present on every page, shows the running version/commit (`portal/metrics.py::get_build_info()`, an in-memory read of the `agentit_build` metric — no per-request I/O) and switches to a live "Deploying · &lt;stage&gt;" state, polled via htmx (`GET /api/deploy-status`, every 15s) from `_get_deploy_status()` in `routes/health.py`. That function combines the running build with the live `agentit-ci` PipelineRun (which Tekton task is currently running, via the same `kube.list_custom_resources("tekton.dev", ...)` helper the Health page already used) and the live `agentit` Argo CD `Application`'s sync/health (same RBAC as the existing Argo-reading code — no new grants needed): a PipelineRun actually running, or Argo out-of-sync/`Progressing`, shows "Deploying"; a failed PipelineRun or `Degraded` Application health shows "Deploy failed" with the real reason; otherwise it's a calm, idle badge. The Health page's "Deployment Status" section reuses the same function (`include_commit_info=True`) to add a stage-by-stage task stepper, the commit message being deployed (`portal/github_pr.py::get_commit_info()`), and — once a deploy is no longer in progress — whether this instance actually ended up running the new version or rolled back (compared against its own live `AGENTIT_GIT_COMMIT`, now wired in `chart/templates/deployment.yaml` from `.Values.image.tag`, the same value the CI pipeline pins to the deployed commit SHA). No field is ever fabricated: an unreachable PipelineRun/Argo/GitHub API surfaces via the badge/panel's `errors`/`reason` fields instead of silently looking idle.
- **Prometheus gauges actually set** — `agentit_active_gates` updates on every gate create/resolve/expire; `agentit_build` is populated once at startup (package version + `AGENTIT_GIT_COMMIT`/`AGENTIT_IMAGE_TAG`, now real live values — see the deploy-status indicator above); `agentit_db_size_bytes` / `agentit_db_rows_total{table=...}` / `agentit_event_buffer_backlog` refresh every 5 minutes from the portal's background maintenance loop (via async helpers -- `refresh_db_metrics()`/`diff_and_log_inventory_changes()`/`prune_stale_agents_and_log()`); `agentit_watcher_last_success_timestamp{watcher=...}` backs the `AgentITWatcherStale` alert described above; `agentit_synthetic_probe_up` / `agentit_route_cert_expiry_days` / `agentit_backup_last_status{target=...}` / `agentit_secret_check_status{secret=...}` (see "Operating AgentIT on itself" above) are all set via internal webhooks from CronJobs, not from in-process code.
- **Audit log** — `agentit/audit.py::audit_log()` is now wired into every privileged action call site: apply-to-cluster (manual and auto-mode's own auto-apply — both go through the shared `cluster_apply.apply_with_verification()` helper below), gate approve/reject, auto-mode toggle, and data purge.

Deferred by design (see [`docs/architecture.md`](docs/architecture.md) if you want to pick this up): distributed tracing (OpenTelemetry spans across the Kafka event chain) and a unified Kafka→store ingestion path (today, watchers and the portal write to the shared `AssessmentStore` directly rather than all events flowing through one consumer) — both are real architectural additions, not incremental fixes, and are intentionally out of scope until the above is stable.

## Getting started

Requires **Python >= 3.12**. Uses [`uv`](https://docs.astral.sh/uv/) for dependency management (a `pyproject.toml` + `uv.lock` are provided; plain `pip install -e ".[dev]"` also works).

```bash
git clone https://github.com/alimobrem/AgentIT.git
cd AgentIT
uv sync --extra dev
```

### CLI

```bash
# Score a repo across all 7 dimensions
uv run agentit assess https://github.com/some-org/some-app --format terminal

# Full pipeline: assess + plan + run agents + skills + validate + summarize
uv run agentit orchestrate https://github.com/some-org/some-app --output-dir ./out

# assess + orchestrate + write assessment.json
uv run agentit onboard https://github.com/some-org/some-app --output-dir ./out

# Continuously re-assess on an interval
uv run agentit watch https://github.com/some-org/some-app --interval 3600

# Re-assess every currently-tracked fleet app once and exit (for CronJobs --
# the CronJob's own schedule controls periodicity, not an internal loop).
# Works on both `watch` and `assess`; `--dimension` optionally scopes the
# per-app finding count reported (e.g. only compliance findings).
uv run agentit watch --rescan
uv run agentit assess --rescan --dimension compliance

# Dogfood: assess AgentIT's own repo
uv run agentit self-assess

# Self-fix loop: assess → skill engine generates → LLM reviews → verify → PR
uv run agentit self-fix . --create-pr

# capability-scout: propose a small, evidence-grounded change to AgentIT
# itself as a draft PR (see "Self-improvement of AgentIT itself" above)
uv run agentit propose-watch --interval 86400 --max-open-prs 1

# Learn new skills from CVE/best-practice research
uv run agentit learn --source cves --limit 5

# Targeted learning from an app's specific stack
uv run agentit learn-for https://github.com/some-org/some-app

# Test a skill loads, matches, and generates valid output
uv run agentit test-skill skills/security/network-policy.md

# Promote a draft skill to active
uv run agentit activate-skill skills/custom/new-skill.md
```

Add `--llm` to enable Claude-backed reasoning, or `--no-llm` to force it off (otherwise auto-detected from `ANTHROPIC_API_KEY` / `ANTHROPIC_VERTEX_PROJECT_ID`).

Agent containerization: agents can run as K8s Jobs with `--profile lightweight|standard|full` and `--agents` filter. Set `AGENTIT_AGENT_MODE=kubernetes` to dispatch agents as Jobs instead of local threads.

### Portal (local)

```bash
uv run agentit portal --port 8080
# open http://localhost:8080
```

**Postgres is the only supported store — for local use too, not just the live cluster.** There is no more SQLite code path. Point `AGENTIT_DB_DSN` at any reachable Postgres instance (e.g. `postgresql://agentit:agentit@localhost:5432/agentit`, or run one via `podman run -d -e POSTGRES_USER=agentit -e POSTGRES_PASSWORD=agentit -e POSTGRES_DB=agentit -p 5432:5432 postgres:16-alpine`); the schema is created automatically on first connection. On the live OpenShift deployment, AgentIT deploys and maintains its own bundled, non-operator Postgres instance (`postgres.bundled.enabled`, on by default, in-namespace, no external dependency or entitlement) and every Deployment/CronJob gets `AGENTIT_DB_DSN` wired in automatically. `portal/store.py`'s `AssessmentStore` is fully async (`asyncpg`) throughout, and every store caller in the codebase (CLI, watchers, the portal, `FleetOrchestrator`/`AutoMode`/`RemediationDispatcher`/`RemediationLoop`) `await`s it directly. See [`docs/postgres-migration-plan.md`](docs/postgres-migration-plan.md) (now marked historically-complete/superseded) for the full migration history — including the two real cutover attempts and the bugs found and fixed along the way — for how this cutover happened. A one-time `agentit migrate-sqlite-to-postgres` command exists for anyone with real data in a legacy local SQLite file to import into Postgres.

## Configuration

All configuration is via environment variables (no config file). Nothing here belongs in `values.yaml` or any committed file — see [Security notes](#security-notes).

<details>
<summary><b>Environment variables</b> (click to expand)</summary>

| Variable | Used by | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `llm.py` | Direct Anthropic API auth (alternative to Vertex) |
| `ANTHROPIC_VERTEX_PROJECT_ID` + `CLOUD_ML_REGION` | `llm.py` | Use Claude via Vertex AI instead of the direct API |
| `AGENTIT_LLM_MODEL` | `llm.py` | Override LLM model (default from env) |
| `GITHUB_TOKEN` | `portal/github_pr.py` | Required for PR creation, infra-repo management, webhook registration |
| `AGENTIT_DB_DSN` | `portal/store.py` | Postgres connection string (required — Postgres is the only supported store) |
| `AGENTIT_KAFKA_BOOTSTRAP` | `events.py`, `consumer.py` | Kafka bootstrap servers; publisher/consumer no-op gracefully if unset |
| `AGENTIT_AUTO_MODE` | `automode.py` | `1`/`true`/`on` to enable autonomous apply (also togglable at runtime via `/settings`) |
| `AGENTIT_PORTAL_URL` | `remediation_loop.py` | Base URL the remediation loop calls back into (default `http://localhost:8080`) |
| `AGENTIT_EXTERNAL_URL` | `portal/routes/assessments.py` | Trusted externally-reachable base URL for outbound registrations (e.g. the GitHub webhook URL). Optional — if unset, the app looks up its own OpenShift Route; only falls back to the request's Host header if neither is available. Never derived from client input. |
| `AGENTIT_AGENT_MODE` | `orchestrator.py` | `local` (default) or `kubernetes` — run agents as K8s Jobs. Falls back to the undocumented `AGENT_MODE` if `AGENTIT_AGENT_MODE` is unset, for backward-compat. |
| `GOOGLE_APPLICATION_CREDENTIALS` | Vertex SDK | Path to mounted GCP credentials JSON |

</details>

## Deploying to OpenShift

AgentIT deploys itself the same way it onboards other apps — via the Helm chart in `chart/` and the Argo CD `Application` in `argocd/application.yaml`. **Argo CD is the sole deployer**; see [`docs/deployment.md`](docs/deployment.md) for the full operational runbook.

- Change behavior: edit `argocd/application.yaml` Helm parameters, commit, push. The CI pipeline's `notify-argocd` task rewrites `image.tag` to the build revision in that file, then `oc apply`s it once — so new Helm params sync without briefly deploying the bootstrap `latest` tag. See [`docs/deployment.md`](docs/deployment.md) for the details and why this exists.
- The `smoke-test-image` Tekton task (and GitHub Actions `image-smoke-test`) sets `git config --global --add safe.directory /opt/app-root/src` before `git status`, because OpenShift runs the image under an arbitrary UID that does not see the build-time USER 1001 gitconfig. The image also writes the same path via `git config --system` in the Containerfile so scout/portal pods can use `.git` at runtime. The same Containerfile step marks `.git/` **and** the L3 allowlist trees (`tests/`, `skills/`, `checks/`, `src/`, `docs/`) group-writable so capability-scout can write source diffs before `gh pr create` under OpenShift's arbitrary UID (gid 0).
- Change a secret: `oc create secret` on-cluster, then reference it via a Helm parameter. Never in Git.
- Never `helm upgrade` manually or `oc edit` the `Rollout`.

Key `chart/values.yaml` feature flags: `rollout.enabled` (canary via Argo Rollouts), `kafka.enabled` / `argoEvents.enabled` (event-driven loop), `tektonCI.enabled` (build pipeline), `cronJobs.cveScan.enabled`, `agents.{vulnWatcher,sloTracker,driftDetector}.enabled`, `monitoring.enabled` (ServiceMonitor + PrometheusRule + Grafana dashboard — chart default: disabled; **enabled on the live deployment via `argocd/application.yaml`**, so AgentIT scrapes and alerts on its own `/metrics`), `postgres.bundled.enabled` (AgentIT's own bundled, non-operator Postgres instance — the only store, not a backend option, see [`docs/postgres-migration-plan.md`](docs/postgres-migration-plan.md)), and `auth.enabled` (OpenShift `oauth-proxy` sidecar in front of the portal — see [Security notes](#security-notes) and [`docs/deployment.md#authentication`](docs/deployment.md#authentication)).

The chart includes: NetworkPolicy, ResourceQuota, LimitRange, PodDisruptionBudget, anti-affinity, backup CronJob, dedicated ServiceAccount (not `default`), and a self-assess step in the CI pipeline.

**Auth + backup sync notes (dogfood):** `session_secret` / webhook `token` are filled by an Argo CD **Sync** hook Job (`chart/templates/secret-init-job.yaml`), not PostSync — PostSync never ran while oauth-proxy CrashLooped on an empty secret or while a WaitForFirstConsumer backup PVC stayed Pending. Bundled Postgres backups also ship a one-shot PVC-bind Sync Job so `gp3-csi` claims Bound during the same sync. Prefer `/data` (writable) for watcher event-buffer SQLite when a PVC is mounted.


The `Route` sets `haproxy.router.openshift.io/timeout: 200s` because `/capabilities/learn` runs synchronous CVE research that can take up to 180s server-side — the router's 30s default would otherwise kill the connection with a 504 before the backend responds.

See the full deployment topology diagram: [`docs/architecture.md#deployment-topology-openshift`](docs/architecture.md#deployment-topology-openshift).

## Testing

```bash
uv run pytest -q
```

2,000+ tests across 100 test files (grows continuously; the counts below are a representative breakdown, not an exact partition — verify current totals with `pytest --collect-only`, since this table isn't regenerated on every commit):

| Suite | Tests | What it covers |
|---|---|---|
| Unit tests | ~600 | Analyzers, agents, orchestrator conflict/gate logic, portal routes, the Postgres store, Helm templates |
| LLM evals | 17 | Safety classification, fix review quality, generation correctness, learning agent, architecture summary |
| Browser tests | 61 | Playwright end-to-end tests for all portal pages, Admin Review/Fleet-badge/Actions-tab UI, retired-route 404s, accessibility |
| Performance tests | 22 | Response time assertions on portal endpoints |
| API contract tests | 14 | JSON response shape validation |
| Template rendering | 16 | HTML rendering correctness |
| Webhook security | 18 | GitHub HMAC signature, internal webhook shared-secret token, SSRF, replay protection |
| CSRF & identity | 11 | Double-submit-cookie enforcement/exemptions, `get_current_user` oauth-proxy header fallback |
| Fleet tests | 5 | Multi-app fleet operations |
| Containerization | 22 | K8s Job agent dispatch |
| Futureproof | 16 | Platform context, skill lifecycle, API drift |
| Durability | 12 | Circuit breaker, TTL cache, error recovery |
| Check engine | ~15 | Data-driven check loading, each check type, integration |
| Skill validation | ~15 | All 40 skills load, valid frontmatter, generate valid YAML |
| Self-observability | ~50 | `agent_runs`/`check_results` persistence, DLQ republish, correlation-id tracing, circuit-breaker/DB-size/event-buffer/watcher-staleness metrics, watcher tick telemetry (`tests/test_watchers_telemetry.py`, `tests/test_durability.py`, extensions to `tests/test_store_extended.py`) |

Additional test markers: `--run-real-repos` (clone live GitHub repos), `--live-cluster` (e2e against OpenShift), `--browser-tests` (Playwright), `--run-llm-evals` (requires API key).

## Security notes

- **Browser authentication is opt-in (`auth.enabled`, default `false`).** An OpenShift `oauth-proxy` sidecar can front the portal's Route with the cluster's built-in OAuth login — see [`docs/deployment.md#authentication`](docs/deployment.md#authentication). Off by default so this doesn't change behavior for any existing deployment; flip it on deliberately. Login needs no custom UI — the proxy redirects unauthenticated requests to the cluster OAuth login automatically, before they ever reach the app. The nav bar (`base.html`) shows a "Logged in as {{ current_user }}" + Logout link (pointed at oauth-proxy's `/oauth/sign_out`) only when a real `X-Forwarded-User` header is present on the request — never a fake "logged in" state when `auth.enabled=false`.
- **CSRF protection is always on.** Every browser-originated `POST`/`PUT`/`PATCH`/`DELETE` route requires a matching double-submit-cookie token (`src/agentit/portal/csrf.py`) — auto-attached by htmx for every form, no per-template changes needed.
- **`/api/webhook/*` requires a shared-secret token.** These routes are called only by in-cluster Argo Events Sensors, never a browser, so neither of the above protects them — `verify_internal_token` (`src/agentit/portal/routes/webhooks.py`) checks an `X-Internal-Webhook-Token` header against an auto-generated Secret instead. GitHub's push webhook keeps its own pre-existing HMAC-SHA256 signature check against `GITHUB_WEBHOOK_SECRET`.
- **None of the above is a substitute for network boundaries.** Run the portal behind a trusted network until `auth.enabled` is turned on; even then, `--openshift-sar` only requires "any authenticated user with a role binding in this namespace" unless tightened further.
- **Secrets never belong in Git.** See [Configuration](#configuration) and `docs/deployment.md`.
- **Destructive actions are LLM-gated and fail closed.** `automode.py` only auto-applies when the orchestrator approves *and* the LLM classifies the change as non-destructive with >= 0.8 confidence; if the LLM is unavailable, unconfident, or flags a risk, the change is gated for human review.
- **Manifests are validated before being trusted.** `agents/base.py::validate_manifest()` checks every generated YAML, and `cluster_apply.py` runs a `--dry-run=client` pass before any real apply.
- **SSRF prevention.** `cloner.py` rejects private IPs, localhost, and internal DNS suffixes. `portal/helpers.py::safe_url()` rejects protocol-relative URLs.
- **Circuit breakers.** LLM and Kubernetes API clients use circuit breakers (`CircuitBreaker` in `portal/helpers.py`) to prevent cascading failures.
- **Cross-namespace apply needs `rbac.clusterWideApply` (default `true`).** "Apply to Cluster" onboards an app into its *own* namespace (e.g. a repo named `pinky` gets namespace `pinky`), which usually doesn't exist yet when onboarding starts. `kube.namespace_exists()` does a cluster-scoped `GET` on that namespace, and the SA's own namespace-scoped RoleBinding (`{{ .Release.Name }}-edit`, to ClusterRole `edit`) can't grant that — only a `ClusterRoleBinding` can. `chart/templates/rbac.yaml` has one gated behind `rbac.clusterWideApply`; if it's disabled, every apply into a not-yet-existing namespace 403s before it reaches manifest application, surfacing on `/onboard-results` as "Cluster apply failed — check server logs" (applying into an already-existing namespace, like this app's own self-assessment, doesn't hit this). Note `edit` still doesn't grant `patch` on `ResourceQuota`/`LimitRange`/`Namespace` even cluster-wide — those three continue to show up as apply errors by design, not a bug.
- **Operator installs use a narrowly scoped grant, not `edit`.** The onboard-results "Install Operator" button (`cluster_apply.install_operator`) needs to create a Namespace+OperatorGroup for OwnNamespace-only operators (VPA, ODF, RHBK/Keycloak) or a Subscription in the shared `openshift-operators` namespace for everything else. Even with `clusterWideApply` on, the "edit" ClusterRole only grants get/list/watch on namespaces (not create), so this is gated behind its own `rbac.operatorInstall` flag (default `true`) with a dedicated 4-rule `ClusterRole`. If installs fail with a permissions error, check that this flag is enabled on the release.

## Repository layout

<details>
<summary><b>Full source tree</b> (click to expand)</summary>

```
AgentIT/
├── src/agentit/                    # ~24K lines across 86 Python files
│   ├── cli.py                      # click CLI: 15+ commands (assess, onboard, orchestrate,
│   │                               #   watch, portal, self-assess, self-fix, learn, learn-for,
│   │                               #   test-skill, activate-skill, run-agent, vuln-watch, slo-track,
│   │                               #   drift-detect, learn-watch, propose-watch, consume)
│   ├── capability_scout.py         # capability-scout's research/propose/gate logic (self-improvement
│   │                               #   of AgentIT's own codebase, not the skills catalog -- see docs/
│   │                               #   self-improvement-for-agentit.md)
│   ├── git_pr.py                   # Shared git branch/commit/push + `gh pr create` mechanics,
│   │                               #   extracted from self-fix --create-pr, reused by capability_scout.py
│   ├── runner.py                   # run_assessment(): stack detection + analyzers + check engine
│   ├── skill_engine.py             # Property-based skill matching, lifecycle, LLM generation
│   ├── check_engine.py             # Data-driven YAML check loader and runner
│   ├── skill_inventory.py          # Snapshot/diff skills+checks catalog, log added/removed events
│   ├── learning_agent.py           # CVE/best-practice research, skill generation
│   ├── platform_context.py         # Cluster API discovery (K8s version, CRDs, operators)
│   ├── api_drift_detector.py       # Snapshot-based API surface comparison
│   ├── assessment_diff.py          # Compare two reports, find new/resolved findings
│   ├── property_verifier.py        # Verify skill properties hold after generation
│   ├── dependency_manager.py       # Dependency lifecycle management
│   ├── resource_tuner.py           # Resource right-sizing recommendations
│   ├── llm.py                      # Claude client (Anthropic/Vertex), safety gate, fail-closed
│   ├── automode.py                 # LLM-gated auto-apply (fail-closed)
│   ├── remediation_loop.py         # detect → assess → onboard → apply → verify pipeline
│   ├── cloner.py                   # Shallow git clone with SSRF prevention
│   ├── models.py                   # Pydantic models
│   ├── events.py / consumer.py     # Kafka publisher/consumer (no-op if unavailable)
│   ├── image_builder.py            # Tekton-driven image build
│   ├── kube.py                     # K8s client with TTL cache, Job dispatch — the single,
│   │                               #   mockable interface for cluster ops (core/apps/batch/custom
│   │                               #   objects); `apply_yaml` is the one remaining `oc` subprocess
│   ├── analyzers/                  # 7 read-only analyzers + stack detector + shared base
│   ├── agents/                     # 3 agents (dependency, cost, codechange) +
│   │                               #   orchestrator + capabilities registry --
│   │                               #   security/observability/cicd/compliance/
│   │                               #   infrastructure/incident/release/
│   │                               #   retirement/chaos are skill-only domains now
│   │   ├── orchestrator.py         # FleetOrchestrator: skills-first, agents supplement
│   │   ├── capabilities.py         # Agent registry with resource tiers
│   │   └── base.py                 # Shared contract: Agent(report, output_dir).run()
│   ├── watchers/                   # Long-lived watcher agents
│   └── portal/
│       ├── app.py                  # FastAPI app setup, CSRF middleware, background maintenance
│       │                           #   loop, lifecycle hooks, template filters — routes live in
│       │                           #   routes/*.py, included via app.include_router(...)
│       ├── routes/                 # 98 routes, one APIRouter per domain
│       │   ├── fleet.py            # Fleet dashboard, fleet-wide SLOs/remediations
│       │   ├── assessments.py      # Assess/onboard/deliver lifecycle, GitOps registration, per-agent PR creation, verification
│       │   ├── gates.py            # Gate resolve/cancel actions; Admin Review page (cluster-admin-
│       │                           #   review gates only -- the other 7 gate types render inline
│       │                           #   on Fleet/Assessment Detail via assessments.py/fleet.py)
│       │   ├── capabilities.py     # Skills/checks catalog, learning agent, agents/watchers
│       │   ├── settings.py         # Auto-mode toggle, retention/purge, settings API
│       │   ├── insights.py         # Fleet insights, LLM decision audit, events feed + DLQ
│       │   ├── remediations.py     # Per-assessment remediation items and recommendations
│       │   ├── slos.py             # Per-assessment SLO definitions and error budgets
│       │   ├── webhooks.py         # /api/webhook/* (internal-token-gated) + GitHub push
│       │   ├── health.py           # /health, /healthz, /readyz, platform drift
│       │   └── schedules.py        # Watcher/cron schedule management
│       ├── store.py                # AssessmentStore -- the only supported store, fully async
│       │                           #   (asyncpg/Postgres, 19+ tables: assessments, apps (app-level
│       │                           #   facts like infra_repo_url that persist across
│       │                           #   re-assessments -- see docs/architecture.md's "Data model"
│       │                           #   section), events, gates, SLOs, remediations,
│       │                           #   skill_effectiveness, agent_feedback, deliveries,
│       │                           #   processed_webhooks)
│       ├── migrate_sqlite.py       # One-time `agentit migrate-sqlite-to-postgres` import for anyone
│       │                           #   with real data in a legacy local SQLite file
│       ├── helpers.py              # CircuitBreaker, clone_assess_cleanup, safe_url, async get_store()
│       ├── cluster_apply.py        # oc/kubectl apply with pre-flight checks
│       ├── delivery.py             # Unified apply flow: classify + route_and_deliver()
│       ├── github_pr.py            # GitHub REST API integration
│       └── templates/              # 31 Jinja2 templates (htmx + Alpine.js)
├── skills/                         # 45 property-based skill definitions (12 domains, incl. chaos + a
│                                   #   dynamically-created custom/ the learning agent writes drafts into)
├── checks/                         # 20 data-driven YAML check files (7 dimensions)
├── chart/                          # Helm chart (30+ templates: Rollout, Services, Route, RBAC,
│                                   #   NetworkPolicy, ResourceQuota, LimitRange, PDB, Tekton,
│                                   #   Kafka, Argo Events, watcher agents, backup CronJob,
│                                   #   bundled non-operator Postgres)
├── argocd/application.yaml         # Argo CD Application for self-deployment
├── docs/                           # 13 files -- living docs, implemented-design docs, and dated
│   │                               #   historical records; see each file's own status line
│   ├── architecture.md             # System diagrams, pipeline, event loop, agent fleet (living)
│   ├── deployment.md               # GitOps operational rules + incident writeups (living + historical)
│   ├── postgres-migration-plan.md  # SQLite → Postgres/asyncpg migration history (historical --
│   │                               #   superseded now that Postgres is the only supported store)
│   ├── kafka-hardening-plan.md     # TLS/SASL + multi-broker Kafka -- not started (plan only)
│   ├── unified-apply-flow.md       # route_and_deliver() design -- implemented
│   ├── ui-redesign-proposal.md     # Admin Review/Fleet-badge/Actions-tab IA -- implemented
│   ├── self-improvement-for-agentit.md  # capability-scout design -- implemented (v1 detail differs, see status line)
│   ├── ledger-design-spec.md       # Next unified-activity-feed proposal, not yet built
│   ├── ux-design-requirements.md   # UX research checklist + stack recommendation, not yet built
│   ├── next-gen-ux-concepts.md     # Blue-sky UX brainstorm, not a build spec
│   ├── agent-removal-readiness.md  # Dated readiness audit backing the 9-agent removal (historical)
│   ├── code-review-2026-07-12.md   # Dated point-in-time code review (historical)
│   └── session-status-2026-07-13.md  # Dated session handoff snapshot (historical)
├── Containerfile                   # UBI9 Python 3.12, HEALTHCHECK, non-root
└── tests/                          # 2,000+ tests across 100 files -- see "Testing" above
```

</details>

## License

[MIT](LICENSE)
