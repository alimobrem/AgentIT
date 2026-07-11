# AgentIT Project Rules

## Deployment

- **Never run `helm upgrade` manually** — Argo CD is the sole deployer. Manual helm commands conflict with Argo CD's server-side apply and the Rollouts controller, causing ownership fights on Services and pod specs.
- **To change config:** Edit `argocd/application.yaml` params, commit, push. Argo CD auto-syncs.
- **To update the running image:** Build and push to the OCP registry, then `oc delete pod` to force pull. Or push a new tag and update the Argo CD param.
- **Never put secrets in values.yaml or any committed file** — the repo is public. Use `oc create secret` on-cluster and reference via Helm params.

## Code

- Never `# type: ignore` — fix the actual type issue.
- Never `except Exception: pass` — always `logger.exception("context")` or `logger.warning(...)`.
- LLM calls must always fail gracefully — catch all exceptions in `_chat()`, return `None`.
- LLM client init must always fail gracefully — if credentials are missing, continue without LLM.
- All agents follow the pattern in `agents/hardening.py` — take `(report, output_dir)`, return a result with `files: list[GeneratedFile]`.
- Shared utilities (IGNORED_DIRS, iter_text_files, calculate_score) live in `analyzers/base.py` — don't duplicate.
