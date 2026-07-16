"""Regression tests for Helm chart templates (argo-events, tekton).

Validates rendered YAML structure against bugs that were previously fixed.
Uses simple string replacement for Helm variables — no helm binary needed.
"""

import re
from pathlib import Path

import yaml

CHART_DIR = Path(__file__).resolve().parent.parent / "chart" / "templates"

# Minimal Helm variable replacements for rendering
HELM_VARS = {
    "{{ .Release.Namespace }}": "test-ns",
    "{{ .Release.Name }}": "agentit",
    "{{ .Release.Service }}": "Helm",
    "{{ .Chart.Name }}": "agentit",
    "{{ .Values.image.tag | quote }}": '"test-image-tag"',
    "{{ .Values.postgres.bundled.image | quote }}": '"registry.redhat.io/rhel9/postgresql-15@sha256:06aeada2ca417445bc4fb711729e65a02ee78421a09c862cbd136ebdd51d7cfa"',
    "{{ .Values.postgres.bundled.credentials.secretName }}": "agentit-postgres-bundled-app",
    "{{ .Values.postgres.bundled.credentials.database | quote }}": '"agentit"',
    '{{ .Values.postgres.bundled.backup.schedule | default "23 */6 * * *" | quote }}': '"23 */6 * * *"',
    '{{ .Values.agents.capabilityScout.mode | default "docs" }}': "docs",
}


def _render(template_path: Path) -> str:
    """Read a template file and do basic Helm variable substitution."""
    raw = template_path.read_text()
    # Strip {{- if ... }}, {{- else }} and {{- end }} conditionals. This keeps
    # both branches' literal content, which is fine for our purposes here:
    # tests target one branch's keys and tolerate the other branch's lines
    # being present too (yaml.safe_load just lets the later duplicate key win).
    raw = re.sub(r"[ \t]*\{\{-?\s*if\s+.*?\}\}\n?", "", raw)
    raw = re.sub(r"[ \t]*\{\{-?\s*else\s*\}\}\n?", "", raw)
    # Strip {{- range ... }} loops the same way -- keeps one literal copy of
    # the loop body (with its now-unbound $key/$value refs collapsed to null
    # by the trailing substitution below), which is enough for tests that
    # only care about the loop's fixed surrounding structure, not its
    # runtime-only entries.
    raw = re.sub(r"[ \t]*\{\{-?\s*range\s+.*?\}\}\n?", "", raw)
    raw = re.sub(r"[ \t]*\{\{-?\s*end\s*\}\}\n?", "", raw)
    # Strip {{- $var := ... }} variable assignments (no rendered output)
    raw = re.sub(r"[ \t]*\{\{-?\s*\$\w+\s*:=.*?-?\}\}\n?", "", raw)
    for var, val in HELM_VARS.items():
        raw = raw.replace(var, val)
    # Any remaining "key: {{ some expression }}" (e.g. `index $x.data "y"`,
    # or a range loop's now-unbound `{{ $key }}`/`{{ $value }}`) we can't
    # literally substitute — collapse to a null value so the line still
    # parses; tests only need the *key* to be present, not the value. Covers
    # both plain mapping entries and "- key: {{ ... }}" list-item entries.
    raw = re.sub(r"^(\s*(?:- )?[\w-]+):\s*\{\{.*?\}\}\s*$", r"\1:", raw, flags=re.MULTILINE)
    return raw


def _load(template_path: Path) -> dict:
    """Render and parse a single-document YAML template."""
    rendered = _render(template_path)
    doc = yaml.safe_load(rendered)
    assert doc is not None, f"Template rendered to empty YAML: {template_path.name}"
    return doc


# ---------------------------------------------------------------------------
# EventSource: eventsource-kafka.yaml
# ---------------------------------------------------------------------------

class TestEventSourceKafka:
    TEMPLATE = CHART_DIR / "argo-events" / "eventsource-kafka.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "EventSource"

    def test_consumer_group_is_dict(self):
        """Bug: consumerGroup was previously a bare string, must be a dict."""
        doc = _load(self.TEMPLATE)
        kafka_spec = doc["spec"]["kafka"]["agentit-events"]
        cg = kafka_spec["consumerGroup"]
        assert isinstance(cg, dict), (
            f"consumerGroup must be a dict, got {type(cg).__name__}: {cg}"
        )
        assert "groupName" in cg

    def test_namespace_label(self):
        doc = _load(self.TEMPLATE)
        assert doc["metadata"]["namespace"] == "test-ns"


# ---------------------------------------------------------------------------
# Sensor: sensor-onboard.yaml
# ---------------------------------------------------------------------------

class TestSensorOnboard:
    TEMPLATE = CHART_DIR / "argo-events" / "sensor-onboard.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "Sensor"

    def test_trigger_uses_http_not_argo_workflow(self):
        """Bug: trigger previously used argoWorkflow instead of http."""
        doc = _load(self.TEMPLATE)
        triggers = doc["spec"]["triggers"]
        for trigger in triggers:
            tmpl = trigger["template"]
            assert "argoWorkflow" not in tmpl, (
                "Sensor trigger must use 'http', not 'argoWorkflow'"
            )
            assert "http" in tmpl, "Sensor trigger missing 'http' config"

    def test_trigger_http_url(self):
        doc = _load(self.TEMPLATE)
        http = doc["spec"]["triggers"][0]["template"]["http"]
        assert "test-ns" in http["url"]
        assert http["method"] == "POST"


# ---------------------------------------------------------------------------
# Pipeline: tekton/pipeline.yaml
# ---------------------------------------------------------------------------

