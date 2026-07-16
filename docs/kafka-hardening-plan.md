# Kafka Hardening Plan

**Status: Phase 1 (chart-only, additive) and part of Phase 2 (`EventPublisher`/
`EventConsumer` SASL support) are implemented and verified (`helm lint`,
`helm template --set kafka.enabled=true --set kafka.auth.enabled=true`, and a
new `pytest` suite covering the plaintext/SASL_SSL code paths) — but
**NOT enabled live**. `kafka.auth.enabled` still defaults to `false`
(`chart/values.yaml`), and `argocd/application.yaml` does not set it, so
today's live, unauthenticated single-broker Kafka is completely unaffected
by this change until a human deliberately flips the flag. See "Progress
update: Phase 1 + partial Phase 2 implemented" below for exactly what
landed, what's still missing before Phase 3 ("flip the switch") can start,
and the coordinated-cutover risk of enabling this on the live cluster.**

Originally written as part of the docs/code-review-2026-07-12.md follow-up
pass (item #4) — the review correctly identified
`chart/templates/kafka/kafka-cluster.yaml` as a single-broker,
replication-factor-1, no-TLS/no-auth listener, and asked whether enabling
TLS + SASL was small enough to do the same night as smaller security fixes
(RBAC dedup, XSS escaping, private-repo creation, etc). It was not — the
rest of this doc (below) explains why and laid out what a real fix needs,
before any of it was implemented.

## Current state (verified against the chart on disk tonight)

`chart/templates/kafka/kafka-cluster.yaml`:

```yaml
kafka:
  listeners:
    - name: plain
      port: 9092
      type: internal
      tls: false
  config:
    offsets.topic.replication.factor: 1
    transaction.state.log.replication.factor: 1
    transaction.state.log.min.isr: 1
```

- One `KafkaNodePool` replica (`replicas: 1`, combined `controller`+`broker`
  roles) — no redundancy; also means "replication factor" is moot today
  regardless of the topic-level config, since there's only one broker to
  replicate to.
- The single listener is `type: internal`, `tls: false` — any pod in the
  cluster that can resolve `agentit-kafka-kafka-bootstrap.agentit.svc:9092`
  (which is any pod, full stop — see the NetworkPolicy gap this session's
  item #5 partially closed for the watcher Deployments, but Kafka itself
  still has no NetworkPolicy of its own) can produce or consume on any
  topic with zero authentication.
- Every producer/consumer in the app connects with a bare `bootstrap_servers`
  string and no security config: `src/agentit/events.py`'s `EventPublisher`
  (`KafkaProducer(bootstrap_servers=self._bootstrap, ...)`,
  `_connect()` around L60-68) and `src/agentit/consumer.py`'s `EventConsumer`
  (`KafkaConsumer(*topics, bootstrap_servers=self._bootstrap, ...)`,
  `__init__` around L50-60). Both use the `kafka-python` library.

## Why this is a bigger change than tonight's other fixes

Every other item in tonight's pass was a one-file (or few-line) change with
an obvious, narrow blast radius. This one touches four independent layers
that all have to agree simultaneously or the whole event pipeline goes dark:

1. **Strimzi Kafka CR** — add a second (or third) listener with
   `tls: true` and `authentication.type: scram-sha-512`, and raise
   `KafkaNodePool.replicas` for real replication (a single-broker cluster
   can't usefully set `replication.factor` above 1 anyway — that's a second,
   related fix, not just a security one).
2. **`KafkaUser` CRs (Strimzi)** — one per real identity that needs to talk
   to Kafka. At minimum: the portal Deployment, the four watcher Deployments
   (`vuln-watcher`, `slo-tracker`, `drift-detector`, `skill-learner` — three
   of which already publish/consume today per `chart/templates/agents/*.yaml`
   setting `AGENTIT_KAFKA_BOOTSTRAP`), and the `remediation_loop.py`/
   `remediation/dispatcher.py` callers if those run as separate processes.
   Each `KafkaUser` needs `spec.authentication.type: scram-sha-512` and an
   `spec.authorization` ACL block scoped to exactly the topics that identity
   actually uses (`agentit-events`, `agentit-assessments`, `agentit-gates`,
   `agentit-decisions`, `agentit-alerts`, `agentit-dlq` — see the `TOPIC_*`
   constants at the top of `events.py`). Strimzi's User Operator then
   generates a Secret per `KafkaUser` containing the SCRAM credentials.
3. **Credential wiring into every caller** — `EventPublisher.__init__` and
   `EventConsumer.__init__` currently take a single `bootstrap_servers`
   string and construct `KafkaProducer`/`KafkaConsumer` with no security
   kwargs at all. Both need:
   - New constructor parameters (or env-var reads, matching the existing
     `AGENTIT_KAFKA_BOOTSTRAP` convention) for `security_protocol` (`SASL_SSL`),
     `sasl_mechanism` (`SCRAM-SHA-512`), `sasl_plain_username`/
     `sasl_plain_password`, and `ssl_cafile` (the cluster CA — Strimzi
     publishes this as a Secret too, `<cluster-name>-cluster-ca-cert`).
   - Every Deployment template that sets `AGENTIT_KAFKA_BOOTSTRAP`
     (`chart/templates/agents/vuln-watcher.yaml`, `slo-tracker.yaml`,
     `drift-detector.yaml`, `skill-learner.yaml`, and the main
     `chart/templates/deployment.yaml`) needs a matching `secretKeyRef` env
     block pointing at its own `KafkaUser`-generated Secret, plus a mounted
     CA cert (or `ssl_cafile` pointed at a mounted ConfigMap — Strimzi also
     publishes the CA as a ConfigMap-friendly PEM).
   - The four CronJob workflow templates fixed earlier this session
     (`chart/templates/workflows/*.yaml`) don't touch Kafka directly today,
     so they're out of scope here — but worth re-checking if that changes.
4. **Local/dev and test compatibility** — `tests/test_event_publisher.py`
   and `tests/test_consumer.py` construct `EventPublisher`/`EventConsumer`
   directly with a plain `bootstrap_servers` string and no SASL config
   (mirroring today's unauthenticated local-dev Kafka). Adding required
   SASL params would break every one of those call sites unless the new
   params are optional and default to today's unauthenticated behavior —
   i.e. this needs to be backward-compatible for local dev (`docker run
   kafka` with no auth) while being mandatory-in-practice for the chart's
   in-cluster deployment. That's a design decision (an `AGENTIT_KAFKA_SASL_*`
   env-var convention, off by default, matching the existing
   `AGENTIT_KAFKA_BOOTSTRAP` "unset = disabled" pattern) that deserves its
   own review, not a rushed same-night call.

## Suggested phasing (mirrors `docs/postgres-migration-plan.md`'s structure)

1. **Phase 1 — chart only.** Add the TLS+SASL listener and `KafkaUser` CRs,
   additively, alongside the existing plaintext listener (Strimzi supports
   multiple listeners on one `Kafka` CR). Nothing in `src/` changes yet, so
   this is safe to land independently — same "additive infra, zero caller
   risk" approach `postgres-migration-plan.md` used for the CloudNativePG
   prep. Bump `KafkaNodePool.replicas` to 3 in the same pass (the redundancy
   half of "single broker" is arguably higher-value than the auth half, and
   is genuinely zero-risk to app code either way).
2. **Phase 2 — `EventPublisher`/`EventConsumer` SASL support.** Add the new
   optional constructor params/env vars, default OFF (plaintext, exactly
   today's behavior) so all existing tests and local dev keep working
   unmodified. Add new tests exercising the SASL_SSL path with mocked
   `kafka-python` client construction (no real broker needed, same pattern
   `test_event_publisher.py` already uses for the plaintext path).
3. **Phase 3 — flip the switch.** Update every Deployment/CronJob template
   to point at its `KafkaUser` Secret + set the new env vars, remove the
   plaintext listener from the `Kafka` CR, re-run the full watcher/consumer
   integration tests against a real Strimzi cluster (this needs a live
   OpenShift cluster with Strimzi installed — `--live-cluster` gate, same as
   `test_live_cluster_e2e.py`) before merging.
4. **Phase 4 — NetworkPolicy for Kafka itself.** Out of scope for tonight's
   item #5 (which only covered the four watcher Deployments per that item's
   explicit instructions) but a natural follow-up once Phase 1-3 land: a
   NetworkPolicy on the Kafka broker pods restricting ingress to exactly the
   `KafkaUser` identities from step 2, instead of today's implicit
   allow-all-in-cluster.

## Why not attempt even Phase 1 tonight

Even the "chart-only, additive" Phase 1 touches
`chart/templates/kafka/kafka-cluster.yaml`, which — unlike the workflow
CronJobs and NetworkPolicy files touched elsewhere in tonight's pass — has
no existing test coverage in `tests/test_helm_templates.py` to catch a
misconfigured Strimzi listener/authentication block before it reaches a
real cluster, and Strimzi's `KafkaUser`/listener schema has enough surface
area (SCRAM vs mTLS, `type: internal` vs `route`/`ingress` listener types,
per-listener vs per-broker cert config) that getting it wrong silently is
easy and expensive to debug live. `kafka.enabled` defaults to `false` in
`chart/values.yaml`, but **`argocd/application.yaml` overrides it to
`"true"` — Kafka is live, unauthenticated, and single-broker on the real
cluster right now**, not a dormant feature (this section's original framing
undersold that). Shipping a rushed, unverified Phase 1 carries real risk (a
broken Strimzi CR can leave the whole `Kafka` resource stuck reconciling)
directly against that live resource. Treat this as work that needs a
non-prod namespace or an explicit maintenance window to verify against
before merging, per Phase 3's explicit `--live-cluster` gate above.

## Progress update: Phase 1 + partial Phase 2 implemented (not enabled live)

Implemented, `helm lint`/`helm template`-verified, and unit-tested — but
gated behind a new `kafka.auth.enabled` flag that defaults to `false`, so
today's live plaintext Kafka is unaffected until a human deliberately flips
it (see the coordinated-cutover risk below for why that's not a same-night
flip):

1. **Phase 1 — chart, additive.** `chart/templates/kafka/kafka-cluster.yaml`
   gained a second listener (`name: tls`, port 9093, `tls: true`,
   `authentication.type: scram-sha-512`) alongside the existing plaintext
   `plain` listener (unchanged), plus `entityOperator.userOperator: {}` (the
   User Operator that actually reconciles `KafkaUser` CRs into Secrets —
   missing from the CR before this pass, and required for step 2 to do
   anything at all). New `chart/templates/kafka/kafka-users.yaml`: one
   `KafkaUser` (SCRAM-SHA-512, ACL scoped to exactly the topics/consumer
   groups it uses in code) per real identity — `agentit-portal`,
   `agentit-vuln-watcher`, `agentit-slo-tracker`, `agentit-drift-detector`,
   `agentit-skill-learner` (`capability-scout` excluded: it never sets
   `AGENTIT_KAFKA_BOOTSTRAP`). New
   `chart/templates/kafka/kafka-networkpolicy.yaml`: ingress on the broker
   pods scoped to those same app pods (+ Strimzi's own in-cluster
   components for the entity-operator/replication traffic it needs) instead
   of today's implicit allow-all-in-cluster — this is Phase 4 from the list
   above, pulled forward since it's additive and independently low-risk.
   `KafkaNodePool.replicas` was deliberately left at 1 — out of scope for
   this pass, still a separate, real gap (see "Current state" above).
2. **Phase 2 — partial.** `src/agentit/events.py` gained
   `kafka_security_kwargs()`: returns `{}` (today's exact plaintext
   behavior) unless `AGENTIT_KAFKA_SASL_USERNAME`/`_PASSWORD` are both set in
   the process env, in which case it returns SASL_SSL/SCRAM-SHA-512 kwargs
   (mechanism, username, password, optional `ssl_cafile` from
   `AGENTIT_KAFKA_SSL_CAFILE`) for the `kafka-python` client.
   `EventPublisher._connect()` (events.py) and `EventConsumer.__init__`
   (`src/agentit/consumer.py`) both now pass `**kafka_security_kwargs()`
   into their `KafkaProducer`/`KafkaConsumer` construction. New tests in
   `tests/test_event_publisher.py`/`tests/test_consumer.py` mock the env
   vars and assert the constructed client's kwargs for both the
   plaintext-unset and SASL_SSL-set paths.
   **NOT done in this pass, still needed for Phase 2 to be complete**: no
   Deployment template (`chart/templates/deployment.yaml`,
   `chart/templates/agents/*.yaml`) was touched — those SASL env vars are
   never actually populated from the `KafkaUser` Secrets today. This was a
   deliberate scope boundary for this pass (concurrent work was touching
   those same Deployment templates for an unrelated change), not an
   oversight — see "What's still needed before Phase 3" below.

**Verification:**

- `helm lint chart/`: 0 failures.
- `helm template chart/ --set kafka.enabled=true --set kafka.auth.enabled=true`:
  renders valid YAML — confirmed the `tls` listener, `userOperator: {}`, all
  5 `KafkaUser` CRs with the expected ACLs, and the broker `NetworkPolicy`
  render exactly as intended.
- `helm template chart/ --set kafka.enabled=true` (no `kafka.auth.enabled`)
  and the chart's own defaults (`kafka.enabled` unset): confirmed **zero**
  `KafkaUser`/broker-`NetworkPolicy`/`tls`-listener/`userOperator` output —
  today's exact plaintext-only shape is unchanged.
- `pytest tests/ -q --ignore=tests/test_real_repos.py
  --ignore=tests/test_browser.py --ignore=tests/test_live_cluster_e2e.py`
  (`KUBECONFIG=/tmp/nonexistent-path`): full suite passes, including the new
  SASL credential-wiring tests.

### The coordinated-cutover risk of actually enabling `kafka.auth.enabled` live

This is the same class of risk `docs/postgres-migration-plan.md` §7 covers
for the Postgres cutover, and for the identical underlying reason: **flipping
the flag on its own does not move any producer or consumer off plaintext.**
Concretely, today, every one of the 5 Deployments
(`deployment.yaml`/`agents/*.yaml`) sets
`AGENTIT_KAFKA_BOOTSTRAP=agentit-kafka-kafka-bootstrap.agentit.svc:9092` (the
plaintext listener) and never sets `AGENTIT_KAFKA_SASL_USERNAME`/`_PASSWORD`
— so even with `kafka.auth.enabled: true` live, `kafka_security_kwargs()`
still returns `{}` everywhere, and every pod keeps talking plaintext on 9092
exactly as before. The new TLS+SASL listener and `KafkaUser` Secrets would
exist on the cluster but be entirely unused. That's actually the *safe*
half-state — the real risk shows up once someone starts wiring the
Deployment env vars (the still-pending Phase 3 work):

- If the plaintext listener is ever removed from the `Kafka` CR (or the
  broker `NetworkPolicy` above is enabled) **before** every Deployment has
  its `KafkaUser` Secret's credentials wired in and has actually restarted
  with them, that Deployment's producer/consumer silently stops connecting
  — `EventPublisher`/`EventConsumer` both fail open (buffer locally /
  polling-only mode) rather than crash, so this would show up as growing
  `event-buffer.db` backlogs and stale consumer-group lag, not an obvious
  outage, until someone checks `/health`.
- The 5 identities (portal + 4 watchers) don't restart in lockstep — each is
  its own Deployment with its own rollout — so a rollout of the env-var
  change has to either (a) keep both listeners live through the whole
  rollout window (the additive design here supports that), or (b) restart
  all 5 in one coordinated action, mirroring exactly how
  `postgres-migration-plan.md` §7 required all-5-Deployments-at-once for
  `AGENTIT_DB_BACKEND=postgres`.
- Unlike the Postgres cutover, this one also touches `NetworkPolicy`: once
  the broker `NetworkPolicy` above is enabled, any pod not covered by its
  `from` rules (portal + the 4 watchers, by label) loses plaintext access
  too, even if it's still trying to use the plaintext listener. Do not
  enable `kafka.auth.enabled` at the same time as removing the plaintext
  listener unless every consumer has already been verified to connect over
  SASL_SSL first.

### What's still needed before Phase 3 ("flip the switch") can actually start

1. Wire `AGENTIT_KAFKA_SASL_USERNAME`/`_PASSWORD` (`secretKeyRef` against
   each `KafkaUser`'s auto-generated Secret, e.g. `agentit-portal` →
   Secret `agentit-portal`, keys `user`/`password`) and
   `AGENTIT_KAFKA_SSL_CAFILE` (the cluster CA Strimzi publishes as
   `agentit-kafka-cluster-ca-cert`, mounted as a volume) into
   `deployment.yaml` and each of `agents/vuln-watcher.yaml`,
   `slo-tracker.yaml`, `drift-detector.yaml`, `skill-learner.yaml` —
   deliberately not done in this pass.
2. Decide and verify the transition window strategy (dual-listener rollout
   vs. single coordinated restart) referenced above, on a non-prod namespace
   first if at all possible.
3. Re-verify the broker `NetworkPolicy`'s `strimzi.io/cluster: agentit-kafka`
   peer selector actually covers entity-operator/kafka-exporter connectivity
   on this cluster's real Strimzi version before relying on it in
   production — flagged as an unverified assumption in
   `kafka-networkpolicy.yaml`'s own header comment.
4. `KafkaNodePool.replicas` is still 1 — the redundancy half of "single
   broker" from the original review remains open, independent of the auth
   work above.
5. Only after 1-3 above: flip `kafka.auth.enabled: "true"` in
   `argocd/application.yaml` (commented-out snippet already prepared next to
   `kafka.enabled` there) as its own deliberate, reviewed change — not
   bundled into an unrelated deploy.
