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

**Template-fallback placeholder substitution is now complete, with a hard-fail safety net.** When an LLM call truncates (`stop_reason=max_tokens`, or the LLM is unavailable at all) and `SkillEngine.generate()` falls back to a skill's raw Markdown template, the old substitution loop only ever replaced `{{app_name}}` — every other placeholder (`{{image}}`, `{{namespace}}`, `{{scanner_image}}`, ...) shipped literally in the final manifest, silently. `_template_variables()`/`_render_template()` now substitute every placeholder this code path has a real, non-fabricated value for — `{{namespace}}`/`{{app_name}}` (the sanitized repo name, matching the exact convention the delivery route already uses for "the app's own namespace"), `{{repo_url}}`/`{{git_url}}` (`report.repo_url`), and `{{image_ref}}` (`image_builder.get_image_ref()`, the same internal-registry path `build_app_image()`'s real production call sites already push to) — then, regardless of the root fix, scans the rendered output for any remaining AgentIT-style `{{...}}` placeholder and raises `UnresolvedPlaceholderError` rather than shipping it: `generate()` catches that, logs an error, and returns no file at all instead of a manifest a user might apply with literal placeholder text still in it (confirmed live: `app-rollout-patch.yaml` shipped `image: "{{image}}"`, `app-compliance-cronjob.yaml` shipped `"--namespace", "{{namespace}}"` verbatim). The substitution regex deliberately only matches bare-identifier placeholders (`{{app_name}}`), never Go-template/Alertmanager notification syntax skills legitimately ship verbatim for the receiving system to evaluate at runtime (`{{ .GroupLabels.alertname }}`, `{{ range .Alerts }}...{{ end }}` in `alertmanager-config.md`/`pagerduty-config.md`).

**`audit-policy` no longer fabricates a never-applyable resource.** `apiVersion: audit.k8s.io/v1, kind: Policy` is not a real Kubernetes REST API resource on any cluster — it's a static file schema consumed only by kube-apiserver's own `--audit-policy-file` startup flag, never something `kubectl apply` accepts. Applying it always failed, and `cluster_apply.py`'s missing-operator heuristic misattributed that failure to a missing Kyverno install (Kyverno's own CRD happens to also be named `Policy`, in a completely unrelated API group) — a wrong, misleading fix suggestion. The skill now delivers the same real audit policy rules as advisory reference documentation in a ConfigMap (the same pattern `incident/runbook.md`/`retirement/decommission-plan.md`/`compliance/compliance-evidence.md` already use), with instructions for how a cluster-admin actually wires it in on vanilla Kubernetes vs. OpenShift — never silently treated as "already enforced," and never reaching `cluster_apply.py`'s CRD-missing/missing-operator path at all.

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