class TestTektonPipeline:
    TEMPLATE = CHART_DIR / "tekton" / "pipeline.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "Pipeline"

    def test_git_clone_params_uppercase(self):
        """Bug: git-clone params were lowercase 'url'/'revision', must be URL/REVISION."""
        doc = _load(self.TEMPLATE)
        tasks = doc["spec"]["tasks"]
        git_clone = next(t for t in tasks if t["name"] == "git-clone")
        param_names = {p["name"] for p in git_clone["params"]}
        assert "URL" in param_names, (
            f"git-clone must use uppercase 'URL', found: {param_names}"
        )
        assert "REVISION" in param_names, (
            f"git-clone must use uppercase 'REVISION', found: {param_names}"
        )
        # Must NOT have lowercase variants
        assert "url" not in param_names, "git-clone has lowercase 'url' — must be 'URL'"
        assert "revision" not in param_names, (
            "git-clone has lowercase 'revision' — must be 'REVISION'"
        )

    def test_run_tests_working_dir_no_agentit_suffix(self):
        """Bug: workingDir had '/agentit' suffix that breaks cloned repo layout."""
        doc = _load(self.TEMPLATE)
        tasks = doc["spec"]["tasks"]
        run_tests = next(t for t in tasks if t["name"] == "run-tests")
        steps = run_tests["taskSpec"]["steps"]
        for step in steps:
            wd = step.get("workingDir", "")
            assert not wd.endswith("/agentit"), (
                f"workingDir must not end with '/agentit', got: {wd}"
            )

    def test_run_tests_sidecar_emits_empty_compute_resources(self):
        """Regression: Tekton always serializes sidecar computeResources as
        {} on Get; without matching that in the chart, Pipeline/agentit-ci
        stays OutOfSync forever under Argo CD."""
        doc = _load(self.TEMPLATE)
        run_tests = next(t for t in doc["spec"]["tasks"] if t["name"] == "run-tests")
        sidecars = run_tests["taskSpec"]["sidecars"]
        assert sidecars[0]["computeResources"] == {}

    def test_pipeline_has_workspaces(self):
        doc = _load(self.TEMPLATE)
        ws = doc["spec"]["workspaces"]
        ws_names = {w["name"] for w in ws}
        assert "source" in ws_names

    def test_pipeline_has_retries(self):
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        assert tasks["git-clone"].get("retries", 0) >= 1
        assert tasks["run-tests"].get("retries", 0) >= 1
        assert tasks["notify-argocd"].get("retries", 0) >= 1

    def test_pipeline_no_restart_rollout(self):
        """Regression: Tekton must NOT restart pods — Argo CD owns deployment."""
        doc = _load(self.TEMPLATE)
        task_names = {t["name"] for t in doc["spec"]["tasks"]}
        assert "restart-rollout" not in task_names, "restart-rollout conflicts with Argo CD"
        assert "notify-argocd" in task_names, "Pipeline should notify Argo CD instead"

    def test_notify_argocd_updates_image_tag(self):
        """Verify notify-argocd pins image.tag to this build's REVISION before apply."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        steps = {s["name"]: s for s in tasks["notify-argocd"]["taskSpec"]["steps"]}
        script = steps["sync-application-spec"]["script"]
        assert "image.tag" in script
        assert "REVISION" in script
        assert "oc apply -f argocd/application.yaml" in script

    def test_notify_argocd_pins_tag_before_applying_application_spec(self):
        """Regression: applying argocd/application.yaml with bootstrap
        image.tag=latest then patching afterward raced Argo selfHeal onto the
        stale :latest digest (scout CrashLoopBackOff). Rewrite REVISION into
        the manifest, then apply once — no separate update-image-tag step."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        task = tasks["notify-argocd"]
        step_names = [s["name"] for s in task["taskSpec"]["steps"]]
        assert "sync-application-spec" in step_names
        assert "update-image-tag" not in step_names, (
            "image.tag must be rewritten into application.yaml before apply; "
            "a post-apply patch races Argo onto :latest"
        )
        steps = {s["name"]: s for s in task["taskSpec"]["steps"]}
        script = steps["sync-application-spec"]["script"]
        assert "sed" in script and "oc apply -f argocd/application.yaml" in script
        assert any(
            e.get("name") == "REVISION" for e in steps["sync-application-spec"].get("env", [])
        ), "sync-application-spec needs REVISION to pin image.tag before apply"
        ws_names = {w["name"] for w in task["taskSpec"]["workspaces"]}
        assert "source" in ws_names
        task_ws_names = {w["name"] for w in task["workspaces"]}
        assert "source" in task_ws_names

    def test_pipeline_has_timeouts(self):
        doc = _load(self.TEMPLATE)
        for task in doc["spec"]["tasks"]:
            assert "timeout" in task, f"Task {task['name']} missing timeout"

    def test_smoke_test_image_runs_after_build_before_argocd_notify(self):
        """The image smoke test must gate promotion: it runs after
        build-image produces the real tag, and notify-argocd (which patches
        the live Argo CD Application to that tag) must wait on it -- so a
        broken image (missing pytest/tests/chart/git/gh, discovered live and
        one at a time this session) can never reach the deployed Rollout."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        assert "smoke-test-image" in tasks
        smoke = tasks["smoke-test-image"]
        assert "build-image" in smoke["runAfter"]
        assert "smoke-test-image" in tasks["notify-argocd"]["runAfter"]

    def test_smoke_test_image_checks_every_regressed_tool(self):
        """Each of these was discovered missing from the deployed image one
        at a time, live: gh, a real .git checkout, pytest, tests/, chart/."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        script = "\n".join(tasks["smoke-test-image"]["taskSpec"]["steps"][0]["args"])
        for expected in (
            "python -m pytest --version",
            "test -d tests",
            "test -d chart",
            "git --version",
            "gh --version",
            "safe.directory",
            "git -C /opt/app-root/src status",
        ):
            assert expected in script, f"smoke-test-image script missing check: {expected!r}"

    def test_smoke_test_image_uses_the_just_built_image(self):
        """Must run the freshly-built tag, not some other/older image, so
        the check is meaningful against this exact commit's build."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        smoke = tasks["smoke-test-image"]
        step = smoke["taskSpec"]["steps"][0]
        assert step["image"] == "$(params.image)"
        param_values = {p["name"]: p["value"] for p in smoke["params"]}
        assert param_values["image"] == "$(params.image-ref):$(params.revision)"

    def test_pipeline_has_finally_block(self):
        doc = _load(self.TEMPLATE)
        assert "finally" in doc["spec"], "Pipeline missing 'finally' block"
        finally_tasks = doc["spec"]["finally"]
        assert len(finally_tasks) >= 2
        task_names = {t["name"] for t in finally_tasks}
        assert "report-status" in task_names
        assert "self-assess" in task_names


# ---------------------------------------------------------------------------
# Kafka → Sensor → HTTP trigger flow validation
# ---------------------------------------------------------------------------

class TestKafkaTriggerFlow:
    """Validate the end-to-end Kafka→EventSource→Sensor→HTTP chain is wired correctly."""

    def test_eventsource_topic_matches_publisher(self):
        doc = _load(CHART_DIR / "argo-events" / "eventsource-kafka.yaml")
        topic = doc["spec"]["kafka"]["agentit-events"]["topic"]
        assert topic == "agentit-events"

    def test_sensor_references_eventsource(self):
        es_doc = _load(CHART_DIR / "argo-events" / "eventsource-kafka.yaml")
        sensor_doc = _load(CHART_DIR / "argo-events" / "sensor-onboard.yaml")
        dep = sensor_doc["spec"]["dependencies"][0]
        assert dep["eventSourceName"] == es_doc["metadata"]["name"]
        assert dep["eventName"] == "agentit-assessments"

    def test_sensor_filters_on_assessment_complete(self):
        doc = _load(CHART_DIR / "argo-events" / "sensor-onboard.yaml")
        filters = doc["spec"]["dependencies"][0]["filters"]["data"]
        action_filter = next(f for f in filters if "action" in f["path"])
        assert "assessment-complete" in action_filter["value"]

    def test_sensor_filters_on_low_score(self):
        doc = _load(CHART_DIR / "argo-events" / "sensor-onboard.yaml")
        filters = doc["spec"]["dependencies"][0]["filters"]["data"]
        score_filter = next(f for f in filters if "score" in f["path"])
        assert score_filter["comparator"] == "<"
        assert "70" in score_filter["value"]

    def test_sensor_http_targets_portal_onboard_webhook(self):
        doc = _load(CHART_DIR / "argo-events" / "sensor-onboard.yaml")
        http = doc["spec"]["triggers"][0]["template"]["http"]
        assert "/api/webhook/onboard" in http["url"]
        assert http["method"] == "POST"

    def test_sensor_passes_event_body_to_webhook(self):
        doc = _load(CHART_DIR / "argo-events" / "sensor-onboard.yaml")
        http = doc["spec"]["triggers"][0]["template"]["http"]
        payload = http["payload"]
        assert len(payload) >= 1
        assert payload[0]["src"]["dataKey"] == "body"

    def test_eventbus_has_replicas(self):
        doc = _load(CHART_DIR / "argo-events" / "eventbus.yaml")
        assert doc["kind"] == "EventBus"
        assert doc["spec"]["nats"]["native"]["replicas"] >= 3


# ---------------------------------------------------------------------------
# Tekton cleanup CronJob
# ---------------------------------------------------------------------------

class TestTektonCleanup:
    TEMPLATE = CHART_DIR / "tekton" / "cleanup-cronjob.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "CronJob"

    def test_schedule_is_daily(self):
        doc = _load(self.TEMPLATE)
        parts = doc["spec"]["schedule"].split()
        assert len(parts) == 5

    def test_uses_pipeline_service_account(self):
        doc = _load(self.TEMPLATE)
        sa = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["serviceAccountName"]
        assert sa == "pipeline"


# ---------------------------------------------------------------------------
# Postgres (bundled, non-operator): postgres/postgres-bundled.yaml,
# postgres-bundled-secret.yaml. An earlier CloudNativePG-operator-based
# design was tried and abandoned (blocked on unprovisioned paid EDB/Red Hat
# Marketplace entitlements) — see docs/postgres-migration-plan.md.
# ---------------------------------------------------------------------------

class TestPostgresBundled:
    TEMPLATE = CHART_DIR / "postgres" / "postgres-bundled.yaml"

    def _docs(self):
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        return {d["kind"]: d for d in docs if d}

    def test_parseable(self):
        by_kind = self._docs()
        assert set(by_kind) == {"PersistentVolumeClaim", "Deployment", "Service"}

    def test_no_operator_apis_used(self):
        """Regression guard: this path must never reintroduce a CNPG/operator CRD."""
        rendered = _render(self.TEMPLATE)
        assert "cnpg.io" not in rendered
        assert "postgresql.cnpg.io" not in rendered

    def test_pvc_requests_persistent_storage(self):
        by_kind = self._docs()
        pvc = by_kind["PersistentVolumeClaim"]
        assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
        assert "storage" in pvc["spec"]["resources"]["requests"]

    def test_deployment_uses_rhel_image_and_credentials_secret(self):
        by_kind = self._docs()
        container = by_kind["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        assert "rhel9/postgresql-15" in container["image"]
        env_names = {e["name"] for e in container["env"]}
        assert {"POSTGRESQL_USER", "POSTGRESQL_PASSWORD", "POSTGRESQL_DATABASE"} <= env_names

    def test_deployment_has_restrictive_security_context(self):
        by_kind = self._docs()
        pod_spec = by_kind["Deployment"]["spec"]["template"]["spec"]
        assert pod_spec["securityContext"]["runAsNonRoot"] is True
        container = pod_spec["containers"][0]
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]

    def test_service_targets_postgres_port(self):
        by_kind = self._docs()
        svc = by_kind["Service"]
        ports = svc["spec"]["ports"]
        assert any(p["port"] == 5432 for p in ports)


class TestPostgresBundledBackup:
    TEMPLATE = CHART_DIR / "postgres" / "postgres-bundled-backup.yaml"

    def _docs(self):
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        return {d["kind"]: d for d in docs if d}

    def test_parseable(self):
        by_kind = self._docs()
        # `Job` is the Sync-hook PVC-bind probe added alongside the
        # CronJob/PVC pair -- see this template's own header comment for why
        # a WaitForFirstConsumer backup PVC needs an immediate consumer.
        assert set(by_kind) == {"PersistentVolumeClaim", "CronJob", "Job"}

    def test_cronjob_has_valid_schedule(self):
        by_kind = self._docs()
        parts = by_kind["CronJob"]["spec"]["schedule"].split()
        assert len(parts) == 5

    def test_cronjob_uses_pg_dump_against_bundled_service(self):
        by_kind = self._docs()
        container = by_kind["CronJob"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        args = "\n".join(container["args"])
        assert "pg_dump" in args
        assert "agentit-postgres-bundled" in args

    def test_pvc_bind_job_is_a_sync_hook_that_mounts_the_backup_claim(self):
        """Regression guard for the fix this Job exists for: it must run
        during Sync (not PostSync, which Argo CD won't fire until the
        Application is already Healthy -- but a WaitForFirstConsumer PVC
        stays Pending, and therefore Progressing, until something mounts
        it), and it must actually mount the same backup PVC to bind it."""
        by_kind = self._docs()
        job = by_kind["Job"]
        annotations = job["metadata"]["annotations"]
        assert annotations["argocd.argoproj.io/hook"] == "Sync"
        policy = annotations["argocd.argoproj.io/hook-delete-policy"]
        # BeforeHookCreation is required so a Failed/DeadlineExceeded Job
        # is replaced on the next sync instead of permanently blocking Argo.
        assert "BeforeHookCreation" in policy
        assert "HookSucceeded" in policy
        volumes = job["spec"]["template"]["spec"]["volumes"]
        claim_names = {v["persistentVolumeClaim"]["claimName"] for v in volumes if "persistentVolumeClaim" in v}
        assert claim_names == {by_kind["PersistentVolumeClaim"]["metadata"]["name"]}

    def test_pvc_bind_job_tolerates_quota_contention(self):
        """Regression guard: this namespace's ResourceQuota is routinely
        near its limits.cpu ceiling from concurrent Tekton PipelineRun task
        pods, which can transiently block scheduling this probe's pod. The
        Job's deadline/retry budget must stay generous enough (matching the
        CronJob's own activeDeadlineSeconds) to wait that out instead of
        failing the whole Argo CD sync on every CI burst."""
        by_kind = self._docs()
        job_spec = by_kind["Job"]["spec"]
        cronjob_spec = by_kind["CronJob"]["spec"]["jobTemplate"]["spec"]
        assert job_spec["activeDeadlineSeconds"] >= cronjob_spec["activeDeadlineSeconds"]
        assert job_spec["backoffLimit"] >= 3


class TestArgoCDApplicationParams:
    """Regression: `postgres.backend` (the historical SQLite/Postgres
    selector) is gone -- Postgres is the only supported store, not a flag
    on the app side -- so this file must never reintroduce it. The bundled
    instance's own backup coverage still needs both
    `postgres.bundled.enabled` and `postgres.bundled.backup.enabled` set
    together, since the backup CronJob's own `{{- if }}` gate
    (postgres-bundled-backup.yaml) requires both. See
    docs/postgres-migration-plan.md's "Backup/retention" section."""
    APPLICATION_YAML = Path(__file__).resolve().parent.parent / "argocd" / "application.yaml"

    def _params(self) -> dict:
        doc = yaml.safe_load(self.APPLICATION_YAML.read_text())
        return {p["name"]: p["value"] for p in doc["spec"]["source"]["helm"]["parameters"]}

    def test_no_postgres_backend_param(self):
        params = self._params()
        assert "postgres.backend" not in params, (
            "postgres.backend is a removed, now-meaningless parameter -- "
            "Postgres is the only supported store, not a selectable backend"
        )

    def test_backup_requires_bundled_instance_enabled(self):
        """The backup CronJob's own {{- if }} gate (postgres-bundled-backup.yaml)
        requires postgres.bundled.enabled too -- if that ever gets disabled
        while backup.enabled stays 'true' in application.yaml, the backup flag
        would silently do nothing."""
        params = self._params()
        if params.get("postgres.bundled.backup.enabled") == "true":
            assert params.get("postgres.bundled.enabled") == "true"

    def test_pipeline_ignore_differences_cover_tekton_normalization(self):
        """Regression: Pipeline/agentit-ci stayed OutOfSync because Tekton
        round-trips taskSpec.metadata/spec and empty sidecar
        computeResources. ignoreDifferences must cover those fields so the
        Application can be Synced without ignoring real task-script drift."""
        doc = yaml.safe_load(self.APPLICATION_YAML.read_text())
        pipeline_ignores = [
            d for d in doc["spec"]["ignoreDifferences"]
            if d.get("kind") == "Pipeline" and d.get("group") == "tekton.dev"
        ]
        assert len(pipeline_ignores) == 1
        exprs = set(pipeline_ignores[0].get("jqPathExpressions") or [])
        assert ".spec.tasks[].taskSpec.metadata" in exprs
        assert ".spec.tasks[].taskSpec.spec" in exprs
        assert ".spec.tasks[].taskSpec.sidecars[].computeResources" in exprs
        assert ".spec.finally[].taskSpec.metadata" in exprs
        assert ".spec.finally[].taskSpec.spec" in exprs


