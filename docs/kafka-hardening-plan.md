# Kafka Hardening Plan (deferred work)

**Status: not started. This is a plan, not an implementation.** Written as
part of the docs/code-review-2026-07-12.md follow-up pass (item #4) — the
review correctly identified `chart/templates/kafka/kafka-cluster.yaml` as a
single-broker, replication-factor-1, no-TLS/no-auth listener, and asked
whether enabling TLS + SASL was small enough to do the same night as smaller
security fixes (RBAC dedup, XSS escaping, private-repo creation, etc). It
is not — this doc explains why and lays out what a real fix needs.

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
easy and expensive to debug live. Given `kafka.enabled` already defaults to
`false` in `chart/values.yaml` — meaning this is not yet protecting
anything running in production — shipping a rushed, unverified Phase 1
carries real risk (a broken Strimzi CR can leave the whole `Kafka` resource
stuck reconciling) for close to zero immediate safety benefit. Treat this as
next-session work with a live cluster available to verify against, per
Phase 3's explicit `--live-cluster` gate above.
