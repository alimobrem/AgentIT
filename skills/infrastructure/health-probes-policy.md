---
name: health-probes-policy
domain: infrastructure
version: 1
triggers:
  - health
  - probe
  - liveness
  - readiness
outputs:
  - Policy
property: "Every container in this app's namespace has liveness and readiness probes configured"
mode: template
---

# Health Probes — Kyverno Mutate Policy

## Property
Kubernetes can detect and restart a hung/unready container only if that
container declares a `livenessProbe`/`readinessProbe`. `ha_dr.py:54-61`'s
`health` finding fires when no manifest anywhere in the app's **source
repo** mentions either — a plain text scan (`ha_dr.py:18-28`:
`"livenessProbe" in content or "readinessProbe" in content`).

## Why a mutate policy, not a Deployment patch
This finding is fundamentally different from `iac`/`manifests`/`quota`/
`scaling`: a probe is not a standalone resource — it's a patch to a
container spec inside a Deployment/Rollout AgentIT does **not** own the
base definition of (unlike `ResourceQuota`/`HorizontalPodAutoscaler`, which
are new, additive resources with no pre-existing owner). Three real,
already-proven precedents in this codebase rule out the two more direct
alternatives:

1. **Source-repo patch to an existing manifest** — `ha_dr.py`'s `Finding`
   for `health` carries no `file_path`, and this codebase's skill
   generation pipeline (`skill_engine.py::generate()`) has no access to the
   app's real, current repo content at generation time (verified: the only
   agent that ever read real file content for context,
   `agents/codechange.py::CodeChangeAgent`, is not instantiated anywhere in
   the live onboarding pipeline — `agents/orchestrator.py` never
   constructs one). Regenerating a whole Deployment from scratch to "patch"
   it (the one precedent that *does* exist, `containerfile.md`'s Dockerfile
   patch) is safe for a Dockerfile's fairly generic build steps; for a
   Deployment it would blindly discard real, unknown fields (image, env,
   volumes, resources, selectors) — exactly the "wrong chart is worse than
   no chart" trap this plan calls out, worse here than for a Dockerfile.
2. **A fabricated full-Deployment copy delivered to gitops** —
   `fleet_hpa.py`/`self_managed_hpa.py` are this exact codebase's own
   hard-won lesson (two live incidents) that generating a manifest
   targeting a workload without first verifying it's real is actively
   harmful. Even with live-cluster discovery of the *real* container spec
   (mirroring `fleet_hpa.discover_namespace_workloads`), delivering a
   second, competing Deployment definition into `apps/{app}/` alongside
   whatever repo already defines that Deployment is a structural
   ownership collision every other cluster-delivery skill in this catalog
   (`hpa.md`/`pdb.md`/`resourcequota.md`) avoids by only ever adding
   resources that don't already exist.
3. **A namespace-scoped Kyverno `mutate` policy** (this skill) has neither
   problem: it doesn't need to know the real Deployment's other fields
   (Kyverno mutates the *live*, already-real, admitted object — every
   other field is untouched), and it's a new, standalone resource with no
   pre-existing owner to collide with, exactly like `hpa.md`/`pdb.md`.

## Constraints
- Scope to the app's own namespace (`{{namespace}}`) only — never
  cluster-wide (`ClusterPolicy`) — matching this fleet's one-namespace-
  per-app convention (`live_evidence.namespace_for_repo`).
- Target `kind: Deployment` (not `Pod`) so the injected probe is visible on
  `kubectl get deployment -o yaml`, not only on the resulting pods.
- Only add a probe to a container that doesn't already have one
  (`preconditions` + `foreach`) — never overwrite an existing probe with a
  different one.
- Use `tcpSocket` on the container's own already-declared `containerPort` —
  never a guessed HTTP path. A TCP check on a real, already-open port is
  strictly safer than the status quo (zero probe) and cannot fail due to an
  unknown app-specific health endpoint.