class TestPostgresBundledNetworkPolicy:
    """Regression test: the bundled Postgres pod carries the chart-wide
    app.kubernetes.io/name label, so it's also selected by the main
    NetworkPolicy (chart/templates/networkpolicy.yaml), which only allows
    ingress on port 8080. Without this dedicated policy, 5432 would be
    silently unreachable even though every other resource looks healthy."""

    TEMPLATE = CHART_DIR / "postgres" / "postgres-bundled-networkpolicy.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "NetworkPolicy"

    def test_selects_only_the_bundled_postgres_pod(self):
        doc = _load(self.TEMPLATE)
        labels = doc["spec"]["podSelector"]["matchLabels"]
        assert labels["app.kubernetes.io/component"] == "postgres-bundled"

    def test_allows_ingress_on_postgres_port(self):
        doc = _load(self.TEMPLATE)
        ports = [p["port"] for rule in doc["spec"]["ingress"] for p in rule["ports"]]
        assert 5432 in ports


class TestPostgresBundledSecret:
    TEMPLATE = CHART_DIR / "postgres" / "postgres-bundled-secret.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "Secret"
        assert doc["type"] == "kubernetes.io/basic-auth"

    def test_has_username_key_only(self):
        # `password` is deliberately NOT rendered by Helm -- see
        # postgres-bundled-secret-init-job.yaml, the only thing that writes
        # it, and the header comment in this template for why (Argo CD's
        # Helm rendering can't use `lookup` to preserve it across syncs, and
        # neither `ignoreDifferences` nor `RespectIgnoreDifferences` reliably
        # protected the field either -- confirmed live, twice).
        doc = _load(self.TEMPLATE)
        assert "username" in doc["data"]
        assert "password" not in doc["data"]


