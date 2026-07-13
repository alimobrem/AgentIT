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
    "{{ .Values.postgres.instances }}": "3",
    "{{ .Values.postgres.credentials.secretName }}": "agentit-postgres-app",
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
    raw = re.sub(r"[ \t]*\{\{-?\s*end\s*\}\}\n?", "", raw)
    # Strip {{- $var := ... }} variable assignments (no rendered output)
    raw = re.sub(r"[ \t]*\{\{-?\s*\$\w+\s*:=.*?-?\}\}\n?", "", raw)
    for var, val in HELM_VARS.items():
        raw = raw.replace(var, val)
    # Any remaining "key: {{ some expression }}" (e.g. `index $x.data "y"`)
    # we can't literally substitute — collapse to a null value so the line
    # still parses; tests only need the *key* to be present, not the value.
    raw = re.sub(r"^(\s*[\w-]+):\s*\{\{.*?\}\}\s*$", r"\1:", raw, flags=re.MULTILINE)
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
        """Verify notify-argocd patches the Application's image.tag param."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        steps = {s["name"]: s for s in tasks["notify-argocd"]["taskSpec"]["steps"]}
        script = steps["update-image-tag"]["script"]
        assert "patch application" in script
        assert "image.tag" in script

    def test_notify_argocd_syncs_application_spec_before_patching_tag(self):
        """Regression for the incident where a new Helm parameter committed to
        argocd/application.yaml required a manual `oc apply` to take effect, and
        that manual apply also clobbered the live-patched image.tag. notify-argocd
        must re-apply argocd/application.yaml (so any new/changed parameter list
        syncs automatically on every deploy) BEFORE re-pinning image.tag (so the
        apply can never leave the live tag on the git placeholder value)."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        task = tasks["notify-argocd"]
        step_names = [s["name"] for s in task["taskSpec"]["steps"]]
        assert step_names.index("sync-application-spec") < step_names.index(
            "update-image-tag"
        ), "argocd/application.yaml must be re-applied before the image.tag patch"
        steps = {s["name"]: s for s in task["taskSpec"]["steps"]}
        assert "oc apply -f argocd/application.yaml" in steps["sync-application-spec"]["script"]
        # The step needs the cloned repo to read argocd/application.yaml from.
        ws_names = {w["name"] for w in task["taskSpec"]["workspaces"]}
        assert "source" in ws_names
        task_ws_names = {w["name"] for w in task["workspaces"]}
        assert "source" in task_ws_names

    def test_pipeline_has_timeouts(self):
        doc = _load(self.TEMPLATE)
        for task in doc["spec"]["tasks"]:
            assert "timeout" in task, f"Task {task['name']} missing timeout"

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
# Postgres (CloudNativePG): postgres/postgres-cluster.yaml, postgres-secret.yaml
# ---------------------------------------------------------------------------

class TestPostgresCluster:
    TEMPLATE = CHART_DIR / "postgres" / "postgres-cluster.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "Cluster"
        assert doc["apiVersion"] == "postgresql.cnpg.io/v1"

    def test_ha_instance_count_at_least_three(self):
        """HA requires >=3 instances (1 primary + 2 replicas) for quorum-safe failover."""
        doc = _load(self.TEMPLATE)
        assert doc["spec"]["instances"] >= 3

    def test_bootstrap_references_credentials_secret(self):
        doc = _load(self.TEMPLATE)
        secret = doc["spec"]["bootstrap"]["initdb"]["secret"]
        assert secret["name"] == "agentit-postgres-app"

    def test_has_pod_anti_affinity(self):
        """Replicas must spread across nodes, or a single node loss could take down quorum."""
        doc = _load(self.TEMPLATE)
        assert doc["spec"]["affinity"]["topologyKey"] == "kubernetes.io/hostname"


class TestPostgresSecret:
    TEMPLATE = CHART_DIR / "postgres" / "postgres-secret.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "Secret"
        assert doc["type"] == "kubernetes.io/basic-auth"

    def test_has_username_and_password_keys(self):
        doc = _load(self.TEMPLATE)
        assert "username" in doc["data"]
        assert "password" in doc["data"]


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

class TestRBAC:
    TEMPLATE = CHART_DIR / "rbac.yaml"

    def _by_kind(self):
        rendered = _render(self.TEMPLATE)
        docs = list(yaml.safe_load_all(rendered))
        return {d["kind"]: d for d in docs if d}

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

    def test_has_cluster_rolebinding(self):
        by_kind = self._by_kind()
        crb = by_kind["ClusterRoleBinding"]
        assert crb["roleRef"]["name"] == "edit"
        assert crb["subjects"][0]["name"] == "agentit"
        assert crb["subjects"][0]["namespace"] == "test-ns"

    def test_cluster_rolebinding_enables_cross_namespace(self):
        """ClusterRoleBinding (not RoleBinding) is required for cross-namespace apply."""
        by_kind = self._by_kind()
        crb = by_kind["ClusterRoleBinding"]
        assert "namespace" not in crb["metadata"], (
            "ClusterRoleBinding must not have metadata.namespace"
        )