- **Skip containers with zero declared ports entirely** — a background
  worker/consumer container often has no `ports` at all, and there is no
  safe TCP target to check for one. Each `foreach` entry's own
  `preconditions` require `element.ports[0].containerPort` to be non-empty
  before attempting either patch. Without this, the JSON patch would try
  to set `tcpSocket.port` to an empty/null value, which the API server's
  own schema validation for `Probe.tcpSocket.port` (a required field)
  rejects at admission time — silently breaking *every* future update to
  that Deployment (Argo sync included), not just this one, until the
  policy is fixed or removed. Leaving a genuinely portless container
  without a probe is strictly safer than that.
- **`livenessProbe` and `readinessProbe` are two independent `foreach`
  entries, each gated on its own field being empty** — not one shared
  precondition covering both. A container that already has exactly one of
  the two (e.g. a real, already-tuned `readinessProbe` but no
  `livenessProbe`) must only get the missing one added. `patchesJson6902`'s
  `op: add` on a JSON path that already has a value *replaces* it (RFC
  6902) — a single precondition gating both patches would silently
  overwrite a real, existing probe the moment the *other* one was missing,
  directly violating the "never overwrite an existing probe" rule above.
- Generous timing (`initialDelaySeconds`, `failureThreshold`) — a probe
  that's too aggressive and causes false restarts on a healthy but
  slow-starting app is the one way this fix could be actively harmful; bias
  conservative.
- `validationFailureAction`/audit mode is not applicable — `mutate` rules
  always apply when matched; there is no Kyverno "audit-only" mode for a
  mutate rule (unlike `kyverno-require-labels.md`'s `validate` rule).

## Closing the source-analyzer disconnect
This policy fixes the **live** workload, which `ha_dr.py`'s source-only
scan never sees — the same gap `quota`/`scaling` already had.
`live_evidence.live_health_probes_present()` reads the live
Deployment/Rollout's actual containers (which *do* reflect Kyverno's
Deployment-level mutation) and clears `health` when every container already
has both probes, regardless of what the source repo says.

## Template

```yaml
apiVersion: kyverno.io/v1
kind: Policy
metadata:
  name: {{app_name}}-health-probes
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  rules:
    - name: add-missing-health-probes
      match:
        any:
          - resources:
              kinds:
                - Deployment
              namespaces:
                - {{namespace}}
      mutate:
        foreach:
          # Two independent entries -- each probe is only added when it,
          # specifically, is missing, and only when the container actually
          # declares a port to check. See "Constraints" above for why a
          # single shared precondition covering both probes (the earlier
          # version of this policy) was unsafe.
          - list: "request.object.spec.template.spec.containers"
            preconditions:
              all:
                - key: "{{ element.livenessProbe || '' }}"
                  operator: Equals
                  value: ""
                - key: "{{ element.ports[0].containerPort || '' }}"
                  operator: NotEquals
                  value: ""
            patchesJson6902: |-
              - path: "/spec/template/spec/containers/{{ elementIndex }}/livenessProbe"
                op: add
                value:
                  tcpSocket:
                    port: "{{ element.ports[0].containerPort }}"
                  initialDelaySeconds: 30
                  periodSeconds: 20
                  failureThreshold: 5
          - list: "request.object.spec.template.spec.containers"
            preconditions:
              all:
                - key: "{{ element.readinessProbe || '' }}"
                  operator: Equals
                  value: ""
                - key: "{{ element.ports[0].containerPort || '' }}"
                  operator: NotEquals
                  value: ""
            patchesJson6902: |-
              - path: "/spec/template/spec/containers/{{ elementIndex }}/readinessProbe"
                op: add
                value:
                  tcpSocket:
                    port: "{{ element.ports[0].containerPort }}"
                  initialDelaySeconds: 15
                  periodSeconds: 10
                  failureThreshold: 3
```

## Verification
- `kubectl get policy {{app_name}}-health-probes -n {{namespace}}` exists.
- `kubectl get deployment -n {{namespace}} -o yaml` shows
  `livenessProbe`/`readinessProbe` on every container after the policy
  admits a new revision (`kubectl rollout restart` or the next real
  deploy).
- `property_verifier._verify_health_probes` recognizes this policy's
  `mutate` rule as satisfying the `health` property directly from its YAML
  (no live cluster needed) — see `tests/test_property_verifier_health.py`.
- Re-Assess after the mutation lands: `live_health_probes_present()` clears
  the `health` finding even though the app's source repo never changed.