class TestPostgresBundledSecretInitJob:
    TEMPLATE = CHART_DIR / "postgres" / "postgres-bundled-secret-init-job.yaml"

    def _by_kind(self):
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        return {d["kind"]: d for d in docs if d}

    def test_parseable(self):
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        assert len(docs) == 4

    def test_job_is_a_postsync_hook(self):
        by_kind = self._by_kind()
        job = by_kind["Job"]
        annotations = job["metadata"]["annotations"]
        assert annotations["argocd.argoproj.io/hook"] == "PostSync"
        assert annotations["argocd.argoproj.io/hook-delete-policy"] == "HookSucceeded"

    def test_job_uses_dedicated_service_account(self):
        by_kind = self._by_kind()
        sa_name = by_kind["ServiceAccount"]["metadata"]["name"]
        job_sa = by_kind["Job"]["spec"]["template"]["spec"]["serviceAccountName"]
        assert job_sa == sa_name
        assert by_kind["RoleBinding"]["subjects"][0]["name"] == sa_name

    def test_role_scoped_to_the_one_named_secret(self):
        by_kind = self._by_kind()
        rule = by_kind["Role"]["rules"][0]
        assert rule["resources"] == ["secrets"]
        assert rule["resourceNames"] == ["agentit-postgres-bundled-app"]
        assert set(rule["verbs"]) == {"get", "patch"}

    def test_script_only_patches_when_password_missing(self):
        by_kind = self._by_kind()
        args = "\n".join(by_kind["Job"]["spec"]["template"]["spec"]["containers"][0]["args"])
        assert 'jsonpath=\'{.data.password}\'' in args
        assert "-n \"$EXISTING\"" in args
        assert "oc patch secret" in args


