# Dogfood self-improve milestone retrospective

**Date:** 2026-07-16  
**Plan:** [2026-07-15-autonomous-self-improve-dogfood.md](./superpowers/plans/2026-07-15-autonomous-self-improve-dogfood.md)

## Explicit claim

- **L4 on AgentIT:** merge/close outcomes feed the next scout cycle; `#23` is recorded as `capability-outcome` `merged` and appears in `cited_merges` on a later `capability-run`.
- **L5 full on app pinky:** the portal path Assess → Generate/Onboard → Gate → Approve & Deliver landed a **merged**, placeholder-free GitOps improvement on non-AgentIT app **pinky** via `agentit-gitops` (`apps/pinky`); `managed-pinky` stayed Synced/Healthy on the merge revision.

## Explicit non-claim

- AutoMode / unattended merge to `main` is still **off**.
- Pinky app workload health (Rollout selector / ImagePullBackOff on `pinky` Deployment) is **out of scope** for the AgentIT click-path proof; `managed-pinky` Argo app is Synced/Healthy while some pinky pods remain degraded for unrelated image/label reasons.
- Argo CD directory apps still do **not** recurse into `apps/<app>/<category>/` by default — nested YAML is the GitOps source of truth after merge; enabling `directory.recurse` without excluding non-manifest files (e.g. Grafana JSON) and uncurated CronWorkflows degrades the app. Nested apply remains a follow-up.

## Evidence

| Item | Link / note |
|------|-------------|
| L3 merges | [#20](https://github.com/alimobrem/AgentIT/pull/20) stack-signature; [#23](https://github.com/alimobrem/AgentIT/pull/23) tick_failure_classifier |
| Duplicate closed (unblocks maxOpenPRs=1) | [#24](https://github.com/alimobrem/AgentIT/pull/24) closed as duplicate of #23 |
| L4 sync + portal | [#30](https://github.com/alimobrem/AgentIT/pull/30) — `gh pr list` discovery for outcomes; Self-Improvement UI shows cited merges + outcome badges; scout interval restored to `86400` |
| L4 store proof | `capability-outcome` for `#23` `merged`; `capability-run` summary `L4 verify: evidence gathered with cited_merges including #23` |
| L5 GitOps PR (sample) | [agentit-gitops#7](https://github.com/alimobrem/agentit-gitops/pull/7) — remove PLACEHOLDER `security-context` Pod causing `InvalidImageName` / ImagePullBackOff |
| L5 portal path (blocked) | Assess `ae0e85b707d3460c8220d949c6bc26cc` → onboard → Deliver opened [agentit-gitops#8](https://github.com/alimobrem/agentit-gitops/pull/8) (closed: unresolved `REPLACE_WITH_AGENTIT_IMAGE`). Fix: [AgentIT#35](https://github.com/alimobrem/AgentIT/pull/35) strips placeholders + opens `gitops-pr-pending`. |
| L5 portal path (full) | Assess `f2d0bae7954f4ed299b6940a6f793389` (high crit) → onboard 11 files → Deliver → `gitops-pr-pending` → Approve & Deliver merged [agentit-gitops#10](https://github.com/alimobrem/agentit-gitops/pull/10) (namespace `default`→`pinky` + `agentit.io/l5-proof` on cost ConfigMap/VPA; **no** `REPLACE_WITH_AGENTIT_IMAGE`). Prior empty [agentit-gitops#9](https://github.com/alimobrem/agentit-gitops/pull/9) also merged via the same gate (identical tree). `managed-pinky` Synced/Healthy at `bcb7c23`. |
| Learner → Activate | Live `skill-activated` events for learner CVE drafts (verify_skill); Capabilities Activate path exercised on cluster |
| skill-improvement mode | [#42](https://github.com/alimobrem/AgentIT/pull/42) unblocked platform `has_api` + discovery auth; live `learning-run` `mode=skill-improvement` drafted `resourcequota-scoped` from flagged `resourcequota` (5 rejects); Activate + `loop_health` 100% |

## Metrics moved

- Scout cadence: dogfood hourly (`3600`) → daily (`86400`) after L3/L4 proof.
- Pinky: broken `pinky-security-context-patch` Pod manifest removed from GitOps source of truth (Argo prune expected after sync).
- Portal Deliver: skip `REPLACE_WITH_AGENTIT_IMAGE` files; create `gitops-pr-pending` after infra-repo PR so Gate → merge matches AutoMode.

## Failures hit

- Outcome sync originally missed human/Cursor merges that never logged a `capability-run` `pr_url` (e.g. `#23`) — fixed by combining store tracking with `gh pr list` prefix discovery.
- Cluster CPU quota (`limits.cpu=12`) blocked scout rolling updates (maxSurge); temporary Recreate + cleanup of completed PipelineRun pods unblocked deploy.
- Live Argo Application Helm params can lag `argocd/application.yaml` in git until the Application CR is patched / re-applied.
- Real Deliver for pinky shipped CronJobs with `REPLACE_WITH_AGENTIT_IMAGE` into [agentit-gitops#8](https://github.com/alimobrem/agentit-gitops/pull/8) — closed without merge; filter added in AgentIT ([#35](https://github.com/alimobrem/AgentIT/pull/35)).
- First clean Deliver after #35 ([agentit-gitops#9](https://github.com/alimobrem/agentit-gitops/pull/9)) opened a PR whose tree matched `main` (0-file merge) — re-Deliver after editing cost manifests produced non-empty [#10](https://github.com/alimobrem/agentit-gitops/pull/10).

## What remains manual

- Human merge of AgentIT self-improve PRs (by design through L4+).
- Occasional Application CR param patch when git `application.yaml` is not the live App-of-Apps source.
- Curate nested GitOps manifests / enable Argo `directory.recurse` with YAML-only include so merged `apps/<app>/<category>/*.yaml` apply into the destination namespace (not required for the L5 portal merge proof).
- Rotate / refresh `oc` token when it expires (operator action; not automated here).
