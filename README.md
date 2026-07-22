<p align="center">
  <img src="docs/assets/banner.png" alt="AgentIT" width="100%">
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-blue">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Built for" src="https://img.shields.io/badge/built%20for-OpenShift-EE0000">
  <img alt="GitOps" src="https://img.shields.io/badge/deploy-Argo%20CD-orange">
</p>

<p align="center"><b>Assess OpenShift apps, generate skill-backed remediations, open GitOps PRs — humans merge; Argo deploys. Scan is the only PR creator.</b></p>

---

## What AgentIT is (today)

AgentIT is a skills-primary, human-in-the-loop platform that:

1. **Assess** — scores a Git repo across 7 dimensions (analyzers + `mode: detect` skills).
2. **Scan / Onboard** — `FleetOrchestrator` runs property-based skills (optional CodeChangeAgent for source patches), then `auto_validate_and_deliver` opens **quality-filtered** PRs.
3. **Human merge** — you merge on GitHub. AgentIT does **not** auto-merge and does **not** Direct-Apply to the cluster.
4. **Argo deploys** — fleet apps via ApplicationSet → `apps/{app}/` in the gitops repo; AgentIT itself via Application `agentit` → Helm `chart/` in this repo.
5. **Operate** — watchers surface CVEs, SLO breaches, drift, and self-health; re-Assess/Scan closes the loop. Learning drafts new skills for human activation.

Docs index: [`docs/README.md`](docs/README.md). Normative delivery split: [`docs/architecture-agentit-vs-fleet-gitops.md`](docs/architecture-agentit-vs-fleet-gitops.md). Full diagrams: [`docs/architecture.md`](docs/architecture.md).

### Product contract (non-negotiable)

| Do | Do not |
| -- | ------ |
| Skills-primary generation; Scan/`auto_delivery` opens PRs | Per-Agent PRs / `create-agent-prs` (removed) |
| GitOps PR → human merge → Argo | Direct Apply / live cluster mutate from portal Deliver |
| SSA dry-run preflight (`kube.apply_yaml(..., dry_run=True)`) | Treat dry-run as `kubectl`/`oc` CLI |
| Self-managed AgentIT → PRs on **AgentIT.git** (`chart/`, `skills/`, `src/`) | Deliver AgentIT into `apps/agentit/` in gitops |
| Fleet apps → PRs under `apps/{app}/` with AppSet `directory.recurse=true` | Assume top-level-only Directory sync |
| Quality filter: finding-tied, one PR per cluster, approve on merge+clear | Catalog dumps; approve-on-PR-open |
| HITL Ledger / GitHub merge | AutoMode auto-merge (removed) |

### Dry-run & delivery (short)

**Dry Run** = apiserver server-side-apply `dryRun=All` via `kube.apply_yaml(..., dry_run=True)`. **Hard** errors (schema/admission/unreachable) block the PR; **soft** errors (Forbidden for AgentIT SA, missing optional CRD, field-manager conflict) warn only. Nothing is left applied. Real apply = merge + Argo. GitHub PR APIs use REST (`portal/github_pr.py`). Quality module: `portal/quality_prs.py` ([plan](docs/plan-quality-helpful-prs.md)).

### Self-managed vs fleet

| | Fleet (e.g. pinky) | AgentIT itself |
| --- | --- | --- |
| Desired state | `agentit-gitops` `apps/{app}/` | This repo: `chart/`, `skills/`, `src/` |
| Argo | ApplicationSet `agentit-managed-apps` (`recurse` + `*.yaml`/`*.yml`) | Application `agentit` (Helm) |
| Image | App’s own CI | Tekton `notify-argocd` pins `image.tag` |
| HPA gates | Live workload discovery (`fleet_hpa.py`) | Rollout/RWO correctness (`self_managed_hpa.py`) |

### Solution contracts (landed + hardened)