# ---------------------------------------------------------------------------
# oauth-proxy-secret.yaml / internal-webhook-token-secret.yaml / secret-init-job.yaml
#
# Same latent bug as bug #1 (postgres-bundled-secret.yaml): both Secrets used
# to generate their random value via a Helm `lookup`-guarded `randAlphaNum`,
# which never worked under real Argo CD syncs (repo-server renders via `helm
# template` with no destination-cluster credentials, so `lookup` always saw
# nothing) -- silently regenerating the value on every sync. Fixed the same
# way: the Secrets render no random field at all, and a shared PostSync hook
# Job (secret-init-job.yaml) generates each one exactly once, only if missing.
# ---------------------------------------------------------------------------

class TestOauthProxySecret:
    TEMPLATE = CHART_DIR / "oauth-proxy-secret.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "Secret"
        assert doc["type"] == "Opaque"

    def test_renders_no_session_secret_key(self):
        # `session_secret` is deliberately NOT rendered by Helm -- see
        # secret-init-job.yaml, the only thing that ever writes it.
        doc = _load(self.TEMPLATE)
        assert "data" not in doc or "session_secret" not in (doc.get("data") or {})

    def test_no_lookup_or_randalphanum(self):
        """Regression guard: this template must never reintroduce the
        lookup-guarded randAlphaNum pattern that silently regenerated the
        cookie secret on every Argo CD sync."""
        raw = self.TEMPLATE.read_text()
        assert "lookup " not in raw
        assert "randAlphaNum" not in raw


class TestOauthProxyHealthzBypass:
    """Regression guard for a live bug: without --skip-auth-regex, the
    oauth-proxy sidecar redirected the *external* synthetic probe's request
    to /healthz (302 to the OAuth login page) instead of reaching the app,
    so synthetic-probe-cronjob.yaml always reported the portal as down. Raw
    text check (not full YAML parse) since deployment.yaml's SAR line uses
    a `{{ $sar | toJson }}` expression this test file's Helm-var stripping
    doesn't attempt to evaluate."""
    TEMPLATE = CHART_DIR / "deployment.yaml"

    def test_skips_auth_for_healthz_only(self):
        raw = self.TEMPLATE.read_text()
        assert "--skip-auth-regex=^/healthz$" in raw


class TestSyntheticProbeCertCheck:
    """Regression guard for a live bug: ubi-minimal (this Job's probe image)
    doesn't ship an `openssl` CLI, so the old `command -v openssl` guard
    always failed and CERT_DAYS stayed "null" on every run -- which, because
    the portal's route_cert_expiry_days Gauge starts at 0 until first
    `.set()`, read as a permanent "0 days remaining" and fired
    AgentITCertExpiringCritical for real, against a certificate that
    actually had ~14 months left."""
    TEMPLATE = CHART_DIR / "synthetic-probe-cronjob.yaml"

    def test_does_not_shell_out_to_openssl(self):
        """Checks the actual probe invocation, not this template's own
        explanatory comment (which legitimately still mentions `openssl`
        by name to explain why it's no longer used)."""
        raw = self.TEMPLATE.read_text()
        assert "openssl s_client" not in raw
        assert "openssl x509 -noout" not in raw
        assert "command -v openssl >/dev/null" not in raw

    def test_extracts_expiry_from_curl_verbose_output(self):
        raw = self.TEMPLATE.read_text()
        assert "expire date:" in raw
        assert "curl -sk -o /dev/null -v" in raw


class TestInternalWebhookTokenSecret:
    TEMPLATE = CHART_DIR / "internal-webhook-token-secret.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "Secret"
        assert doc["type"] == "Opaque"
        assert doc["metadata"]["name"] == "agentit-internal-webhook-token"

    def test_renders_no_token_key(self):
        # `token` is deliberately NOT rendered by Helm -- see
        # secret-init-job.yaml, the only thing that ever writes it.
        doc = _load(self.TEMPLATE)
        assert "data" not in doc or "token" not in (doc.get("data") or {})

    def test_no_lookup_or_randalphanum(self):
        """Regression guard: this template must never reintroduce the
        lookup-guarded randAlphaNum pattern that silently regenerated the
        webhook token on every Argo CD sync (confirmed live via
        managedFields -- argocd-controller owned data.token before this
        fix)."""
        raw = self.TEMPLATE.read_text()
        assert "lookup " not in raw
        assert "randAlphaNum" not in raw

    def test_always_rendered_not_gated(self):
        """This Secret must always template (no top-level `if`) -- the app's
        fail-open path for a missing token is meant only for local dev/tests,
        not a real cluster."""
        raw = self.TEMPLATE.read_text()
        assert not raw.lstrip().startswith("{{- if")


class TestSecretInitJob:
    TEMPLATE = CHART_DIR / "secret-init-job.yaml"

    def _by_kind(self):
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        return {d["kind"]: d for d in docs if d}

    def test_parseable(self):
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        assert len(docs) == 4

    def test_job_is_a_sync_hook(self):
        """Regression: this used to run as a PostSync hook, but PostSync
        only fires once the Application is already Healthy -- and
        oauth-proxy CrashLoops without session_secret (this Job's own job)
        until it's healthy, so PostSync never actually ran on a fresh
        auth-enabled sync. See this template's own header comment."""
        by_kind = self._by_kind()
        job = by_kind["Job"]
        annotations = job["metadata"]["annotations"]
        assert annotations["argocd.argoproj.io/hook"] == "Sync"
        assert annotations["argocd.argoproj.io/hook-delete-policy"] == "HookSucceeded"

    def test_job_uses_dedicated_service_account(self):
        by_kind = self._by_kind()
        sa_name = by_kind["ServiceAccount"]["metadata"]["name"]
        job_sa = by_kind["Job"]["spec"]["template"]["spec"]["serviceAccountName"]
        assert job_sa == sa_name
        assert by_kind["RoleBinding"]["subjects"][0]["name"] == sa_name

    def test_role_scoped_to_only_the_named_secrets(self):
        """Not a blanket grant -- resourceNames must list exactly the
        Secrets this Job is allowed to touch, both branches present since
        the test harness keeps both sides of the auth.enabled conditional."""
        by_kind = self._by_kind()
        rule = by_kind["Role"]["rules"][0]
        assert rule["resources"] == ["secrets"]
        assert set(rule["resourceNames"]) == {
            "agentit-internal-webhook-token",
            "agentit-proxy-session",
        }
        assert set(rule["verbs"]) == {"get", "patch"}

    def test_script_bootstraps_webhook_token_and_proxy_session(self):
        by_kind = self._by_kind()
        args = "\n".join(by_kind["Job"]["spec"]["template"]["spec"]["containers"][0]["args"])
        assert 'init_secret_key "agentit-internal-webhook-token" "token" 40' in args
        assert 'init_secret_key "agentit-proxy-session" "session_secret" 24' in args

    def test_script_only_patches_when_key_missing(self):
        by_kind = self._by_kind()
        args = "\n".join(by_kind["Job"]["spec"]["template"]["spec"]["containers"][0]["args"])
        assert 'jsonpath="{.data.$key}"' in args
        assert '-n "$existing"' in args
        assert "oc patch secret" in args

    def test_not_gated_behind_a_top_level_flag(self):
        """The webhook token half must always run -- that Secret is always
        templated, unlike the proxy-session half which is conditional
        entirely within the Role's resourceNames and the script body."""
        raw = self.TEMPLATE.read_text()
        assert not raw.lstrip().startswith("{{- if")


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

