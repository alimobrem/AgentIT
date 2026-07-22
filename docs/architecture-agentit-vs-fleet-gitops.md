# Architecture: AgentIT vs fleet GitOps delivery

**Status:** normative decision (2026-07-20)  
**Audience:** founders, contributors changing delivery / Argo / onboard routing  
**Related:** [deployment.md](./deployment.md), [adr/0001-gitops-scan-hitl.md](./adr/0001-gitops-scan-hitl.md), [history/unified-apply-flow.md](./history/unified-apply-flow.md), [history/self-improvement-for-agentit.md](./history/self-improvement-for-agentit.md)

---

## Recommended pattern (one winner)

**AgentIT is self-managed via Application `agentit` → Helm `chart/` in AgentIT.git. Fleet apps are managed via ApplicationSet `agentit-managed-apps` → `apps/{app}/` in agentit-gitops. Do not deliver AgentIT onboard/harden output into `apps/agentit/`.**

| Concern | Fleet app (e.g. pinky) | AgentIT itself |
| ------- | ---------------------- | -------------- |
| Desired state repo | `agentit-gitops` `apps/{app}/` | **AgentIT.git** (`chart/`, `skills/`, `src/`) — **never** overwrite `argocd/application.yaml` from onboard |
| Argo object | `managed-{app}` from AppSet | Hand-crafted Application **`agentit`** |
| Image promotion | App’s own CI / image digest in gitops (as designed per app) | Tekton `notify-argocd` pins live `image.tag` on Application `agentit` |
| Human gate | Merge PR on agentit-gitops | Merge PR on AgentIT.git (never auto-merge) |
| Deployer | Argo only | Argo only |
| Solution contracts (`delivery: cluster`) | gitops `apps/{app}/…` | app repo `chart/` (source PR) |
| Solution contracts (`delivery: source`) | app repo patch | app repo patch |

**Why this is correct**

1. **Something already syncs AgentIT.** `argocd/application.yaml` sources `path: chart` from AgentIT.git with automated sync/prune/selfHeal. That is the sole deployer for the `agentit` namespace.
2. **AppSet exclusion is intentional, not a bug.** `ensure_applicationset()` excludes `apps/agentit` so AppSet selfHeal cannot fight CI’s live `image.tag` patch ([`github_pr.py`](../src/agentit/portal/github_pr.py) generators; [deployment.md](./deployment.md) “A new Helm parameter…”).
3. **Skills are image-baked from AgentIT.git.** Activation already opens PRs under `skills/` in this repo; the next build+sync loads them. Writing `apps/agentit/skills/*` in gitops cannot update the running catalog ([`tests/test_portal.py`](../tests/test_portal.py) activation durability).
4. **Self-improve already targets the right repo.** `capability-scout` / skill activate use AgentIT.git + HITL merge — same product contract as fleet, different git remote.
5. **Fleet registration ≠ fleet deploy.** Tekton `register-self-in-fleet` posts Assess so AgentIT appears on Fleet/scoreboard watchers; it does **not** mean ApplicationSet should own the `agentit` namespace.

