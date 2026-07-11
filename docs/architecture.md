# Architecture

This doc covers how AgentIT is put together: the system components, the assessment/onboarding pipeline, the event-driven autonomous loop, and how it deploys itself on OpenShift. For setup and usage, see the [README](../README.md).

## Table of Contents

- [System overview](#system-overview)
- [Assessment → onboarding pipeline](#assessment--onboarding-pipeline)
- [Autonomous remediation loop](#autonomous-remediation-loop)
- [Deployment topology (OpenShift)](#deployment-topology-openshift)
- [The agent fleet](#the-agent-fleet)
- [Assessment dimensions](#assessment-dimensions)

## System overview

```mermaid
graph TB
    subgraph Sources["Sources"]
        Repo["Target Git repo\n(the app being onboarded)"]
        GH["GitHub API\n(PRs, webhooks)"]
    end

    subgraph Core["AgentIT Core (src/agentit)"]
        CLI["CLI\n(click)"]
        Portal["Portal\nFastAPI + Jinja2"]
        Cloner["cloner.py\nshallow git clone"]
        Runner["runner.py\nrun_assessment()"]
        Analyzers["7 Analyzers\n(stack, security, cicd,\ninfra, compliance,\ndata-gov, ha/dr)"]
        Orchestrator["FleetOrchestrator\nagents/orchestrator.py"]
        Agents["Agent Fleet\n(10 specialized agents)"]
        AutoMode["automode.py\nLLM safety gate"]
        RemLoop["remediation_loop.py\ndetect→fix→apply→verify"]
        LLM["llm.py\nClaude (Anthropic / Vertex)"]
        Store[("SQLite\nportal/store.py")]
    end

    subgraph Cluster["OpenShift Cluster"]
        ArgoCD["Argo CD\n(GitOps sync)"]
        Rollout["Argo Rollouts\n(canary Deployment)"]
        Kafka["Kafka\n(Strimzi, topics:\nagentit-events / -alerts)"]
        ArgoEvents["Argo Events\n(EventSource + Sensors)"]
        Tekton["Tekton\n(build/push pipeline)"]
        Watchers["Long-lived watcher agents\nvuln-watcher / slo-tracker\ndrift-detector"]
        Target["Onboarded app\nDeployment/Rollout"]
    end

    Repo -->|"clone"| Cloner --> Runner --> Analyzers --> Runner
    CLI --> Runner
    Portal --> Runner
    Runner -->|"AssessmentReport"| Orchestrator
    Orchestrator --> Agents
    Agents -->|"generated files"| Store
    Agents -.->|"classify secrets, summarize, classify actions"| LLM
    Orchestrator -->|"plan + gates"| Store
    Portal --> Store
    Portal -->|"create PR"| GH
    Portal -->|"dry-run + apply"| Cluster
    AutoMode -->|"LLM-gated apply"| Cluster
    RemLoop -->|"calls portal webhooks"| Portal

    ArgoCD -->|"renders chart/"| Target
    ArgoCD --> Rollout
    Tekton -->|"build & push image"| Target
    Kafka <--> ArgoEvents
    ArgoEvents -->|"score under 70 → onboard"| Portal
    Portal -->|"publish events"| Kafka
    Watchers <--> Kafka
    Watchers -->|"CVE / SLO / drift alerts"| Portal
```

## Assessment → onboarding pipeline

This is what happens for a single `assess` / `onboard` run (CLI, portal form, or webhook — same code path).

```mermaid
flowchart TD
    A["Repo URL"] --> B["cloner.py: shallow clone\n(or use local path)"]
    B --> C["StackDetector\nlanguages, frameworks, DBs, runtimes"]
    B --> D["7 Analyzers run\n(read-only, no writes)"]
    D --> E["DimensionScore + Findings\nper dimension, 0-100"]
    C --> F["AssessmentReport\noverall_score, criticality, summary,\nremediation_plan"]
    E --> F
    F -->|"optional"| G["LLM: summarize_architecture()\n2-3 sentence summary"]
    G --> F

    F --> H["FleetOrchestrator.plan()"]
    H --> I{"_select_agents()\nbased on criticality\n+ overall_score"}
    I --> J["Always: security, observability,\ncicd, compliance, release"]
    I --> K["high/critical → +dependency,\n+incident, +cost"]
    I --> L["score under 30 → +retirement"]
    I --> M["not critical → +chaos"]
    I --> N["high/critical OR score under 50\n→ +codechange"]

    J & K & L & M & N --> O["Run each agent:\n(report, output_dir) → GeneratedFile list"]
    O --> P["validate_manifest()\non every .yaml/.yml output"]
    P --> Q["_detect_conflicts()\npriority matrix, e.g.\nsecurity beats cicd/observability/compliance"]
    Q --> R{"_can_auto_approve()?\ncriticality not high/critical\nAND no critical findings\nAND score 70 or higher"}
    R -->|"yes, no warnings"| S["AUTO-APPROVED"]
    R -->|"no"| T["Gates created:\nsecurity-review (if critical findings)\ndeploy-approval (if high/critical)\nfinal-approval (always)"]
    S --> U["orchestration-summary.md\n+ recommendation"]
    T --> U

    U --> V{"Human / operator decides"}
    V -->|"create PR"| W["github_pr.py\nper-agent branches + PRs\n.agentit/CATEGORY/*.yaml"]
    V -->|"apply directly"| X["cluster_apply.py\ndry-run → classify → oc apply"]
```

## Autonomous remediation loop

When Kafka + Argo Events + auto-mode are all enabled, AgentIT can close the loop without a human in it — but every apply still goes through an LLM safety gate that **fails closed** (gates for human review) if the LLM is unavailable, unconfident, or flags the change as destructive.

```mermaid
sequenceDiagram
    participant GH as GitHub
    participant Portal as Portal (FastAPI)
    participant Kafka as Kafka (agentit-events)
    participant Sensor as Argo Events Sensor
    participant Orch as FleetOrchestrator
    participant Auto as AutoMode
    participant LLM as Claude (LLM)
    participant K8s as OpenShift API
    participant SLO as SLO Tracker

    GH->>Portal: POST /api/webhook/github-push
    Portal->>Portal: re-assess managed repo
    Portal->>Kafka: publish "assessment-complete" (score)
    Kafka->>Sensor: event delivered
    Sensor->>Portal: POST /api/webhook/onboard\n(if score under 70)
    Portal->>Orch: FleetOrchestrator.run()
    Orch-->>Portal: manifests + auto_approve flag
    Portal->>Auto: execute(files, criticality, auto_approve)
    Auto->>LLM: classify_action(manifests)\nis_destructive? confidence?
    alt LLM says safe, confidence ≥ 0.8, auto_approve true
        Auto->>K8s: dry-run apply
        Auto->>K8s: apply
        Auto->>Portal: mark remediations complete
        Portal->>Kafka: publish "auto-applied"
        loop for 5 minutes
            SLO->>SLO: poll SLO status
        end
        alt SLO breach after apply
            SLO->>Portal: create gate "rollback-review"
            SLO->>Kafka: publish "rollback-recommended"
        end
    else gated
        Auto->>Portal: create_gate("auto-mode-review")
        Portal->>Kafka: publish "gated" (severity=warning)
    end
```

## Deployment topology (OpenShift)

AgentIT deploys **itself** the same way it onboards other apps: Argo CD is the sole deployer (see [`deployment.md`](deployment.md) — never `helm upgrade` manually against a running install).

```mermaid
graph LR
    subgraph Git["This repo"]
        Chart["chart/ (Helm)"]
        AppYaml["argocd/application.yaml"]
    end

    subgraph GitOps["openshift-gitops namespace"]
        ArgoCD["Argo CD Application: agentit"]
    end

    subgraph NS["agentit namespace"]
        Rollout["Rollout: agentit\n(canary 5%→25%→50%→100%,\n60s pauses)"]
        SvcStable["Service: agentit"]
        SvcCanary["Service: agentit-canary"]
        Route["Route"]
        PVC["PVC: agentit-data\n(SQLite db)"]
        KafkaCluster["Kafka cluster (Strimzi)\ntopics: agentit-events, agentit-alerts"]
        EventSource["EventSource\n(Kafka → Argo Events)"]
        SensorOnboard["Sensor: agentit-onboard\n(score under 70 → /api/webhook/onboard)"]
        SensorAutoApply["Sensor: auto-apply triggers"]
        Pipeline["Tekton Pipeline\ngit-clone→build→test→\nimage-build→image-push→deploy"]
        CronCVE["CronWorkflow: CVE scan"]
        VulnWatcher["vuln-watcher\n(long-lived agent)"]
        SloTracker["slo-tracker\n(long-lived agent)"]
        DriftDetector["drift-detector\n(long-lived agent)"]
    end

    AppYaml -->|"watched by"| ArgoCD
    ArgoCD -->|"renders + syncs"| Chart
    Chart --> Rollout & SvcStable & SvcCanary & Route & PVC
    Chart --> KafkaCluster & EventSource & SensorOnboard & SensorAutoApply
    Chart --> Pipeline & CronCVE
    Chart --> VulnWatcher & SloTracker & DriftDetector
    Rollout --> SvcStable
    Rollout -.canary steps.-> SvcCanary
    KafkaCluster --> EventSource --> SensorOnboard & SensorAutoApply
    VulnWatcher & SloTracker & DriftDetector <--> KafkaCluster
    DriftDetector -->|"reads"| ArgoCD
```

## The agent fleet

Every agent shares the same contract (`agents/base.py`): `Agent(report: AssessmentReport, output_dir: Path).run() -> Result` where `Result.files` is a `list[GeneratedFile]`. The `FleetOrchestrator` decides which agents run for a given assessment (see the pipeline diagram above) and resolves overlaps via a priority matrix.

| Agent | Category | Always runs? | Generates |
|---|---|---|---|
| **HardeningAgent** | `security` | Yes | Deny-all `NetworkPolicy`, hardened `Containerfile`, minimal RBAC (`ServiceAccount`/`Role`/`RoleBinding`), `SecurityContext` patches |
| **ObservabilityAgent** | `observability` | Yes | `ServiceMonitor`, Grafana dashboard JSON, Prometheus alerting rules, OpenTelemetry Collector config |
| **CICDAgent** | `cicd` | Yes | Tekton `Pipeline`, Argo CD `Application`/`ApplicationSet`, Argo `Rollout` canary manifest |
| **ComplianceAgent** | `compliance` | Yes | Kyverno `ClusterPolicy` set (require-labels, require-limits, restrict-registries, disallow-`:latest`), SBOM generation script, compliance evidence doc |
| **ReleaseCoordinatorAgent** | `release` | Yes | Argo Rollouts `AnalysisTemplate`, rollout patch, rollback policy, release runbook; also seeds default SLOs by criticality |
| **DependencyAgent** | `dependency` | high/critical | Dependency risk report, Renovate config, weekly CVE-scan `CronWorkflow` |
| **IncidentAgent** | `incident` | high/critical | Incident runbook, PagerDuty service config, Alertmanager routing |
| **CostOptimizationAgent** | `cost` | high/critical | Cost report, right-sizing recommendations, cost-attribution labels, weekly cost `CronWorkflow` |
| **ChaosAgent** | `chaos` | not critical | LitmusChaos experiments: pod-kill recovery, network-latency injection, CPU-stress vs. HPA |
| **RetirementAgent** | `retirement` | score under 30 | Decommission plan, cleanup script, pre-deletion data-archive `Job` |
| **CodeChangeAgent** | `codechange` | high/critical or score under 50 | LLM-generated **source-level** patches (e.g., health-check endpoints, `.gitignore`, OTel instrumentation) — the only agent that touches app code rather than infra |
| **FleetOrchestrator** | — | meta-agent | Selects agents, resolves conflicts, decides auto-approve, writes `orchestration-summary.md` |

Three additional agents run as **long-lived processes** (via `agentit vuln-watch` / `slo-track` / `drift-detect`, deployed as their own Deployments in the chart) rather than one-shot onboarding agents:

| Long-lived agent | Loop | Role |
|---|---|---|
| **vuln-watcher** | every 6h (default) | Consumes Kafka events, checks fleet for critical findings, triggers `RemediationLoop` when auto-mode is on |
| **slo-tracker** | every 5m (default) | Polls SLO status per assessment, publishes breach alerts, opens a `rollback-review` gate if a breach follows a recent apply |
| **drift-detector** | every 10m (default) | Polls Argo CD `Applications` for `OutOfSync`, publishes drift alerts, auto-syncs if auto-mode is on |

## Assessment dimensions

`runner.py` runs the `StackDetector` plus 7 analyzers over the cloned repo (read-only — analyzers never write to the repo). Each produces a `DimensionScore` (0–100) with `Finding`s at `critical`/`high`/`medium`/`low`/`info` severity, which feed both the overall score and the `RemediationItem` plan.

| Dimension | Analyzer | Checks (examples) |
|---|---|---|
| `security` | `SecurityAnalyzer` | Hardcoded secrets (regex + LLM false-positive filtering), root containers, missing `HEALTHCHECK`, `:latest` tags, missing `NetworkPolicy`, missing vuln scanning in CI, non-UBI base images |
| `observability` | `ObservabilityAnalyzer` | Metrics/tracing/logging instrumentation, health probes |
| `cicd` | `CICDAnalyzer` | CI pipeline presence, GitOps wiring, deployment automation |
| `infrastructure` | `InfrastructureAnalyzer` | IaC presence, manifest completeness |
| `compliance` | `ComplianceAnalyzer` | Policy-as-code, labeling, SBOM/provenance |
| `data_governance` | `DataGovernanceAnalyzer` | Data handling, retention, PII exposure signals |
| `ha_dr` | `HADRAnalyzer` | Replica counts, backup/restore, multi-AZ signals |

Findings are sorted by severity into a prioritized `remediation_plan`, each with an estimated effort (`critical` → 2 agent-hours … `info` → 5 agent-minutes) and the agent responsible for fixing it.