class TestRBAC:
    TEMPLATE = CHART_DIR / "rbac.yaml"

    def _by_kind(self):
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        return {d["kind"]: d for d in docs if d}

    def _all(self, kind: str) -> list[dict]:
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        return [d for d in docs if d and d["kind"] == kind]

    def test_parseable(self):
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        assert len(docs) >= 4

    def test_has_service_account(self):
        by_kind = self._by_kind()
        assert "ServiceAccount" in by_kind
        sa = by_kind["ServiceAccount"]
        assert sa["metadata"]["name"] == "agentit"

    def test_has_namespace_rolebinding(self):
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        rbs = [d for d in docs if d and d["kind"] == "RoleBinding"]
        rb = rbs[0]
        assert rb["metadata"]["namespace"] == "test-ns"
        assert rb["roleRef"]["name"] == "edit"
        assert rb["subjects"][0]["name"] == "agentit"

    def test_no_duplicate_edit_rolebinding(self):
        """Regression test: rbac.yaml used to define two separate namespace-scoped
        RoleBindings (`-edit` and `-cross-namespace-apply`) both granting the
        identical ClusterRole `edit` to the same SA in the same namespace -- the
        second one was pure duplication (a RoleBinding can never grant
        cross-namespace access regardless of its name or roleRef kind)."""
        rbs = self._all("RoleBinding")
        edit_grants_to_agentit_sa = [
            rb for rb in rbs
            if rb["roleRef"]["name"] == "edit"
            and rb["metadata"].get("namespace") == "test-ns"
            and rb["subjects"][0]["name"] == "agentit"
        ]
        assert len(edit_grants_to_agentit_sa) == 1

    def test_has_cluster_rolebinding(self):
        # rbac.yaml renders two ClusterRoleBindings (clusterWideApply's "edit"
        # grant, and operatorInstall's scoped "operator-installer" grant) --
        # find the "edit" one specifically rather than assuming there's only one.
        crbs = self._all("ClusterRoleBinding")
        crb = next(c for c in crbs if c["roleRef"]["name"] == "edit")
        assert crb["subjects"][0]["name"] == "agentit"
        assert crb["subjects"][0]["namespace"] == "test-ns"

    def test_cluster_rolebinding_enables_cross_namespace(self):
        """ClusterRoleBinding (not RoleBinding) is required for cross-namespace apply."""
        crbs = self._all("ClusterRoleBinding")
        crb = next(c for c in crbs if c["roleRef"]["name"] == "edit")
        assert "namespace" not in crb["metadata"], (
            "ClusterRoleBinding must not have metadata.namespace"
        )

    def test_operator_installer_rbac_is_scoped_not_edit(self):
        """The Install Operator button's ClusterRole must stay minimal -- not the
        broad "edit" role used by clusterWideApply -- since it's granted by default."""
        cluster_roles = self._all("ClusterRole")
        role = next(c for c in cluster_roles if c["metadata"]["name"] == "agentit-operator-installer")
        resources = {
            (group, res)
            for r in role["rules"]
            for group in r["apiGroups"]
            for res in r["resources"]
        }
        assert resources == {
            ("", "namespaces"),
            ("operators.coreos.com", "operatorgroups"),
            ("operators.coreos.com", "subscriptions"),
            ("operators.coreos.com", "clusterserviceversions"),
        }

        # Regression guard: kube.apply_yaml() shells out to `oc apply
        # --server-side`, which always issues a PATCH (even to create an
        # object that doesn't exist yet) -- verified live against the real
        # cluster, "create"+"get" alone still 403s with "cannot patch
        # resource \"namespaces\"". Every resource this ClusterRole creates
        # via that path needs "patch", not just "create".
        namespaces_rule = next(r for r in role["rules"] if "namespaces" in r["resources"])
        assert "patch" in namespaces_rule["verbs"]
        operatorgroups_rule = next(r for r in role["rules"] if "operatorgroups" in r["resources"])
        assert "patch" in operatorgroups_rule["verbs"]

        crbs = self._all("ClusterRoleBinding")
        crb = next(c for c in crbs if c["roleRef"]["name"] == "agentit-operator-installer")
        assert crb["subjects"][0]["name"] == "agentit"
        assert crb["subjects"][0]["namespace"] == "test-ns"

    def test_cluster_wide_apply_defaults_to_true_in_values(self):
        """Regression test: rbac.clusterWideApply previously defaulted to false,
        which left the ClusterRoleBinding above ungranted on real releases. Since
        "Apply to Cluster" onboards apps into namespaces that don't exist yet,
        kube.namespace_exists() 403s on the cluster-scoped namespace GET before
        ever reaching manifest application -- surfacing as "Cluster apply failed
        — check server logs" for any app not already sharing this release's own
        namespace. The template logic (tested above) was already correct; only
        the default value was wrong."""
        values_path = CHART_DIR.parent / "values.yaml"
        values = yaml.safe_load(values_path.read_text())
        assert values["rbac"]["clusterWideApply"] is True


# ---------------------------------------------------------------------------
# Watcher NetworkPolicies (networkpolicy-agents.yaml)
# ---------------------------------------------------------------------------

