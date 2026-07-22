# CI merge gate + post-merge deploy tip

AgentIT brand. **Do not squash-merge while required checks are queued or red.**

## Merge gate (before merge)

Required GitHub Actions on the PR (must all be **completed + success**):

| Check | Workflow job |
| --- | --- |
| `test` | Tests |
| `browser-critical` | Tests |
| `image-smoke-test` | Tests |
| `scan` | Security Scan |

```bash
# From a PR number:
gh pr checks <N> --repo alimobrem/AgentIT

# Refuse merge while anything is pending/queued/failing:
./scripts/ci-merge-gate.sh <N>
```

`gh pr merge` only after the script exits 0 (or after you have verified the four checks yourself). Pending/queued is a hard stop — not “probably fine.”

## After merge (main tip)

1. **GHA on the merge commit** — same four checks green on `main`:

   ```bash
   SHA=$(gh api repos/alimobrem/AgentIT/commits/main --jq .sha)
   gh api "repos/alimobrem/AgentIT/commits/${SHA}/check-runs" \
     --jq '.check_runs[] | {name, status, conclusion}'
   ```

2. **Tekton image build + promote** — merge alone does not move the portal.
   Context `agentit-ci/tekton` on the tip SHA must become **success** after
   PipelineRun `agentit-ci` finishes (`run-tests` → `build-image` →
   `smoke-test-image` → `notify-argocd` pins `image.tag`).

   ```bash
   # Commit status (no cluster creds needed beyond GitHub):
   gh api "repos/alimobrem/AgentIT/commits/${SHA}/status" \
     --jq '.statuses[] | select(.context=="agentit-ci/tekton") | {state, description}'

   # On-cluster (when you have oc + kubeconfig for the dogfood cluster):
   oc get pipelinerun -n agentit -l tekton.dev/pipeline=agentit-ci \
     --sort-by=.metadata.creationTimestamp | tail -5
   # Inspect the tip run: Succeeded? Then notify-argocd should have pinned image.tag.
   ```

3. **Rollout / Health** — optional live confirmation (needs cluster access):

   ```bash
   # Argo Application image.tag vs tip SHA
   oc get application agentit -n openshift-gitops \
     -o jsonpath='{.spec.source.helm.parameters}' ; echo

   # Portal Health deploy-status (browser or API behind oauth-proxy)
   # /health — Platform / deploy cards should show tip SHA once promoted.
   ```

No cluster credentials are stored in-repo. Use your local `oc` login / VPN as usual.

## Failure modes

| Symptom | Likely cause |
| --- | --- |
| Merged with pending checks | Human skipped the gate — tip may be red; revert or fix-forward |
| GHA green, `agentit-ci/tekton` failure | Tip chart/tests OK but cluster Pipeline failed (often `run-tests` timeout or smoke) — see [`deployment.md`](./deployment.md) |
| Tekton green, portal still old image | `notify-argocd` skipped or Argo not synced — check PipelineRun task results + Application `image.tag` |

Related: [`deployment.md`](./deployment.md), [`history/changelog-dogfood-notes.md`](./history/changelog-dogfood-notes.md) (image promotion).
