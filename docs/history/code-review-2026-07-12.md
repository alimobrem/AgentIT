# AgentIT Code Review

Date: 2026-07-12
Scope: full repository (~34,000 lines across `src/agentit`, `chart/`, `argocd/`, `.github/workflows`, templates). Reviewed against the project's CLAUDE.md rules plus general code quality, correctness, and security.

## Top priorities

These are the findings most worth acting on first, ordered roughly by risk.

1. **The portal has no authentication, authorization, or CSRF protection on any route**, including ones that apply manifests to the live cluster (`/assessments/{id}/apply`, `/gates/{id}/resolve`), install operators, flip auto-mode on, and export the full database. Combined with the app's service account holding cluster `edit` (see #3), any request reaching the portal can mutate the cluster.
2. **Webhook endpoints are unauthenticated and reachable cluster-wide.** `/api/webhook/assess`, `/onboard`, `/auto-apply`, `/finding`, `/remediate` have no signature or token check, and the NetworkPolicy has no ingress `from` restriction, so any pod in the cluster (and, via the Tekton EventListener's public Route with no GitHub secret verification, potentially anyone on the internet) can trigger cluster-mutating actions or forge findings.
3. **RBAC is broad.** The app's service account holds ClusterRole `edit` (full read/write on Secrets, workloads, etc. in-namespace), the Tekton pipeline SA (which runs untrusted repo code from PRs/pushes) holds the same, and there's an unused/default-off cluster-wide `edit` ClusterRoleBinding one param flip away from being enabled.
4. **Reflected XSS in `/api/operator-status`** (query param interpolated unescaped into HTML) and a **stored XSS-adjacent JS-string breakout** in `fleet.html`/`onboard_results.html` (template values embedded in single-quoted JS inside `@click`, not JS-escaped).
5. **SQLite migration ordering bug in `store.py`**: an `ALTER TABLE` runs before the corresponding `CREATE TABLE IF NOT EXISTS`, so on a fresh database every `save_apply_results` call raises until the process restarts.
6. **Two correctness bugs that trigger false rollbacks**: `remediation_loop._verify_slos` treats `value > target` as "breached" for every metric, including `availability` where higher is better — a perfectly healthy app gets auto-rolled-back. `watchers/slo_tracker.py` inherits the same bug.
7. **Kafka consumer can silently drop events.** `consumer.py` commits offsets past a failed message on the next successful poll, so failed events never reach the DLQ and are lost.
8. **`chart/templates/deployment.yaml` and the three agent Deployments all mount the same ReadWriteOnce PVC.** With pod anti-affinity spreading replicas across nodes, this causes Multi-Attach scheduling failures, and even when co-scheduled, multiple writers hitting one SQLite file risk corruption.
9. **CI has no supply-chain pinning and doesn't gate on findings**: `trivy-action@master` and `sbom-action@v0` are floating refs, Trivy doesn't set `exit-code`, so the "Security Scan" workflow never fails the build regardless of findings.
10. **`argocd/application.yaml` commits `image.tag: latest`**, which is the value CI overwrites in-cluster after every build. Any re-apply/resync of the committed manifest silently reverts the fleet to `latest`.

## CLAUDE.md rule violations

Direct violations of the rules in the repo's own CLAUDE.md:

- **`except Exception: pass` (forbidden):**
  - `src/agentit/platform_context.py` ~L114-115 — swallows the OpenShift-detection check with no logging.
  - `src/agentit/agents/codechange.py` ~L121-124 — `_get_file_context` returns `""` silently on any error.
- **Silent/under-logged exception handling (violates the spirit of "always log"):**
  - `src/agentit/agents/orchestrator.py` ~L147-150 — platform discovery failures are invisible (no log call at all before falling back to offline context).
  - `src/agentit/agents/orchestrator.py` ~L165-166 — the skill-engine step (described in comments as the "primary generation path") logs failures at `debug`, below the required `warning`/`exception` level.
  - `src/agentit/automode.py` ~L37-38, `src/agentit/property_verifier.py` ~L81-82/108-109/133-134, `src/agentit/events.py` ~L224-225, `src/agentit/image_builder.py` ~L214-215, `src/agentit/portal/store.py` ~L1300-1307, `src/agentit/portal/routes/health.py` ~L380.
- **LLM client init not fully graceful:** `src/agentit/llm.py` L63-66 — `LLMClient.__init__` calls `_create_client()` with no try/except of its own; the "fails gracefully" property currently depends on every call site wrapping construction (which they do today, but the class itself doesn't guarantee it).
- **Validation helpers re-implemented instead of reused** (rule: `validate_manifest`/`validate_generated_files` live in `agents/base.py` and should be used, not re-wrapped): `src/agentit/agents/infrastructure.py` ~L69-72 and `src/agentit/agents/orchestrator.py` ~L501-517 both roll their own per-file validation loop around `validate_manifest`.
- **No agent besides `infrastructure.py` calls the shared validation helpers at all** — the CLAUDE.md rule is only enforced indirectly by the orchestrator's post-hoc check, which is bypassed when agents run standalone (CLI `run-agent`, tests, the chaos agent).
- **Inline styles (`style="..."`) present in templates**, contrary to "never use inline styles — all styling goes in base.html":
  - `assessment_detail.html:76`, `dashboard.html:63`, `slos.html:57`, `insights.html:59`, `capabilities.html:63` (all `style="width:{{ score }}%"` progress bars).
- **Loading spinner not consistently wired:** `dashboard.html` L74-75/94-95 duplicate spinner behavior locally via inline `onsubmit` handlers instead of the global base.html handler; and every confirm-modal-gated form (Apply to Cluster, gate Approve & Apply, Install Operator, all deletes, Enable Auto-Mode, Purge) submits via `form.submit()` from JS, which does not fire a `submit` event — so the global spinner/loading-bar never triggers for exactly the slow, destructive actions that most need it.
- **Errors not always surfaced:** several POST routes raise `HTTPException` with specific detail that gets discarded client-side under htmx (`beforeSwap` shows only a generic "Invalid request" toast), including `/schedules/create`, `/schedules/delete`, `/assessments/{id}/slos/add`, `/assessments/{id}/fix`, `/gates/{id}/resolve`.
- **No secrets found hardcoded** in `values.yaml`, `application.yaml`, chart templates, or source — this rule is being followed correctly.
- **No `# type: ignore`** found anywhere in the codebase — followed correctly.
- **`GeneratedFile`/`_sanitize_name`** are consistently imported from `agents/base.py`, never redefined — followed correctly.
- **Deployment flow** (Argo CD as sole deployer, CI patches `image.tag` param) is internally consistent in the Tekton pipeline and application.yaml — followed correctly, aside from the committed `latest` tag issue above.

## Findings by area

### Agents (`src/agentit/agents/`)

- `capabilities.py`: `ChaosAgent` is never registered in `AGENT_CLASSES`/`AGENT_CAPABILITIES` — dead code in production despite being documented.
- `chaos.py`: generated LitmusChaos experiment name/env vars (`pod-kill`/`KILL_COUNT`) aren't valid Litmus identifiers (should be `pod-delete`/`PODS_AFFECTED_PERC`); a `fieldSelector` is used where Litmus needs a `labelSelector`. Manifests are never validated.
- `cicd.py`: dedup check for Containerfile generation compares paths across agent subdirectories that never collide, so hardening and cicd agents both emit a Containerfile; generated pipelines can reference Tekton Tasks (`{name}-image-scan`, `{name}-sbom-generate`) that only exist conditionally and may never be generated; cicd and release both generate a Rollout named `{name}`, causing output conflicts the orchestrator doesn't fully suppress.
- `codechange.py` (high): the finding-category filter and the deterministic-fix dispatcher disagree — `dockerfile`/`container`/`health` fixes are unreachable dead code, while `secrets`/`logging`/`structured` findings pass the filter but have no handler and silently produce nothing. Also no path-traversal guard on `finding.file_path` when reading repo files into LLM prompts (unlike orchestrator.py's `_safe_path`).
- `compliance.py`: generates namespaced Kyverno `Policy` objects but describes them as cluster-wide `ClusterPolicy` protection.
- `cost.py` / `infrastructure.py`: cost agent's VPA (`updateMode: Auto`) and infrastructure agent's CPU-based HPA target the same Deployment — a known Kubernetes anti-pattern (scaling fights) with no conflict entry registered between the two agents.
- `hardening.py`: the generated Tekton image-scan script's vuln-count extraction is broken (`grep -c` behavior misused, error hidden), the severity posted to the webhook is hardcoded to "critical" regardless of actual finding severity, and it posts to an unauthenticated in-cluster webhook.
- `infrastructure.py`: generated PDB and HPA selectors/target-kind (`app.kubernetes.io/name`, `kind: Deployment`) don't match the labels/kind actually produced by cicd/release's Argo Rollout (`app: {name}`) — both objects would be inert against the real workload.
- `observability.py`: a Grafana legend template `"{{{{pod}}}}"` is a plain string, not an f-string, so Grafana receives literal doubled braces instead of `{{pod}}`. Error-rate PromQL label (`code=~"5.."`) is inconsistent with `release.py`'s canary AnalysisTemplate (`status=~"5.."`) for the same metric — one of the two will always match nothing.
- `orchestrator.py`: unlogged `except Exception` on platform discovery (rule violation, above); the `PRIORITY_MATRIX` records a "conflict" whenever two agents both merely succeed, which under the default profile always populates `warnings`, making the auto-approve branch effectively unreachable; `_safe_path` flattens nested output paths (e.g. `.github/dependabot.yml` → `dependabot.yml`) for on-disk writes but not for the `files_generated` record used by post-validation, causing bogus "missing from disk" reports and silent basename collisions.
- `release.py` / `retirement.py`: generated Rollout and archive-Job pod specs omit resource requests/limits, contradicting the compliance agent's own `require-resource-limits` Kyverno policy generated for the same app. The retirement archive Job also mounts a PVC that no agent ever creates.

### Core library, analyzers, remediation, watchers

- `consumer.py` (high): retry/commit logic loses failed events — see priority #7 above.
- `remediation_loop.py` (high): inverted breach comparison for `availability`-type SLOs — see priority #6.
- `kube.py` (high): `get_api_resources()` only queries the core `v1` API group, so `PlatformContext.has_api(...)` is false for Deployments, StatefulSets, Ingresses, HPAs, etc. on real clusters; this silently breaks `SkillEngine`'s platform-gating and the API drift detector's coverage.
- `cli.py`: `self-fix` writes fixes to the current working directory but re-clones a fresh repo for the before/after assessment, so the verification step can never see the fixes it just wrote; the claimed "reverting fixes on score decrease" path doesn't actually revert anything; generated file paths from LLM/skill output aren't sanitized before being joined with the target directory (potential path traversal via `..`/absolute paths).
- `cloner.py` (medium, security): the SSRF/private-host guard only applies when a URL scheme is present — scp-style `git@host:path` and bare local paths bypass it entirely even with `allow_local=False`. The DNS-based private-IP check is also fail-open and subject to DNS-rebinding (TOCTOU).
- `platform_context.py`: `api_version_for()` compares a resource *kind* against API *group* names, so it essentially never matches correctly; Tekton version-deprecation checks fire regardless of whether Tekton is installed.
- `skill_engine.py`: platform-kind gating uses naive `+"s"` pluralization (fails for `NetworkPolicy`, `Ingress`, etc.), compounding the `kube.py` core-only-API bug above.
- `learning_agent.py` (medium, design): `research_cves` asks the LLM to produce CVE IDs from training data with no validation against a real feed — fabricated/stale CVEs can be persisted as skills.
- `watchers/drift_detector.py` (high): references `DriftResult.has_warnings`/`.deprecated_apis`, attributes that don't exist on the model — raises `AttributeError` every tick, swallowed by a `debug`-level catch, so the feature silently never runs.
- `analyzers/security.py`, `observability.py`, `data_governance.py`, `compliance.py`: several checks use loose substring matching (e.g. "trace" matches "traceback", "bom" matches unrelated filenames, any `CronJob` counts as backup coverage) that produce false positives/negatives in assessment scoring.

### Portal (`src/agentit/portal/`)

Covered in priorities #1, #2, #4, #5 above. Additional notable items:

- `app.py` ~L782 (medium): the GitHub webhook registration URL is built from the client-supplied `Host` header — a forged Host causes the app to register a webhook pointing at an attacker-controlled server.
- `github_pr.py` ~L547-567 (medium): auto-created GitOps infra repos are created public, committing cluster manifests (namespace names, internal service names, schedule commands) to a world-readable repo.
- `helpers.py` vs `app.py` (medium): two drifted duplicate implementations of `run_onboarding` — the webhook-driven path omits `auto_approve`/`gates` from the stored summary, so auto-approve is silently always `False` for event-driven onboarding.
- `store.py`: single shared SQLite connection with `check_same_thread=False` accessed from the request threadpool and daemon threads with no locking, alongside the migration-ordering bug in priority #5.
- `routes/schedules.py` (medium): `/schedules/create` accepts an arbitrary free-text shell `command` from an unauthenticated form with only superficial cron-format validation.
- Several templates reach for JS/inline patterns the CLAUDE.md frontend rules exist to prevent (see rule-violations section).

### Deployment / infrastructure (`chart/`, `argocd/`, `Containerfile`, CI)

Covered in priorities #3, #8, #9, #10 above. Additional notable items:

- `chart/templates/tekton/trigger.yaml` (high): the EventListener has no GitHub webhook-secret interceptor and sits behind a public Route — anyone can POST a forged push event and trigger a pipeline run using the `pipeline` service account, which holds `edit` plus rights to patch the ArgoCD Application.
- `chart/templates/tekton/pipeline.yaml` (medium): test and build steps run in parallel, so an image is built and pushed even when tests fail; the test step executes untrusted repo code under the same privileged `pipeline` SA.
- `chart/templates/kafka/kafka-cluster.yaml` (medium): single broker, replication factor 1, no TLS/auth on the listener — any in-cluster pod can produce forged events onto topics that drive auto-apply/remediation.
- `chart/templates/networkpolicy.yaml` (medium): ingress has no `from` restriction (effectively allow-all), and only the main portal Deployment is covered — the three agent Deployments, Kafka, EventBus, and Tekton pods have no NetworkPolicy at all.
- `chart/templates/rbac.yaml` (medium): duplicate RoleBindings granting the identical ClusterRole `edit` twice; a default-off cluster-wide `edit` ClusterRoleBinding is a single param flip away from being live.
- `chart/templates/workflows/*.yaml` (medium): the CVE-scan/compliance/dependency rescan CronJobs mount no data volume and set no `AGENTIT_DB_PATH`, so as deployed they likely run against an empty ephemeral DB rather than the portal's real data.
- `.github/workflows/security.yml`: floating action refs (`@master`, `@v0`) and no `permissions:` block, no failure gate on scan severity — see priority #9.

## What's working well

- The core `(report, output_dir) -> files: list[GeneratedFile]` agent pattern is followed consistently across all agent modules.
- `GeneratedFile`/`_sanitize_name` are always imported from `agents/base.py`, never redefined.
- `_chat()` in `llm.py` correctly catches all exceptions and returns `None`; LLM-dependent code paths degrade gracefully at the call-site level.
- No hardcoded secrets were found anywhere in scope — all secret consumption goes through `secretKeyRef`/mounted secret volumes.
- No `# type: ignore` anywhere in the codebase.
- The image-tag update flow (CI builds SHA-tagged image → patches ArgoCD Application param → Argo CD auto-syncs → Rollout triggers) is internally consistent between the Tekton pipeline and `argocd/application.yaml`.
- Most templates correctly use the shared `.btn`/`.btn-sm`/`.btn-green`/`.btn-outline`/`.action-bar` classes and rely on Jinja's default autoescaping (no `| safe` or `{% autoescape false %}` found).

## Methodology

Four parallel reviews were run against read-only file access, each scoped to one area (agents, core library/analyzers/watchers, portal, deployment/CI) and instructed to check both the project's own CLAUDE.md rules and general correctness/security. This document merges and de-duplicates their findings. Line numbers are approximate (file content may have shifted slightly during review) — verify against current file state before making changes. This is a static read-through, not a penetration test or a test-suite run; no code was executed.
