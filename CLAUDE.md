# AgentIT Project Rules

## Deployment

- **Never run `helm upgrade` manually** — Argo CD is the sole deployer. Manual helm commands conflict with Argo CD's server-side apply and the Rollouts controller, causing ownership fights on Services and pod specs.
- **To change config:** Edit `argocd/application.yaml` params, commit, push. Argo CD auto-syncs.
- **To update the running image:** The CI pipeline builds with the commit SHA as the image tag, then patches the ArgoCD Application's `image.tag` param. ArgoCD auto-syncs the new tag, triggering the Argo Rollout. For manual updates: push a new tag to the registry, then `oc -n openshift-gitops patch application agentit --type=json -p '[{"op":"replace","path":"/spec/source/helm/parameters/1/value","value":"<tag>"}]'`.
- **Never put secrets in values.yaml or any committed file** — the repo is public. Use `oc create secret` on-cluster and reference via Helm params.

## Code

- Never `# type: ignore` — fix the actual type issue.
- Never `except Exception: pass` — always `logger.exception("context")` or `logger.warning(...)`.
- LLM calls must always fail gracefully — catch all exceptions in `_chat()`, return `None`.
- LLM client init must always fail gracefully — if credentials are missing, continue without LLM.
- All agents follow the pattern in `agents/base.py` — take `(report, output_dir)`, return a result with `files: list[GeneratedFile]`.
- `GeneratedFile` and `_sanitize_name` live in `agents/base.py` — import from there, never redefine locally.
- `validate_manifest()` and `validate_generated_files()` live in `agents/base.py` — use to validate generated YAML.
- Shared analyzer utilities (IGNORED_DIRS, iter_text_files, calculate_score) live in `analyzers/base.py` — don't duplicate.

## Frontend / Templates

- **Never use inline styles** — all styling goes in `base.html` `<style>` block as CSS classes.
- Use `.btn`, `.btn-sm`, `.btn-green`, `.btn-outline`, `.action-bar` for buttons and action groups.
- Errors must always be visible to the user — every form/endpoint must surface error messages.
- All form submissions must show a loading spinner — handled globally in `base.html` JS.
