# AgentIT score methodology

How AgentIT turns a git repository into an **overall score** and per-dimension findings. Source of truth: `src/agentit/scoring.py`, `src/agentit/models.py`, `src/agentit/runner.py`.

## Quick path

```bash
uv run agentit assess <repo-url> --format terminal
# Heuristics only (no API key):
uv run agentit assess <repo-url> --no-llm --format terminal
# Machine-readable:
uv run agentit assess <repo-url> --format json --output report.json
```

No cluster and no Postgres are required for a single-repo assess.

Checked-in sample (no install beyond reading the files):

- [`examples/sample-assessment.md`](../examples/sample-assessment.md)
- [`examples/sample-assessment.json`](../examples/sample-assessment.json)

## Seven dimensions

| Dimension | Analyzer | What it looks for (examples) |
| --- | --- | --- |
| `security` | `SecurityAnalyzer` | Container hygiene, NetworkPolicy, secrets posture |
| `observability` | `ObservabilityAnalyzer` | Probes, metrics, structured logging |
| `cicd` | `CICDAnalyzer` | CI pipeline, Dockerfile, GitOps Application |
| `infrastructure` | `InfrastructureAnalyzer` | Helm/IaC, workloads, quotas |
| `compliance` | `ComplianceAnalyzer` | Admission policies, LICENSE, SBOM |
| `data_governance` | `DataGovernanceAnalyzer` | Backup / retention posture |
| `ha_dr` | `HADRAnalyzer` | HPA, PDB, multi-replica |

Each dimension also absorbs `mode: detect` skills / YAML checks merged in `runner.py`.

## Score model v2 (current)

New assessments set `score_version: 2` ([ADR 0003](./adr/0003-score-model-v2.md)).

### Per-dimension

`score = 100 × passed / applicable` over applicable controls:

- When data-driven checks ran for the dimension: each check row is a control (`passed` true/false). Extra analyzer findings without a matching check add failed controls.
- When only analyzer findings exist: baseline of 8 controls, each finding counts as one failure.

Clamped to `[0, 100]`.

### Overall

Criticality-weighted mean of dimension scores (`scoring.DIMENSION_WEIGHTS`). Security and compliance weigh more for `critical` / `high` apps.

### Letter grades

| Grade | Score |
| --- | ---: |
| A | ≥ 90 |
| B | ≥ 80 |
| C | ≥ 70 (`SCORE_GOOD`) |
| D | ≥ 40 (`SCORE_OK`) |
| F | &lt; 40 |

Portal color bands use the same thresholds (`scoring.score_band`).

## Legacy model v1

Historical reports with `score_version: 1` (or missing): start at 100, subtract severity penalties (`DEFAULT_PENALTIES`), equal average of dimensions. Not used for new assessments.

## What “enterprise-ready” means here

There is **no shipped certification threshold**. Practically:

- Continuous readiness signal across seven dimensions.
- Portal celebrates a first perfect score (`overall_score >= 100`).
- Scan quality gates refuse empty catalog-dump PRs (`min_score_delta=5.0`).

## Per-PR / fix impact

Assessment Detail ranks **top fixes by estimated overall-score delta** if that finding alone were cleared (same model as the report’s `score_version`). Labelled “estimated” in the UI. Ledger decision cards show category targets + approve/reject.

## Shareable score badge

```text
GET /badge/{repo_name}.svg
```

Authorization (any one):

- `AGENTIT_BADGE_PUBLIC=1`, or
- `?token=` matching `AGENTIT_BADGE_TOKEN`, or
- `repo_name` listed in `AGENTIT_PUBLIC_BADGE_APPS` (comma-separated).

Example markdown:

```markdown
![score](https://<your-portal>/badge/my-app.svg?token=<token>)
```

## Related

- [ADR 0003](./adr/0003-score-model-v2.md)
- [CHANGELOG](../CHANGELOG.md)