1. **Feedback loop, wired into every real apply path.** `record_skill_outcome()` — previously only called from `self-fix` — now fires from the unified Deliver action (`routes/assessments.py::deliver`), gate resolve (`routes/gates.py::resolve_gate`, both approve and reject), and auto-mode's successful auto-apply (`automode.py::AutoMode.execute`), via the shared `skill_engine.record_skill_outcomes()` helper. The manual apply route and `AutoMode.execute()` themselves share one orchestration function, `cluster_apply.apply_with_verification()` — it preserves the one real difference between them (the manual route makes a single call honoring its own `dry_run` form flag with no automatic follow-up; auto-mode always dry-runs first via `force_dry_run_first=True` and only proceeds to a real apply if that dry-run is clean) while consolidating `record_skill_outcomes()` and `audit_log()` so neither call site can drift out of sync — closing a real gap where auto-mode's own auto-applies previously left no audit trail at all. Each generated file's producing skill is recovered from `SkillEngine.generate()`'s `{app_name}-{skill.name}.yaml` naming convention (`skill_engine.skill_name_from_path()`) since neither `AgentResult` nor `onboarding_results.files_json` carry a structured skill-name field; `GeneratedFile.skill_name` (set directly by `SkillEngine.generate()`) gives the CLI's `self-fix` path exact attribution instead of the previous "app-name-skill-name" bug. Effectiveness is a **recency-weighted** rate (`AssessmentStore.get_skill_effectiveness()`/`get_low_effectiveness_skills()`, half-life ~90 days) so a skill that was bad months ago and has since improved can recover off the "Skills Needing Review" list on Insights, rather than being stuck flagged forever by outcomes that no longer reflect its current behavior. `SkillEngine.run_all()` now actually uses its `store` parameter: a skill whose domain has been rejected 3+ times for an app is skipped outright (mirroring the same threshold `webhooks.py` already used for auto-fix), and a human's most recent correction for that app+domain is passed to LLM generation as extra guidance (`get_human_override()`). Skill activation (`capabilities.py`'s "Activate" button) now runs a real functional check (`skill_engine.verify_skill()` — frontmatter completeness plus an actual generation smoke test against a synthetic fixture, validated with `agents/base.py::validate_manifest()`) before flipping `status: draft` → `active`, instead of a blind string replace; a skill that can't produce valid output is blocked, not silently activated. **Activation durability (real incident, confirmed live):** `skills/` is baked into the container image at build time (`Containerfile`'s `COPY skills/ skills/`, no PVC/volume mount), so the in-pod file write above used to be the *only* effect of activating a skill — silently wiped back to `status: draft` by the very next redeploy (confirmed via direct `oc exec` file checks on the live pods, cross-referenced against the `skill-activated` event log: a trio of CVE-mitigation skills activated on 2026-07-16 was wiped by a redeploy minutes later, and again on 2026-07-17). `activate_skill_route()` now also calls `_persist_skill_activation()`, reusing `git_pr.create_branch_commit_push()` + `git_pr.open_draft_pr()` — the exact branch/commit/push/draft-PR mechanics `capability_scout.py`'s `_open_pr()` already uses for AgentIT's own repo — to open a draft PR for the `status: active` flip, rather than pushing straight to `main`: every existing automated flow that touches this repo opens a PR instead of committing directly (capability_scout.py's own docstring states that convention explicitly), and `main` having no GitHub branch-protection rule doesn't change that established precedent. The in-pod activation still happens immediately either way (so the skill is usable right away and a git failure never makes things worse than before); if the git/PR step fails, the Capabilities page surfaces an explicit warning toast (alongside the success toast) that the activation is temporary and will be lost on the next redeploy unless retried or committed manually — never a silently swallowed persistence failure.
   - **Edit-before-apply flow, now built** (previously a "Known gap" — manifests were approve-as-is or reject-and-regenerate only, with no "diff between generated and applied content" to capture). The onboard-results page (`onboard_results.html`) now lets a human edit any generated file's raw content in place before delivery, via a real textarea editor, not a mockup. Saving an edit (`POST /assessments/{id}/onboard-results/edit-file` → `AssessmentStore.update_onboarding_file()`) rewrites the *same* `onboarding_results` row's file entry — the exact row `get_onboarding()`/`route_and_deliver()` already read — so the SAVED (possibly-edited) content, not the original LLM/template output, is what a subsequent "Deliver" click actually delivers: a genuine round trip, not a preview. The first edit captures `original_content` (never overwritten by later edits of the same file) alongside the live `content`, so a real line-level diff — `portal/content_diff.py::diff_lines()`, tagged add/remove/context rows rendered via CSS classes per this repo's "no inline styles" rule — is always reconstructable and shown inline on the results page. YAML/YAML-adjacent edits are re-validated through the existing `agents/base.py::validate_manifest()` path before being persisted (a human's raw edit could introduce a syntax or structural error the original generation wouldn't have had); an invalid edit is rejected outright with a visible error, never partially saved. Because `route_and_deliver()`'s `classify_file()` always classifies off whatever `content` is currently persisted, editing a file's `kind` in a way that changes its taxonomy category (e.g. a ConfigMap edited into a Secret) is classified — and blocked or rerouted — off the real edited content; there is no separate edit-only code path that could bypass the router's existing taxonomy/safety gates. `route_and_deliver()` now also records `edited_files` in each `deliveries` row's `details_json`, so "was this delivery's content edited from what was originally generated" is a permanent, queryable fact, not a transient UI detail — surfaced today on the onboard-results page's Delivery History table, and available to any future Ledger/Decisions view over the same `deliveries` table.

2. **Learning agent, now reading its own effectiveness data.** The research cycle (`SkillLearner.research_once()`, and the portal's "Research Skills" button) checks `get_low_effectiveness_skills()` **first**: if any skill is flagged, the LLM is asked specifically to propose a replacement (`learning_agent.research_skill_improvement()`) for each flagged skill (up to the configured limit), and only falls back to the generic CVE sweep when nothing's flagged. This is the wiring that actually closes the loop — before it, the learning agent was blind to which of its own already-shipped skills humans kept rejecting. Runs automatically every 24h via the `skill-learner` watcher (chart default: disabled — enable with `agents.skillLearner.enabled=true`; currently enabled on the live deployment via `argocd/application.yaml`), and can also be triggered on demand from the Capabilities page or via `agentit learn` / `agentit learn-for` on the CLI. Draft skills get an "Activate" button right next to them on the Capabilities page — the full research → draft → human-review → active loop runs end-to-end in the portal, no CLI required.
   - **Persistence and cross-pod visibility.** The `skill-learner` watcher runs in its own pod, separate from the portal, with no shared filesystem between them — this cluster has no ReadWriteMany storage class available (confirmed via `oc get storageclass`: only `gp2-csi`/`gp3-csi`, both EBS-backed and `ReadWriteOnce`-only), which ruled out a shared/RWX PVC as the fix. Instead, every draft the watcher generates is pushed straight to the portal via an internal-token-authenticated API call (`POST /api/webhook/skill-draft`, `routes/webhooks.py`) — the same `AGENTIT_PORTAL_URL` + `AGENTIT_INTERNAL_WEBHOOK_TOKEN` pattern `RemediationLoop` already uses to call back into the portal from a separate watcher pod. That endpoint calls the exact same `save_skill()` the portal's own in-process "Research Skills" button uses, into the portal's own `skills/` tree, and busts its 60s skills cache, so a watcher-drafted skill is visible on the Capabilities page on the very next page load — no restart or manual sync step needed. If the portal can't be reached that cycle, the draft falls back to the watcher's own dedicated, single-consumer PVC (`agents.skillLearner.persistence`, default on, mounted at `/data/skills` via `AGENTIT_SKILLS_DIR`) so nothing is lost, and `SkillLearner._save_draft()` logs a loud warning in that (expected to be rare) case, since a draft that only exists on that PVC stays invisible until a human recovers it. **Real 2026-07-15 incident:** `AGENTIT_PORTAL_URL` points at the Argo Rollouts *stable* Service (`http://agentit.agentit.svc:8080`), whose selector only flips over to a canary's new ReplicaSet once that rollout fully promotes — mid-rollout, the still-serving old pod genuinely 404s a route that's correctly wired in the code about to become stable (confirmed live: `oc exec`-curling the exact path returned 404 from the stable-hash pod and 401 — i.e. route present — from the canary-hash pod behind the same Service). `_submit_draft_to_portal()` now retries specifically on a 404 (a few attempts with a short delay) before falling back to the PVC, so a routine rollout window doesn't cost a draft.
   - ~~**Documented future idea (not built):** cross-onboarding stack-signature detection~~ — **shipped**: `src/agentit/stack_signature_detector.py` detects repeated uncommon stacks (auto-trigger `learn-for` remains a separate follow-up). Do not re-propose the detector. `tick_failure_classifier` classifies permission-denied (and similar) tick failures into remediation hints.

3. **Platform awareness** — `PlatformContext` discovers the cluster's K8s version, available APIs, CRDs, and operators. Every skill generation includes this context. The API drift detector auto-deprecates a skill specifically when the API kind it generates has been removed from the cluster (a narrower guarantee than the effectiveness-based flagging in tier 1). `FleetOrchestrator.run()` treats an empty `available_kinds` result from `discover_platform()` as "the has_api() gate has no signal to work with" and skips gating entirely (`platform=None`) — this is checked independently of whether `k8s_version` resolved, since K8s/OpenShift expose the version endpoint even to identities with no other RBAC (e.g. a least-privilege ServiceAccount), which previously let a real-but-empty discovery result silently gate every skill's output to zero instead of triggering the fallback. **Dogfood fix (skill-improvement unblock):** `has_api()` now re-syncs after `discover_platform()` assigns `available_kinds` (dataclass post-init left `_lower_kinds` empty, so every skill was gated out and `skill_effectiveness` stayed empty), and `kube.get_api_resources()` passes `auth_settings=["BearerToken"]` on named-group discovery so calls are not anonymous 403s that left only ~26 core kinds. **Proven live (2026-07-16):** after [#42](https://github.com/alimobrem/AgentIT/pull/42), pinky skill generate produced 24 skill files; 5 rejects flagged `resourcequota`; learner `mode=skill-improvement` drafted and activated `resourcequota-scoped` (`loop_health` 100%).

**Loop visibility.** The Capabilities page's skill table links each skill to a per-skill lifecycle page (`/capabilities/skills/{name}/history`) showing its full effectiveness trend (every recorded outcome, most recent first) and its activation/deprecation history (matched from the `events` table by skill name — `skill-added`/`skill-removed`/`skill-activated`/`skill-deprecated`/`skipped-rejected`/`skill-improvement-drafted`). The Insights page adds one loop-health meta-metric: of the skills currently flagged low-effectiveness, what percentage have had an improvement actually drafted for them in the last 30 days (`AssessmentStore.get_loop_health()`) — a live snapshot of whether the loop is actually turning, not just theoretically closed, using data that's only non-trivially populated now that tier 1's wiring exists.

**Learn-button transparency.** The "Research Skills" button previously left no trace beyond a transient toast — `learning_agent.describe_learning_run()` now backs a `learning-run` event that both entry points (the button and the `skill-learner` watcher's own tick) log for **every** outcome, not just the ones that generated a skill: success, a no-op skip, or an outright failure (LLM unavailable, exception mid-run) all leave a durable, queryable row. A "Learning Agent Runs" table on the Capabilities page surfaces the last 15 of these (timestamp, manual vs. automatic trigger, what was researched, outcome), and the button's own description now dynamically previews what it's *about* to do — naming the currently-flagged low-effectiveness skill(s) it will try to improve, or stating that it'll fall back to a generic CVE sweep — plus the `skill-learner` watcher's real last-heartbeat status (not the chart's default, which the live deployment overrides), so "is automatic research even on, and did it already run today" no longer requires guessing.

**Real incident: "Automatic (24h watcher)" rows appearing every ~5-7 minutes, stuck on one skill.** Two distinct root causes, both in `watchers/skill_learner.py`, neither a trigger-mislabeling bug like capability-scout's "Run Scan"/"Automatic" fix (no other caller invokes `SkillLearner.research_once()` directly, so `_log_run`'s `trigger="watcher"` is never actually wrong here). (1) `run()` unconditionally called `research_once()` immediately on startup with no memory of when this watcher last actually ticked, so any pod restart (crash, redeploy, rescheduling — this Deployment redeploys often) produced an extra, unscheduled tick regardless of how little wall-clock time had passed, even though `--interval` is genuinely `86400` everywhere (`chart/values.yaml`, `argocd/application.yaml`). `SkillLearner._seconds_since_last_tick()` now reads the watcher's own persisted heartbeat (`agent_heartbeat("skill-learner")`, already written by `record_tick` every loop iteration) so `run()` sleeps out the remainder of `--interval` instead of re-ticking right after a restart. (2) `get_low_effectiveness_skills()` flags a skill purely by historical human-rejection rate, which doesn't change just because a research attempt failed — with no dedup/cooldown logic at all, every tick re-researched the identical flagged skill with zero memory of prior failures (same category as capability-scout's resourcequota-rejection-sampler stuck-loop bug). `learning_agent.count_recent_improvement_failures()` replays this watcher's own `learning-run` history (each failed attempt already lands in that run's `skipped` list under `mode="skill-improvement"`) so `research_once()` can back off a skill that's already failed `improvement_cooldown_attempts` (default 3) times within `improvement_cooldown_hours` (default 24h), falling through to the next flagged skill or the CVE sweep instead of spinning forever — the window ages out naturally, so a skill isn't banned permanently, just given a rest.

**Auditing LLM decisions.** Every place the LLM's output directly gates an outcome (not just generates content) persists a real, attributed record, surfaced on the **Decisions** page: `self-fix`'s Step 3 first-approver gate (`LLMClient.review_fix`) — attributed by real skill name via `skill_effectiveness` — auto-mode's safety classification (`LLMClient.classify_action`, `AutoMode.execute`) — attributed by the real originating agent when the caller knows it (e.g. the dispatcher's `result["agent"]`), otherwise the generic `auto-mode` component name, which is still the common case since most callers apply a whole bundle of manifests spanning several agents at once — the security analyzer's LLM-based secret false-positive filter (`classify_secret`, `SecurityAnalyzer._check_secrets`), which decides per match whether to keep or drop a potential-secret finding, attributed to the generic `security-analyzer` component and persisted via `llm_decisions.build_secret_classify_events()` + `store.log_event()` (action `secret-classify`) — and capability-scout's self-improvement proposal cycle (`LLMClient.propose_capability_improvement`), attributed to the generic `capability-scout` component. See `llm_decisions.py`'s module docstring for the full attribution details of each.

## Self-improvement of AgentIT itself (capability-scout)

The loop above (`skill-learner`) improves what AgentIT *generates for other apps* — the skills catalog. It has no counterpart that improves AgentIT's *own* codebase, its portal routes, its watchers, its CLI. `capability-scout` is that counterpart: a separate, opt-in, 24h watcher (`agents.capabilityScout.enabled`, default **off** — this is a live-deployment decision for the repo owner to make explicitly, not something enabled as a side effect of shipping it) that mirrors `skill-learner`'s shape exactly — **research → propose → verify → human review → merge** — but aimed at AgentIT's own repo. See [`docs/self-improvement-for-agentit.md`](docs/self-improvement-for-agentit.md) for the full design; this is deliberately named distinctly from `self-assess`/`self-fix` (which run AgentIT's existing hardening pipeline *against* AgentIT's own repo, generating K8s manifests for it — not proposing new Python features for AgentIT's product surface).

**Ownership split (skill-learner ↔ scout).** `skill-learner` owns catalog drafts and the Capabilities **Activate** path for other-apps skills. `capability-scout` owns AgentIT-repo PRs (`agentit/self-improve/*`). Scout may propose a skill/check fix when effectiveness is low, but only as a source PR against this repo — never a second draft of the same artifact via the learner webhook. One owner per artifact: learner → Activate; scout → GitHub PR.

**The loop, end to end, every 24h:**

1. **Real signal, never fabricated.** `capability_scout.gather_evidence()` reads fleet-wide rejection rates by finding category (`AssessmentStore.get_fleet_wide_rejection_stats()`, a new `GROUP BY` aggregate over `agent_feedback`), agent run health (`get_agent_stats()`), check compliance (`get_check_compliance()`), skill effectiveness (`get_skill_effectiveness()`/`get_low_effectiveness_skills()`/`get_recent_skill_activity()`/`get_loop_health()`), recent watcher tick failures, prior proposal outcomes (`capability-outcome` merged/closed/stale), a real, introspected list of the store's own public methods (`list_store_capabilities()`, e.g. `record_skill_outcome`), and — the highest-precision signal — a static grep of this repo's own `docs/*.md` for "Known gap" / "Deliberately deferred" / "Documented future idea" / "not built" text (`capability_scout.scan_doc_gaps()`; the image `COPY`s `docs/` into `/opt/app-root/src` so that path resolves in the capability-scout pod, same as `tests/`/`chart/`). Fewer than 5 real data points anywhere → the cycle logs an honest no-op, never an invented proposal (the store-capabilities/recent-skill-activity fields are pure "does this already exist" context, not counted toward that signal floor). **Gap-detection fix (root cause of PRs #47/#53/#63/#88, all "record per-rejection reasons for `resourcequota`," all closed):** `gather_evidence()` used to never tell the LLM that this is already `skill_effectiveness.reason`/`record_skill_outcome()` under the hood, and `proposal_already_implemented()` only ever checked a literal expected filename — see item 6 below for the second half of the fix.
2. **One LLM proposal, or none.** `LLMClient.propose_capability_improvement()` (mirrors `detect_eol_risks()`'s `_chat()`/graceful-failure/JSON-parsing convention) is given only that real evidence and asked to propose **at most one** small, evidence-cited change — title, gap description, the exact evidence that grounded it, suggested target files, risk, and a test plan — or to explicitly propose nothing. It's instructed to prefer a documented doc-gap over inventing one, and to never suggest touching `chart/`, `argocd/`, `.github/workflows/`, or anything secret/RBAC-related. `_chat()` takes an explicit `max_tokens` per caller rather than one global default: this call's 7-field, multi-paragraph response gets a 2048-token budget (`detect_eol_risks()`'s open-ended risk list gets 1024) while the simple safe/unsafe-style classifiers keep the 512-token default — a real proposal was previously getting cut off mid-JSON under that same 512 budget and correctly logging a `no-proposal` outcome for a genuinely-too-small budget rather than a bad proposal.
3. **Real, executable safety gates — not stubs.** `capability_scout.run_safety_gates()` runs, in order, fail-closed: diff-size cap (≤3 files, ≤150 lines), scope allowlist (`src/agentit/`, `skills/`, `checks/`, `tests/`, `docs/` only), a secret-pattern regex scan, a test-plan-required check, `py_compile` on every touched `.py` file, a `gh pr list`-backed check that no `agentit/self-improve/*` PR is already open (configurable via `agents.capabilityScout.maxOpenPRs`, default 1), and finally the **exact** `pytest tests/ -q --ignore=...` invocation `.github/workflows/tests.yml` uses. Any failing gate discards the cycle — no PR opens, but the attempt (and exactly which gate blocked it) is still logged.
4. **A real draft PR, never a direct commit.** When every gate passes, `git_pr.py` (extracted from `self-fix --create-pr`'s existing branch/commit/push mechanics, not reimplemented) creates a new `agentit/self-improve/<slug>-<date>` branch, commits the one artifact this cycle produced (a reviewable `docs/proposals/<slug>.md` write-up citing the evidence verbatim — see the module docstring in `capability_scout.py` for why v1 documents a proposed change rather than mechanically applying a source diff to files the LLM has never seen the contents of), pushes it, and opens it via `gh pr create --draft`. Existing CI (`tests.yml`/`security.yml`) runs on it like any other PR. Nothing here ever auto-merges.
5. **Every outcome is logged, every cycle.** One `capability-run` event (`capability_scout.CAPABILITY_RUN_ACTION`) is logged whether the cycle proposed something, got gate-blocked, or found no signal — mirroring `learning-run`'s "every run leaves a trace" convention exactly.
6. **L4 outcome feedback.** Each cycle starts by polling self-improve PR URLs via `get_pr_status` and logging durable `capability-outcome` events (`merged` / `closed` / `stale`, with a `reject_reason` from an explicit `agentit:reject-reason:…` label/body line, **or**, when no human used that convention, a real-phrase heuristic over the PR's actual comment thread — `fetch_pr_close_comments()` + `gh pr view --json comments`, since `get_pr_status()` never reads comments and every real capability-scout PR closed so far explained its reason there, not in a label or body edit). Discovery combines prior `capability-run` `pr_url`s **and** `gh pr list` for `agentit/self-improve/*` branches, so human/Cursor merges that never logged a scout `pr_url` (e.g. `#23`) still get an outcome row. The next `gather_evidence()` prefers untried doc gaps, deprioritizes recent `wontfix`/`duplicate` titles, skips already-merged modules (e.g. `#20` stack-signature, `#23` tick-failure classifier), and cites `cited_merges` in the run's details JSON. Close a PR with label `agentit:reject-reason:wontfix` (or the same line in the body) to keep scout off that gap for 30 days; a `reject_reason` of `duplicate` (explicit label/body, or inferred from a comment like "this duplicates existing functionality") blocks the same title/slug **permanently**, not just for 30 days — an already-existing capability doesn't stop existing after a cooldown, unlike a deprioritized-but-still-real `wontfix` gap. `proposal_already_implemented()` also checks the new `store_capabilities` evidence directly (not just a literal expected filename), so a proposal whose title/gap matches a known already-existing store method (currently: any "record/monitor per-rejection reasons" phrasing against a confirmed `record_skill_outcome`) is flagged before it ever reaches the safety gates.

**Fully transparent from inside the portal, without needing to already know a PR exists:**
- A **Self-Improvement** tab on the Capabilities page (`/capabilities/self-improvement`) — a "Self-Improvement Runs" table mirroring "Learning Agent Runs" (timestamp, trigger, evidence considered, distinct outcome badges for `proposed` / `already-implemented` / `gate-blocked` / `no-signal` / …, live PR status), plus a **Cited merges (L4)** panel from recent `capability-outcome` rows.
- A per-run drill-through (`/capabilities/self-improvement/runs/{event_id}`) mirroring `/capabilities/skills/{name}/history`'s layout: evidence, `cited_merges` / proposal-outcome context, a per-gate pass/fail table, and the resulting PR's live status — polled via the same `github_pr.get_pr_status()` call `onboarding_history()` already uses, no `gh` needed inside the portal process itself.
- A `capability-proposal` entry on the **Decisions** page (`llm_decisions.py`), filterable alongside every other real LLM decision, attributed to the `capability-scout` component.
- A one-line addition to the **Schedules** page's watcher table (`WATCHER_AGENTS`) — real heartbeat-derived status, zero new route/template code, the same mechanism every other watcher already gets for free.

GitHub's own PR UI stays the surface for reviewing the actual code diff; the portal is where a human sees what the loop considered and why.

```bash
# Long-lived watcher (24h default; dogfood cadence restored from temporary 1h) -- mirrors `learn-watch`
uv run agentit propose-watch --interval 86400 --max-open-prs 1

# One-shot cycle for dogfood / debugging (no startup grace, no loop)
uv run agentit propose-once --mode auto --max-open-prs 1
```

**Build modes:** `docs` (proposal markdown only), `source` (edit `skills/`/`checks/`/`tests/`/`src/agentit/` when every target is in that allowlist), `auto` (source when eligible, else docs). Prefer **new small modules** over rewriting large files — full-file generation of big modules fails and (in `source`/`auto`) skips the cycle rather than opening a docs-only PR. When a proposal targets an existing file already over the 150-line size cap, scout rewrites that target to a new `src/agentit/<feature>.py` sibling before calling the LLM (so L3 cycles are not stuck gate-blocked on `diff-size`). File generation uses a higher token budget and a compact JSON retry when the first reply truncates. Dogfood sets `agents.capabilityScout.mode=auto` via Helm.

See [`docs/superpowers/plans/2026-07-15-autonomous-self-improve-dogfood.md`](docs/superpowers/plans/2026-07-15-autonomous-self-improve-dogfood.md) for the L0→L5 dogfood milestone plan (substrate → source PRs → outcome loop), and [`docs/dogfood-self-improve-milestone.md`](docs/dogfood-self-improve-milestone.md) for the 2026-07-16 retrospective (L4 on AgentIT; L5 full on pinky via portal Approve & Deliver → [agentit-gitops#10](https://github.com/alimobrem/agentit-gitops/pull/10)).

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

**Per-agent advisory PRs are deduped against the live default branch (2026-07-17 fix).** The **Per-Agent PRs** delivery choice (`github_pr.create_agent_prs()`) used to unconditionally branch/commit/push/open a PR for each of `codechange`/`cost`/`dependency`'s generated files on every call, even when that exact content had already merged via an earlier run. Because those three agents regenerate deterministic advisory content from unchanged repo state, and each always reuses the same branch name (`agentit/{agent_name}`), a later identical run opened a genuinely empty, redundant PR once the first one merged — confirmed live: [#89](https://github.com/alimobrem/AgentIT/pull/89)/[#90](https://github.com/alimobrem/AgentIT/pull/90)/[#91](https://github.com/alimobrem/AgentIT/pull/91) were each a byte-identical re-run of content already merged via [#85](https://github.com/alimobrem/AgentIT/pull/85)/[#86](https://github.com/alimobrem/AgentIT/pull/86)/[#83](https://github.com/alimobrem/AgentIT/pull/83) (`files: []`, `+0/-0` against current `main`). `create_agent_prs()` now diffs each agent's generated files against what's already at their destination path on the target repo's *freshly fetched* default branch (a live GitHub Contents API call every invocation, never a cached/stale ref) before committing anything — byte-identical content skips the branch/commit/PR entirely for that agent and is reported back as `skipped` (surfaced as an `agentit-prs-skipped` event, `routes/assessments.py::create_agent_prs_route`) instead of opening a no-op PR.

**GitOps registration** is a real, current query — `kube.get_custom_resource("argoproj.io", "v1alpha1", "applications", f"managed-{app}", namespace="openshift-gitops")` — not just "was an infra repo URL ever set." It only falls back to `report.infra_repo_url is not None` when the cluster call itself fails (offline/unreachable, e.g. tests); a successful call that simply finds no `Application` is **not** registered regardless of `infra_repo_url`.

**A brand-new app could never actually bootstrap into GitOps-registered state (2026-07-17 fix).** `route_and_deliver()`'s cluster-config mechanism decision picked `MECHANISM_DIRECT_APPLY` whenever `registered` was `False`, even when `infra_repo_url` was already known — but Direct Apply never commits anything to the infra repo's `apps/{app}/` directory, and Argo's `ApplicationSet` only ever creates a live `Application` by discovering that directory already committed. So an app whose infra repo was known but not yet live-registered could never reach `registered=True` via this path — a closed loop with no escape. `resolve_cluster_config_mechanism()` is now the single source of truth for this decision (used by `route_and_deliver()`, `gate_delivery_confirmation()`, and the Onboard Results dry-run preview): knowing an infra repo URL is what matters for whether to commit there, not `registered` already being `True` — the very first delivery for a known infra repo now bootstraps `apps/{app}/` itself. Direct Apply remains the true fallback only when no infra repo is known at all.

**Silent infra-repo auto-create failures on the primary Assess path (2026-07-17 fix).** `_auto_create_infra_repo()` (called from `assess_submit`'s background job — the path every human Assess uses) swallowed every exception with only a `logger.warning()`, unlike the standalone `register_gitops()` retry route, which already surfaces failures via a real `?error=` flash. A failed auto-create now logs a real `infra-repo-creation-failed` event (visible on Events and, via a `_EVENT_ACTION_TO_CARD_TYPE` entry, the Ledger) and shows a dedicated banner on Assessment Detail — distinct from the generic "never registered" nudge, and cleared automatically once a later successful registration event supersedes it.

**`cluster-conflict-review` gate type removed as unreachable — step 3 of 5 (2026-07-17).** Verified precisely (not assumed) before removing anything: the only creator of this gate type was `automode.py`'s `_gate_for_conflicts()`, called from `_finish_direct_apply()`'s two conflict-handling branches, which require `apply_with_verification()`/`apply_manifests_to_cluster()`/`kube.apply_yaml()` to have actually been called for the cluster-config category — provably impossible now that step 2 removed `MECHANISM_DIRECT_APPLY` as a live outcome (that whole call chain is dead code, confirmed by grepping every caller). `_gate_for_conflicts()` and `_conflict_gate_summary()` are removed; the two call sites now fold a conflict into the same generic dry-run-failed/partial-failure gating as any other error, without creating a dedicated review gate for it. `routes/gates.py::resolve_gate()`'s `cluster-conflict-review`-specific branch (the one place that ever passed `force=True` to `kube.apply_yaml()`) is removed — a gate of this type, if one somehow still exists from before this directive, now falls through to the same generic `route_and_deliver()` path any other gate type does. `cluster-admin-review` (the CI/CD-shared-namespace escalation gate) is a genuinely different, still fully reachable code path — it's created independently of the cluster-config category's mechanism entirely (`route_and_deliver()` always escalates a manifest targeting a shared operator namespace to this gate, regardless of Direct Apply's fate) and was deliberately left untouched, per `docs/onboarding-loop-vision-gap-analysis.md` §1's own recommendation that it answers a fundamentally different question ("does this service account have RBAC for a shared namespace," not "should this fix be delivered via GitOps or directly"). `kube.apply_yaml()`'s `force` parameter is now unreachable too (no caller passes `force=True` anymore) but deliberately left in place — its removal belongs with `cluster_apply.py`'s own dead-code cleanup (step 5), not this step's gate-type-focused scope.

**Direct Apply removed from the onboarding/delivery UI and mechanism resolution — step 2 of 5 (2026-07-17).** `resolve_cluster_config_mechanism()` (`delivery.py`, the single source of truth `route_and_deliver()`/`gate_delivery_confirmation()`/the Onboard Results dry-run preview all go through) can no longer select `MECHANISM_DIRECT_APPLY` as a live outcome, for any caller, ever — it now takes only `infra_repo_url` (the `registered` parameter is gone; it never changed the decision, only strengthened it once already known): a known infra repo always resolves to `MECHANISM_INFRA_REPO_COMMIT` (bootstrapping `apps/{app}/` on the very first delivery, exactly as before), and no known infra repo at all (only possible for an assessment saved before step 1 landed) resolves to `MECHANISM_NONE` — a hard refusal, never a fallback to a direct cluster apply. `route_and_deliver()`'s now-fully-dead literal `apply_manifests_to_cluster()` call for this category is removed. `onboard_results.html`'s Deliver button is hardcoded to "Commit & Open PR" (never "Apply to Cluster" — `_deliver_label`'s ternary on `gitops_registered` is gone), and the "Override" bypass button (which skipped the Dry Run soft-gate with a danger confirm) is removed entirely — Deliver now always requires a real, successful Dry Run first, with no escape hatch; if no infra repo is known at all, Deliver is additionally blocked outright with a clear "Not GitOps-registered" message instead of a soft gate. Fleet's and Assessment Detail's "Direct apply" badge is replaced with "Not GitOps-registered" (or "GitOps (pending)" for the bootstrap case: infra repo known, first delivery not yet merged) — never implying a live cluster-mutating fallback exists. `MECHANISM_DIRECT_APPLY`'s string constant and its `confirmation_text()`/`MECHANISM_DESCRIPTIONS` handling are deliberately kept (never selectable, but still renderable) purely so historical `deliveries`/`gates` rows already persisted with this mechanism (from before this directive) still show honest text instead of a blank/`KeyError` lookup. `AutoMode`'s own direct-apply branch, its dead conflict-handling code (`cluster-conflict-review` gate creation, now provably unreachable), and the auto-mode allowlist's fate are **not yet addressed** — that is steps 3-4, tracked separately; this step only updated the tests that broke as a direct, mechanical consequence of this mechanism change (without restructuring `AutoMode`'s own code yet). One real, discovered behavior change worth flagging: `AutoMode`'s remediation-completion bookkeeping (`_complete_remediations()`) was only ever wired to the direct-apply success path, never the GitOps-commit path (opening a PR isn't delivery — the cluster isn't mutated until a human merges) — since GitOps commit is now cluster-config's only reachable outcome, a cluster-config remediation now stays `"generated"`, not `"completed"`, until a human decision wires completion to the eventual PR-merge event instead.

**GitOps registration is now mandatory — Direct Apply removal, step 1 of 5 (2026-07-17).** Product directive: all apps must use GitOps; Direct Apply is being removed as a concept entirely, across several separately-tested, separately-committed steps (this is step 1 — see the following entries in this section for the rest as they land). Before any UI/mechanism removal, the gate itself has to exist: `assess_submit()`/`_assess_sync()`/`self_assess_route()` now resolve a real, trusted-host `infra_repo_url` via `_resolve_mandatory_infra_repo_url()` **before** cloning/running the assessment pipeline at all — auto-created via `_auto_create_infra_repo()` when the human didn't supply one, otherwise the human-supplied URL, either way validated against `AGENTIT_TRUSTED_GIT_DOMAINS` (`github_pr.is_trusted_git_host()`, factored out of `ensure_applicationset()` so both enforce the identical allowlist). Any failure — an untrusted human-supplied host (rejected synchronously, before the background job even starts), a repo-creation permission error, an auto-created repo somehow landing off-allowlist — now raises `InfraRepoRequiredError`: a hard stop on the whole Assess job (no assessment is ever saved), with a real, actionable `infra-repo-creation-failed` critical event and the exact message surfaced on the failed job's progress page. This is a deliberate, accepted tradeoff: an app whose GitHub org/token can't auto-create a private infra repo now hits a hard stop with **no Direct Apply fallback** — not a bug to work around. Verified live against a local Postgres portal (`AGENTIT_OFFLINE=1`, no `GITHUB_TOKEN` configured): Assess fails visibly with the actionable message, no assessment row is created, and the critical event shows up on `/events`.

**"The repo" is never ambiguous — every app has two.** Its own code repo (`report.repo_url`, where `source-repo-pr`/`app-repo-pr` open PRs) and, once GitOps-registered, a distinct infra/GitOps repo (`report.infra_repo_url`, where `infra-repo-commit` commits+PRs land for Argo CD to sync). Fleet's table and Assessment Detail's header now show clearly labeled **Code repo** / **GitOps repo** links side by side (the GitOps link only renders once both `gitops_registered` and `infra_repo_url` are known, never a broken/empty href). Every PR reference — Delivery History, the Ledger's delivery cards, the Deliver flash alert, Per-Agent PRs, and a merged `gitops-pr-pending` gate — now says which repo it targets ("PR opened against the code repo" / "...the GitOps repo") instead of a bare link, traced from the real mechanism via `delivery.repo_kind_for_mechanism()` rather than guessed.

**AutoMode's terminal action** (`should_auto_apply()`'s safety classification is completely unchanged) now depends on that registration check: not registered → the exact same direct-apply behavior as before. Registered → AutoMode still autonomously commits and opens a PR (it already trusts its own safety classification enough to skip human review for a direct apply), but creates a `gitops-pr-pending` gate instead of finishing — a human must merge the PR; **AgentIT never auto-merges**, matching this project's own "Argo CD is the sole deployer" stance for its own deployment. The manual portal **Deliver** path does the same for GitOps-registered apps: after a successful infra-repo PR it creates `gitops-pr-pending` so Approve & Deliver can merge. Files still containing unresolved placeholders (e.g. `REPLACE_WITH_AGENTIT_IMAGE`) are stripped from every delivery mechanism before commit/apply.

**AutoMode genuinely routes through this router now (2026-07-17 fix).** Despite this section's own framing above, `AutoMode.execute()` previously did **not** call `route_and_deliver()` at all — it hand-rolled an equivalent-looking but separately-maintained routing decision (its own GitOps-registration check, then a raw `apply_with_verification()` call on the whole approved batch) and never called `classify_file()`. Concretely, that meant AutoMode's auto-apply path was missing every guard every other path got for free: no `Secret` hard-block (a `kind: Secret` manifest could theoretically reach its direct-apply branch), no unresolved-placeholder guard, and no CI/CD-shared-namespace escalation (a CI/CD manifest destined for `openshift-pipelines`/etc. hit `cluster_apply.py`'s older `skip_operator_ns` silent-skip instead of a `cluster-admin-review` gate). `AutoMode.execute()` now calls `route_and_deliver()` for the actual routing/classification/mechanism decision once its own two additive safety layers — `should_auto_apply()`'s LLM classification and the per-(namespace, kind) allowlist scoping below — have approved a batch, so `classify_file()`'s guards apply uniformly to every delivery path, AutoMode included. Its own `force_dry_run_first=True` behavior (always dry-run first before a real apply) is threaded straight through to the router's direct-apply branch unchanged, and AutoMode still reacts to that outcome itself (dry-run-failed / field-manager-conflict gating) since no other router caller exercises that knob. See `tests/test_automode_extended.py::TestExecuteUnifiedRouterGuards` for before/after proof of the closed gaps.

**Every delivery is tracked** in a new `deliveries` table (`store.py`, same as every other table) — what was routed, which mechanism was chosen, delivery status, and verification outcome. Direct applies and GitOps commits with an actual SLO already being tracked for that assessment get the same 60-second SLO-watch tail (generalized out of `RemediationLoop` into `remediation_loop.verify_slos()`/`rollback_action()`) run in the background; `DriftDetector`'s existing ~10-minute Argo poll closes the loop for GitOps deliveries specifically once it observes the committed revision synced (Argo sync isn't synchronous, so this can't happen inline).

**Rollout verification outcomes were silent (2026-07-17 fix).** `verify_and_close_delivery()` updated a `deliveries` row's status column (`verified`/`rolled_back`/`breach-reported`) but never called `log_event()`/`publish_event()` — confirming a delivery healthy, or catching a real SLO breach, produced no Ledger card and no observable event, only a status column nobody was watching. Mirrors `slo_tracker.py`'s `rollback-recommended` pattern (Kafka publish + best-effort store event) for all three outcomes, mapped into `ledger.py`'s card F so they render in the Ledger too.

**The `gitops-pr-pending` gate's PR link was inert text (2026-07-17 fix).** `gate_card()`'s summary embeds the real infra-repo PR URL as plain text, auto-escaped by Jinja into something a human had to copy/paste by hand. `create_gate()` now takes a structured, optional `pr_url` (a new `gates.pr_url` column), and `gate_card()` renders it as a real "View pull request" `<a href>` alongside the existing summary.

**Point-of-no-return confirmation.** A dynamically-labeled single Deliver CTA (`onboard_results.html`: **Apply to Cluster** when Direct, **Commit & Open PR** when GitOps) replaces the old independent apply/PR buttons — the mechanism is no longer a human's choice for cluster/app config. Confirm modal `confirmText`, step-guide, and dry-run alerts use those same labels (never "Deliver Now"). Both that button and every gate's "Approve & Deliver" button show the exact same "AgentIT will: ..." statement (`delivery.confirmation_text()`) twice: once as page-level preview text, and again inside the un-skippable confirm modal — never only on an earlier dry-run screen. Confirm consequence copy is mechanism-honest: GitOps confirms that a PR opens and the **cluster is not mutated until merge**; Direct Apply names the target namespace and says it **may modify cluster resources**. The PR path never uses "cannot be undone" / "modifies production".

**Onboarding action UX.** On `/assessments/{id}/onboard-results`, the primary path is a vertical **Dry Run → deliver choice** step stack (numbered markers + connector, lifecycle-stepper tokens): after Dry Run, two equal primaries — **Commit & Open PR** / **Apply to Cluster** (one combined delivery) and **Per-Agent PRs** — with a one-line hint (“One PR for everything, or a PR per agent”). **Download** stays secondary. Status chips ("No dry run yet", "Dry run passed") sit outside the CTAs. Both deliver options are soft-gated until a successful Dry Run: the server passes `dry_run_done` (persisted `apply_results` **or** `dry_run_summary` flash) into the template and Alpine `dryDone`, so GitOps dry-runs unlock Commit and never show “Dry run complete” next to “No dry run yet”. Messaging stays to one status region; GitOps / Admin Review live in compact `<details>`. Danger "Override" remains for the combined path. Fleet is the home name everywhere (H1 / breadcrumbs / error page — not "Dashboard" / "Enterprise Readiness"). Assessment Detail's primary CTA follows lifecycle (Onboard → Review & Deliver → demoted Re-onboard). **Per-finding Fix** on the Findings / Remediation Plan tabs is hidden while lifecycle is `assessed` (Onboard This App is the generation path); after onboarding it returns with the shared confirm modal + htmx busy indicator and still only generates — Dry Run → deliver choice on Onboard Results remains the delivery path.

**Onboarding progress stuck forever on a live instance (2026-07-17 fix).** The stall-fallback added to `onboard_progress.html` a day earlier (commit `2c7c461`) correctly re-fetches the progress URL once the SSE stream goes quiet and redirects on if the job's row has reached a terminal `status` — but a job on a real, deployed instance (`assessment_id=f98b8a445...`, self-assessing AgentIT's own repo) stayed on `status='running'` for 11+ minutes with no `onboarding_results` row ever written, confirmed live via a direct query against the bundled Postgres pod: the already-shipped fix was working exactly as designed, re-checking a status that itself was never going to change. Root cause: `onboard_submit`'s FastAPI `BackgroundTasks` coroutine (and `assess_submit`'s equivalent `threading.Thread`) tracks its `remediation_jobs` row from *within the process that started it* — there's no persistent queue, and nothing else ever revisits that row. This cluster's `agentit` Deployment rolls a new ReplicaSet every few minutes (confirmed via `oc get rs`/`oc get events` — ordinary CI-driven redeploys, not a malfunction), so any job whose owning pod is replaced before it finishes is orphaned forever at its last non-terminal status. `AssessmentStore.reap_orphaned_jobs()` now fails any job non-terminal for longer than `with_timeout`'s 300s ceiling plus a wide buffer (900s total) — safe even with multiple replicas, since a job genuinely still in progress on a *live* pod can never legitimately be older than that ceiling. Called once at startup (a fresh process could never have legitimately created a still-running row itself) and every 5 minutes after (`portal/app.py::_background_maintenance`), logging a `job-reaped` event so the interruption is visible on the app's own Timeline, not silent.

**Refresh Onboard (re-assess chain).** Re-assess always creates a **new** assessment row, so lifecycle drops back to `assessed` and manifests must be regenerated — that used to mean two clicks (Re-assess, then Onboard again). For apps that have already onboarded once, Fleet (and Cmd+K) replace **Re-assess** with a single primary **Refresh Onboard** CTA: one confirm posts `/assess` with `continue_onboard=1`, and when the assess job completes the portal atomically chains into onboard progress (same as clicking Onboard). Never-onboarded apps keep plain Re-assess → Assessment Detail → Onboard.

**Assess→Onboard chaining is now the default for every Assess (2026-07-17 fix).** `continue_onboard` used to default to off, so only Fleet's Refresh Onboard button (above) ever chained — the "New Assessment" modal, the command palette's re-assess, and plain Re-assess all left a human to separately click Onboard. `assess_submit()`'s `continue_onboard` Form field now defaults to `"1"`, so every real caller chains unless it explicitly opts out (`continue_onboard=0`) — nothing does today. `assess_progress.html`'s existing "onboarding will start automatically" message (previously shown only for Refresh Onboard) now fires for every chained Assess, so the automation is visible, not silent.

**The `diff.auto_fixable` webhook loop discarded its own generated fixes (2026-07-17 fix).** `webhook_github_push`'s re-assessment path (triggered on every push to a managed repo's default branch) dispatches a fix for every newly-detected auto-fixable finding when `auto_mode` is on, but used to call `RemediationDispatcher.dispatch()` and throw the result away — the generated files were produced and immediately discarded, with no persisted remediation and no delivery attempt. Now mirrors `fix_finding()`'s `save_remediation()` persistence pattern, then — since this branch only runs once `auto_mode` is already on — also calls `AutoMode.execute()` to actually deliver, the same fully-autonomous treatment `RemediationLoop.trigger()` already gives webhook-driven fixes elsewhere.

**Fleet showed AgentIT as two rows (2026-07-17 fix).** `get_fleet_data()`'s `GROUP BY repo_url` really is exactly one row per unique `repo_url` — the bug was never the grouping, it was that two different literal `repo_url` strings existed for the same real repo: `self_assess_route`'s hardcoded canonical URL (`https://github.com/alimobrem/AgentIT`, no `.git`) vs. the CI/CD Tekton pipeline's post-build fleet-refresh call to `/api/webhook/assess` (`chart/templates/tekton/pipeline.yaml`'s `repo-url` param, `https://github.com/alimobrem/AgentIT.git` — matching `argocd/application.yaml`'s own `repoURL` convention). `repo_name` (`repo_url.rstrip("/").split("/")[-1].removesuffix(".git")`) already collapsed both to the same displayed name "AgentIT", which is exactly what made two genuinely separate Fleet rows look like a grouping bug rather than an identity-string mismatch — confirmed live: `.../AgentIT` (23 assessments, 3 onboardings, GitOps-registered) and `.../AgentIT.git` (20 assessments, 0 onboardings) were two real `assessments`/`apps` rows. `AssessmentStore.save()` now runs every `repo_url` through `normalize_repo_url()` (strips trailing slash(es) and a trailing `.git` suffix; deliberately never touches letter case) before storing, so self-assess, the portal form, the CLI, and every webhook path collapse to one identity going forward regardless of which entry point submitted the URL. Pre-existing duplicate rows from before this fix are not auto-merged — see the store's test suite (`test_fleet_collapses_git_suffix_and_trailing_slash_duplicates`) for the normalization contract, and re-run `/self-assess` (or re-POST `/assess` with the canonical URL) for any already-duplicated app to consolidate its history under one `repo_url` going forward.

**Real per-field-manager server-side-apply.** `kube.apply_yaml()` no longer shells out to `oc apply --server-side --force-conflicts` — it applies each document in a manifest individually via the Kubernetes Python client's dynamic-client server-side-apply support (`content_type="application/apply-patch+yaml"`, `field_manager="agentit"`), with `force` defaulting to `False`. On a genuine field-manager conflict (HTTP 409), it returns a structured, distinguishable result (`conflict: True`, `conflict_details: [...]`) instead of either failing silently or forcing through — every apply path (`AutoMode`, the unified delivery router, gate resolution) routes a real conflict to a dedicated `cluster-conflict-review` gate. Approving that gate is the **only** place in the app that ever passes `force=True`, seizing ownership from the other manager after explicit human review.

**Per-(namespace, resource-kind) auto-mode allowlist.** The `auto_mode` setting is still a single global on/off toggle by default (see Settings below) — but an operator can additionally scope it to specific `namespace/kind` patterns (`*` wildcards allowed on either side, e.g. `*/ConfigMap`, `prod/NetworkPolicy`) via the same Settings page. `AutoMode.execute()` splits each apply batch per file against this allowlist rather than treating it all-or-nothing: an allowed `ConfigMap` and a disallowed `ClusterRoleBinding` in the same batch apply and gate independently (`auto-mode-scope-review` gate for the denied portion). `Secret`/`Role`/`RoleBinding`/`ClusterRole`/`ClusterRoleBinding` can never be allowlisted, even by an explicit or wildcard pattern naming them. With no allowlist configured (the default), this is a pure no-op — identical whole-batch behavior to before it existed.

## Web portal

`agentit portal` launches a FastAPI + Jinja2 app (htmx + Alpine.js for interactivity, no frontend framework). 98 routes.

**Masthead / IA boundaries:** `/` redirects to **Ledger** (ops home). Primary nav is Ledger (Needs You badge), Fleet, Admin Review (only when elevated count &gt; 0), Health, Insights. **Events** is bell-only (drawer → `/events` + DLQ; not ops home). The drawer overlay/panel default to CSS `display: none` and open only via Alpine `.open` (not x-show alone), so an hx-boost Alpine re-init race cannot leave an invisible full-screen click catcher. **Decisions** stays in the account menu (Ledger owns the stream). When Admin Review count is 0 it is buried in the account menu with subtitle “Elevated approvals”. Exclusive-job table: [`docs/portal-experience-design-language.md`](docs/portal-experience-design-language.md) §1. The Cmd+K command-palette search sits in the **right** masthead cluster with Events / Menu (max-width constrained) so it never covers primary links. Drawer rows with an `assessment_id` deep-link to Assessment **Actions** (else Ledger Needs You by app, else Events correlation). **Back to Assessment** links on Onboard Results / Remediations / SLOs / History / progress use `hx-boost="false"` so they always land on Assessment Detail. **Running Assessment** (`/assess/progress/{job_id}`) keeps the same masthead shell: its 2s htmx poll targets `#main-content` (not `body`), so progress never becomes a full-viewport chrome-less takeover.

**Attention signals:** the primary-nav gate badge and Ledger “Needs You” chips use `badge-accent` (`--color-accent`). Fleet is scoreboard-only — a quiet “N need you → Ledger” link, not a pending-ops column. Assessment Detail shows a **next-step hint** under the lifecycle stepper: while `assessed`, **Onboard This App** always wins (leftover Actions gates are demoted — Onboard generates remediations for all findings; don’t fix one-by-one on Actions first); after onboard, pending Actions win. The Actions link uses `?tab=actions`, not a dead Alpine click outside `x-data`. Pending gates dedupe by `repo_url` + `gate_type` (app-scoped); slo-tracker ticks each app once so re-assess does not create Actions ×N. Delete is visually de-emphasized in a danger-zone slot opposite the primary Onboard action. Capabilities collapses reference catalogs (skills/checks/how-onboarding-works) with `<button>` toggles by default so activity/stats stay above the fold.

**Experience Design Language (EDL):** normative portal UI contract — button hierarchy (short ≤3-word CTAs, `.btn` skin, no status-inside-button), Dry Run → deliver-choice onboarding path (combined vs per-agent), modals/a11y, badges, feedback, and compact `.filter-bar` / `.filter-field` toolbars for list/log GET filters (Decisions, Events, Ledger) — in [`docs/portal-experience-design-language.md`](docs/portal-experience-design-language.md). Agents should load [`.cursor/rules/portal-edl.mdc`](.cursor/rules/portal-edl.mdc). Enforce with `uv run pytest tests/test_portal_edl.py -q` (also `uv run python scripts/check_portal_edl.py`; button SHOULD rules for label length / `.btn` class are asserted in CI).

Key pages:

| Page | Purpose |
|---|---|
| **Fleet** (`/fleet`) | Portfolio scoreboard — apps, scores, Assess / Re-assess (or **Refresh Onboard** when previously onboarded) / Delete, GitOps vs Direct-apply + sync badges. Pending human gates are **not** Fleet's job: a quiet “N need you → Ledger” link points at Needs You (`fleet.py::_attach_pending_actions` still computes counts for Ledger) |
| **Ledger** (`/ledger`; `/` → here) | Morning inbox — Needs You (default), what happened, human gates needing action. Primary-nav badge = Needs You count. Backed by `ledger.py::get_ledger_cards()` (16 card types in [`docs/ledger-design-spec.md`](docs/ledger-design-spec.md)); app-scoped Ledger tab on Assessment Detail; rewind scrubber at `/ledger/chain/<correlation_id>` |
| **Assessment Detail** | 7-dimension scores, lifecycle stepper, score trend + a rendered score-history table with deltas, a GitOps-registration badge + a dismissible **Register for GitOps** nudge (confirm + busy state, optional infra-repo URL, inline error/success after POST; auto-creates `agentit-gitops` under the token user when blank) for unregistered apps, a 4th **Actions** tab showing that app's own pending gates (Approve & Deliver / Reject / Dismiss) across **every** historical assessment of this app, not just the current one (`AssessmentStore.list_gates_for_assessment()` joins by `repo_url`), timeline, remediation items |
| **Onboarding Results** | Generated manifests + vertical Dry Run → deliver choice (Commit & Open PR / Apply to Cluster **or** Per-Agent PRs; both soft-gated until Dry Run; status chips outside CTAs; Download secondary). See "Onboarding action UX" above. |
| **Admin Review** | Elevated RBAC queue only: `cluster-admin-review` gates. Primary nav when count &gt; 0 (badge); when 0, buried in account menu with “Elevated approvals”. App-owner gates live on Ledger Needs You / Assessment Detail Actions. `/gates` redirects here. |
| **Insights** | Fleet stats, agent performance (from real `agent_runs` records), low-effectiveness skills, fleet-wide check compliance (pass rate per data-driven check across every recorded assessment), and fleet-wide learning feedback. Actionable rollups deep-link to Fleet / remediations / Ledger Needs You / Events; agent rows → `/agents/{name}`; skills needing review → per-skill history |
| **Decisions** | Audit of every real LLM *decision* point (fix-review, auto-mode classify — not just LLM-generated content), attributed by the agent or skill that triggered it, with the LLM's actual reasoning and a per-agent/skill approve/reject/gate breakdown. See `llm_decisions.py` for exactly what's covered and what isn't. |
| **Capabilities** | Skills/checks catalog, onboarding agents, watchers, and the **Research Skills** trigger. **Skill Activity** reads `skill_effectiveness` (`skill_name` / `outcome` / `reason` / `app_name`) via `get_recent_skill_activity()` — not `agent_feedback` field names. Its **Self-Improvement** tab has the matching **Run Scan** trigger for `capability-scout` (previously only reachable via its 24h watcher tick). Tabbed with **Agents** (live registry of who's actually run, their real success rate, and a per-agent run-history table with duration/resource tier/error; Registry/Detail status badge is heartbeat-age-derived like Schedules', not the always-`'active'` `agent_registry.status` column, so a crashed/long-stopped agent shows stale instead of Active forever) |
| **Events** | Bell feed + DLQ (filters/pagination) — not ops home. `correlation_id` Chain column; DLQ republishes Kafka dead-letters |
| **Health** | Live infrastructure telemetry — rollout/pod/pipeline status, Kafka, circuit breakers, deploy status |
| **SLOs** | SLO definitions and error budgets. Fleet SLOs (`/fleet/slos`) and per-app SLO lists read `AssessmentStore.list_slos()`, which is scoped by app `repo_url` (so SLOs survive re-assessment) and keeps only the newest assessment's copy of each `(metric_name, target_value)` left by repeated onboarding — otherwise each metric appeared once per historical assessment. Same-identity rows on one assessment stay visible. |
| **Settings** | Auto-mode toggle, per-(namespace, resource-kind) auto-mode allowlist, decision matrix, configuration. Tabbed with **Schedules** (watcher status — now backed by real heartbeats — and cron jobs, each paired with a real English description from a general 5-field cron parser, `schedules.py::humanize_cron()` — covers the weekly-on-a-day-and-hour / monthly-on-a-day-and-hour shapes AgentIT's own skill templates actually generate (plus daily/yearly), replacing a 5-entry exact-string lookup that echoed any other cron back as its own "human-readable" version, rendering it duplicated on the page; a genuinely unparseable cron now shows no second span instead of that duplicate) |

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
- **Clickable System Health cards** — each `/health` stat card (and Argo/Tekton/Kafka rows) deep-links to the best real ops surface: OpenShift Observe metrics, console Pods/Rollout/PipelineRun/Application/Kafka pages, or GitHub commit/Actions. URLs come from `AGENTIT_CONSOLE_URL`, the cluster `Console` CR, or the portal's own Route apps-domain (`portal/health_links.py`); unresolved destinations stay non-clickable with a tooltip reason — never mocked.
- **Ambient deploy-status indicator** — a compact badge in `base.html`'s nav, present on every page, shows the running version/commit (`portal/metrics.py::get_build_info()`, an in-memory read of the `agentit_build` metric — no per-request I/O) and switches to a live "Deploying · &lt;stage&gt;" state, polled via htmx (`GET /api/deploy-status`, every 15s) from `_get_deploy_status()` in `routes/health.py`. That function combines the running build with the newest `agentit-ci` PipelineRun by `creationTimestamp` (list order is not guaranteed) and the live `agentit` Argo CD `Application`'s sync/health (same RBAC as the existing Argo-reading code — no new grants needed): a PipelineRun actually running, Argo out-of-sync / `Progressing` / `Suspended` (canary pause), or an in-flight sync (`operationState.phase=Running`) shows "Deploying"; a failed PipelineRun or settled `Degraded` Application health shows "Deploy failed" with the real reason; **Cancelled** / `PipelineRunCancelled` CI (capacity / concurrency noise) stays idle rather than pinning the badge to Failed. The badge path is hardened against a wedged kube-apiserver: short per-call timeouts (2s), an overall `asyncio.wait_for` deadline (~3s), and a ~20s last-good cache so htmx polling cannot pin portal workers until oauth-proxy returns 502/503 — timeouts return **200** with degraded/unknown (or last-good) HTML instead of hanging. The Health page's "Deployment Status" section reuses the same function (`include_commit_info=True`) to add a stage-by-stage task stepper, the commit message being deployed (`portal/github_pr.py::get_commit_info()`), and — once a deploy is no longer in progress — whether this instance actually ended up running the new version or rolled back (compared against its own live `AGENTIT_GIT_COMMIT`, now wired in `chart/templates/deployment.yaml` from `.Values.image.tag`, the same value the CI pipeline pins to the deployed commit SHA). No field is ever fabricated: an unreachable PipelineRun/Argo/GitHub API surfaces via the badge/panel's `errors`/`reason` fields instead of silently looking idle.
- **Prometheus gauges actually set** — `agentit_active_gates` updates on every gate create/resolve/expire; `agentit_build` is populated once at startup (package version + `AGENTIT_GIT_COMMIT`/`AGENTIT_IMAGE_TAG`, now real live values — see the deploy-status indicator above); `agentit_db_size_bytes` / `agentit_db_rows_total{table=...}` / `agentit_event_buffer_backlog` refresh every 5 minutes from the portal's background maintenance loop (via async helpers -- `refresh_db_metrics()`/`diff_and_log_inventory_changes()`/`prune_stale_agents_and_log()`/`_reap_orphaned_jobs()`, the last of which also runs once immediately at startup -- see "Onboarding progress stuck forever on a live instance" above); `agentit_watcher_last_success_timestamp{watcher=...}` backs the `AgentITWatcherStale` alert described above; `agentit_synthetic_probe_up` / `agentit_route_cert_expiry_days` / `agentit_backup_last_status{target=...}` / `agentit_secret_check_status{secret=...}` (see "Operating AgentIT on itself" above) are all set via internal webhooks from CronJobs, not from in-process code.
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

**Postgres is the only supported store — for local use too, not just the live cluster.** There is no more SQLite code path. GitHub Actions CI (`.github/workflows/tests.yml`) provisions an ephemeral `postgres:16-alpine` service with `POSTGRES_HOST_AUTH_METHOD=trust` and a passwordless `AGENTIT_TEST_PG_DSN` so secret scanners are not tripped by throwaway CI credentials. Point `AGENTIT_DB_DSN` at any reachable Postgres instance (e.g. `postgresql://agentit:agentit@localhost:5432/agentit`, or run one via `podman run -d -e POSTGRES_USER=agentit -e POSTGRES_PASSWORD=agentit -e POSTGRES_DB=agentit -p 5432:5432 postgres:16-alpine`); the schema is created automatically on first connection. On the live OpenShift deployment, AgentIT deploys and maintains its own bundled, non-operator Postgres instance (`postgres.bundled.enabled`, on by default, in-namespace, no external dependency or entitlement) and every Deployment/CronJob gets `AGENTIT_DB_DSN` wired in automatically. `portal/store.py`'s `AssessmentStore` is fully async (`asyncpg`) throughout, and every store caller in the codebase (CLI, watchers, the portal, `FleetOrchestrator`/`AutoMode`/`RemediationDispatcher`/`RemediationLoop`) `await`s it directly. See [`docs/postgres-migration-plan.md`](docs/postgres-migration-plan.md) (now marked historically-complete/superseded) for the full migration history — including the two real cutover attempts and the bugs found and fixed along the way — for how this cutover happened. A one-time `agentit migrate-sqlite-to-postgres` command exists for anyone with real data in a legacy local SQLite file to import into Postgres.

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
| `AGENTIT_OFFLINE` | `kube.py` | `1`/`true`/`on` for a genuine hard-offline guarantee: `kube.get_client()` (and everything else in `kube.py`, which all resolve through it) raises `KubeError` immediately instead of resolving a real client. Exists because unsetting `KUBECONFIG` alone is **not** sufficient — the Kubernetes Python client's default config-resolution chain still falls back to the ambient default `~/.kube/config` regardless, which is exactly how two independent live reviews (each explicitly `unset KUBECONFIG` expecting zero cluster access) ended up connecting to a real, live cluster anyway. Set this when testing/reviewing any code path that could reach `kube.py` and you want a hard guarantee it won't touch a real cluster. |
| `GOOGLE_APPLICATION_CREDENTIALS` | Vertex SDK | Path to mounted GCP credentials JSON |

</details>

## Deploying to OpenShift

AgentIT deploys itself the same way it onboards other apps — via the Helm chart in `chart/` and the Argo CD `Application` in `argocd/application.yaml`. **Argo CD is the sole deployer**; see [`docs/deployment.md`](docs/deployment.md) for the full operational runbook.

- Change behavior: edit `argocd/application.yaml` Helm parameters, commit, push. The CI pipeline's `notify-argocd` task rewrites `image.tag` to the build revision in that file, then `oc apply`s it once — so new Helm params sync without briefly deploying the bootstrap `latest` tag. See [`docs/deployment.md`](docs/deployment.md) for the details and why this exists.
- The `smoke-test-image` Tekton task (and GitHub Actions `image-smoke-test`) sets `git config --global --add safe.directory /opt/app-root/src` before `git status`, because OpenShift runs the image under an arbitrary UID that does not see the build-time USER 1001 gitconfig. The image also writes the same path via `git config --system` in the Containerfile so scout/portal pods can use `.git` at runtime. The same Containerfile step marks `.git/` **and** the L3 allowlist trees (`tests/`, `skills/`, `checks/`, `src/`, `docs/`) group-writable (`chmod -R g+w`) so capability-scout can create **and overwrite** source diffs before `gh pr create` under OpenShift's arbitrary UID (gid 0). Runtime half: `src/agentit/write_guard.py` preflights those paths in `_open_pr`, drops stale permission-denied tick-failure evidence once the tree is writable again, and the scout Deployment mounts an `emptyDir` at `/tmp/agentit-scout` (`TMPDIR`) for git/gh/py_compile scratch.
- Change a secret: `oc create secret` on-cluster, then reference it via a Helm parameter. Never in Git.
- Never `helm upgrade` manually or `oc edit` the `Rollout`.

Key `chart/values.yaml` feature flags: `rollout.enabled` (canary via Argo Rollouts), `kafka.enabled` / `argoEvents.enabled` (event-driven loop), `tektonCI.enabled` (build pipeline), `cronJobs.cveScan.enabled`, `agents.{vulnWatcher,sloTracker,driftDetector}.enabled`, `monitoring.enabled` (ServiceMonitor + PrometheusRule + Grafana dashboard — chart default: disabled; **enabled on the live deployment via `argocd/application.yaml`**, so AgentIT scrapes and alerts on its own `/metrics`), `postgres.bundled.enabled` (AgentIT's own bundled, non-operator Postgres instance — the only store, not a backend option, see [`docs/postgres-migration-plan.md`](docs/postgres-migration-plan.md)), and `auth.enabled` (OpenShift `oauth-proxy` sidecar in front of the portal — see [Security notes](#security-notes) and [`docs/deployment.md#authentication`](docs/deployment.md#authentication)).

The chart includes: NetworkPolicy, ResourceQuota, LimitRange, PodDisruptionBudget, anti-affinity, backup CronJob, dedicated ServiceAccount (not `default`), and a self-assess step in the CI pipeline. Namespace `ResourceQuota` defaults leave headroom for canary Rollouts plus concurrent Tekton runs (`limits.cpu=20`, `pods=60`); tighter quotas can stall canary pods (`ProgressDeadlineExceeded`) and leave the portal on a stale image tag.

**Auth + backup sync notes (dogfood):** `session_secret` / webhook `token` are filled by an Argo CD **Sync** hook Job (`chart/templates/secret-init-job.yaml`), not PostSync — PostSync never ran while oauth-proxy CrashLooped on an empty secret or while a WaitForFirstConsumer backup PVC stayed Pending. Bundled Postgres backups optionally ship a one-shot PVC-bind Sync Job (`postgres.bundled.backup.pvcBindHook`, default **false** — enable only when first binding a Pending WaitForFirstConsumer backups PVC); leaving it on after Bound can re-create a Pending Job on a zone-pinned RWO volume and surface Argo as Degraded. The chart PDB selects `app.kubernetes.io/component: portal` only so Job pods (secret-init, backup) do not trigger PDB SyncFailed (`jobs.batch` has no scale subresource). Prefer `/data` (writable) for watcher event-buffer SQLite when a PVC is mounted.

**Build identity env:** Portal and watcher Deployments set `AGENTIT_GIT_COMMIT` / `AGENTIT_IMAGE_TAG` (scout also `GIT_REVISION`) from `.Values.image.tag` — the same Helm param CI pins to the deploy SHA — so those env vars cannot lag the container image after an Argo sync.

**Argo Pipeline drift:** `argocd/application.yaml` `ignoreDifferences` covers Tekton-normalized `Pipeline` fields (`taskSpec.metadata`/`spec`, empty sidecar `computeResources`); the chart also emits matching `computeResources: {}` on the `run-tests` postgres sidecar so `Pipeline/agentit-ci` can stay Synced.


The `Route` sets `haproxy.router.openshift.io/timeout: 200s` because `/capabilities/learn` runs synchronous CVE research that can take up to 180s server-side — the router's 30s default would otherwise kill the connection with a 504 before the backend responds.

See the full deployment topology diagram: [`docs/architecture.md#deployment-topology-openshift`](docs/architecture.md#deployment-topology-openshift).

## Testing

```bash
uv run pytest -q
```

Portal EDL conformance (static template walk + key-page HTML assertions):

```bash
uv run pytest tests/test_portal_edl.py -q
uv run python scripts/check_portal_edl.py
```

Asserts MUST rules from [`docs/portal-experience-design-language.md`](docs/portal-experience-design-language.md): no status badges inside `<button>`, Dry Run → deliver choice with status outside CTAs, modals `role="dialog"` + Escape, `pr_url|safe_url`, badge ≥12px, Events bell/drawer IA, `#toasts` / `.btn-danger` present. Also asserts button SHOULD rules: ≤3-word labels and `.btn` (or documented icon control) on interactive `<button>`s. GitOps dry-run unlock coverage in `tests/test_deliver_route.py` accepts HTML-escaped `Commit &amp; Open PR`.

### Browser / journey tests (CI-gated)

GitHub Actions runs a dedicated **`browser-critical`** job (Playwright + Chromium) on every PR. It executes the lean Alpine/htmx journeys in `tests/test_browser_critical.py` only:

- Dry Run success → **Commit & Open PR** enabled (no contradictory “No dry run yet” / `NO DRY RUN YET`)
- **Back to Assessment** clickable after hx-boost (Events drawer overlay not blocking)
- **Register for GitOps** surfaces success/error after a boosted redirect (toast or inline alert)

The full crawl (`tests/test_browser.py`) stays ignored in CI and Tekton — too broad/flaky for a merge gate. Tekton’s `run-tests` task mirrors the default pytest ignores (no Chromium in the UBI image); browser coverage is the Actions job above.

```bash
uv sync --extra dev --extra browser
uv run playwright install chromium
uv run pytest tests/test_browser_critical.py --browser-tests -q
```

`--browser-tests` is required (see `tests/conftest.py`); without it, `@pytest.mark.browser` tests skip so capability-scout and the default suite never need Chromium.

2,000+ tests across 100 test files (grows continuously; the counts below are a representative breakdown, not an exact partition — verify current totals with `pytest --collect-only`, since this table isn't regenerated on every commit):

| Suite | Tests | What it covers |
|---|---|---|
| Portal EDL | ~8 | Experience Design Language MUST rules (`tests/test_portal_edl.py` + `scripts/check_portal_edl.py`) |
| Unit tests | ~600 | Analyzers, agents, orchestrator conflict/gate logic, portal routes, the Postgres store, Helm templates |
| LLM evals | 17 | Safety classification, fix review quality, generation correctness, learning agent, architecture summary |
| Browser critical (CI) | 4 | Playwright journeys gated in Actions `browser-critical` (`tests/test_browser_critical.py`) |
| Browser crawl (local) | 61 | Full Playwright crawl of portal pages — ignored in CI (`tests/test_browser.py`) |
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

Additional test markers: `--run-real-repos` (clone live GitHub repos), `--live-cluster` (e2e against OpenShift), `--browser-tests` (Playwright critical journeys), `--run-llm-evals` (requires API key).

## Security notes

- **Browser authentication is opt-in (`auth.enabled`, default `false`).** An OpenShift `oauth-proxy` sidecar can front the portal's Route with the cluster's built-in OAuth login — see [`docs/deployment.md#authentication`](docs/deployment.md#authentication). Off by default so this doesn't change behavior for any existing deployment; flip it on deliberately. Login needs no custom UI — the proxy redirects unauthenticated requests to the cluster OAuth login automatically, before they ever reach the app. The nav bar (`base.html`) shows a "Logged in as {{ current_user }}" + Logout link (pointed at oauth-proxy's `/oauth/sign_out`) only when a real `X-Forwarded-User` header is present on the request — never a fake "logged in" state when `auth.enabled=false`.
- **CSRF protection is always on.** Every browser-originated `POST`/`PUT`/`PATCH`/`DELETE` route requires a matching double-submit-cookie token (`src/agentit/portal/csrf.py`) — auto-attached by htmx for every form, no per-template changes needed.
- **`/api/webhook/*` requires a shared-secret token.** These routes are called only by in-cluster Argo Events Sensors, never a browser, so neither of the above protects them — `verify_internal_token` (`src/agentit/portal/routes/webhooks.py`) checks an `X-Internal-Webhook-Token` header against an auto-generated Secret instead. GitHub's push webhook keeps its own pre-existing HMAC-SHA256 signature check against `GITHUB_WEBHOOK_SECRET`.
- **None of the above is a substitute for network boundaries.** Run the portal behind a trusted network until `auth.enabled` is turned on; even then, `--openshift-sar` only requires "any authenticated user with a role binding in this namespace" unless tightened further.
- **Secrets never belong in Git.** See [Configuration](#configuration) and `docs/deployment.md`.
- **Destructive actions are LLM-gated and fail closed.** `automode.py` only auto-applies when the orchestrator approves *and* the LLM classifies the change as non-destructive with >= 0.8 confidence; if the LLM is unavailable, unconfident, or flags a risk, the change is gated for human review.
- **Manifests are validated before being trusted.** `agents/base.py::validate_manifest()` checks every generated YAML, and `cluster_apply.py` runs a `--dry-run=client` pass before any real apply.
- **SSRF prevention.** `cloner.py` rejects private IPs, localhost, and internal DNS suffixes. `portal/helpers.py::safe_url()` rejects protocol-relative URLs.
- **Circuit breakers.** LLM and Kubernetes API clients use circuit breakers (`CircuitBreaker` in `portal/helpers.py`, `llm_breaker`/`kube_breaker`) to prevent cascading failures. `llm.py`'s `LLMClient._chat()` checks/records against `llm_breaker` around every real Anthropic call; `kube.py`'s real API-calling functions (`list_pods`, `list_custom_resources`, `apply_yaml`, `namespace_exists`, ...) do the same against `kube_breaker` via a shared `_kube_breaker_scope()` choke point — each checks `is_open` before attempting a call (skipping with a safe fallback matching its own existing failure contract if open) and records success/failure around the real call itself. `AGENTIT_OFFLINE`'s hard-stop (a dedicated `KubeOfflineError`) is explicitly exempted from ever counting as a `kube_breaker` failure, and expected non-failure statuses (404 "not found", 409 "already exists"/conflict) don't count either — both are the Health page's "kube" row now being a real, tripsable signal instead of permanently green.
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
│   ├── write_guard.py              # Preflight writability for scout source-mode writes (OpenShift EACCES)
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
│   ├── portal-experience-design-language.md  # Portal EDL (normative) -- enforced by test_portal_edl
│   ├── self-improvement-for-agentit.md  # capability-scout design -- implemented (v1 detail differs, see status line)
│   ├── ledger-design-spec.md       # Next unified-activity-feed proposal, not yet built
│   ├── ux-design-requirements.md   # UX research checklist + stack recommendation, not yet built
│   ├── next-gen-ux-concepts.md     # Blue-sky UX brainstorm, not a build spec
│   ├── agent-removal-readiness.md  # Dated readiness audit backing the 9-agent removal (historical)
│   ├── code-review-2026-07-12.md   # Dated point-in-time code review (historical)
│   └── session-status-2026-07-13.md  # Dated session handoff snapshot (historical)
├── scripts/check_portal_edl.py     # Static EDL MUST/SHOULD walker for portal templates
├── Containerfile                   # UBI9 Python 3.12, HEALTHCHECK, non-root
└── tests/                          # 2,000+ tests across 100 files -- see "Testing" above
```

</details>

## License

[MIT](LICENSE)