[#154](https://github.com/alimobrem/AgentIT/pull/154) landed `SOLUTION_CONTRACTS` so each finding declares clearing skill, delivery surface (`source` / `cluster` / `none`), and refuse companions. This tree hardens that into a founder-trustable bar:

| Layer | What it does |
| --- | --- |
| `SOLUTION_CONTRACTS` | Every analyzer category contracted; `auto_pr=False` for detect-only (`license`, `secrets`, …) |
| `evidence_kind` | Machine check before open (`dockerfile_pin`, `audit_wired`, `hpa_target`, …) |
| Pre-open simulation | `remediation/clear_evidence.py` + `auto_delivery` refuse if MERGE would not clear |
| Skill ↔ contract CI | `tests/test_skill_registry_agreement.py` fails on FIX_REGISTRY / skill / delivery drift |
| Fleet vs self-managed | Cluster → gitops `apps/{app}/`; self-managed → app `chart/`; source → app repo |
| PR / portal honesty | Body: `Clears X by Y (evidence: …)`; Assessment Detail PR cards show contract lines |

[#155](https://github.com/alimobrem/AgentIT/pull/155) (detect_only coverage) is **superseded** by this hardening (closed; do not revive the old substring `contract_for`). Session notes: [`docs/changelog-dogfood-notes.md`](docs/changelog-dogfood-notes.md).

### Checks vs resolutions (layers)

```mermaid
flowchart LR
  subgraph detect [Detect]
    A[7 analyzers]
    D[20 mode detect skills]
  end
  subgraph score [Score]
    F[Findings by category]
  end
  subgraph resolve [Resolve via Scan]
    C[SOLUTION_CONTRACTS]
    PR[Source or gitops PR]
  end
  A --> F
  D --> F
  F --> C
  C --> PR
```

| Layer | What | Opens a Scan PR? |
| ----- | ---- | ---------------- |
| Analyzers (7 dims → ~27 categories) | Pattern / source / cluster posture | Only if contracted + `auto_pr` |
| `mode: detect` skills (~20) | File/YAML rule checks | Never by themselves — emit findings only |
| Remediable contracts | Skill + `delivery: source\|cluster` + evidence | **Yes** (quality-gated) |
| Detect-only contracts | e.g. `license`, `backup`, `secrets` | **No** — human-only |

**Live catalog:** Capabilities → **Checks & resolutions** (`/capabilities#checks-resolutions`) and `GET /api/check-catalog`. Built from `portal/check_catalog.py` so the UI cannot drift from `SOLUTION_CONTRACTS`.

**Portal map:** Capabilities = definitions + contracts; Assessment Detail = per-finding badges + Fix only for remediable; Insights = fleet rollup + contract badges (deep-links to Agents/Capabilities/Decisions/Ledger — not duplicate tables); Ledger/Fleet = PRs/scores (Fleet table omits Namespace/Trend); **Health** = tabbed platform telemetry (Overview / Workloads / Messaging / Access — not app checks); **Schedules** = CronWorkflows + reminders (watcher status via Agents); **Decisions** = LLM approve/reject audit leading with filter + log (not the check catalog).

### Image promotion (live portal)

Merge to `main` alone does not move the portal. Tekton `agentit-ci`: `run-tests` → `build-image` → `smoke-test-image` → `notify-argocd` (pins Application `agentit` `image.tag`). GitHub context `agentit-ci/tekton` must succeed. Details: [`docs/deployment.md`](docs/deployment.md).

## Table of Contents

- [Why AgentIT](#why-agentit)
- [Architecture, at a glance](#architecture-at-a-glance)
- [Checks vs resolutions (layers)](#checks-vs-resolutions-layers)
- [Skills & check engine](#skills--check-engine)
- [The agent fleet](#the-agent-fleet)
- [Self-improvement loop](#self-improvement-loop)
- [Self-improvement of AgentIT itself (capability-scout)](#self-improvement-of-agentit-itself-capability-scout)
- [Delivery & GitOps](#delivery--gitops)
- [Web portal](#web-portal)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Deploying to OpenShift](#deploying-to-openshift)
- [Testing](#testing)
- [Resilience](#resilience)
- [Security notes](#security-notes)
- [Repository layout](#repository-layout)
- [License](#license)

## Why AgentIT

AI makes building an MVP fast. Making that MVP operable on OpenShift — security posture, observability, CI/CD, GitOps, HA — is the slow part. AgentIT turns assessment findings into **reviewable PRs** via property-based skills, with a human merge as the deploy gate.

It runs **on** OpenShift **for** OpenShift: Argo CD, Argo Rollouts, Tekton, optional Kafka + Argo Events, OLM for operator CRDs skills may require. It does **not** claim SLSA factory certification or unsupervised production mutation.

## Architecture, at a glance

```mermaid
flowchart LR
    A["Git repo"] --> B["Assess\n7 dimensions + detect skills"]
    B --> C["Scan / Onboard\nSkillEngine + optional codechange"]
    C --> D["Quality filter + clear-evidence sim + SSA dry-run"]
    D --> E["Open GitOps / source PRs"]
    E --> F["Human merges on GitHub"]
    F --> G["Argo CD syncs"]
    G --> H["Watchers\nCVE / SLO / drift / self-health"]
    H -.->|"re-Assess / Scan"| B
    B -.->|"draft skills"| I["Learning agent\nHITL Activate"]
    I -.-> C
```

Two delivery destinations (never Direct Apply):

```mermaid
flowchart TB
    Scan["Scan / auto_validate_and_deliver"] --> Q{"Self-managed AgentIT?"}
    Q -->|yes| SM["PR on AgentIT.git\nchart/ · skills/ · src/"]
    Q -->|no| FL["PR on gitops\napps/{app}/…"]
    SM --> M1["Human merge"]
    FL --> M2["Human merge"]
    M1 --> A1["Application agentit\nHelm chart/"]
    M2 --> A2["ApplicationSet\nrecurse YAML under apps/{app}"]
```

**See [`docs/architecture.md`](docs/architecture.md)** for the system graph, assessment pipeline, and OpenShift topology.

## Skills & check engine

AgentIT uses two complementary systems for assessment and remediation:

### Property-based skills (67 skills across 14 domains)

Skills are Markdown files with YAML frontmatter that define **what must be true** (properties), not how to generate manifests. The skill engine matches skills to assessment findings; the LLM generates tailored fixes using the skill's constraints and the app's platform context. `FleetOrchestrator` builds and passes an LLM client into the skill engine on every run (CLI, portal, and webhook onboarding alike) whenever `ANTHROPIC_API_KEY`/`ANTHROPIC_VERTEX_PROJECT_ID` is configured, so LLM-only skills (no template block — e.g. `network-policy`, `containerfile`, `tekton-pipeline`, `helm-chart`) actually produce tailored output in production, not just template substitution. 20 of the 67 are `mode: detect` — declarative detection rules, not remediation templates, one per former `checks/*.yaml` file (see "Data-driven checks" below, which now lives entirely in this catalog).

```
skills/
├── security/         # network-policy, rbac, containerfile, security-context, resource-limits, image-scan-task, + 3 mode: detect (containerfile-exists, network-policy-exists, secrets-scanning-in-ci)
├── observability/     # service-monitor, grafana-dashboard, alerting-rules, otel-collector, + 3 mode: detect (health-probes-check, prometheus-metrics-exists, structured-logging-detected)
├── cicd/              # tekton-pipeline, argocd-application, argo-rollout, + 3 mode: detect (ci-pipeline-exists, dockerfile-exists, argocd-application-exists)
├── compliance/        # kyverno-policies, audit-policy, sbom-task, compliance-evidence, image-registry-policy, compliance-cronjob, + 3 mode: detect (admission-policies-exist, license-file-exists, sbom-exists)
├── infrastructure/    # hpa, pdb, resourcequota, limitrange, namespace, helm-chart, health-probes-policy, + 3 mode: detect (helm-chart-exists, k8s-deployment-exists, resource-quota-exists)
├── ha_dr/             # 3 mode: detect (hpa-exists, pdb-exists, multi-replica-deployment) -- detection-only domain, no remediation skills of its own (HPA/PDB remediation lives under infrastructure/)
├── data_governance/   # 2 mode: detect (backup-config-exists, retention-policy-exists) -- detection-only domain
├── cost/              # vpa, cost-labels, cost-cronjob
├── dependency/        # renovate-config, dependabot-config, dependency-cronjob
├── incident/          # runbook, pagerduty-config, alertmanager-config
├── release/           # analysis-template, rollout-patch, rollback-policy, release-runbook
├── retirement/        # decommission-plan, cleanup-task, data-archive-job
├── chaos/             # pod-delete, network-latency (LitmusChaos, template-only, no LLM needed)
└── custom/            # learning-agent-generated skills (created on first draft; not present until then)
```

Skills have lifecycle management: `draft` → `active` → `deprecated` → `retired`. The API drift detector auto-deprecates skills when their target APIs are removed from the cluster. Low-effectiveness skills (< 30% human approval rate) are flagged for review.

**Template-fallback placeholder substitution is now complete, with a hard-fail safety net.** When an LLM call truncates (`stop_reason=max_tokens`, or the LLM is unavailable at all) and `SkillEngine.generate()` falls back to a skill's raw Markdown template, the old substitution loop only ever replaced `{{app_name}}` — every other placeholder (`{{image}}`, `{{namespace}}`, `{{scanner_image}}`, ...) shipped literally in the final manifest, silently. `_template_variables()`/`_render_template()` now substitute every placeholder this code path has a real, non-fabricated value for — `{{namespace}}`/`{{app_name}}` (the sanitized repo name, matching the exact convention the delivery route already uses for "the app's own namespace"), `{{repo_url}}`/`{{git_url}}` (`report.repo_url`), and `{{image_ref}}` (`image_builder.get_image_ref()`, the same internal-registry path `build_app_image()`'s real production call sites already push to) — then, regardless of the root fix, scans the rendered output for any remaining AgentIT-style `{{...}}` placeholder and raises `UnresolvedPlaceholderError` rather than shipping it: `generate()` catches that, logs an error, and returns no file at all instead of a manifest a user might apply with literal placeholder text still in it (confirmed live: `app-rollout-patch.yaml` shipped `image: "{{image}}"`, `app-compliance-cronjob.yaml` shipped `"--namespace", "{{namespace}}"` verbatim). The substitution regex deliberately only matches bare-identifier placeholders (`{{app_name}}`), never Go-template/Alertmanager notification syntax skills legitimately ship verbatim for the receiving system to evaluate at runtime (`{{ .GroupLabels.alertname }}`, `{{ range .Alerts }}...{{ end }}` in `alertmanager-config.md`/`pagerduty-config.md`).

**LLM-only skill generation and drafting now request a manifest-sized token budget, not the classifier default.** `SkillEngine._generate_with_llm()` and `learning_agent.generate_skill_from_research()` both call `llm_client._chat(system, user)` directly (external callers, not methods on `LLMClient` itself) and, unlike every in-module caller, never overrode `max_tokens` — silently inheriting `_DEFAULT_MAX_TOKENS` (512), a budget sized for short, fixed-shape classifier JSON, not a full K8s manifest or a complete skill Markdown file. Confirmed live: activating the draft `resourcequota-contextual` skill (a `mode: llm` skill with no `` ```yaml `` template block, so it has nothing to fall back to) hit the portal's toast **"Activation blocked — skill failed verification: skill matched the verification fixture but generated no output."** The live pod's own logs showed `stop_reason=max_tokens` on every attempt — the LLM response was truncated before `validate_manifest()` ever saw a complete manifest, `_generate_with_llm()` exhausted both retries and returned no files, and `verify_skill()` correctly (if confusingly) reported that as "generated no output" and blocked activation. The same bug, in the same skill, truncated its own draft when `generate_skill_from_research()` first wrote it — the saved body is missing its Constraints/Verification sections entirely, cut off mid-sentence. Both call sites now request `_SKILL_GENERATION_MAX_TOKENS` (4096, sized like `_CAPABILITY_PROPOSAL_MAX_TOKENS`'s prose budget plus real headroom for a multi-resource YAML body) instead of the 512-token default.

**`audit-policy` no longer fabricates a never-applyable resource.** `apiVersion: audit.k8s.io/v1, kind: Policy` is not a real Kubernetes REST API resource on any cluster — it's a static file schema consumed only by kube-apiserver's own `--audit-policy-file` startup flag, never something `kubectl apply` accepts. Applying it always failed, and `cluster_apply.py`'s missing-operator heuristic misattributed that failure to a missing Kyverno install (Kyverno's own CRD happens to also be named `Policy`, in a completely unrelated API group) — a wrong, misleading fix suggestion. The skill now delivers the same real audit policy rules as advisory reference documentation in a ConfigMap (the same pattern `incident/runbook.md`/`retirement/decommission-plan.md`/`compliance/compliance-evidence.md` already use), with instructions for how a cluster-admin actually wires it in on vanilla Kubernetes vs. OpenShift — never silently treated as "already enforced," and never reaching `cluster_apply.py`'s CRD-missing/missing-operator path at all.

### Data-driven checks — now fully unified into `mode: detect` skills

`checks/` (YAML declarative rules that supplement the Python analyzers) is, as of Phase 4 below, **completely empty** — every check that used to live there is now a `mode: detect` skill in `skills/**/*.md` (see the skills tree above; 20 of the 67 skills are `mode: detect`, one per former check plus Phase 1's pilot). Check types — `file_exists`, `file_contains`, `file_missing`, `yaml_kind_exists`, `yaml_kind_missing`, each accepting a `pattern:` list (OR semantics) and an optional `case_insensitive: true` — are unchanged; a `mode: detect` skill's `rule:` block uses the exact same 5 types, run through `check_engine`'s own unchanged runners (`_RUNNERS`/`run_checks*`). `check_engine.py`'s YAML-*loading* half (`load_checks`/`_parse_check_file`) is deliberately still in the codebase — `skill_engine.detect_check_definitions()` depends directly on the rule-*running* half those loading functions share a module with, and per the migration plan's own explicit instruction, the loader stays until every check is gone (now true) plus one release cycle of confidence before actually deleting the dead loading code, to leave a clean revert path if a production issue surfaces. The learning agent can still create new detection rules without touching Python code — as a `.md` file instead of a `.yaml` one.

**Unification, Phase 1 (2026-07-18): checks are becoming a peer of skills, sharing skills' exact Markdown format and lifecycle.** A skill's `mode:` frontmatter field now accepts `detect` in addition to `template`/`llm` — a `mode: detect` skill defines a declarative `rule:` (the same 5 types above) instead of a remediation template, and `skill_engine.detect_check_definitions()` runs it through `check_engine`'s own runners, merging its findings into `runner.run_assessment()` exactly like a legacy `checks/*.yaml` file would. This means a detection rule gets the same lifecycle (`draft`/`active`/`deprecated`/`retired`), the same Activate/Deprecate/Reactivate UI, and the same git-PR-backed persistence the Capabilities page already built for remediation skills — with zero changes to that UI's own code. `skills/observability/health-probes-check.md` is the first one, ported from (and replacing) the former `checks/observability/health-check.yaml`, proven equivalent by a parity test before the YAML file was deleted. See [`docs/extension-model-unification-plan-2026-07-18.md`](docs/extension-model-unification-plan-2026-07-18.md) for the full design, the reconciliation with the original design spec's "keep skills and checks separate" recommendation (narrower than it sounds — see that doc), and the backlog for unifying the remaining `checks/*.yaml` files and the 3 Python agents + 6 watchers the same way.

**Unification, Phases 2-5 (2026-07-20): the remaining backlog — agents, watchers, all 19 remaining checks, and Capabilities UI — finished.** Picks up exactly where Phase 1 left off, per `docs/extension-model-unification-plan-2026-07-18.md`'s own "Backlog (not built in Phase 1)" section, in its stated priority order.
- **Phase 2 — `mode: agent` registration metadata.** `agents/{cost,dependency,codechange}.md` (new top-level `agents/` dir, sibling to `checks/`/`skills/` — not `src/agentit/agents/`, the Python package) replace `agents/capabilities.py`'s hardcoded `AGENT_CLASSES` dict literal. `load_agent_classes()` (`src/agentit/agents/capabilities.py:105`) parses each file's frontmatter (`name`/`category`/`code_ref`/`resource_tier`/`description`) into the exact same `{name: (category, module_path, class_name, resource_tier)}` shape the literal had; `AGENT_CLASSES` is now this loader's output, computed once at import time. `code_ref` (a `module:ClassName` string) is only ever *recorded* — `get_agent_class()` still does the real lazy import at its one existing call site, so `CostOptimizationAgent`/`DependencyAgent`/`CodeChangeAgent`'s own `.run()` implementations are byte-for-byte untouched. `tests/test_agent_registration.py` proves zero behavior change (the loader's output is asserted equal to the pre-Phase-2 hardcoded dict) plus per-file schema validation (required fields, an importable `code_ref`, a known `resource_tier`).
- **Phase 3 — watcher registration metadata.** Same idea, for the long-lived watchers: `watchers/*.md` (new top-level dir, sibling to `agents/`/`checks/`/`skills/` — not `src/agentit/watchers/`) replace `WATCHER_AGENTS`'s hardcoded list literal via `load_watcher_agents()` (`src/agentit/agents/capabilities.py`). Re-verified the real, current list against `agents/capabilities.py` before porting rather than trusting the plan doc's own table (written before `self-health-check` was added) — **7 watchers**, not 6: `vuln-watcher`, `slo-tracker`, `drift-detector`, `skill-learner`, `capability-scout`, `reassess-scheduler`, `self-health-check`. `watchers/__init__.py`'s registration/heartbeat wiring (`record_tick`, `sleep_with_heartbeat`) is completely unchanged — every watcher is still keyed on its own name string at its real call sites, never on this list; only the *listing* of which watchers exist moved from Python to Markdown. `tests/test_watcher_registration.py` mirrors Phase 2's parity + schema discipline.
- **Phase 4 — all 19 remaining `checks/*.yaml` files ported to `mode: detect` skills, one file at a time.** Re-verified the real, current list first (19, matching the plan doc's own estimate exactly) rather than assuming. Followed Phase 1's own proven method for every single one, no exceptions: port the file, write a parity test proving the new skill fires/passes identically to the deleted YAML under both conditions (rule triggers a Finding / rule is satisfied, matching category/severity/description/recommendation exactly), verify that test passes, delete the YAML, commit — 19 separate commits, each individually revertible, none bulk-converted. All 39 parity tests (2 per check: fires + passes) live in `tests/test_phase4_check_migrations.py`. Per the plan's own explicit warning about the "mixed analyzer" dimensions (`cicd`/`compliance`/`data_governance`/`ha_dr`), every port preserved the original check's exact scope rather than silently widening it to match its analyzer counterpart — e.g. `admission-policies-exist.md` keeps the original's namespaced-`Policy`-only match (never `ClusterPolicy`, unlike `analyzers/compliance.py`'s own broader, separate scoring), proven by a dedicated `test_does_not_match_clusterpolicy_only` case. New skill names were chosen to avoid colliding with pre-existing remediation skills of the same short name in the same domain (e.g. `hpa-exists`/`pdb-exists` under `ha_dr`, not `hpa`/`pdb`, which are `infrastructure`'s own template-mode remediation skills) — each such collision is called out in the ported skill's own "Constraints" section. This work also caught and fixed two latent test-isolation gaps the ports exposed (both fixed in the same commits that surfaced them): `tests/test_check_engine.py::TestSampleAppFixture`'s `real_checks` fixture only ever read the legacy YAML directory (now merges checks + detect-mode skills, exactly like `runner.run_assessment()` does in production); and `TestRunnerIntegration::test_duplicate_findings_not_doubled` implicitly depended on the real skills catalog never containing a NetworkPolicy detection (now isolated with an explicit empty `skills_dir`, matching the isolation its `checks_dir` already had).
- **Phase 5 — Capabilities UI reflects the unified model.** All three items from the plan's Phase 5 section: (a) a mode badge (`detect`/`llm`/`template`) on the "Skills by Domain" table, so a detection rule no longer looks identical to a remediation skill; (b) the old checks-only "Assessment Checks" section — which Phase 4 would otherwise have left permanently showing "0 checks" — replaced with a merged "Detections" section (`_build_detections_by_dimension()`, `src/agentit/portal/routes/capabilities.py`) combining legacy `checks/*.yaml` (still supported, currently none) and `mode: detect` skills into one dimension-grouped view, each row tagged with its real source and, for a skill, a link to its lifecycle page; (c) the skill detail page (`skill_detail.html`) now surfaces a detect-mode skill's actual rule (type, pattern(s), severity, category, recommendation) instead of the meaningless (always-empty) triggers/outputs table template/llm-mode skills use. Fixed a real, previously-invisible bug while rebuilding the severity-badge column: the old checks table's `badge-{{ check.severity.value }}` rendered the `Severity` `IntEnum`'s *int* value (e.g. `badge-1`), which never matched any real `badge-critical`/`badge-high`/etc. CSS class — both sources now consistently use `.name`.

Full test suite after all four phases: **2926 passed, 370 skipped, 0 failed** (`pytest tests/ --ignore=test_browser.py --ignore=test_browser_critical.py`).

### Catalog change tracking

Additions and removals to the `skills/`/`checks/` catalog are no longer only visible via `git log` — `skill_inventory.py` snapshots the catalog (by `(domain, name)` / `(dimension, name)` identity, so status-only transitions like `active → deprecated` aren't double-counted alongside the existing `skill-activated` event) and diffs it against the last saved snapshot once an hour from the portal's background maintenance loop. Every skill/check added or removed is logged as a `skill-added` / `skill-removed` / `check-added` / `check-removed` event, which shows up automatically on the **Events** feed (`/events`) and in a "Recent Catalog Changes" section on the **Capabilities** page.

### EOL / end-of-life detection

The `infrastructure` dimension's analyzer now flags base images and language runtimes that are past or approaching end-of-life (`analyzers/eol.py`). A deterministic baseline (always on, no LLM required) matches `Dockerfile`/`Containerfile` `FROM` lines, `.python-version`/`runtime.txt`/`pyproject.toml`, and `package.json`'s `engines.node` against real, cited support-lifecycle dates for Python, Node.js, Ubuntu, Debian, CentOS, and Alpine. When an LLM client is configured, `LLMClient.detect_eol_risks()` additionally reasons over the repo's detected stack and key files to flag EOL/near-EOL components the fixed table doesn't cover — purely additive on top of the baseline, and it degrades to nothing (never fabricates a date) on any LLM failure or low-confidence result.

## The agent fleet

**Skills own remediations; one optional Python source-patch agent; watchers stay.** Skills run **first**, unconditionally, for every domain (including cost/dependency — VPA, Renovate, etc.). Cost/dependency Python agents and Per-Agent PRs are gone. The only remaining one-shot Python onboarding agent is optional **CodeChangeAgent** (source patches to the app repo — not a peer "domain agent" to skills). Long-lived watchers are unchanged (below; `capability-scout` covered separately). See [`docs/agent-removal-readiness.md`](docs/agent-removal-readiness.md).

| Agent | Category | When it runs | Generates | Role |
|---|---|---|---|---|
| **CodeChangeAgent** | `codechange` | high/critical or score < 50 | `.gitignore`, health endpoints, OTel/structured-logging / Dockerfile patches to the **app's own repo** | Optional **source-patch** path — skills don't model app source trees. Not a domain peer to skills. |

Conflict detection only flags *real* collisions across distinct AgentResults (same output path). VPA+HPA both come from the skills result today, so the old cost-vs-skills kind matrix is empty.

**Registration metadata for both tables above now lives in `agents/*.md`/`watchers/*.md`, not a Python dict literal** (Unification Phases 2-3, 2026-07-20 — see "Data-driven checks" below for the full writeup). Each table's actual content (what an agent generates, why it's still Python, a watcher's role) is unchanged by this — it's still real prose in this README, not generated from the `.md` files' frontmatter — this only changed *where `agents/capabilities.py`'s `AGENT_CLASSES`/`WATCHER_AGENTS` registries get their data from* (a file diff instead of a Python edit), not what either table says.

Long-lived watchers (deployed as separate pods):

| Watcher | Loop | Role |
|---|---|---|
| **vuln-watcher** | 6h | Fleet CVE monitoring, surfaces critical/high findings as alerts (fixing them requires a human Assess/Onboard/Deliver -- no autonomous fix pipeline) |
| **slo-tracker** | 5m | Collects fresh `availability`/`error_rate`/`latency_p99_ms` metrics for every tracked SLO (via `slo_collector`: `availability`/`error_rate` from pod status via the kubernetes client, `latency_p99_ms` from Prometheus — `histogram_quantile(0.99, ...)` over `http_request_duration_seconds_bucket`, scoped to the app's namespace, same `AGENTIT_PROMETHEUS_URL` connection `resource_tuner` uses; apps with no data yet are skipped/logged, not silently ignored), checks breaches with the correct per-metric direction (`availability` = higher is better; `error_rate`/`latency_p99_ms` = lower is better), publishes breach alerts, and opens rollback gates |
| **drift-detector** | 10m | Argo CD sync monitoring, API drift detection, auto-deprecation of affected skills, reports still-in-use deprecated APIs (`PlatformContext.deprecated_apis`), and a self-check that `agentit`'s deployed revision hasn't silently fallen behind `origin/main` (see "GitOps pipeline stall detection" below) |
| **skill-learner** | 24h | Researches CVEs via LLM, drafts new skills for human review — opt-in via `agents.skillLearner.enabled` (chart default: disabled; enabled on the live deployment via `argocd/application.yaml`), requires an LLM connection |
| **reassess-scheduler** | 1h tick | Automatically re-Assesses apps once their own configured cadence (`apps.assessment_cadence`: daily/weekly/monthly, set per-app on the Assessment Detail page; `manual` opts an app out) has elapsed since its last assessment — via the same `/api/webhook/assess` route the manual Fleet Re-assess button already uses, not a second assess pipeline. Opt-in via `agents.reassessScheduler.enabled` (chart default: disabled; enabled on the live deployment via `argocd/application.yaml`). Before this watcher, nothing re-checked an onboarded app on a timer at all — only a manual Re-assess click or a push to its repo. |
| **self-health-check** | 15m tick | Verifies AgentIT's own critical infrastructure end to end, not just "is the pod running" — GitHub webhook delivery health, `agentit-ci` PipelineRun stall detection, maintenance CronJob success, and cleanup-CronJob effectiveness (a stale-terminal-pod backlog proxy). Publishes one pass/fail event per check per tick, surfaced on the Health page's new "AgentIT Self-Health" panel and the sitewide critical/high Events badge. See [`docs/self-health-check-backlog.md`](docs/self-health-check-backlog.md) for the design rationale and the incident inventory it was built against. Opt-in via `agents.selfHealthCheck.enabled` (chart default: disabled; enabled on the live deployment via `argocd/application.yaml`). |

**Product decision (2026-07-18): every watcher above, plus all 4 fleet-rescan CronJobs below, is now enabled on the live deployment.** Every one of these still ships **opt-in / off by chart default** (`chart/values.yaml`) — that pattern is unchanged and lets anyone installing this chart fresh start from a quiet baseline — but `argocd/application.yaml`, the file that actually controls this live deployment, now sets every `agents.*.enabled` and `cronJobs.*.enabled` flag to `true`. One overlap worth knowing about: `cronJobs.cveScan`/`complianceRescan`/`dependencyUpdate`/`costReport` all call the same underlying full-fleet LLM re-assessment (`cli.py`'s `_rescan_fleet()` — `--dimension` only filters which findings a job's summary line counts, it does not skip re-assessing any app or dimension), so with all 4 plus the hourly `reassess-scheduler` now live, the same fleet can be fully re-assessed by more than one mechanism in the same week. `cveScan`'s schedule was moved off `costReport`'s exact time (both were `0 6 * * 1`) so the two don't fire in the same minute; `dependencyUpdate` (Mon 04:00) still runs ~2h before `costReport` (Mon 06:00) on the same day — not an exact collision, left as-is, but worth revisiting if fleet size or LLM rate limits become a real constraint.

Every watcher records real tick telemetry after each loop iteration — a `tick-complete`/`tick-failed` event plus an `AssessmentStore.agent_heartbeat()` call (`agentit/watchers/__init__.py::record_tick`) — so "last seen" on the **Agents** and **Schedules** pages reflects an actual heartbeat instead of a static "—". A Prometheus gauge, `agentit_watcher_last_success_timestamp{watcher=...}`, backs an `AgentITWatcherStale` alert (one rule per watcher, threshold = 2x its expected interval) in the chart's `PrometheusRule`. The liveness probe's own `/tmp/heartbeat` file is kept fresh independently of `record_tick` — `vuln-watcher`/`skill-learner`'s tick intervals (6h/24h) far exceed the probe's 900s staleness window, so `run()` sleeps between ticks via a shared `agentit.watchers.sleep_with_heartbeat` helper that touches the file every 300s instead of only once per full tick, avoiding a restart loop.

## Self-improvement loop

AgentIT improves the **skills catalog** it generates for apps (separate from capability-scout, which improves AgentIT itself):

1. **Feedback from real delivery outcomes.** `record_skill_outcome()` runs from Scan/`auto_delivery` and PR lifecycle correlation — **never approve-on-PR-open**. Approval waits for merge + finding clear (`correlate_delivery_finding`). Effectiveness is recency-weighted; low-effectiveness skills surface on Insights. Skill **Activate** verifies generation then opens a draft PR so `skills/` survives image rebuilds.
2. **Learning agent.** `skill-learner` (and Capabilities “Research”) drafts skills from CVE/best-practice research, preferring replacements for low-effectiveness skills. Humans Activate; nothing auto-promotes to `active` without verification.
3. **Platform-aware deprecation.** Drift detector snapshots the API surface and auto-deprecates skills targeting removed kinds.

Edit-before-deliver still exists on Onboard Results (persisted file content is what `route_and_deliver` ships). Historical AutoMode/gates wiring notes: [`docs/changelog-dogfood-notes.md`](docs/changelog-dogfood-notes.md).

## Self-improvement of AgentIT itself (capability-scout)

The loop above (`skill-learner`) improves what AgentIT *generates for other apps* — the skills catalog. It has no counterpart that improves AgentIT's *own* codebase, its portal routes, its watchers, its CLI. `capability-scout` is that counterpart: a separate, opt-in, 24h watcher (`agents.capabilityScout.enabled`, chart default **off** — this is a live-deployment decision for the repo owner to make explicitly, not something enabled as a side effect of shipping it; enabled on this live deployment via `argocd/application.yaml` as of the 2026-07-18 "enable all watchers" decision) that mirrors `skill-learner`'s shape exactly — **research → propose → verify → human review → merge** — but aimed at AgentIT's own repo. See [`docs/self-improvement-for-agentit.md`](docs/self-improvement-for-agentit.md) for the full design; this is deliberately named distinctly from `self-assess`/`self-fix` (which run AgentIT's existing hardening pipeline *against* AgentIT's own repo, generating K8s manifests for it — not proposing new Python features for AgentIT's product surface).

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
- Watcher heartbeat status surfaces on **Agents** (Schedules links there for long-lived agents; CronWorkflows/reminders stay on Schedules).

GitHub's own PR UI stays the surface for reviewing the actual code diff; the portal is where a human sees what the loop considered and why.

```bash
# Long-lived watcher (24h default; dogfood cadence restored from temporary 1h) -- mirrors `learn-watch`
uv run agentit propose-watch --interval 86400 --max-open-prs 1

# One-shot cycle for dogfood / debugging (no startup grace, no loop)
uv run agentit propose-once --mode auto --max-open-prs 1
```

**Build modes:** `docs` (proposal markdown only), `source` (edit `skills/`/`checks/`/`tests/`/`src/agentit/` when every target is in that allowlist), `auto` (source when eligible, else docs). Prefer **new small modules** over rewriting large files — full-file generation of big modules fails and (in `source`/`auto`) skips the cycle rather than opening a docs-only PR. When a proposal targets an existing file already over the 150-line size cap, scout rewrites that target to a new `src/agentit/<feature>.py` sibling before calling the LLM (so L3 cycles are not stuck gate-blocked on `diff-size`). File generation uses a higher token budget and a compact JSON retry when the first reply truncates. Dogfood sets `agents.capabilityScout.mode=auto` via Helm.

See [`docs/superpowers/plans/2026-07-15-autonomous-self-improve-dogfood.md`](docs/superpowers/plans/2026-07-15-autonomous-self-improve-dogfood.md) for the L0→L5 dogfood milestone plan (substrate → source PRs → outcome loop), and [`docs/dogfood-self-improve-milestone.md`](docs/dogfood-self-improve-milestone.md) for the 2026-07-16 retrospective (L4 on AgentIT; L5 full on pinky via portal Approve & Deliver → [agentit-gitops#10](https://github.com/alimobrem/agentit-gitops/pull/10)).

## Delivery & GitOps

Single router: `portal/delivery.py::route_and_deliver()` — called from Scan/`auto_validate_and_deliver` (and remaining API deliver helpers). Mechanisms:

- **Infra-repo commit** → PR under `apps/{app}/` (fleet).
- **Source-patch PR** → app repo or AgentIT.git (`chart/` / `skills/` / source).
- **No Direct Apply** — unknown/missing GitOps registration fails closed.

Quality Phases A–F ([`docs/plan-quality-helpful-prs.md`](docs/plan-quality-helpful-prs.md)): refuse empty/score-less dumps; one PR per finding cluster; SSA + property checks; helpful PR bodies; learn from merge/reject; fleet parity.

In-app **gates table removed** — approval UX is Ledger + GitHub merge. Design history of the old Direct Apply / AutoMode world: [`docs/unified-apply-flow.md`](docs/unified-apply-flow.md) (**historical** — see banner there).

## Web portal

`agentit portal` launches a FastAPI + Jinja2 app (htmx + Alpine.js for interactivity, no frontend framework). 98 routes.

**Masthead / IA boundaries:** `/` redirects to **Ledger** (ops home). Primary nav is Ledger (Needs You badge), Fleet, Admin Review (only when elevated count &gt; 0), Health, Insights. **Events** is bell-only (drawer → `/events` + DLQ; not ops home). The drawer overlay/panel default to CSS `display: none` and open only via Alpine `.open` (not x-show alone), so an hx-boost Alpine re-init race cannot leave an invisible full-screen click catcher. **Decisions** stays in the account menu (Events owns the chronological stream — see "Events, Ledger, Decisions" below). When Admin Review count is 0 it is buried in the account menu with subtitle “Elevated approvals”. Exclusive-job table: [`docs/portal-experience-design-language.md`](docs/portal-experience-design-language.md) §1. The Cmd+K command-palette search sits in the **right** masthead cluster with Events / Menu (max-width constrained) so it never covers primary links. Drawer rows with an `assessment_id` deep-link to Assessment **Actions** (else Ledger Needs You by app, else Events correlation). **Back to Assessment** links on Onboard Results / SLOs / History / progress use `hx-boost="false"` so they always land on Assessment Detail. **Running Assessment** (`/assess/progress/{job_id}`) keeps the same masthead shell: its 2s htmx poll targets `#main-content` (not `body`), so progress never becomes a full-viewport chrome-less takeover.

**Attention signals:** the primary-nav badge and Ledger's own "Waiting for your approval" section use `badge-accent`/`badge-warning`; both are PR-approval-specific now (Ledger's job narrowed to strictly PRs — see "Ledger redesigned" above) and, as of the 2026-07-19 fix, purely PR-status-derived (any PR still open and unmerged, gate-tracked or not — see "Ledger's 'Waiting for your approval' undercounted" above), not gated on an in-app `gitops-pr-pending`/`-shared-namespace` row existing. Fleet is scoreboard-only — a quiet "N PR(s) need your approval → Ledger" link (same definition), not a pending-ops column; every other pending, app-owner-scoped gate type shows as that app's own row-level "N pending action(s)" badge instead. Assessment Detail shows a **next-step hint** under the lifecycle stepper: while `assessed`, **Onboard This App** always wins (leftover Actions gates are demoted — Onboard generates remediations for all findings; don’t fix one-by-one on Actions first); after onboard, pending Actions win. The Actions link uses `?tab=actions`, not a dead Alpine click outside `x-data`. Pending gates dedupe by `repo_url` + `gate_type` (app-scoped); slo-tracker ticks each app once so re-assess does not create Actions ×N. Delete is visually de-emphasized in a danger-zone slot opposite the primary Onboard action. Capabilities collapses reference catalogs (skills/checks/how-onboarding-works) with `<button>` toggles by default so activity/stats stay above the fold.

**Experience Design Language (EDL):** normative portal UI contract — button hierarchy (short ≤3-word CTAs, `.btn` skin, no status-inside-button), Dry Run → deliver-choice onboarding path (combined vs per-agent), modals/a11y, badges, feedback, and compact `.filter-bar` / `.filter-field` toolbars for list/log GET filters (Decisions, Events, Ledger) — in [`docs/portal-experience-design-language.md`](docs/portal-experience-design-language.md). Agents should load [`.cursor/rules/portal-edl.mdc`](.cursor/rules/portal-edl.mdc). Enforce with `uv run pytest tests/test_portal_edl.py -q` (also `uv run python scripts/check_portal_edl.py`; button SHOULD rules for label length / `.btn` class are asserted in CI).

**Shared `pending_actions.py` helper closes the rollback/escalation query drift a reuse-and-refactor review flagged (2026-07-20).** `store.list_unresolved_events("rollback-recommended"/"finding-escalated", ...)` was independently re-typed at six call sites — `routes/assessments.py:499` (single-app, unwrapped), `routes/fleet.py::_attach_pending_actions`/`_attach_next_action_state` (fleet-wide, try/except-and-fallback), `routes/insights.py`'s Ledger page (fleet-wide, unwrapped), and `portal/delivery.py::escalate_unresolved_finding`/`get_next_action_state` (app-scoped) — each free to drift from the others. New `portal/pending_actions.py` (`store.py` untouched, per the concurrent domain-split effort's constraint — only its existing public `list_unresolved_events()` is called) provides `list_unresolved_rollbacks()`/`list_unresolved_escalations()`/`list_unresolved_recommendations()` (the paired helper the three multi-site callers actually want) and now owns the four action-name constants as the single source of truth; `routes/recommendations.py` re-exports them instead of defining its own copies. Every call site's own try/except-and-fallback behavior (or lack of it) is preserved verbatim — pure extraction, not a behavior change. One drift the review predicted, found and deliberately left as-is rather than silently unified: `helpers.get_nav_pending_action_counts()` (the nav badge, see the rename entry directly below) never called `list_unresolved_events()` at all — it's purely PR-status-derived, a genuinely different scope from this pair. Tested: new `tests/test_pending_actions.py` (7 cases); full non-browser suite unaffected (2950 passed, 375 skipped).

**Renamed `helpers.py`'s `nav_gate_*` naming leftovers to `nav_pending_action_*` (2026-07-20).** `get_nav_gate_badge_counts()`/`_nav_gate_badges_cache`/`_NAV_GATE_BADGES_CACHE_TTL`/`_nav_gate_badges_lock` still carried a `gate`-prefixed name from when the (now-removed, 2026-07-19) `gates` table backed this count. Verified before renaming: it's been purely PR-status-derived (`pr_tracking.count_fleet_prs_waiting_for_approval()`) since that date and never called into `pending_actions.py`'s scope (see the entry above) — a pure naming leftover, not live gate functionality. Renamed to `get_nav_pending_action_counts()`/`_nav_pending_actions_cache`/`_NAV_PENDING_ACTIONS_CACHE_TTL`/`_nav_pending_actions_lock` throughout `helpers.py` and `app.py`, including a stale `nav_badges_middleware` docstring that said "the two gate-count nav badges" — there has only ever been one (`pending_actions`). Zero behavior change; every test reference (`test_ttl_cache_locking.py`'s `TestNavGateBadgesCacheLock` → `TestNavPendingActionsCacheLock`, `test_ia_boundaries.py` ×3, `test_ui_redesign.py` ×1) updated to match.

**`routes/health.py`'s deploy-status computation extracted into `portal/deploy_status.py` (2026-07-20).** `health.py` (1032 lines) mixed HTTP route handlers with a large, self-contained deploy-status block — the Tekton `agentit-ci` PipelineRun lookup, the Argo CD `agentit` Application lookup, the commit-comparison/resolved-outcome logic, and the TTL-cache/bounded-executor apparatus backing both the ambient nav badge (`GET /api/deploy-status`) and the Health page's detailed section (see "Health" in Key pages below). Moved verbatim — same function signatures/return shapes, comments/docstrings intact — into new `portal/deploy_status.py`: `_get_deploy_status()`, `_get_deploy_status_bounded()`, `_taskrun_status()`, `_is_confirmed_unreleased_revision()`, the cache/executor apparatus. `routes/health.py` (now 694 lines) imports the six names its two routes actually call and keeps only its own route handlers plus the genuinely-local helpers those alone use (`_get_cluster_health`'s separate "Platform" card computation, pod/pipeline detail helpers, the webhook-delivery-health cache) — confirmed by reading the whole file first that `_get_cluster_health()` never touches deploy-status before moving anything. Tests calling the computation directly (not through a `health.py` route) now patch `agentit.portal.deploy_status.kube`/`.github_pr` instead of `agentit.portal.routes.health.*`, since that's the module those functions actually resolve those names from post-move — updated `tests/test_deploy_status.py` (43 tests) and `test_ttl_cache_locking.py`'s deploy-status-cache-lock test accordingly; route-level patches (`agentit.portal.routes.health._get_deploy_status`/`_get_deploy_status_bounded`) needed zero changes, since `from module import name` still binds them into `health.py`'s own namespace for its routes to resolve. Full non-browser suite: 2950 passed, 375 skipped — identical counts to before this split.

Key pages:

| Page | Purpose |
|---|---|
| **Fleet** (`/fleet`) | Portfolio scoreboard — apps, scores, one consolidated **Scan** action (or **Re-scan** when previously onboarded) / Delete, GitOps vs Direct-apply + sync badges, an **Open PRs / Total** column (see below), and a **Criticality** badge with a tooltip explaining exactly what it controls (never a bare, unexplained value). Two row-level buttons only — Scan-equivalent + Delete; app name is the click-through to Assessment Detail. Pending human gates are **not** Fleet's job: a quiet "N PR(s) need your approval → Ledger" link is PR-approval-specific and purely PR-status-derived (`pr_tracking.py::count_fleet_prs_waiting_for_approval()` — any PR still open and unmerged, gate-tracked or not; 2026-07-19 fix, see above); every other pending, app-owner-scoped gate type shows as that app's own row-level "N pending action(s)" badge (`fleet.py::_attach_pending_actions`), linking straight to its Actions tab |
| **Ledger** (`/ledger`; `/` → here) | Fleet-wide PR list/lifecycle view (product direction, 2026-07-18) — every PR AgentIT has opened, across every app: a "Waiting for your approval" section (any PR still open and unmerged on GitHub — real Approve & Deliver/Reject actions via `gate_card()` for gate-tracked ones, a plain "review it on GitHub" pointer for the rest; 2026-07-19 fix, see above) plus a filterable (category/app/lifecycle status) PR-history table. Backed by `pr_tracking.py::collect_fleet_pr_records()`/`fleet_prs_waiting_for_approval()`, not `ledger.py`'s original A-P card-type union — that system (and Assessment Detail's own per-app Ledger tab, which still uses it unchanged) is documented in [`docs/ledger-design-spec.md`](docs/ledger-design-spec.md) as historical context for the superseded design. Behind-the-scenes system activity (watcher ticks, drift, catalog changes, ...) is Events' job now, not Ledger's — see "Events, Ledger, Decisions" below. Rewind scrubber at `/ledger/chain/<correlation_id>` (unchanged, still the full card-type system) |
| **Assessment Detail** | 7-dimension scores, lifecycle stepper, score trend + a rendered score-history table with deltas, a GitOps-registration badge + a dismissible **Register for GitOps** nudge (confirm + busy state, optional infra-repo URL, inline error/success after POST; auto-creates `agentit-gitops` under the token user when blank) for unregistered apps, an **Actions** tab showing that app's own pending gates (Approve & Deliver / Reject / Dismiss) across **every** historical assessment of this app, not just the current one (`AssessmentStore.list_gates_for_assessment()` joins by `repo_url`), a standalone **Scan**/**Re-scan** action (same `POST /assess` Fleet's row action uses, now also reachable from the app's own page — plain submit when never onboarded, a confirm dialog first when it will auto-chain into onboard), an **Open PRs** list on Overview (currently-open PRs against the code repo and/or GitOps infra repo, real GitHub state), a **PR History** tab (every PR ever opened for this app + its final outcome — merged / rejected [with reason, when captured] / open — see below), timeline |
| **Onboarding Results** | **Scan-results-only.** Lists PRs opened by Scan/`auto_delivery` (skills-primary generation summary; optional codechange source patches). Happy path: open PRs → **merge on GitHub**. No Commit / Per-Agent / Direct Apply CTAs. **Retry Scan delivery** appears only when the latest job is `needs_attention` and no PR is open yet. Download + PR cards remain. |
| **Insights** | Fleet stats, agent performance (from real `agent_runs` records), low-effectiveness skills, fleet-wide check compliance (pass rate per data-driven check across every recorded assessment), and fleet-wide learning feedback. Actionable rollups deep-link to Fleet (incl. a real "Total PRs" count, no live GitHub call) / Events; agent rows → `/agents/{name}`; skills needing review → per-skill history |
| **Decisions** | Audit of every real LLM *decision* point (fix-review, secret-classify, capability-proposal — not just LLM-generated content; auto-mode classify was retired along with AutoMode itself, 2026-07-18), attributed by the agent or skill that triggered it, with the LLM's actual reasoning and a per-agent/skill approve/reject/gate breakdown. See `llm_decisions.py` for exactly what's covered and what isn't. |
| **Capabilities** | Skills/checks catalog, onboarding agents, watchers, and the **Research Skills** trigger. A **Needs Review** section (skills with `status: draft`) sits above the fold, uncollapsed — the one thing on this page that needs a real human decision, not reference material. Per-skill actions follow the app's primary/secondary/destructive button convention: **Activate**/**Reactivate** (`.btn-green`) for draft/deprecated skills, **Deprecate** (`.btn-danger-outline`, reason required) for active ones — both persisted via the same git-commit-and-PR flow (`skills/` is baked into the container image, no volume mount). **Skill Activity** reads `skill_effectiveness` (`skill_name` / `outcome` / `reason` / `app_name`) via `get_recent_skill_activity()` — not `agent_feedback` field names. Its **Self-Improvement** tab has the matching **Run Scan** trigger for `capability-scout` (previously only reachable via its 24h watcher tick). Onboarding-agent/watcher reference rows link to their real `/agents/{name}` page. Tabbed with **Agent Activity** (live registry of who's actually run, their real success rate, and a per-agent run-history table with duration/resource tier/error; status badge is heartbeat-age-derived like Schedules', not the always-`'active'` `agent_registry.status` column, so a crashed/long-stopped agent shows stale instead of Active forever) — see the per-skill detail page (`/capabilities/skills/{name}/history`) for triggers/outputs/mode/version/body plus effectiveness trend and activation/deprecation history. |
| **Events** | The system's real-time activity/audit-trail feed — every action the system itself takes, behind the scenes (watcher ticks, webhooks, gates opened, drift detected, ...), regardless of whether it involves a PR. Bell feed + full page with filters/pagination — not ops home. `correlation_id` Chain column; DLQ republishes Kafka dead-letters |
| **Health** | Live infrastructure telemetry — rollout/pod/pipeline status, Kafka, circuit breakers, deploy status |
| **SLOs** | SLO definitions and error budgets. Fleet SLOs (`/fleet/slos`) and per-app SLO lists read `AssessmentStore.list_slos()`, which is scoped by app `repo_url` (so SLOs survive re-assessment) and keeps only the newest assessment's copy of each `(metric_name, target_value)` left by repeated onboarding — otherwise each metric appeared once per historical assessment. Same-identity rows on one assessment stay visible. |
| **Settings** | Retention/purge, LLM availability status, configuration (the Auto-Mode toggle/allowlist/decision-matrix are gone — AutoMode was removed entirely, 2026-07-18). Tabbed with **Schedules** (watcher status — now backed by real heartbeats — and cron jobs, each paired with a real English description from a general 5-field cron parser, `schedules.py::humanize_cron()` — covers the weekly-on-a-day-and-hour / monthly-on-a-day-and-hour shapes AgentIT's own skill templates actually generate (plus daily/yearly), replacing a 5-entry exact-string lookup that echoed any other cron back as its own "human-readable" version, rendering it duplicated on the page; a genuinely unparseable cron now shows no second span instead of that duplicate) |

**Events, Ledger, Decisions — three feeds, three distinct jobs (2026-07-18 holistic UX pass, product-direction clarification).** All three read from overlapping backing data (the `events` table, `gates`, `deliveries`), which invited the question of whether they're really three separate concepts or one page split three ways. The authoritative answer: **Events** is the system's own real-time activity/audit-trail feed — every action the system itself takes, behind the scenes, regardless of whether it involves a PR (watcher ticks, webhooks received, gates opened, drift detected, and so on); it is the one page that should never gate what it shows behind "does this need a human." **Ledger** (redesign in progress separately) is the cross-app view of PRs that need attention — real lifecycle status (waiting for approval / rejected with reason / merged / open), filterable by type and app; it is not a general activity log. **Decisions** stays the narrower LLM-safety-classification audit (fix-review, secret-classify, capability-proposal — auto-mode classify was retired along with AutoMode itself, see "AutoMode removed entirely" below) — unchanged by this clarification. One real, structural consequence flagged for the in-progress Ledger redesign rather than fixed here (to avoid duplicating or contradicting that work): today's `ledger.py::get_ledger_cards()` unions in several card types that are pure system activity with no PR involved at all (watcher ticks — card H, drift detection — card K, skill learning runs — card M, catalog changes — card N, auto-mode setting changes — card P, API-removed/skill-deprecated — card L), which means the exact same row can currently render on **both** Events and Ledger. Once Ledger's redesign narrows its own query to PR-lifecycle cards only, this overlap resolves as a natural side effect — no separate fix needed on the Events side, which already renders the full, unfiltered `events` table correctly.

**PR tracking (`pr_tracking.py`)** aggregates every real PR AgentIT has ever opened for an app from the three places one can land: a `gitops-pr-pending` gate's own `pr_url` + `status` (a reliable, already-known outcome — approving a gate *is* the merge, so no live GitHub call is needed there), `deliveries.details_json` outcomes for the `source-repo-pr`/`app-repo-pr` mechanisms, and `onboarding_results.pr_url` (may be several `|`-joined URLs from multi-cluster Scan delivery). The latter two carry no stored outcome, so Fleet's **Open PRs** column and Assessment Detail's **Open PRs**/**PR History** all resolve their live GitHub state via `github_pr.get_pr_status()` — cached fleet-wide for 120s and batched into one round of concurrent calls (`routes/fleet.py::_attach_pr_counts`) so a Fleet page load never fires one GitHub call per app. A rejected `gitops-pr-pending` gate's reason (`agent_feedback.human_reason`, recorded by `resolve_gate()`) is best-effort correlated back onto its PR History row; anything not tracked renders **Unknown** rather than a fabricated status. Fixed a real bug found while wiring this up: `resolve_gate()` read a gate's app name as `target_app` (an `events`-table column name) instead of `app_name` (what `list_gates()`'s join actually produces), so every gate-rejection feedback row — and the `rollback-approved`/`finding-escalation-acknowledged`/`gitops-pr-merged` events — was silently recorded with no app attribution at all.

**Criticality** is a plain user-selected value at Assess time (`low`/`medium`/`high`/`critical`, sticky across Re-assess) — never LLM-inferred or config-driven. It no longer gates anything reachable today: every entry point that used to read some form of criticality-derived "can this auto-deliver" signal (`AutoMode.should_auto_apply`, the post-onboarding auto-deliver chain, the webhook/dispatcher auto-apply path, `cli.py --auto-apply`) was removed along with AutoMode itself (2026-07-18, see "AutoMode removed entirely" below) — Scan/`auto_delivery` opens GitOps PRs; humans merge. Criticality no longer gates auto-apply (AutoMode is gone). `FleetOrchestrator._can_auto_approve()`/`OrchestrationPlan.auto_approve` (the computation this criticality check used to live in) were themselves deleted entirely 2026-07-20 -- an architecture-review audit confirmed their only real consumer was AutoMode, so keeping the computation around after AutoMode's removal just meant an unused plan attribute and a CLI echo line with nothing acting on either. Criticality's one remaining real effect today: **default SLO strictness** — the availability/error-rate/latency targets `FleetOrchestrator._create_default_slos()` seeds (independently monitored and enforced by `watchers/slo_tracker.py`) scale with criticality. Fleet and Assessment Detail's Criticality badge, and the Assess form's own Criticality field, carry help text/tooltips spelling this out instead of showing an unexplained value.

**Criticality re-verified against current code and one dead effect removed (2026-07-18).** A since-superseded investigation had described a third effect — "`high`/`critical` adds an extra required `deploy-approval` gate" — as if it were a real, separate blocking mechanism, distinct from (1) above. Re-checked against current code: `_determine_gates()`'s `gates_required` list (which used to include `"deploy-approval"` for `high`/`critical`) was never passed to `store.create_gate()`, never checked by any delivery code path, and never rendered in the portal UI — its only two consumers were the CLI's plain `echo` and an `orchestration-summary.md` file written into a `tempfile.TemporaryDirectory()` that's deleted the instant `helpers.py`'s onboarding call returns. No gate a human could ever see or resolve was ever created from it; it was dead on arrival, not a casualty of today's earlier `cluster-admin-review` removal. Removed the dead `if criticality in ("high", "critical"): gates.append("deploy-approval")` line from `_determine_gates()` (`security-review`/`final-approval`, equally unconsumed but unrelated to criticality, were left alone as out of scope). The real "extra step" a high-criticality app's user experiences was — and still is — effect (1) above, not a distinct gate type. Tested: `tests/test_orchestrator.py::TestPlanSelectsAgents::test_gates_required_no_longer_varies_by_criticality`.

**Scan/Re-scan button consolidation:** Fleet and Assessment Detail used to show two differently-labeled buttons for what had quietly become the exact same backend action (`assess_submit()`, always auto-chaining into Onboard → Dry Run → Deliver) — plain **Re-assess** for never-onboarded apps, **Refresh Onboard** for previously-onboarded ones. Copy-only rename, same `POST /assess` route and same conditional confirm dialog: **Scan** for never-onboarded apps (no confirm needed, nothing to overwrite), **Re-scan** for previously-onboarded apps (confirm dialog, since it regenerates existing onboard manifests). Applied consistently on Fleet's row action, Assessment Detail's standalone action, and the Cmd+K command palette's per-app action.

Webhook endpoints power the event-driven loop: `/api/webhook/assess`, `/api/webhook/github-push`, `/api/webhook/onboard`, `/api/webhook/finding`, plus three self-monitoring endpoints described below (`/api/webhook/synthetic-probe`, `/api/webhook/backup-status`, `/api/webhook/secret-check`). All but `github-push` require the shared-secret `X-Internal-Webhook-Token` header (see [Security notes](#security-notes)). (`/api/webhook/auto-apply` and `/api/webhook/remediate` were removed 2026-07-20 along with the dead `RemediationLoop` pipeline they only existed to serve — see the changelog entry below.)

**`base.html`'s embedded CSS/JS extracted into real static files; a `StaticFiles` mount established for the first time (2026-07-20, reuse & refactor review follow-up).** `templates/base.html` (the shared page shell every page `{% extends %}`) had grown to 2274 lines by carrying a ~1430-line inline `<style>` block and a ~560-line inline `<script>` block alongside its actual HTML structure — the review flagged it as a hotspot. Read the whole file first to map exactly what was CSS vs. JS vs. real markup, and grepped the `<script>` block specifically for `{{`/`{%` Jinja syntax (docs/portal templates route dynamic values into JS via server-rendered `{{ }}` interpolation elsewhere, e.g. the nav's `build_info`/`current_user` values, so this had to be checked, not assumed) — found **zero**: every value the script reads (the CSRF cookie, `window.location`'s query params, `/api/events`/`/api/fleet` fetch responses) is already a real-time client-side value, never a server-templated one, so no `window.AGENTIT_CONFIG`-style bridge was needed anywhere. The nav's own Jinja-templated values (`current_user`, `nav_pending_actions`, `build_info`, `request.url.path`-derived active-link classes, the CSRF token echoed via a cookie `getCsrfCookie()` already read) all live in HTML markup/attributes, untouched by this move.

New layout: `static/css/base.css` (design tokens, reset, primary nav, events drawer) + `static/css/components.css` (every reusable page-level UI component — cards, buttons, forms, tables, badges, modals, command palette, lifecycle stepper, filter bar, timeline, and this stylesheet's own `@media` responsive overrides) — content byte-identical to the original inline block, verified programmatically (not by eyeballing a diff) before touching `base.html` itself. `static/js/base.js` (CSRF header injection, htmx error/loading wiring, the shared confirm modal, toast manager, timestamp formatting) + `static/js/events-drawer.js` + `static/js/command-palette.js` (the two genuinely separable Alpine components) — same byte-identical-content guarantee. `base.html` itself shrank from 2274 to 302 lines: real HTML structure plus `<link rel="stylesheet" href="/static/css/...">`/`<script src="/static/js/...">` tags.

**Static-file serving:** this app had no prior `StaticFiles` mount, no `static/` directory, and no existing convention to follow — `app.py` now does `app.mount("/static", StaticFiles(directory=...), name="static")`, the standard FastAPI pattern, immediately after the `Jinja2Templates` setup it sits beside. **Caching:** no existing static asset (there were none) had a cache-busting convention either, so this establishes one rather than reusing something already load-bearing: every `<link>`/`<script>` tag carries `?v={{ build_info.commit }}` (defaulting to `'unknown'` when unset, e.g. local dev), the exact same value `portal/metrics.py::get_build_info()` already exposes for the nav's deploy-status badge — a real deploy's new commit SHA always invalidates any cached copy, with no separate asset-versioning pipeline. Starlette's `StaticFiles` already emits `ETag`/`Last-Modified` for conditional-GET/304 support on top of that, so no extra `Cache-Control` header handling was added. **CSP:** grepped `app.py`, every middleware, and `chart/` for `Content-Security-Policy`/CSP — this app sets none today, so there was nothing to keep compatible and nothing added; moving from inline to external `<style>`/`<script>` does mean that if a CSP is added later, it can omit `'unsafe-inline'` for style-src/script-src far more easily than it could have with the old inline blocks (a real, if secondary, benefit of this move worth knowing about whenever that happens).

Test coverage: several existing tests and `scripts/check_portal_edl.py`'s static EDL checker used to grep `base.html`'s raw text (or a rendered page's HTML) for CSS rule text or JS implementation details that only ever appeared there because of the old inline blocks — all updated to check the new `static/css/`/`static/js/` files instead (`tests/test_template_rendering.py`, `test_portal.py`, `test_portal_edl.py`, `test_ux_requirements.py`, `test_customer_trust_ux.py`), plus a new `test_static_css_and_js_actually_resolve` that fetches every static file through the real app (not just string-matching a `<link>`/`<script>` href/src) so a broken mount or typo'd path fails loudly. Along the way, found and fixed a latent gap two tests had been masking: `test_fleet_uses_design_system_classes`/`test_assessment_detail_uses_design_system_classes` asserted CSS class names that only render in HTML under conditions (`total_apps > 1`, an onboarded lifecycle stage) their fixtures never actually triggered — they only ever passed because the old inline `<style>` block's CSS rule text happened to contain the same class-name substring on every page regardless of what rendered; fixed the fixtures to genuinely trigger those conditions. Landed as two commits (CSS extraction, then JS extraction), each independently verified against the full non-browser suite (2944 passed, 375 skipped, 0 failed at both checkpoints) before the next. Zero visual or behavioral change: every page renders the identical DOM/computed styles, same Alpine reactivity, same htmx wiring, same command palette/nav/events drawer.

### Operating AgentIT on itself

AgentIT is a platform that assesses and hardens *other* apps — the gap was that it never fully ran that same playbook on itself. AgentIT is now registered in its own fleet (`chart/templates/tekton/pipeline.yaml`'s `register-self-in-fleet` task calls `/api/webhook/assess` with its own repo URL on every CI build), which is what lets the fleet-wide `vuln-watcher` and the `cost-report` CronJob cover AgentIT the same way they cover every app it onboards. Alongside that, six new opt-in chart features close the gaps kubelet-level health checks and the existing watchers structurally can't reach:

| Capability | Chart flag | What it catches |
|---|---|---|
| External synthetic uptime probe | `syntheticProbe.enabled` | Route/router-layer failures — kubelet's `/healthz`/`/readyz` probes hit the container directly and can't see these |
| TLS certificate-expiry watch | (same CronJob) | Alerts at <30d/<14d/<7d via `agentit_route_cert_expiry_days` |
| Backup success/failure reporting | (always active once `backup.enabled` / `postgres.bundled.backup.enabled`) | A silently-failing backup job now sets `agentit_backup_last_status`/`agentit_backup_last_success_timestamp` instead of only leaving a line in a CronJob pod's logs |
| Secret rotation + drift detection | `secretRotation.enabled` | Monthly rotation of `agentit-internal-webhook-token` (+ automatic restart of every consumer); a lighter existence check for `github-webhook-secret` (can't be safely auto-rotated — see the CronJob's own comment) that would have caught the 2026-07-13 incident below in minutes instead of ~8.5 hours |
| Rate limiting on the portal's own routes | `rateLimit.enabled` | A runaway/replayed webhook loop hitting the routes that deliver changes to a live cluster — see `rate_limit.py` for exactly what this is (and isn't) a substitute for |
| Slack alert routing | `monitoring.slack.enabled` (needs an `agentit-slack-webhook` Secret you create yourself — never generated or hardcoded) | The alerts in `prometheusrule.yaml` previously fired with no confirmed destination |
| Own-repo dependency/base-image bumps | `.github/dependabot.yml` | `pip` + `github-actions` + `docker` (Containerfile *and* the two pinned images in `chart/values.yaml`) — the same `dependabot-config` skill AgentIT generates for apps it onboards, now pointed at itself. Native Dependabot PR bodies already carry changelog/CVE reasoning; see that file's own comments for one open dependabot-core limitation (`Containerfile`-named PR creation) and one structural one (Helm-templated `image:` refs in `chart/templates/**` aren't reachable by any static scanner) |

See [`docs/deployment.md`](docs/deployment.md) for the full incident writeup these were prioritized against, and this repo's own commit history for the "what's worth adding now vs. later" reasoning behind which checklist items got picked first.

**Hardened against duplicate Fleet rows for AgentIT's own self-registration.** A deployment-lag incident (`register-self-in-fleet`'s pipeline param still had a hardcoded `.git`-suffixed `repo-url` default in `chart/templates/tekton/pipeline.yaml`, briefly live before `normalize_repo_url()` shipped) created a second, duplicate Fleet row for AgentIT itself — cleaned up manually that session via a live DB query / `AssessmentStore.delete()`. A real customer deployment has no one with that access, so this needed to be structurally impossible, not just fixed at the one known call site. Three layers now cover this:
1. **The known source is fixed** — the pipeline's `repo-url` default no longer has a `.git` suffix, matching `normalize_repo_url()`'s canonical form.
2. **DB-layer normalization trigger** (`normalize_repo_url_before_write` in `store.py`'s `SCHEMA_SQL`) mirrors `normalize_repo_url()` in SQL and fires on every `INSERT`/`UPDATE` of `repo_url` on `assessments`/`apps` — so a non-canonical value can't reach storage even from a write path that bypasses the Python normalizer entirely (a raw SQL statement, a future code path, a stale hardcoded URL). Combined with `apps.repo_url`'s existing `PRIMARY KEY`, this makes that key a true DB-enforced uniqueness constraint on the *normalized* identity, not just the raw string — a second raw `INSERT` for a `.git`-suffixed spelling of an already-registered repo is now rejected outright by Postgres.
3. **Self-healing dedupe** (`AssessmentStore.dedupe_repo_urls()`) covers rows written *before* that trigger existed (the actual incident): it merges any repo genuinely represented under two or more raw `repo_url` spellings into the canonical form, preserving every assessment's history (only the identity string changes; dependents stay attached by `assessment_id`) and folding `apps.infra_repo_url`/timestamps with the same "latest write wins" policy `_upsert_app()` already uses. Runs once at every `AssessmentStore.create()` (so a fresh deploy heals whatever it inherited immediately) and every 5 minutes from the existing background maintenance loop (`app.py::_background_maintenance`, alongside `reap_orphaned_jobs()`) — so a duplicate introduced by any other means self-heals on its own, with no live DB access required. See `tests/test_store.py`'s `TestRepoUrlNormalizationDbTrigger`/`TestDedupeRepoUrls` for real-Postgres proof of both layers.

**Fixed a permanently-stuck "GitOps (pending)" badge for AgentIT's own Fleet row.** `gitops_registered`/`is_gitops_registered()` (`delivery.py`) only ever checked for a live Argo CD Application named `managed-{app}` — the name `github_pr.ensure_applicationset()`'s shared `apps/*`-directory ApplicationSet generates. But that ApplicationSet deliberately excludes `apps/agentit` (to avoid a circular/duplicate Application, since AgentIT already deploys itself via a hand-crafted `argocd/application.yaml` Application named literally `agentit`), so `managed-agentit` can never exist — Fleet showed AgentIT stuck on "GitOps infra repo known; awaiting the first delivery to bootstrap Argo CD registration" no matter how many deliveries actually landed in `apps/agentit/` (verified live: multiple merged/open PRs against the infra repo, and the `agentit` Application itself Synced/Healthy). `is_gitops_registered()` and the Fleet-wide Argo enrichment (`routes/fleet.py`) now also check for a literal-named Application and confirm it's genuinely this app's own deployment by comparing its `spec.source.repoURL` against the app's own `repo_url` (`delivery.is_self_managed_application()`) — not just presence-by-name, which would wrongly match an unrelated Application that happens to share the app's name.

### Self-observability

AgentIT's own Postgres store and `/metrics` endpoint are the source of truth for "what is the platform actually doing", not just what it can do:

- **`agent_runs` table** — `FleetOrchestrator` writes one structured row (agent, mode, status, duration, resource tier, error, assessment_id) per agent execution, local or Kubernetes-Job. `AssessmentStore.get_agent_stats()` and the new `list_agent_runs()` read from this table instead of pattern-matching event `action` strings, so the Agents/Insights pages reflect real run history.
- **`check_results` table** — every data-driven check run during an assessment (`check_engine.run_checks_by_dimension_with_status`) is snapshotted pass/fail, keyed by assessment. `get_check_compliance()` aggregates this into a fleet-wide pass-rate view on the Insights page.
- **`correlation_id` on events** — `AssessmentStore.log_event()` accepts a `correlation_id` (populated with the `assessment_id` by `save()`, `save_onboarding()`, and `FleetOrchestrator`), matching the same id already used for Kafka's `correlationId`. The Events page's new "Chain" column links straight to `/events?correlation_id=...` to trace an assess → onboard → apply run end to end.
- **DLQ end-to-end** — `EventConsumer._dead_letter()` now persists to the `events` table (not just the Kafka `agentit-dlq` topic) so `/events/dlq` actually shows failures, and `retry_dlq_message()` republishes the original message to its original topic via the Kafka producer instead of only relabelling the row.
- **Circuit breaker visibility** — `portal/helpers.py::get_circuit_breaker_states()` exposes live LLM/Kubernetes breaker state, shown on the Health page and set on the `agentit_circuit_breaker_open{name=...}` gauge every time `/health` is polled.
  **The "Platform" card's bare "Partial"/"Degraded" word gave no way to tell why — now it names the exact sub-condition(s) (2026-07-20, prompted by a product-owner screenshot of a live "Platform: Partial" state while Rollout/Pods/Last Pipeline/the `agentit` Argo Application/Last Good CI all read healthy).** Traced `health.html`'s `truly_healthy` (`argo_synced and pods_failed == 0 and kafka_ready`) to its exact real inputs in `_get_cluster_health()` and confirmed, by reading the code rather than assuming from the plausible-looking candidate list (credential state, watcher heartbeats, webhook delivery health, deploy status, capability-scout's known test-gate gap), that **none of those actually feed this card at all** — only three things do: `argo_synced` (computed across *every* Argo CD Application matched to this AgentIT instance — its own platform Application **and** every fleet app it has onboarded via GitOps, not just AgentIT's own infrastructure), `pods_failed`, and `kafka_ready`. That first fact is itself the live root cause class: a "Partial" Platform card can be caused entirely by an unrelated managed customer app drifting out of sync in Argo CD, with nothing wrong in AgentIT's own infrastructure — a screenshot showing the `agentit` Application itself `Synced`/`Healthy` alongside a Partial Platform card is exactly that shape, not a contradiction. `_explain_platform_status()` (`routes/health.py`) now builds a concrete reasons list straight from these same already-computed fields — which named Argo CD Application(s) aren't `Synced` (out of `len(argo_apps)` total, not just a count), whether Kafka was found at all versus found-but-not-Ready, which pod(s) actually failed — mirroring `get_credential_states()`'s real-data-only `{ok, status, detail}` convention rather than introducing a new shape. `health.html` renders it as a `title` tooltip on the stat value (this page's own existing convention for a disabled/non-clickable card's "why") plus a visible muted line directly under "Partial"/"Degraded" (never shown when truly Healthy, and never a placeholder — an empty reasons list means every sub-condition genuinely passed). Deliberately does not change what `argo_synced`/`kafka_ready` mean or how any of the three sub-conditions are computed — surfacing *which* known-short condition is active is this pass's job; whether the Platform card's `argo_synced` should ever be re-scoped to exclude managed fleet apps is a separate product decision, not decided here. See `tests/test_portal.py`'s `test_platform_healthy_shows_no_explanatory_reasons`/`test_platform_partial_explains_which_fleet_app_is_out_of_sync`/`test_platform_partial_explains_kafka_not_ready`/`test_platform_partial_explains_no_kafka_resource_found`/`test_platform_degraded_still_lists_pod_names`.
- **Clickable System Health cards** — each `/health` stat card (and Argo/Tekton/Kafka rows) deep-links to the best real ops surface: OpenShift Observe metrics, console Pods/Rollout/PipelineRun/Application/Kafka pages, or GitHub commit/Actions. URLs come from `AGENTIT_CONSOLE_URL`, the cluster `Console` CR, or the portal's own Route apps-domain (`portal/health_links.py`); unresolved destinations stay non-clickable with a tooltip reason — never mocked.
- **Ambient deploy-status indicator** — a compact badge in `base.html`'s nav, present on every page, shows the running version/commit (`portal/metrics.py::get_build_info()`, an in-memory read of the `agentit_build` metric — no per-request I/O) and switches to a live "Deploying · &lt;stage&gt;" state, polled via htmx (`GET /api/deploy-status`, every 15s) from `_get_deploy_status()` in `portal/deploy_status.py` (via `routes/health.py`). That function combines the running build with the newest `agentit-ci` PipelineRun by `creationTimestamp` (list order is not guaranteed) and the live `agentit` Argo CD `Application`'s sync/health (same RBAC as the existing Argo-reading code — no new grants needed): a PipelineRun actually running, Argo `Progressing` / `Suspended` (canary pause), an in-flight sync (`operationState.phase=Running`), or OutOfSync with non-Healthy health shows "Deploying"; **residual OutOfSync while Healthy and idle** (common HPA vs chart `replicas` / `restartAt` drift — also covered by Rollout `ignoreDifferences` in `argocd/application.yaml`) stays idle like Synced, not "Deploying · syncing"; a failed PipelineRun or settled `Degraded` Application health shows "Deploy failed" with the real reason; **Cancelled** / `PipelineRunCancelled` CI (capacity / concurrency noise) stays idle rather than pinning the badge to Failed. The badge path is hardened against a wedged kube-apiserver: short per-call timeouts (2s), an overall `asyncio.wait_for` deadline (~3s), and a ~20s last-good cache so htmx polling cannot pin portal workers until oauth-proxy returns 502/503 — timeouts return **200** with degraded/unknown (or last-good) HTML instead of hanging. The Health page's "Deployment Status" section reuses the same function (`include_commit_info=True`) to add a stage-by-stage task stepper, the commit message being deployed (`portal/github_pr.py::get_commit_info()`), and — once a deploy is no longer in progress — whether this instance actually ended up running the new version or rolled back (compared against its own live `AGENTIT_GIT_COMMIT`, now wired in `chart/templates/deployment.yaml` from `.Values.image.tag`, the same value the CI pipeline pins to the deployed commit SHA). No field is ever fabricated: an unreachable PipelineRun/Argo/GitHub API surfaces via the badge/panel's `errors`/`reason` fields instead of silently looking idle.

  **AgentIT Self-Health: a systematic self-check watcher, not just ad hoc fixes (2026-07-18).** A single session surfaced eight distinct incidents (a stuck CI pipeline, a stale deploy, a misleading badge, a silently-broken cleanup CronJob, a silently-blocked GitHub webhook, a corrupted ApplicationSet, duplicate Fleet rows, and a silently-empty skill generation) that shared one pattern: zero proactive signal, every one found only because a human happened to notice and ask. Each bug is fixed point-by-point (see `docs/self-health-check-backlog.md`'s incident inventory for exactly which commit fixed which), but nothing generalized the *lesson* until now. `self-health-check` (`watchers/self_health_check.py`) is a new watcher, following the exact same tick/heartbeat/`record_tick` pattern every other watcher already uses, that periodically runs four genuinely new self-checks: (1) **webhook reachability** — reuses `github_pr.check_webhook_delivery_health()` (a live check landed concurrently, same session, backing the Health page's own new "Webhook Deliveries" section) rather than a second, independent implementation; this watcher's own contribution is running that same check periodically *in the background* and persisting the result, so a failure reaches the sitewide badge even if nobody has opened `/health` recently — the live check alone only updates on page load; (2) **CI pipeline progress** — flags the latest `agentit-ci` PipelineRun if it's been "Running" past a generous threshold (~60 minutes, sized off the pipeline's own task timeouts), a more direct and earlier signal than `DriftDetector`'s existing GitOps-lag check for the same failure class; (3) **maintenance CronJob success** — generically checks every non-suspended CronJob in the namespace has completed successfully on its most recent scheduled run (`kube.list_cronjobs()`), covering any current or future CronJob, not a hardcoded list; (4) **cleanup effectiveness** — `kube.count_stale_terminal_pods()` catches a CronJob that reports success while its cleanup logic silently does nothing (the generalized version of the exact tekton-cleanup word-splitting bug documented in `docs/cicd-stall-hardening-2026-07-17.md`). Every result — pass *and* fail — is dual-written to Kafka *and* `store.log_event()` (mirroring `SloTracker._recommend_rollback()`'s convention, deliberately unlike `DriftDetector`'s Kafka-only `gitops-lag-detected`/`applicationset-repo-drift-healed`, which this audit flagged as likely not actually reaching `/api/events` today), so it's genuinely visible on the sitewide critical/high badge and a new "AgentIT Self-Health" panel on the Health page (pass/fail per check, plain-language summary, and a concrete next-step for anything failing — never a raw error dump). None of these four checks self-heal: each surfaces a condition for a human to act on, since none of the underlying fixes (re-registering an unknown webhook state, restarting a wedged pipeline, deleting cluster objects) are safe to apply without oversight. See `docs/self-health-check-backlog.md` for the full incident-by-incident categorization (self-healable vs. detect-and-surface vs. genuinely-new-check-needed) and the prioritized backlog of what's deliberately not built yet.

  **Rebased onto post-AutoMode-removal `main` (2026-07-18).** This watcher has no functional dependency on AutoMode — none of its four checks read the `auto_mode` setting or call into anything AutoMode-gated — so rebasing onto the concurrent effort that removed AutoMode gating from `drift-sync`/`vuln-watch` (and webhook handlers) required no logic changes here, only a docstring correction: the comparison this watcher's module docstring drew to `DriftDetector`'s drift-resync ("safe/idempotent, so it auto-syncs when auto-mode is on") was already stale by the time it was written, since that gating had already been removed a few commits earlier in the same branch history. Full suite (2808 passed, 311 skipped) re-verified green post-rebase.

  **GitOps pipeline stall detection (2026-07-17 incident fix).** Commits merged to `main` stopped reaching the live cluster for hours one night — nothing told anyone until a human happened to notice, because the existing "did the last CI run/deploy succeed" checks above all assume a PipelineRun for the new commit actually *ran*; none of them catch "no PipelineRun ever promoted this commit at all" (e.g. `notify-argocd` stuck on pod scheduling — see [`docs/cicd-stall-hardening-2026-07-17.md`](docs/cicd-stall-hardening-2026-07-17.md) for the full root-cause writeup). `drift-detector`'s existing 10-minute Argo poll now also compares the `agentit` Application's actually-synced revision against `origin/main`'s real HEAD via `github_pr.get_commits_behind()` (GitHub's Compare API — works without a `GITHUB_TOKEN`, unlike every other call in that module, so a missing token can't silently disable it) and publishes a `critical`-severity `gitops-lag-detected` event once the deploy is more than 3 commits *or* 1 hour behind. No new UI needed: `base.html`'s existing events drawer/badge already surfaces any `critical`/`high` event sitewide.

  **`agentit-managed-apps` ApplicationSet self-heal — closing a real, twice-repeated incident (2026-07-18).** An earlier investigation that same day found `openshift-gitops`'s fleet-wide `agentit-managed-apps` ApplicationSet had its `spec.generators[0].git.repoURL` / `spec.template.spec.source.repoURL` overwritten with a bogus placeholder host **twice in one day** by manual `oc create`/`oc patch` commands run directly against the live cluster — confirmed via `metadata.managedFields` field-manager fingerprinting (`kubectl-create`/`kubectl-patch`, neither of which is AgentIT) — breaking GitOps rollout for the entire fleet both times until a human noticed and manually restored it. No code/chart fix was possible at the time (this object isn't Helm-templated by this repo at all), so this closes the loop with active self-healing instead: `DriftDetector.detect_once()` now also runs `_check_applicationset_drift()` every tick (independent of the Argo Applications poll above, since an ApplicationSet is a different resource) — it reads the live ApplicationSet's current repoURL(s), compares against `github_pr.expected_managed_apps_repo_url()` (derived the exact same way `ensure_infra_repo()`/`_auto_create_infra_repo()` already do: `_parse_owner_repo()` on AgentIT's own repo, plus the "one shared `agentit-gitops` repo per GitHub owner" naming convention — never a second hardcoded guess of the URL), and if either field has drifted, calls `github_pr.ensure_applicationset()` — the exact same function onboarding already uses to write this spec, so there's one source of truth for "correct", not a second copy — to patch it back. Deliberately narrow: this only ever *corrects* an existing, drifted value; it never creates or deletes the ApplicationSet (that stays owned by onboarding's own `ensure_applicationset()` call) and only touches the two repoURL fields, via the same full-object merge-patch `ensure_applicationset()` always performed. Publishes a `critical`-severity `applicationset-repo-drift-healed` (or `-heal-failed`, if the patch itself errors) event via the same `EventPublisher` pattern as the `gitops-lag-detected` check above, so a heal is visible/auditable on Events rather than silently correcting in the dark. RBAC needs no changes: verified live (`oc auth can-i patch applicationsets.argoproj.io -n openshift-gitops --as=system:serviceaccount:agentit:agentit` → `yes`) that the `agentit` ServiceAccount already has this via the existing cluster-wide `edit` `ClusterRoleBinding` (`rbac.clusterWideApply`, default `true`) — the same grant `ensure_applicationset()` already relied on to create this object in the first place. Tested (`tests/test_drift_detector.py::TestApplicationSetDriftHeal`, `tests/test_portal_pr.py`): detects and heals a drifted repoURL shaped exactly like the real live incident (both fields, or either alone); no-ops when already correct; never creates the object when missing; a failed heal attempt still publishes a critical event; a `KubeError` fetching the ApplicationSet doesn't crash the tick; the check runs even when the Argo Applications list itself is unreachable; `expected_managed_apps_repo_url()` is proven to be genuinely derived via `_parse_owner_repo()` (not a hardcoded literal) by swapping its return value and asserting the computed URL changes with it. Live-verified against the real cluster while building this fix: the live ApplicationSet was *actually* drifted to the same bogus placeholder at the time (a third occurrence, live), confirming both the derivation (matches the object's own `kubectl.kubernetes.io/last-applied-configuration` annotation, `https://github.com/alimobrem/agentit-gitops`) and the RBAC check against the real SA.
  **A CI build failure for a not-yet-live commit used to look like a live outage (2026-07-18 fix).** Confirmed live during a real etcd-timeout incident: the latest `agentit-ci` PipelineRun failed with `CouldntGetTask` (Tekton couldn't resolve the `git-clone` Task's `ClusterTask` reference — `error requesting remote resource: etcdserver: request timed out`) *before creating a single TaskRun* (`childReferences` empty), while the actually-live Rollout/Argo `Application` stayed `Healthy`/`Synced` throughout on the previous good commit — yet the badge/Health page showed "Deploy failed", indistinguishable from a real live regression. `_get_deploy_status()` now treats `CouldntGetTask`/`CouldntGetPipeline` (Tekton's Task/Pipeline-*resolution* failure reasons, which by definition precede any TaskRun) the same way as the existing `Cancelled` carve-out **only when no TaskRun was ever created** (`childReferences` empty) — nothing was ever built or pushed for that commit, so it can't be a regression in what's live. This is recorded separately as `unreleased_ci_failure` (reason + target revision) rather than forcing `state="failed"`, and is cleared if Argo/Rollout turns out genuinely `Degraded` for an unrelated reason so a real live failure is never masked. The ambient badge keeps its healthy/idle color and appends "· CI build failed for newer commit"; the Health page adds an explicit "Live deploy above is healthy and unaffected" note naming the failed commit and reason — see `tests/test_deploy_status.py`'s `test_deploy_status_task_resolution_failure_*` / `test_deploy_status_unreleased_ci_failure_cleared_when_argo_actually_degraded` for the covered cases, including the defensive one (a resolution-failure reason on a run that somehow did start a TaskRun still reports Failed — the carve-out only fires with the same "nothing ever ran" evidence the real incident had).
  **The same false alarm resurfaced in a new form hours later — generalized the fix (2026-07-18, later same day).** The morning fix above only allowlisted two specific reason strings (`Cancelled`, `CouldntGetTask`/`CouldntGetPipeline`); a few hours later, under the day's heavy concurrent CI load, `run-tests` hit its 10-minute `TaskRunTimeout` on the newest commit on `main` — a real task failure/timeout, not a resolution error, so neither carve-out applied — while the already-deployed older commit kept serving healthy traffic (Rollout 2/2 `Available`, Argo `Healthy`/`Synced`) throughout. The badge again showed "Deploy failed", confirmed live via `oc get rollout`/`oc get application.argoproj.io`/curling `/api/deploy-status` from inside the running pod. Rather than adding `TaskRunTimeout` as a third allowlisted string (a fix that would again be one incident behind the next new reason), `_get_deploy_status()` now checks the one fact that's actually true regardless of reason: a PipelineRun's target `revision` compared against what this instance is *actually* running (`get_build_info()`'s real, live commit) via `_is_confirmed_unreleased_revision()`. Any non-`Succeeded` reason on a PipelineRun whose revision provably differs from the running commit is recorded as `unreleased_ci_failure` instead of `state="failed"` — but only when *both* commits are known and unambiguous; an "unknown" build-info commit (or missing revision) never excuses a real failure, so a genuine live regression still reports Failed exactly as before (`test_deploy_status_generic_failure_on_currently_running_commit_still_reported_failed`). See `test_deploy_status_task_timeout_on_confirmed_unreleased_commit_is_not_deploy_failed` for the covered case.
- **Prometheus gauges actually set** — `agentit_active_gates` updates on every gate create/resolve/expire; `agentit_build` is populated once at startup (package version + `AGENTIT_GIT_COMMIT`/`AGENTIT_IMAGE_TAG`, now real live values — see the deploy-status indicator above); `agentit_db_size_bytes` / `agentit_db_rows_total{table=...}` / `agentit_event_buffer_backlog` refresh every 5 minutes from the portal's background maintenance loop (via async helpers -- `refresh_db_metrics()`/`diff_and_log_inventory_changes()`/`prune_stale_agents_and_log()`/`_reap_orphaned_jobs()`, the last of which also runs once immediately at startup -- see "Onboarding progress stuck forever on a live instance" above); `agentit_watcher_last_success_timestamp{watcher=...}` backs the `AgentITWatcherStale` alert described above; `agentit_synthetic_probe_up` / `agentit_route_cert_expiry_days` / `agentit_backup_last_status{target=...}` / `agentit_secret_check_status{secret=...}` (see "Operating AgentIT on itself" above) are all set via internal webhooks from CronJobs, not from in-process code.
- **Audit log** — `agentit/audit.py::audit_log()` is now wired into every privileged action call site: apply-to-cluster (via the shared `cluster_apply.apply_with_verification()` helper below), gate approve/reject, and data purge.

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

**Postgres is the only supported store — for local use too, not just the live cluster.** There is no more SQLite code path. GitHub Actions CI (`.github/workflows/tests.yml`) provisions an ephemeral `postgres:16-alpine` service with `POSTGRES_HOST_AUTH_METHOD=trust` and a passwordless `AGENTIT_TEST_PG_DSN` so secret scanners are not tripped by throwaway CI credentials. Point `AGENTIT_DB_DSN` at any reachable Postgres instance (e.g. `postgresql://agentit:agentit@localhost:5432/agentit`, or run one via `podman run -d -e POSTGRES_USER=agentit -e POSTGRES_PASSWORD=agentit -e POSTGRES_DB=agentit -p 5432:5432 postgres:16-alpine`); the schema is created automatically on first connection. On the live OpenShift deployment, AgentIT deploys and maintains its own bundled, non-operator Postgres instance (`postgres.bundled.enabled`, on by default, in-namespace, no external dependency or entitlement) and every Deployment/CronJob gets `AGENTIT_DB_DSN` wired in automatically. `portal/store.py`'s `AssessmentStore` is fully async (`asyncpg`) throughout, and every store caller in the codebase (CLI, watchers, the portal, `FleetOrchestrator`/`RemediationDispatcher`/`RemediationLoop`) `await`s it directly. See [`docs/postgres-migration-plan.md`](docs/postgres-migration-plan.md) (now marked historically-complete/superseded) for the full migration history — including the two real cutover attempts and the bugs found and fixed along the way — for how this cutover happened. A one-time `agentit migrate-sqlite-to-postgres` command exists for anyone with real data in a legacy local SQLite file to import into Postgres.

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
| `AGENTIT_PORTAL_URL` | `remediation_loop.py` | Base URL the remediation loop calls back into (default `http://localhost:8080`) |
| `AGENTIT_EXTERNAL_URL` | `portal/routes/assessments.py` | Trusted externally-reachable base URL for outbound registrations (e.g. the GitHub webhook URL). Optional — if unset, the app looks up its own OpenShift Route; only falls back to the request's Host header if neither is available. Never derived from client input. |
| `AGENTIT_AGENT_MODE` | `orchestrator.py` | `local` (default) or `kubernetes` — run agents as K8s Jobs. Falls back to the undocumented `AGENT_MODE` if `AGENTIT_AGENT_MODE` is unset, for backward-compat. |
| `AGENTIT_OFFLINE` | `kube.py` | `1`/`true`/`on` for a genuine hard-offline guarantee: `kube.get_client()` (and everything else in `kube.py`, which all resolve through it) raises `KubeError` immediately instead of resolving a real client. Exists because unsetting `KUBECONFIG` alone is **not** sufficient — the Kubernetes Python client's default config-resolution chain still falls back to the ambient default `~/.kube/config` regardless, which is exactly how two independent live reviews (each explicitly `unset KUBECONFIG` expecting zero cluster access) ended up connecting to a real, live cluster anyway. Set this when testing/reviewing any code path that could reach `kube.py` and you want a hard guarantee it won't touch a real cluster. |
| `GOOGLE_APPLICATION_CREDENTIALS` | Vertex SDK | Path to mounted GCP credentials JSON |

</details>

## Deploying to OpenShift

AgentIT deploys itself the same way it onboards other apps — via the Helm chart in `chart/` and the Argo CD `Application` in `argocd/application.yaml`. **Argo CD is the sole deployer**; see [`docs/deployment.md`](docs/deployment.md) for the full operational runbook.

- Change behavior: edit `argocd/application.yaml` Helm parameters, commit, push. The CI pipeline's `notify-argocd` task rewrites `image.tag` to the build revision in that file, then `oc apply`s it once — so new Helm params sync without briefly deploying the bootstrap `latest` tag. See [`docs/deployment.md`](docs/deployment.md) for the details and why this exists.
- The `smoke-test-image` Tekton task (and GitHub Actions `image-smoke-test`) sets `git config --global --add safe.directory /opt/app-root/src` before `git status`, because OpenShift runs the image under an arbitrary UID that does not see the build-time USER 1001 gitconfig. The image also writes the same path via `git config --system` in the Containerfile so scout/portal pods can use `.git` at runtime. The same Containerfile step marks `.git/` **and** the L3 allowlist trees (`tests/`, `skills/`, `checks/`, `src/`, `docs/`) group-writable (`chmod -R g+w`) so capability-scout can create **and overwrite** source diffs before opening a PR (REST via `portal/github_pr.py`, not the `gh` CLI) under OpenShift's arbitrary UID (gid 0). Both smoke jobs assert `git` plus importable `agentit.kube` / `github_pr` — not `gh --version`. Runtime half: `src/agentit/write_guard.py` preflights those paths in `_open_pr`, drops stale permission-denied tick-failure evidence once the tree is writable again, and the scout Deployment mounts an `emptyDir` at `/tmp/agentit-scout` (`TMPDIR`) for git/py_compile scratch.
- Change a secret: `oc create secret` on-cluster, then reference it via a Helm parameter. Never in Git.
- Never `helm upgrade` manually or `oc edit` the `Rollout`.

Key `chart/values.yaml` feature flags: `rollout.enabled` (canary via Argo Rollouts), `kafka.enabled` / `argoEvents.enabled` (event-driven loop), `tektonCI.enabled` (build pipeline), `cronJobs.{cveScan,complianceRescan,dependencyUpdate,costReport}.enabled` (4 fleet-rescan CronJobs — chart default: disabled; **all 4 enabled on the live deployment via `argocd/application.yaml`** as of the 2026-07-18 "enable all watchers" decision), `agents.{vulnWatcher,sloTracker,driftDetector,skillLearner,capabilityScout,reassessScheduler}.enabled` (6 long-lived watchers — chart default: disabled; **all 6 enabled on the live deployment**), `monitoring.enabled` (ServiceMonitor + PrometheusRule + Grafana dashboard — chart default: disabled; **enabled on the live deployment via `argocd/application.yaml`**, so AgentIT scrapes and alerts on its own `/metrics`), `postgres.bundled.enabled` (AgentIT's own bundled, non-operator Postgres instance — the only store, not a backend option, see [`docs/postgres-migration-plan.md`](docs/postgres-migration-plan.md)), and `auth.enabled` (OpenShift `oauth-proxy` sidecar in front of the portal — see [Security notes](#security-notes) and [`docs/deployment.md#authentication`](docs/deployment.md#authentication)).

The chart includes: NetworkPolicy, ResourceQuota, LimitRange, PodDisruptionBudget, anti-affinity, backup CronJob, dedicated ServiceAccount (not `default`), and a self-assess step in the CI pipeline. Namespace `ResourceQuota` defaults leave headroom for canary Rollouts plus concurrent Tekton runs (`limits.cpu=20`, `pods=60`); tighter quotas can stall canary pods (`ProgressDeadlineExceeded`) and leave the portal on a stale image tag.

**Auth + backup sync notes (dogfood):** `session_secret` / webhook `token` are filled by an Argo CD **Sync** hook Job (`chart/templates/secret-init-job.yaml`), not PostSync — PostSync never ran while oauth-proxy CrashLooped on an empty secret or while a WaitForFirstConsumer backup PVC stayed Pending. Bundled Postgres backups optionally ship a one-shot PVC-bind Sync Job (`postgres.bundled.backup.pvcBindHook`, default **false** — enable only when first binding a Pending WaitForFirstConsumer backups PVC); leaving it on after Bound can re-create a Pending Job on a zone-pinned RWO volume and surface Argo as Degraded. The chart PDB selects `app.kubernetes.io/component: portal` only so Job pods (secret-init, backup) do not trigger PDB SyncFailed (`jobs.batch` has no scale subresource). Prefer `/data` (writable) for watcher event-buffer SQLite when a PVC is mounted.

**Build identity env:** Portal and watcher Deployments set `AGENTIT_GIT_COMMIT` / `AGENTIT_IMAGE_TAG` (scout also `GIT_REVISION`) from `.Values.image.tag` — the same Helm param CI pins to the deploy SHA — so those env vars cannot lag the container image after an Argo sync.

**Argo Pipeline / Rollout drift:** `argocd/application.yaml` `ignoreDifferences` covers Tekton-normalized `Pipeline` fields (`taskSpec.metadata`/`spec`, empty sidecar `computeResources`) and Rollout controller-owned fields (`/spec/replicas` from HPA, `/spec/restartAt` from secret-rotation); the chart also emits matching `computeResources: {}` on the `run-tests` postgres sidecar so `Pipeline/agentit-ci` can stay Synced.

**CI run coalescing under concurrent pushes (2026-07-18):** with many agents pushing to `main` in quick succession, both CI systems previously ran every single push's CI to completion independently — piling up retries and node-resource contention (the same `ExceededNodeResources` congestion class as the incident above) for commits that were already stale before their own run finished. GitHub Actions' `Tests`/`Security Scan` workflows (`.github/workflows/tests.yml`, `security.yml`) now set `concurrency: {group: "${{ github.workflow }}-${{ github.ref }}", cancel-in-progress: true}`, so a new push to the same ref automatically cancels any still-running workflow run for that ref — standard, built-in GitHub Actions behavior. Tekton has no equivalent primitive, so `agentit-ci` (`chart/templates/tekton/pipeline.yaml`) gained its own first task, `supersede-stale-runs`: before any real work starts, it finds other still-active `agentit-ci` PipelineRuns strictly *older* than itself (compared by real `creationTimestamp`, never by name/listing order, so two runs created around the same instant can never cancel each other) and cancels them (`oc patch pipelinerun <name> -p '{"spec":{"status":"Cancelled"}}' --type=merge`) — **unless** that run has already started `build-image` (detected via `status.childReferences`), in which case it's left alone: a run already building/pushing an image on its way to `notify-argocd` must never be superseded by one that hasn't even built yet. Selection uses an explicit `tekton.dev/pipeline: agentit-ci` label this chart's own `TriggerTemplate` now sets on every PipelineRun it creates (`trigger.yaml`), and the `pipeline` ServiceAccount's existing namespace-scoped Role (`rbac.yaml`) gained the one additional `patch` verb on `pipelineruns` this needs — no cluster-admin config outside the chart, same self-containment bar as `tekton-cleanup` below. The whole check is deliberately best-effort/fail-safe (every `oc` call is guarded so a transient API error only skips that one comparison, never fails the pipeline). Verified with `tests/test_helm_templates.py::TestTektonSupersedeStaleRuns` against a fake `oc` on `PATH`.

**Tekton CI reliability (2026-07-17 incident fix, made fully self-contained 2026-07-18):** the namespace's `tekton-cleanup` CronJob (`chart/templates/tekton/cleanup-cronjob.yaml`, runs every 10 minutes) looked like it was pruning stale pods/PipelineRuns, but every multi-field `for x in $(oc get ... -o jsonpath=...)` loop in it word-split its own output apart — the paired timestamp/owner field was always empty, so the `[ -z ... ] && continue` guard fired on every iteration and five of its seven cleanup loops never deleted anything, ever (confirmed live: pods that failed at 03:19 UTC were still present, un-cleaned, 12+ hours later). Fixed to redirect into a file and `while read` line-by-line instead. That CronJob now also directly cleans up standalone/orphaned TaskRuns (never part of a Pipeline, or whose owning PipelineRun's owner-ref GC is delayed/stuck) and prunes the `agentit` ImageStream down to its 10 most recent tags — both closing gaps that were previously only covered by a cluster-admin-configured, cluster-wide `TektonConfig` pruner / `oc adm prune images`, which AgentIT (shipped onto arbitrary customer clusters) cannot assume exists. AgentIT's own namespace hygiene now depends only on what its own chart ships and manages, via namespace-scoped RBAC (`chart/templates/tekton/rbac.yaml`) — never on cluster-level configuration outside its control. Separately, `notify-argocd`'s pod inherits the `source` workspace's zone-scoped EBS PVC node affinity — on this cluster's 3-worker/3-master, one-node-per-zone topology, that pins the whole PipelineRun to a single node for its duration; `trigger.yaml` now tolerates the control-plane taint for that one lightweight task (only that task — not `run-tests`/`build-image`, which would add real load to control-plane nodes) so it has a same-zone fallback, and its timeout is `5m0s` (was `2m0s`) to tolerate real transient scheduling/etcd delays without adding retries (a different task's retry pileup under the same node pressure caused a cascading failure earlier that session). Full root-cause writeup, cluster topology evidence, and the one genuinely structural exception (registry blob-storage GC needs cluster-scoped RBAC AgentIT will never hold): [`docs/cicd-stall-hardening-2026-07-17.md`](docs/cicd-stall-hardening-2026-07-17.md).


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

Asserts MUST rules from [`docs/portal-experience-design-language.md`](docs/portal-experience-design-language.md): no status badges inside `<button>`, Dry Run → deliver choice with status outside CTAs, modals `role="dialog"` + Escape, `pr_url|safe_url`, badge ≥12px, Events bell/drawer IA, `#toasts` / `.btn-danger` present. Also asserts button SHOULD rules: ≤3-word labels and `.btn` (or documented icon control) on interactive `<button>`s. Deliver-route tests cover Scan-only Onboard Results (no Commit / Per-Agent product path).

**Tests that run real `git` subprocess commands must set a local identity first.** A test that does its own `git init --bare`/`git clone` + `git commit` (e.g. `test_activate_skill_survives_simulated_redeploy`, or the throwaway repos in `test_cli.py`/`test_watch.py`/`test_onboard.py`/`test_cloner.py`/`test_comprehensive.py`) needs `git -C <dir> config user.email/user.name` right after clone/init — a dev machine's ambient `~/.gitconfig` masks a missing identity locally, but Tekton's `run-tests` UBI container has none, so `git commit` fails fast with "Please tell me who you are." Confirmed live (2026-07-17): that failure on the *first* `run-tests` attempt triggered its configured retry (`chart/templates/tekton/pipeline.yaml`), and the retry pod then sat `Unschedulable` (`ExceededNodeResources`) until `TaskRunTimeout` force-killed it at exactly 10 minutes — 15 straight CI failures from one missing `git config` call, not a hung test process. `git_pr.py::create_branch_commit_push()` (the production path this test exercises via `activate_skill_route`) now also sets an explicit bot identity (env vars) for its own `commit` step and a `timeout=` on every call, so it never depends on ambient git config either.

### Browser / journey tests (CI-gated)

GitHub Actions runs a dedicated **`browser-critical`** job (Playwright + Chromium) on every PR. It executes the lean Alpine/htmx journeys in `tests/test_browser_critical.py` only:

- Scan / Onboard Results surfaces opened GitOps PRs (or Retry Scan delivery when `needs_attention`) — no Commit / Per-Agent CTAs
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
| Fault injection / resilience | ~30 | `kube_breaker`/`llm_breaker` real-failure-shape injection (`test_kube_breaker.py`, `test_llm_breaker.py`), a genuinely wedged Postgres via `pg_sleep()` (`test_store_resilience.py`), lock-usage proofs (`test_ttl_cache_locking.py`), concurrent-write races (`test_store.py`'s gate/remediation-job concurrency tests) — see [`docs/resilience-audit-2026-07-18.md`](docs/resilience-audit-2026-07-18.md) for the pattern and findings |
| Check engine | ~15 | Data-driven check loading, each check type, integration |
| Skill validation | ~15 | All 40 skills load, valid frontmatter, generate valid YAML |
| Self-observability | ~50 | `agent_runs`/`check_results` persistence, DLQ republish, correlation-id tracing, circuit-breaker/DB-size/event-buffer/watcher-staleness metrics, watcher tick telemetry (`tests/test_watchers_telemetry.py`, `tests/test_durability.py`, extensions to `tests/test_store_extended.py`) |

Additional test markers: `--run-real-repos` (clone live GitHub repos), `--live-cluster` (e2e against OpenShift), `--browser-tests` (Playwright critical journeys), `--run-llm-evals` (requires API key).

## Resilience

**Proactive resilience audit — 4 fragility fixes shipped, testing pattern established (2026-07-18).** Distinct from the same day's reactive incident fixes (webhook dedup races, `create_gate`/`save_remediation` races, unlocked TTL caches, `kube_breaker` wiring — each found from one observed symptom), this pass systematically re-checked the same six fragility categories (concurrency, external-dependency resilience, single points of failure, resource exhaustion, cascading failure, data-integrity-under-partial-failure) for further, not-yet-found instances of those same bug classes. Found and fixed: (1) `CircuitBreaker` itself (`llm_breaker`/`kube_breaker`'s shared class) had zero locking around its own failure-count bookkeeping, despite existing specifically to protect against concurrent-failure surges; (2) `AssessmentStore.create()`'s Postgres pool had no `command_timeout`, so a wedged (not fully down) DB could hang every request indefinitely and exhaust the connection pool; (3) `update_remediation_job()`'s `steps_completed` append was a lost-update race, the same shape as the already-fixed `create_gate`/`save_remediation` races; (4) three skills-cache-bust call sites bypassed the existing `_skills_cache_lock` entirely. Full findings — including a prioritized, evidence-based backlog of what was found but deliberately not fixed and why, the fault-injection testing pattern extended from the existing `test_kube_breaker.py`, and a periodic-verification proposal coordinated with the concurrent self-health-check watcher effort — are in [`docs/resilience-audit-2026-07-18.md`](docs/resilience-audit-2026-07-18.md).

**How we know we're getting more resilient over time, concretely:** a
named, growing fault-injection test suite (the "Fault injection /
resilience" row above), re-run on every commit, that fails against
pre-fix code and passes against the fix — not a vague claim — plus the
audit doc's own categorized backlog naming exactly what's still open and
why, so the next pass has a concrete starting point instead of
re-discovering the same gaps.

**Fault-injection pattern, for reuse on the next external dependency:**
mock the client at its lowest-level accessor (`test_kube_breaker.py`/
`test_llm_breaker.py`), inject a real-shaped exception, and assert the
three invariants every breaker-wrapped function should have — N failures
open the breaker at its documented threshold, an open breaker skips the
real call (`assert_not_called()`) and returns each function's own
documented "unavailable" contract, and a success resets the count. For
locking correctness, prefer a deterministic proof over a probabilistic
race: swap in a tracking lock and count concurrent holders
(`test_ttl_cache_locking.py`) if the critical section has real work in
it, or hold the real lock open on a background thread and time how long
the call under test blocks if it doesn't. For Postgres/network
timeouts, prefer a real dependency with an injected fault
(`pg_sleep()`, `test_store_resilience.py`) over a mock, when the fault
is expressible at that level.

**Circuit breakers are thread-safe (`llm_breaker`/`kube_breaker`,
`portal/helpers.py::CircuitBreaker`).** `record_failure()`/
`record_success()`/`is_open` are guarded by a `threading.Lock` — both
breakers are one shared instance called from many concurrent OS threads
(every `LLMClient._chat()` call and most of `kube.py`'s real
API-calling functions run inside `asyncio.to_thread`), so an unlocked
critical section could lose a failure-count update exactly when a
dependency is genuinely struggling.

**Postgres queries/connections are time-bounded (`portal/store.py::
AssessmentStore.create()`).** `command_timeout` (default 30s) and
`connect_timeout` (default 15s) are passed to `asyncpg.create_pool()` —
without this, a wedged (not fully down) Postgres could hang any query
indefinitely and, since every route holds its connection for that whole
wait, exhaust the connection pool and cascade into total portal
unavailability with zero user-facing signal.

## Security notes

- **Browser authentication is opt-in (`auth.enabled`, default `false`).** An OpenShift `oauth-proxy` sidecar can front the portal's Route with the cluster's built-in OAuth login — see [`docs/deployment.md#authentication`](docs/deployment.md#authentication). Off by default so this doesn't change behavior for any existing deployment; flip it on deliberately. Login needs no custom UI — the proxy redirects unauthenticated requests to the cluster OAuth login automatically, before they ever reach the app. The nav bar (`base.html`) shows a "Logged in as {{ current_user }}" + Logout link (pointed at oauth-proxy's `/oauth/sign_out`) only when a real `X-Forwarded-User` header is present on the request — never a fake "logged in" state when `auth.enabled=false`.
- **CSRF protection is always on.** Every browser-originated `POST`/`PUT`/`PATCH`/`DELETE` route requires a matching double-submit-cookie token (`src/agentit/portal/csrf.py`) — auto-attached by htmx for every form, no per-template changes needed.
- **`/api/webhook/*` requires a shared-secret token.** These routes are called only by in-cluster Argo Events Sensors, never a browser, so neither of the above protects them — `verify_internal_token` (`src/agentit/portal/routes/webhooks.py`) checks an `X-Internal-Webhook-Token` header against an auto-generated Secret instead. GitHub's push webhook keeps its own pre-existing HMAC-SHA256 signature check against `GITHUB_WEBHOOK_SECRET`.
- **None of the above is a substitute for network boundaries.** Run the portal behind a trusted network until `auth.enabled` is turned on; even then, `--openshift-sar` only requires "any authenticated user with a role binding in this namespace" unless tightened further.
- **Secrets never belong in Git.** See [Configuration](#configuration) and `docs/deployment.md`.
- **`gitpython` bumped past 4 newly-disclosed HIGH CVEs (2026-07-21).** Trivy started failing the `Security Scan` CI job on `main` itself (not from any code change in this repo — a new CVE published against the already-pinned `gitpython==3.1.50` in `uv.lock`, blocking every subsequent PR's scan check): `GHSA-2f96-g7mh-g2hx`/`GHSA-956x-8gvw-wg5v`/`GHSA-v396-v7q4-x2qj` (command-injection via unguarded `git` options/long-option-prefix abbreviation) and `GHSA-rwj8-pgh3-r573` (environment-variable exfiltration via `os.path.expandvars()` on a `Repo.clone_from()` URL) — all fixed by 3.1.51/3.1.52. `pyproject.toml`'s `gitpython` constraint raised to `>=3.1.52`; `uv lock` resolved `3.1.53`. Verified directly (not just by trusting the version bump): ran the same Trivy scanner locally against the regenerated `uv.lock` — 0 vulnerabilities, down from 4 HIGH. Also fixed two unrelated, low-severity stale-comment leftovers a batch review surfaced while investigating this: `chart/templates/workflows/cost-report-cronjob.yaml`'s comment still claimed `CostOptimizationAgent`/`agents/cost.py` was "a live, surviving Python agent" after it was fully removed (skills-primary simplification, 2026-07-21); `_onboard_progress_fragment.html`'s `needs_attention` copy said "deliver manually on Onboard Results," stale since the Scan-only pivot removed manual Commit/Per-Agent delivery — both corrected to describe what's actually there today (the real "Retry Scan delivery" button). Tested: full non-browser suite, 3005 passed, 375 skipped, 0 failed.
- **Delivery always requires an explicit human action.** AutoMode (which used to auto-apply an orchestrator-approved, LLM-classified-non-destructive change without a human click) has been removed entirely (2026-07-18) — every real delivery path (Deliver from Onboard Results, approving a `gitops-pr-pending` gate, `route_and_deliver()`) now stops at a GitOps commit + PR and waits for a human to merge it. AgentIT never auto-merges.
- **Manifests are validated before being trusted.** `agents/base.py::validate_manifest()` checks every generated YAML, and `cluster_apply.py` runs a `--dry-run=client` pass before any real apply.
- **SSRF prevention.** `cloner.py` rejects private IPs, localhost, and internal DNS suffixes. `portal/helpers.py::safe_url()` rejects protocol-relative URLs.
- **Circuit breakers.** LLM and Kubernetes API clients use circuit breakers (`CircuitBreaker` in `portal/helpers.py`, `llm_breaker`/`kube_breaker`) to prevent cascading failures. `llm.py`'s `LLMClient._chat()` checks/records against `llm_breaker` around every real Anthropic call; `kube.py`'s real API-calling functions (`list_pods`, `list_custom_resources`, `apply_yaml`, `namespace_exists`, ...) do the same against `kube_breaker` via a shared `_kube_breaker_scope()` choke point — each checks `is_open` before attempting a call (skipping with a safe fallback matching its own existing failure contract if open) and records success/failure around the real call itself. `AGENTIT_OFFLINE`'s hard-stop (a dedicated `KubeOfflineError`) is explicitly exempted from ever counting as a `kube_breaker` failure, and expected non-failure statuses (404 "not found", 409 "already exists"/conflict) don't count either — both are the Health page's "kube" row now being a real, tripsable signal instead of permanently green. `CircuitBreaker`'s own failure-count bookkeeping is `threading.Lock`-guarded (2026-07-18 resilience audit) — both breakers are one shared instance called from many concurrent OS threads (every `_chat()` call and most of `kube.py`'s real calls run inside `asyncio.to_thread`), so this was a real, if ironic, gap in the mechanism meant to protect against concurrent-failure surges.
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
│       │   ├── fleet.py            # Fleet dashboard, fleet-wide SLOs
│       │   ├── assessments.py      # Assess/onboard/deliver lifecycle, GitOps registration, per-agent PR creation, verification
│       │   ├── gates.py            # Gate resolve/cancel actions -- every gate type renders inline
│       │                           #   on Fleet/Assessment Detail via assessments.py/fleet.py
│       │                           #   (the cross-app Admin Review page/cluster-admin-review gate
│       │                           #   type it existed solely for were retired 2026-07-18)
│       │   ├── capabilities.py     # Skills/checks catalog, learning agent, agents/watchers
│       │   ├── settings.py         # Retention/purge, LLM availability status, settings API
│       │   ├── insights.py         # Fleet insights, LLM decision audit, events feed + DLQ,
│       │                           #   the Ledger PR list (pr_tracking.py-backed)
│       │   ├── slos.py             # Per-assessment SLO definitions and error budgets
│       │   ├── webhooks.py         # /api/webhook/* (internal-token-gated) + GitHub push
│       │   ├── health.py           # /health, /healthz, /readyz, platform drift
│       │   └── schedules.py        # Watcher/cron schedule management
│       ├── pr_tracking.py          # Every real PR AgentIT has opened, per-app and fleet-wide,
│       │                           #   from gates.pr_url/deliveries.details_json outcomes/
│       │                           #   onboarding_results.pr_url -- backs Fleet's Open PRs/Total
│       │                           #   PRs columns, Assessment Detail's PR History tab, and Ledger
│       ├── store/                  # AssessmentStore -- the only supported store, fully async
│       │   │                       #   (asyncpg/Postgres, 19 tables: assessments, apps (app-level
│       │   │                       #   facts like infra_repo_url that persist across
│       │   │                       #   re-assessments -- see docs/architecture.md's "Data model"
│       │   │                       #   section), events, SLOs, skill_effectiveness,
│       │   │                       #   agent_feedback, deliveries, processed_webhooks. Split
│       │   │                       #   2026-07-20 (see changelog) from one 2685-line store.py
│       │   │                       #   into this package: __init__.py is a slim facade class
│       │   │                       #   inheriting 109 methods from 12 domain mixins below --
│       │   │                       #   the public `from agentit.portal.store import
│       │   │                       #   AssessmentStore` import path is unchanged.
│       │   ├── __init__.py         # AssessmentStore facade: __init__/create()/close() + re-exports
│       │   ├── _shared.py          # SCHEMA_SQL, table-name lists, row-shape/timestamp helpers
│       │   ├── assessments.py      # assessments/apps/onboarding_results/apply_results + history
│       │   ├── fleet.py            # Fleet-wide read aggregates (get_fleet_data, get_fleet_insights)
│       │   ├── events.py           # events table + dead-letter queue
│       │   ├── deliveries.py       # deliveries table + webhook-dedup/delivery-lock concurrency
│       │   ├── jobs.py             # remediation_jobs (assess-job + onboard-job tracking)
│       │   ├── schedules.py        # scheduled_operations
│       │   ├── agents.py           # agent_registry + agent_runs
│       │   ├── slos.py             # slos
│       │   ├── feedback.py         # agent_feedback + pr_outcomes
│       │   ├── checks.py           # check_results + suppressed_checks
│       │   ├── skills.py           # skill_effectiveness + skill_inventory_snapshots
│       │   └── admin.py            # settings + export_all/purge_old_data/get_db_stats
│       ├── migrate_sqlite.py       # One-time `agentit migrate-sqlite-to-postgres` import for anyone
│       │                           #   with real data in a legacy local SQLite file
│       ├── helpers.py              # CircuitBreaker, clone_assess_cleanup, safe_url, async get_store()
│       ├── cluster_apply.py        # oc/kubectl apply with pre-flight checks
│       ├── delivery.py             # Unified apply flow: classify + route_and_deliver()
│       ├── github_pr.py            # GitHub REST API integration
│       └── templates/              # 31 Jinja2 templates (htmx + Alpine.js)
├── skills/                         # 67 property-based skill definitions (14 domains, incl. chaos + a
│                                   #   dynamically-created custom/ the learning agent writes drafts into;
│                                   #   20 of the 67 are mode: detect -- see checks/ below)
├── checks/                         # Empty as of Unification Phase 4 (2026-07-20) -- every check that
│                                   #   used to live here is now a mode: detect skill in skills/**/*.md.
│                                   #   check_engine.py's rule-running half stays; only the YAML files
│                                   #   (and, pending one release of confidence, its now-dead YAML-
│                                   #   loading functions) are gone.
├── agents/                         # 3 mode: agent registration files (name/category/code_ref/
│                                   #   resource_tier/description) -- backs agents/capabilities.py's
│                                   #   AGENT_CLASSES. Not src/agentit/agents/, the Python package.
├── watchers/                       # 7 watcher registration files (name/mode/interval/description/
│                                   #   code_ref) -- backs agents/capabilities.py's WATCHER_AGENTS.
│                                   #   Not src/agentit/watchers/, the Python package.
├── chart/                          # Helm chart (30+ templates: Rollout, Services, Route, RBAC,
│                                   #   NetworkPolicy, ResourceQuota, LimitRange, PDB, Tekton,
│                                   #   Kafka, Argo Events, watcher agents, backup CronJob,
│                                   #   bundled non-operator Postgres)
├── argocd/application.yaml         # Argo CD Application for self-deployment
├── docs/                           # 14 files -- living docs, implemented-design docs, and dated
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
│   ├── session-status-2026-07-13.md  # Dated session handoff snapshot (historical)
│   └── cicd-stall-hardening-2026-07-17.md  # Tekton cleanup-CronJob bug, notify-argocd
│                                   #   scheduling fix, GitOps lag alerting -- all implemented
│                                   #   + made fully self-contained (own namespace-scoped
│                                   #   TaskRun/ImageStream-tag cleanup, no cluster-pruner dep)
├── scripts/check_portal_edl.py     # Static EDL MUST/SHOULD walker for portal templates
├── Containerfile                   # UBI9 Python 3.12, HEALTHCHECK, non-root
└── tests/                          # 2,000+ tests across 100 files -- see "Testing" above
```

</details>

## License

[MIT](LICENSE)