class TestWatcherNetworkPolicies:
    """Regression coverage for docs/code-review-2026-07-12.md item #5: the
    watcher Deployments (vuln-watcher, slo-tracker, drift-detector,
    skill-learner, capability-scout) previously had no NetworkPolicy at
    all."""

    TEMPLATE = CHART_DIR / "networkpolicy-agents.yaml"

    def _by_name(self) -> dict[str, dict]:
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        return {d["metadata"]["name"]: d for d in docs if d}

    def test_parseable_and_covers_all_five_watchers(self):
        by_name = self._by_name()
        assert set(by_name) == {
            "agentit-vuln-watcher",
            "agentit-slo-tracker",
            "agentit-drift-detector",
            "agentit-skill-learner",
            "agentit-capability-scout",
        }
        for policy in by_name.values():
            assert policy["kind"] == "NetworkPolicy"

    def test_denies_all_ingress(self):
        """These are background pollers, not servers -- nothing should reach them."""
        for name, policy in self._by_name().items():
            assert "Ingress" in policy["spec"]["policyTypes"], name
            assert policy["spec"]["ingress"] == [], name

    def test_podselector_matches_its_own_watcher_deployment(self):
        expected = {
            "agentit-vuln-watcher": "agentit-vuln-watcher",
            "agentit-slo-tracker": "agentit-slo-tracker",
            "agentit-drift-detector": "agentit-drift-detector",
            "agentit-skill-learner": "agentit-skill-learner",
            "agentit-capability-scout": "agentit-capability-scout",
        }
        by_name = self._by_name()
        for name, app_label in expected.items():
            assert by_name[name]["spec"]["podSelector"]["matchLabels"]["app"] == app_label

    def test_capability_scout_has_no_portal_or_kube_api_egress(self):
        """Unlike the other 4 watchers, capability-scout never calls
        kube.py and has no cross-pod draft-push mechanism -- it opens a PR
        directly against the git remote instead."""
        policy = self._by_name()["agentit-capability-scout"]
        for rule in policy["spec"]["egress"]:
            ports = {p.get("port") for p in rule.get("ports", [])}
            assert 8080 not in ports
            assert 6443 not in ports

    def test_egress_allows_dns_and_api_server(self):
        """capability-scout is the one exception -- see
        test_capability_scout_has_no_portal_or_kube_api_egress above: it
        never calls kube.py, so it has no Kubernetes API server egress rule."""
        for name, policy in self._by_name().items():
            if name == "agentit-capability-scout":
                continue
            egress_ports = {
                (rule.get("ports", [{}])[0].get("protocol"), rule.get("ports", [{}])[0].get("port"))
                for rule in policy["spec"]["egress"]
            }
            assert ("TCP", 6443) in egress_ports, name

    def test_dns_rule_has_no_ports_restriction(self):
        """Regression test for the live DNS-blocking incident documented in
        docs/postgres-migration-plan.md: on this cluster's OVN-Kubernetes, a
        namespaceSelector peer combined with a `ports` restriction never
        matches traffic to the DNS ClusterIP Service (dns-default), for
        either port 53 (the Service port) or port 5353 (the actual CoreDNS
        container port). The DNS egress rule must have a namespaceSelector
        peer scoped to openshift-dns and NO `ports` key at all."""
        for name, policy in self._by_name().items():
            dns_rules = [
                rule
                for rule in policy["spec"]["egress"]
                if rule.get("to", [{}])[0].get("namespaceSelector", {}).get("matchLabels", {}).get(
                    "kubernetes.io/metadata.name"
                )
                == "openshift-dns"
            ]
            assert len(dns_rules) == 1, name
            assert "ports" not in dns_rules[0], name


# ---------------------------------------------------------------------------
# skill-learner Deployment (chart/templates/agents/skill-learner.yaml)
# ---------------------------------------------------------------------------

class TestSkillLearnerDeployment:
    """Regression test for the liveness-probe crash-loop shape vuln-watcher
    also hit: skill-learner's 24h tick interval touched /tmp/heartbeat only
    once per tick, which previously had to be papered over by loosening
    this probe's threshold to 172800s (48h). Now that watchers/skill_learner.py's
    run() loop refreshes the heartbeat every HEARTBEAT_REFRESH_SECONDS via
    the shared agentit.watchers.sleep_with_heartbeat helper (same fix
    vuln-watcher.yaml's probe already relies on), the threshold must be
    back down to a real, fast-detecting value -- not silently
    re-loosened."""
    TEMPLATE = CHART_DIR / "agents" / "skill-learner.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "Deployment"
        assert doc["metadata"]["name"] == "agentit-skill-learner"

    def test_liveness_probe_threshold_matches_vuln_watcher(self):
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        command = container["livenessProbe"]["exec"]["command"][-1]
        assert "-lt 900" in command
        assert "172800" not in command


# ---------------------------------------------------------------------------
# All watcher agent Deployments must run as the `agentit` SA, not the
# namespace's bare `default` SA -- see rbac.yaml, which grants that SA
# fleet-wide pod read access and read access to Argo CD Applications in
# openshift-gitops specifically so these watchers can use it. Regression
# guard for a live bug: these five Deployments omitted serviceAccountName
# entirely, so slo-tracker and drift-detector were 403ing on every tick.
# ---------------------------------------------------------------------------

class TestAgentDeploymentsUseTheAgentitServiceAccount:
    AGENT_TEMPLATES = [
        CHART_DIR / "agents" / "vuln-watcher.yaml",
        CHART_DIR / "agents" / "slo-tracker.yaml",
        CHART_DIR / "agents" / "drift-detector.yaml",
        CHART_DIR / "agents" / "skill-learner.yaml",
        CHART_DIR / "agents" / "capability-scout.yaml",
    ]

    def test_all_agent_deployments_set_service_account_name(self):
        for template in self.AGENT_TEMPLATES:
            doc = _load(template)
            pod_spec = doc["spec"]["template"]["spec"]
            assert pod_spec.get("serviceAccountName") == "agentit", (
                f"{template.name}: expected serviceAccountName 'agentit' "
                f"(the release name), got {pod_spec.get('serviceAccountName')!r} "
                "-- without it this pod runs as the namespace's unprivileged "
                "'default' SA instead of the one rbac.yaml actually grants "
                "fleet-wide/Argo CD read access to."
            )


# ---------------------------------------------------------------------------
# capability-scout Deployment (chart/templates/agents/capability-scout.yaml)
# ---------------------------------------------------------------------------

