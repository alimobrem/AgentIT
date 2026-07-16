# Dogfood self-improve milestone retrospective

**Date:** 2026-07-16  
**Plan:** [2026-07-15-autonomous-self-improve-dogfood.md](./superpowers/plans/2026-07-15-autonomous-self-improve-dogfood.md)

## Explicit claim

- **L4 on AgentIT:** merge/close outcomes feed the next scout cycle; `#23` is recorded as `capability-outcome` `merged` and appears in `cited_merges` on a later `capability-run`.
- **L5 sample on app pinky:** a merged GitOps improvement landed on non-AgentIT app **pinky** via `agentit-gitops` (`apps/pinky`), the same delivery surface AgentIT uses for fleet apps (`managed-pinky` Argo Application).

## Explicit non-claim

- AutoMode / unattended merge to `main` is still **off**.
- L5 did **not** require a full Assess → Generate → Deliver click-path replay in this pass; the proof is the shared GitOps PR discipline on a second app with a real cluster failure removed.

## Evidence

| Item | Link / note |
|------|-------------|
| L3 merges | [#20](https://github.com/alimobrem/AgentIT/pull/20) stack-signature; [#23](https://github.com/alimobrem/AgentIT/pull/23) tick_failure_classifier |
| Duplicate closed (unblocks maxOpenPRs=1) | [#24](https://github.com/alimobrem/AgentIT/pull/24) closed as duplicate of #23 |
| L4 sync + portal | [#30](https://github.com/alimobrem/AgentIT/pull/30) — `gh pr list` discovery for outcomes; Self-Improvement UI shows cited merges + outcome badges; scout interval restored to `86400` |
| L4 store proof | `capability-outcome` for `#23` `merged`; `capability-run` summary `L4 verify: evidence gathered with cited_merges including #23` |
| L5 GitOps PR | [agentit-gitops#7](https://github.com/alimobrem/agentit-gitops/pull/7) — remove PLACEHOLDER `security-context` Pod causing `InvalidImageName` / ImagePullBackOff |

## Metrics moved

- Scout cadence: dogfood hourly (`3600`) → daily (`86400`) after L3/L4 proof.
- Pinky: broken `pinky-security-context-patch` Pod manifest removed from GitOps source of truth (Argo prune expected after sync).

## Failures hit

- Outcome sync originally missed human/Cursor merges that never logged a `capability-run` `pr_url` (e.g. `#23`) — fixed by combining store tracking with `gh pr list` prefix discovery.
- Cluster CPU quota (`limits.cpu=12`) blocked scout rolling updates (maxSurge); temporary Recreate + cleanup of completed PipelineRun pods unblocked deploy.
- Live Argo Application Helm params can lag `argocd/application.yaml` in git until the Application CR is patched / re-applied.

## What remains manual

- Human merge of AgentIT self-improve PRs (by design through L4+).
- Occasional Application CR param patch when git `application.yaml` is not the live App-of-Apps source.
- Full L5 replay via portal Assess → Onboard → Deliver on pinky (optional hardening of this sample).
- Rotate / refresh `oc` token when it expires (operator action; not automated here).
