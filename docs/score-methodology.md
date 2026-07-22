# AgentIT score methodology

How AgentIT turns a git repository into an **overall score** and per-dimension findings. Source of truth: `src/agentit/models.py`, `src/agentit/analyzers/base.py`, `src/agentit/runner.py`.

## Quick path

```bash
uv run agentit assess <repo-url> --format terminal
# Heuristics only:
uv run agentit assess <repo-url> --no-llm --format terminal
# Machine-readable:
uv run agentit assess <repo-url> --format json --output report.json
```

No cluster and no Postgres are required for a single-repo assess (unless you use `--rescan`, which reads the fleet store).

## Seven dimensions

Equal contribution to the overall score. Analyzers in `runner.run_assessment()`:

| Dimension | Analyzer | What it looks for (examples) |
| --- | --- | --- |
| `security` | `SecurityAnalyzer` | Container hygiene, NetworkPolicy, secrets posture |
| `observability` | `ObservabilityAnalyzer` | Probes, metrics, structured logging |
| `cicd` | `CICDAnalyzer` | CI pipeline, Dockerfile, GitOps Application |
| `infrastructure` | `InfrastructureAnalyzer` | Helm/IaC, workloads, quotas |
| `compliance` | `ComplianceAnalyzer` | Admission policies, LICENSE, SBOM |
| `data_governance` | `DataGovernanceAnalyzer` | Backup / retention posture |
| `ha_dr` | `HADRAnalyzer` | HPA, PDB, multi-replica |

Each dimension also absorbs findings from `mode: detect` skills (and legacy YAML checks) merged in `runner.py`. Display labels: `portal/helpers.py` (`ha_dr` → “HA/DR”, `cicd` → “CI/CD”, `data_governance` → “Data Governance”).

## Weights (as implemented)

There are **no per-dimension product weights**. Overall score is the **simple average** of dimension scores:

```text
overall_score = sum(dimension.score) / len(scores)
```

(`AssessmentReport.model_post_init` in `models.py`.)

### Per-dimension score

Each dimension starts at **100** and subtracts severity penalties (`analyzers/base.py` `DEFAULT_PENALTIES` / `calculate_score()`):

| Severity | Penalty |
| --- | ---: |
| critical | 25 |
| high | 20 |
| medium | 10 |
| low | 3 |
| info | 0 |

Clamped to `[0, 100]` per dimension (`DimensionScore` validator).

## What “enterprise-ready” means here

There is **no shipped numeric threshold** labeled “enterprise-ready” in product code. Practically:

- CLI help frames assess as scoring **enterprise readiness** across the seven dimensions.
- Portal celebrates a **first perfect score** when `overall_score >= 100` (joy copy only — not a gate).
- Scan quality gates refuse empty “catalog dump” PRs: open remediable findings, or a claimed score delta of at least **5.0** (`quality_prs.finding_gate_allows_pr`, `min_score_delta=5.0`).

Treat the score as a continuous readiness signal, not a pass/fail certification.

## Per-PR impact framing

Intent of quality-filtered Scan PRs ([`plan-quality-helpful-prs.md`](./plan-quality-helpful-prs.md)):

- PRs are **finding-tied** (or material score-delta claims), not whole-catalog dumps.
- Bodies explain **finding → change → expected clear** (evidence kinds / clear-evidence simulation).
- Assessment Detail shows score history with **deltas** between assessments (`get_score_history` + `delta` in the portal).
- After merge + re-assess, finding resolution / score movement is how impact is verified — AgentIT does not auto-merge.

## Shareable score badge

**Planned:** a shareable score badge (e.g. shields-style URL for overall or per-dimension score). **Not shipped** — there is no public badge endpoint or documented badge URL today. Do not invent one; track under [`history/backlog.md`](./history/backlog.md).

## Related

- Dimensions catalog also summarized in [`architecture.md`](./architecture.md#assessment-dimensions)
- Solution contracts / detect-only categories: [`release-notes.md`](./release-notes.md#solution-contracts)