class TestCapabilityScoutDeployment:
    TEMPLATE = CHART_DIR / "agents" / "capability-scout.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "Deployment"
        assert doc["metadata"]["name"] == "agentit-capability-scout"

    def test_invokes_propose_watch_with_configured_flags(self):
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == ["python", "-m", "agentit", "propose-watch"]
        assert "--interval" in container["args"]
        assert "--max-open-prs" in container["args"]

    def test_reads_github_token_secret_optionally(self):
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        env_by_name = {e["name"]: e for e in container["env"] if "name" in e}
        assert "GITHUB_TOKEN" in env_by_name
        secret_ref = env_by_name["GITHUB_TOKEN"]["valueFrom"]["secretKeyRef"]
        assert secret_ref["name"] == "github-token"
        assert secret_ref["optional"] is True

    def test_build_env_tracks_image_tag(self):
        """Regression: live scout pods once kept orphan AGENTIT_IMAGE_TAG /
        GIT_REVISION values that lagged the container image after Argo
        updated image.tag. These must be chart-owned from .Values.image.tag."""
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        env_by_name = {e["name"]: e.get("value") for e in container["env"] if "name" in e}
        assert env_by_name["AGENTIT_GIT_COMMIT"] == "test-image-tag"
        assert env_by_name["AGENTIT_IMAGE_TAG"] == "test-image-tag"
        assert env_by_name["GIT_REVISION"] == "test-image-tag"

    def test_no_persistence_volume(self):
        """Deliberately stateless -- unlike skill-learner, this watcher
        opens PRs against the git remote instead of writing drafts to a
        local PVC."""
        doc = _load(self.TEMPLATE)
        pod_spec = doc["spec"]["template"]["spec"]
        volumes = pod_spec.get("volumes") or []
        assert not any("persistentVolumeClaim" in v for v in volumes)

    def test_empty_dir_tmpdir_for_git_gh_scratch(self):
        """Writable TMPDIR emptyDir for git/gh/py_compile temps; L3 source
        trees stay on the image layer with Containerfile g+w."""
        doc = _load(self.TEMPLATE)
        pod_spec = doc["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in (pod_spec.get("volumes") or [])}
        assert "scout-tmp" in volumes
        assert "emptyDir" in volumes["scout-tmp"]
        container = pod_spec["containers"][0]
        mounts = {m["name"]: m["mountPath"] for m in (container.get("volumeMounts") or [])}
        assert mounts.get("scout-tmp") == "/tmp/agentit-scout"
        env_by_name = {e["name"]: e.get("value") for e in container["env"] if "name" in e}
        assert env_by_name.get("TMPDIR") == "/tmp/agentit-scout"

    def test_has_restrictive_security_context(self):
        doc = _load(self.TEMPLATE)
        pod_spec = doc["spec"]["template"]["spec"]
        assert pod_spec["securityContext"]["runAsNonRoot"] is True
        container = pod_spec["containers"][0]
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]

    def test_has_liveness_probe_via_heartbeat_file(self):
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        assert "heartbeat" in container["livenessProbe"]["exec"]["command"][-1]

    def test_memory_limit_headroom_for_the_tests_pass_gate_subprocess(self):
        """Regression test: at the previous 256Mi (then 1Gi) limit, the
        tests-pass safety gate (capability_scout.py's run_test_suite(),
        which shells out to `python -m pytest tests/ ...` as a subprocess
        of this same container) OOMKilled the pod outright -- confirmed
        live on the real agentit-capability-scout pod, twice (exit 137 at
        256Mi immediately, exit 137 at 1Gi after ~15 minutes of a CPU-
        throttled full-suite run). Must stay comfortably above the portal
        Rollout's 512Mi, which can trigger the same gate via the
        manual-run route."""
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        limit = container["resources"]["limits"]["memory"]
        assert limit.endswith("Gi") or (limit.endswith("Mi") and int(limit[:-2]) >= 512), (
            f"capability-scout memory limit {limit} is too low for the "
            "tests-pass gate's pytest subprocess -- it will OOMKill the pod"
        )

    def test_cpu_limit_headroom_for_the_tests_pass_gate_subprocess(self):
        """Regression test: at 250m CPU, the ~1900-test suite the
        tests-pass gate runs took long enough under throttling (~15
        minutes, confirmed live) that memory climbed past even a raised
        1Gi limit before pytest finished. Needs real CPU, not just memory,
        to finish in a reasonable window."""
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        limit = container["resources"]["limits"]["cpu"]
        millicores = int(float(limit[:-1]) * 1000) if limit.endswith("m") else int(float(limit) * 1000)
        assert millicores >= 1000, (
            f"capability-scout cpu limit {limit} is too low for the "
            "tests-pass gate's pytest subprocess to finish in a reasonable time"
        )


# ---------------------------------------------------------------------------
# Workflow CronJobs (chart/templates/workflows/*.yaml)
# ---------------------------------------------------------------------------

class TestCostReportCronJob:
    """Regression coverage for bug: this CronJob called `watch --cost-report`,
    a CLI flag that never existed (`cli.py`'s `watch()` only ever supported
    `--rescan`/`--dimension`) -- it had presumably never worked. Fixed to
    match the exact working pattern its siblings (compliance-rescan,
    dependency-update) already use."""

    TEMPLATE = CHART_DIR / "workflows" / "cost-report-cronjob.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "CronJob"

    def test_does_not_call_nonexistent_cost_report_flag(self):
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        assert "--cost-report" not in container["command"]

    def test_command_uses_a_real_watch_flag_combination(self):
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        command = container["command"]
        assert command[:4] == ["python", "-m", "agentit", "watch"]
        assert "--rescan" in command
        assert "--dimension" in command


class TestCapabilityScoutDefaultsOff:
    """agents.capabilityScout.enabled must default to false, matching every
    other agent flag's opt-in convention -- this is a live-deployment
    decision for the repo owner to make explicitly (see
    docs/self-improvement-for-agentit.md), never a side effect of shipping
    the feature."""

    def test_defaults_to_disabled_in_values(self):
        values_path = CHART_DIR.parent / "values.yaml"
        values = yaml.safe_load(values_path.read_text())
        assert values["agents"]["capabilityScout"]["enabled"] is False


class TestWorkflowCronJobsShareTheSameRescanPattern:
    """All 3 fleet-rescan CronJobs (compliance-rescan, dependency-update,
    cost-report) should invoke the exact same real, supported command shape
    -- only the --dimension value should differ."""

    TEMPLATES = {
        "compliance": CHART_DIR / "workflows" / "compliance-rescan-cronjob.yaml",
        "dependencies": CHART_DIR / "workflows" / "dependency-update-cronjob.yaml",
        "cost": CHART_DIR / "workflows" / "cost-report-cronjob.yaml",
    }

    def test_all_use_rescan_with_a_dimension(self):
        for dimension, template in self.TEMPLATES.items():
            doc = _load(template)
            container = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
            command = container["command"]
            assert command == ["python", "-m", "agentit", "watch", "--rescan", "--dimension", dimension], template.name