**Incident that proved the anti-pattern:** onboard opened [agentit-gitops#12](https://github.com/alimobrem/agentit-gitops/pull/12) under `apps/agentit/skills/*`; human merged; nothing rolled out — no Application watches that path, and gitops CI is empty.

---

## Rejected alternatives

| Alternative | Why not |
| ----------- | ------- |
| **Second Argo source / companion Application** applying `apps/agentit` from gitops (multi-source or sibling app) | Dual writers into the same namespace with prune/selfHeal → ownership fights (the failure mode `unified-apply-flow.md` closed). Skills still need the image path, not a parallel YAML tree. |
| **Include AgentIT in AppSet like pinky** (`managed-agentit`) | Fights Application `agentit` + CI `image.tag` pin; AppSet template assumes directory-of-manifests, not Helm+notify-argocd. Exclusion exists precisely to avoid this. |
| **Keep writing AgentIT into gitops and “fix sync later”** | Dead letters: merge looks like success; cluster unchanged. Violates PR-only HITL honesty (approval that cannot deploy). |
| **Direct Apply for AgentIT only** | Product forbids Direct Apply; Argo is sole deployer for every app including self. |

---

## Correct end-to-end flows

### (a) Fleet app (e.g. pinky)

```text
Scan (Assess→Onboard) → quality filter + SSA dry-run → route_and_deliver()
  → MECHANISM_INFRA_REPO_COMMIT → PR on agentit-gitops under apps/{app}/…
  → human merge
  → ApplicationSet agentit-managed-apps (directory.recurse=true, *.yaml/*.yml)
    discovers/updates managed-{app}
  → Argo sync (prune/selfHeal) → app namespace
```

Registration: `ensure_applicationset(infra_repo_url)` once; first merge bootstraps `apps/{app}/` so `managed-{app}` appears. Without `recurse`, nested skill/category YAML never syncs (dogfood: Synced/Healthy, 0 resources).

### (b) AgentIT itself (onboard / harden / skills / self-improve)

```text
Scan or Activate / capability-scout → PR on AgentIT.git
  (skills/* | src/* | chart/templates/* — as appropriate)
  → human merge
  → Tekton CI (build → smoke → notify-argocd pins image.tag)
  → Application agentit syncs Helm chart/ → agentit namespace
```

**Do not** open cluster-config / skills PRs under `apps/agentit/` in agentit-gitops for this path.  
**Do not** let onboard rewrite `argocd/application.yaml` (live Application CR + image.tag pin) — drop generated `kind: Application` manifests from self-managed cicd remap (PR #109 regression).

### Fail-closed chart delivery gate (P0 after PR #116)

Destination routing (#114) is necessary but not sufficient. Before any self-managed PR writes under `chart/`, `validate_self_managed_chart_delivery()` **refuses** (no PR; auto_delivery → `needs_attention`) when:

1. Content is **not Helm-shaped** (must contain `{{ .Values`, `{{ .Release`, or `{{-`) — raw skill dumps are refused.
2. `target_path` **already exists** on the default branch (collision / overwrite).
3. Manifest includes a **forbidden kind**: `PipelineRun`, `ClusterRole`, `ClusterRoleBinding`, `ClusterTask`, `Application`.

`skills/` markdown is not gated by this check.

### P1 generation quality (produce good PRs, not only refuse bad ones)

The #119 gate alone left Auto-Scan generating fleet-style YAML that was always refused. Self-managed now **filters and constrains generation** so high-quality PRs can still open:

1. **`filter_self_managed_delivery_files()`** (after remap, before #119 gate): drop raw fleet YAML / forbidden kinds / hardcoded `namespace: agentit` with explicit reasons; **keep** `skills/**` markdown and Helm-shaped chart patches. Partial batches still open a PR for the good files; notes go on the delivery outcome (`filtered_reasons`).
2. **SkillEngine `self_managed=True`** (orchestrator when app name is `agentit`): LLM prompt requires Helm (`{{ .Release.Namespace }}` / `.Values`), skips skills whose outputs are fleet-only forbidden kinds, and skips non-Helm template fallback (empty > junk).
3. **Honest skill outcomes:** self-managed deliveries do **not** record skill outcome `approved` on PR open — opening is not acceptance.
4. **`skills/**/*.md`** classifies as `source_patch` (AgentIT.git skill catalog PR), not `.agentit/` at-rest.

Findings that cannot become safe Helm remain Ledger / `needs_attention` with why — never a fake chart PR. The #119 gate stays as belt-and-suspenders (collisions, direct `create_source_patch_pr` callers).

| Artifact class | Destination in AgentIT.git | How it reaches cluster |
| -------------- | -------------------------- | ---------------------- |
| Skill markdown | `skills/**` | Merge → image rebuild → Argo rolls pods |
| Platform code | `src/agentit/**`, `tests/**` | Same |
| Runtime K8s shape | `chart/templates/**`, `chart/values.yaml` | Merge → Argo Helm sync (image tag via notify-argocd) |
| Live Helm params / feature flags | `argocd/application.yaml` | **Human / CI only** — not onboard auto-PR |
| Fleet scoreboard only | N/A (webhook Assess) | `register-self-in-fleet` — no gitops trees |

---

## What to change (product / code / docs)

High level — implementation can follow in a focused PR.

1. **Delivery router:** when the assessment is AgentIT (self-managed / source repo matches Application `agentit`), do **not** use `MECHANISM_INFRA_REPO_COMMIT` for cluster-config **or** CI/CD shared-namespace. Route both to AgentIT.git under `chart/templates/` / `skills/`. Drop onboard `kind: Application` files. Never land under `apps/agentit/`.
2. **Chart content gate (done):** refuse non-Helm / colliding / forbidden-kind payloads before `create_source_patch_pr` — see “Fail-closed chart delivery gate” above.
3. **P1 generation quality (done):** filter fleet junk before the gate; self-managed SkillEngine constraints; no approve-on-PR-open — see “P1 generation quality” above.
4. **UX copy:** Onboard Results / confirmation text for AgentIT must say “PR to AgentIT.git → CI → Application `agentit`”, never “commit to agentit-gitops” / never fail-close cicd with an apps/agentit error.
5. **Hygiene:** archive or delete dead `apps/agentit/` content in agentit-gitops (or leave a README that the path is excluded and unused).
6. **Keep:** AppSet exclude of `apps/agentit`; Application `agentit` + notify-argocd image pin; `is_self_managed_application()` for Fleet badge; scout/activate PR-to-AgentIT.git; never auto-merge.
7. **Docs:** this file is normative; [deployment.md](./deployment.md) already states the exclude reason — link here from README / lessons when touching delivery.

---

## Code citations (current truth)

| Fact | Where |
| ---- | ----- |
| AppSet watches `apps/*`, excludes `apps/agentit`; `directory.recurse=true` + yaml include | `ensure_applicationset()` in [`src/agentit/portal/github_pr.py`](../src/agentit/portal/github_pr.py) |
| Application `agentit` → AgentIT.git `chart` + `image.tag` bootstrap note | [`argocd/application.yaml`](../argocd/application.yaml) |
| Self-managed = literal Application sourcing app’s own repo (not `managed-{app}`) | `is_self_managed_application()` / `is_gitops_registered()` in [`src/agentit/portal/delivery.py`](../src/agentit/portal/delivery.py) (~328–384) |
| Fleet deliver = `apps/{app}/{category}/…` via `commit_to_infra_repo` | [`src/agentit/portal/github_pr.py`](../src/agentit/portal/github_pr.py) (~854–892); `route_and_deliver()` in [`delivery.py`](../src/agentit/portal/delivery.py) |
| Self-managed chart fail-closed gate (Helm / collision / forbidden kinds) | `validate_self_managed_chart_delivery()` in [`delivery.py`](../src/agentit/portal/delivery.py); belt-and-suspenders in `create_source_patch_pr` |
| Self-managed P1 filter + generation constraints | `filter_self_managed_delivery_files()` / `_deliver_self_managed_source_pr()` in [`delivery.py`](../src/agentit/portal/delivery.py); `SkillEngine(self_managed=…)` in [`skill_engine.py`](../src/agentit/skill_engine.py) |
| Exclude reason (image.tag fight) | [`docs/deployment.md`](./deployment.md) (~140–147) |
| Deploy topology diagram | [`docs/architecture.md`](./architecture.md) “Deployment topology” |
| Skills baked into image; activate → AgentIT.git PR | [`tests/test_portal.py`](../tests/test_portal.py) (~3174–3217); skill-learner / activate routes |
| Fleet self-register webhook (not gitops deploy) | `register-self-in-fleet` in [`chart/templates/tekton/pipeline.yaml`](../chart/templates/tekton/pipeline.yaml) (~366–399) |
| PR-only + Argo sole deployer (product) | [ADR 0001](./adr/0001-gitops-scan-hitl.md), [release-notes.md](./release-notes.md) |

---

## One-line rule for reviewers

If the PR path starts with `apps/agentit/` in agentit-gitops, it is almost certainly the wrong destination — route to AgentIT.git and Application `agentit` instead.
