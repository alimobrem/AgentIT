---
name: helm-chart
domain: infrastructure
version: 1
triggers:
  - helm
  - chart
  - iac
  - kustomize
  - terraform
  - k8s manifest
  - kubernetes manifest
outputs:
  - Chart.yaml
  - Deployment
  - Service
# Source-repo patch: infrastructure.py's "iac" (no Helm/Kustomize/Terraform)
# and "manifests" (no K8s manifests) findings are both computed by scanning
# the app's own source repo (iter_yaml_files(repo_path)), not gitops — the
# fix must land there too. delivery: source → CATEGORY_SOURCE_PATCH.
delivery: source
property: "Application is packaged as a real Helm chart with Kubernetes manifests"
mode: llm
---

# Helm Chart — Deployable Manifests (source patch)

## Property
The application ships a real Helm chart (`Chart.yaml` + `values.yaml` +
`templates/`) in its own repo, containing at minimum a `Deployment` and a
`Service` with literal `apiVersion:`/`kind:` keys — clearing both
`infrastructure.py`'s `iac` finding (`has_helm`: any file literally named
`Chart.yaml`) and its `manifests` finding (`has_k8s_manifests`: any YAML
file whose raw text contains both `"apiVersion:"` and `"kind:"`) in one
shot, since only *values* are Helm-templated, never the `apiVersion`/`kind`
keys themselves — verified in `tests/test_helm_chart_skill.py`.

## Why one skill, not two
`infrastructure.py:41-56` fires `iac` when none of Helm/Kustomize/Terraform
are present, and `manifests` when there are no K8s manifests *and* no Helm
chart. A real Helm chart satisfies `has_helm` (a `Chart.yaml` file) and,
because its Deployment/Service templates always carry literal
`apiVersion:`/`kind:` text even with `{{ }}` value-templating elsewhere in
the same file, also satisfies `has_k8s_manifests`. Both findings are almost
always open together in practice (an app with zero IaC tooling has zero
K8s manifests too) — one chart clears both real gaps with one PR instead of
two.

## Why this is LLM-mode
A static template cannot know an app's real port, language/framework, or a
sane replica count — those come from `report.stack`/`report.architecture`
(the same `PlatformContext`/stack-detection data every LLM-mode skill's
prompt already carries, see `skill_engine.py::_generate_with_llm`). The
deterministic template fallback below (used with no LLM client, or when the
LLM's output fails validation) is deliberately conservative: it never
invents a value that isn't already real and available (`app_name`, the real
internal registry `image_ref` `image_builder.get_image_ref()` already
provides, and a language-based port convention this codebase already uses
in `agents/codechange.py::_fix_dockerfile` — 3000 for Node/JS/TS, 8080
otherwise).

## Constraints (both LLM and fallback paths)
- `Chart.yaml` must exist with `apiVersion: v2`, a real `name`, and a
  `version`.
- At least one `templates/*.yaml` file must have literal `apiVersion:` and
  `kind:` text (never templated away).
- **No control-flow Helm directives** (`{{- if }}`, `{{- range }}`,
  `{{- with }}`) in `templates/*.yaml` — those break plain YAML parsing
  (`yaml.safe_load_all`), and this skill's own generated output must pass
  `agentit.agents.base.validate_manifest()` before it ships. Value
  substitutions (`{{ .Values.x }}`, `{{ .Chart.Name }}`) inline within a
  normal YAML value position are fine — only bare control-flow lines are
  forbidden.
- **Quote every `{{ .Values.x }}`/`{{ .Chart.x }}` substitution that starts
  a YAML scalar value** (e.g. `replicas: "{{ .Values.replicaCount }}"`, not
  `replicas: {{ .Values.replicaCount }}`) — unquoted, a leading `{{ ... }}`
  parses as YAML flow-mapping syntax (`{` opens a flow map), not a plain
  string, and fails `yaml.safe_load`/`validate_manifest()` with an
  "unhashable key" error.
- Never fabricate a hostname/Ingress/Route: a wrong or placeholder-looking
  hostname is worse than no Ingress at all. This skill deliberately ships
  only Deployment + Service — the finding's own check (`has_k8s_manifests`)
  never requires an Ingress, and a human who knows the real hostname can
  add one.
- Never invent specific environment variable names/values — none are
  available from `AssessmentReport`, and a fabricated one is actively
  misleading. Omit `env:` entirely rather than guess.
- Include `livenessProbe`/`readinessProbe` (TCP socket on the container's
  own declared port, generous timing) on the Deployment's container — this
  is a brand-new file this skill authors from scratch (not a patch to an
  unknown existing manifest), so there's no "wrong chart" risk in including
  a real, safe default; it also means an app with **zero** manifests before
  this PR gets its `health` finding cleared as a side effect, honestly and
  for free (see `skills/infrastructure/health-probes-policy.md` for why
  *patching* an already-existing, unknown Deployment is a different,
  higher-risk problem this skill does not attempt).
- Self-managed AgentIT is out of scope for this skill in practice: AgentIT's
  own `chart/Chart.yaml` (with real probes) already exists, so `iac`/
  `manifests`/`health` are never open findings for the `agentit` repo
  itself.

## LLM response format
Respond with ONLY this exact delimited format, one block per file, no
commentary before/after:

```
===FILE: Chart.yaml===
<content>
===FILE: values.yaml===
<content>
===FILE: templates/deployment.yaml===
<content>
===FILE: templates/service.yaml===
<content>
===END===
```

## Deterministic template (fallback — no LLM / LLM output failed validation)

```yaml
apiVersion: v2
name: {{app_name}}
description: Helm chart for {{app_name}}, generated by AgentIT to satisfy IaC/manifest baselines
type: application
version: 0.1.0
appVersion: "1.0.0"
```

## Verification
- `helm lint helm/` (or the app's chosen chart directory) passes.
- `helm template helm/` renders valid YAML with a `Deployment` and
  `Service`.
- Every file passes `agentit.agents.base.validate_manifest()` (used by this
  skill's own generation gate before any PR opens).
- Next Assess of the app's source repo clears both `iac` and `manifests`
  (`tests/test_helm_chart_skill.py`'s functional/parity test).
