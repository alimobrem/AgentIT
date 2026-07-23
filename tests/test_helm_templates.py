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
    '{{ .Values.agents.capabilityScout.testPostgres.dsn | default "postgresql://agentit_test:agentit_test@127.0.0.1:5432/agentit_test" | quote }}':
        '"postgresql://agentit_test:agentit_test@127.0.0.1:5432/agentit_test"',
    "{{ .Values.agents.capabilityScout.testPostgres.image | default .Values.postgres.bundled.image | quote }}":
        '"registry.redhat.io/rhel9/postgresql-15@sha256:06aeada2ca417445bc4fb711729e65a02ee78421a09c862cbd136ebdd51d7cfa"',
}


def _render(template_path: Path) -> str:
    """Read a template file and do basic Helm variable substitution."""
    raw = template_path.read_text()
    # Strip {{- /* ... */ -}} Helm comment blocks (e.g. trigger.yaml's
    # webhook-secret setup note) -- these can span multiple lines, so this
    # must run before the single-line if/else/end/range stripping below.
    raw = re.sub(r"[ \t]*\{\{-?\s*/\*.*?\*/\s*-?\}\}\n?", "", raw, flags=re.DOTALL)
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

    def test_repo_url_default_has_no_dotgit_suffix(self):
        """Regression: this default feeds both `git-clone`'s URL param AND
        `register-self-in-fleet`'s webhook body verbatim (the latter never
        normalizes it) -- a `.git`-suffixed default briefly created a
        second, duplicate Fleet row for AgentIT itself (a distinct
        `repo_url` string from every other write path's `.git`-less form)
        before `normalize_repo_url()` (store.py) existed to collapse it.
        Keeping this default pre-normalized closes that specific source
        for good, independent of the general DB-layer/self-healing
        safeguards in store.py."""
        doc = _load(self.TEMPLATE)
        params = {p["name"]: p for p in doc["spec"]["params"]}
        assert "repo-url" in params
        default = params["repo-url"]["default"]
        assert not default.lower().endswith(".git"), (
            f"Pipeline repo-url default must not have a '.git' suffix, got: {default!r}"
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
        assert "oc apply" in script

    def test_notify_argocd_pins_tag_before_applying_application_spec(self):
        """Regression: applying argocd/application.yaml with bootstrap
        image.tag=latest then patching afterward raced Argo selfHeal onto the
        stale :latest digest (scout CrashLoopBackOff). Rewrite REVISION into
        the manifest, then apply once via oc apply — no separate
        update-image-tag step. (kube.apply_yaml notify was reverted after
        tip chart sync + promote stall; see pipeline.yaml notify comments.)"""
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
        assert "oc apply" in script
        assert "kube.apply_yaml" not in script
        assert any(
            e.get("name") == "REVISION" for e in steps["sync-application-spec"].get("env", [])
        ), "sync-application-spec needs REVISION to pin image.tag before apply"
        ws_names = {w["name"] for w in task["taskSpec"]["workspaces"]}
        assert "source" in ws_names
        task_ws_names = {w["name"] for w in task["workspaces"]}
        assert "source" in task_ws_names
        # Proven path: openshift/cli + oc apply (agentit-ci-argocd-patch RBAC).
        assert "openshift/cli" in steps["sync-application-spec"]["image"]

    def test_run_tests_is_hermetic_like_gha(self):
        """Tekton run-tests must set AGENTIT_OFFLINE + KUBECONFIG like GHA."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        step = tasks["run-tests"]["taskSpec"]["steps"][0]
        env = {e["name"]: e["value"] for e in step.get("env", [])}
        assert env.get("AGENTIT_OFFLINE") == "1"
        assert env.get("KUBECONFIG") == "/tmp/nonexistent-path"

    def test_pipeline_has_timeouts(self):
        doc = _load(self.TEMPLATE)
        for task in doc["spec"]["tasks"]:
            assert "timeout" in task, f"Task {task['name']} missing timeout"

    def test_run_tests_timeout_tolerates_ubi_pip_and_retry(self):
        """Cluster UBI pip+pytest is slower than GHA (~3m). A retried
        TaskRun under node pressure hit TaskRunTimeout at 10m
        (agentit-ci-bwb76 on b4ae400f), blocking notify-argocd / image pin.
        Keep headroom at ≥20m so one retry can finish."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        assert tasks["run-tests"]["timeout"] == "20m0s"
        assert tasks["run-tests"].get("retries", 0) >= 1

    def test_smoke_test_image_runs_after_build_before_argocd_notify(self):
        """The image smoke test must gate promotion: it runs after
        build-image produces the real tag, and notify-argocd (which patches
        the live Argo CD Application to that tag) must wait on it -- so a
        broken image (missing pytest/tests/chart/git/agentit.kube, discovered
        live and one at a time this session) can never reach the deployed
        Rollout."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        assert "smoke-test-image" in tasks
        smoke = tasks["smoke-test-image"]
        assert "build-image" in smoke["runAfter"]
        assert "smoke-test-image" in tasks["notify-argocd"]["runAfter"]

    def test_smoke_test_image_checks_every_regressed_tool(self):
        """Each of these was discovered missing from the deployed image one
        at a time, live: a real .git checkout, pytest, tests/, chart/, and
        (now) importable agentit.kube / github_pr — not the gh CLI."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        script = "\n".join(tasks["smoke-test-image"]["taskSpec"]["steps"][0]["args"])
        for expected in (
            "python -m pytest --version",
            "test -d tests",
            "test -d chart",
            "git --version",
            "from agentit import kube",
            "safe.directory",
            "git -C /opt/app-root/src status",
        ):
            assert expected in script, f"smoke-test-image script missing check: {expected!r}"
        assert "gh --version" not in script, "runtime must not require the gh CLI"

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

    def test_notify_argocd_timeout_tolerates_real_scheduling_delays(self):
        """2026-07-17 incident: notify-argocd's pod couldn't schedule (PV
        node-affinity + untolerated taints) and separately hit etcd
        timeouts during Task resolution -- both took longer to clear than
        the old 2m timeout. 5 minutes gives real transient delays room."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        assert tasks["notify-argocd"]["timeout"] == "5m0s"

    def test_notify_argocd_retries_not_bumped_above_one(self):
        """Deliberate: a different task's retry pileup under node-resource
        exhaustion turned one hang into a 10-minute cascading failure this
        same session. More retries on a task that fails due to sustained
        resource pressure compounds that pressure instead of fixing it --
        the toleration fix (trigger.yaml) and the longer timeout above are
        the safe levers here, not additional retries."""
        doc = _load(self.TEMPLATE)
        tasks = {t["name"]: t for t in doc["spec"]["tasks"]}
        assert tasks["notify-argocd"]["retries"] == 1


# ---------------------------------------------------------------------------
# Tekton Pipeline's supersede-stale-runs task: coalesces concurrent pushes
# to the same branch so only the latest commit's PipelineRun matters,
# cancelling still-active OLDER agentit-ci PipelineRuns instead of letting
# every one of them run to completion (the same congestion class as
# ExceededNodeResources retries piling up under many concurrent agents).
# ---------------------------------------------------------------------------

class TestTektonSupersedeStaleRuns:
    TEMPLATE = CHART_DIR / "tekton" / "pipeline.yaml"

    def _tasks(self) -> dict:
        doc = _load(self.TEMPLATE)
        return {t["name"]: t for t in doc["spec"]["tasks"]}

    def test_task_exists_and_runs_before_git_clone(self):
        tasks = self._tasks()
        assert "supersede-stale-runs" in tasks
        assert "supersede-stale-runs" in tasks["git-clone"]["runAfter"]

    def test_task_has_no_runafter_of_its_own(self):
        """Must run first, immediately, before any real pipeline work
        starts -- coalescing is only useful if it happens before git-clone/
        run-tests do wasted work for a run about to be superseded."""
        tasks = self._tasks()
        assert "runAfter" not in tasks["supersede-stale-runs"]

    def _script(self) -> str:
        tasks = self._tasks()
        return tasks["supersede-stale-runs"]["taskSpec"]["steps"][0]["script"]

    def test_script_uses_creationtimestamp_not_listing_order(self):
        """Regression guard: comparing by list order or by name would not
        guarantee "newer supersedes older" -- generateName suffixes are
        random, not chronological. Must compare real creationTimestamps."""
        script = self._script()
        assert "context.pipelineRun.name" in script
        assert "creationTimestamp" in script
        assert 'CREATED" \\< "$SELF_CREATED' in script

    def test_script_never_cancels_a_run_past_build_image(self):
        script = self._script()
        assert 'pipelineTaskName=="build-image"' in script
        assert "Leaving older PipelineRun" in script

    def test_script_skips_already_completed_and_already_cancelling_runs(self):
        script = self._script()
        assert 'if [ -n "$COMPLETED" ]; then continue; fi' in script
        assert "Cancelled|CancelledRunFinally" in script

    def test_script_patches_status_cancelled_with_merge(self):
        script = self._script()
        assert "oc patch pipelinerun" in script
        assert "--type=merge" in script
        assert '{"spec":{"status":"Cancelled"}}' in script

    def test_no_bare_for_loop_over_multi_field_command_substitution(self):
        """Same regression class as TestTektonCleanup's identically-named
        guard: the bulk PipelineRun listing emits 3 space-separated fields
        per item and must be consumed via redirect-to-file + `while read`,
        never a bare `for x in $(...)` (which would word-split the
        NAME/CREATED/COMPLETED pairing apart)."""
        script = self._script()
        listing_lines = [
            line for line in script.splitlines()
            if "jsonpath=" in line and '{" "}' in line
        ]
        assert len(listing_lines) >= 1
        assert "while read -r NAME CREATED COMPLETED" in script

    def _run_supersede_script(self, tmp_path):
        """Execute the real rendered script (Tekton's own
        `$(context.pipelineRun.name)` substituted with a fixed "self-pr",
        the same way the real Tekton controller would substitute it at
        runtime) against a fake `oc` on PATH, against several canned
        PipelineRun fixtures, and return which PipelineRuns got patched."""
        import os
        import subprocess
        import textwrap

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_file = tmp_path / "oc-patches.log"
        fake_oc = bin_dir / "oc"
        fake_oc.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import sys

            args = sys.argv[1:]
            verb = args[0] if args else ""
            resource = args[1] if len(args) > 1 else ""
            log_path = {str(log_file)!r}

            def log(line):
                with open(log_path, "a") as f:
                    f.write(line + "\\n")

            SELF_CREATED = "2026-01-02T00:00:00Z"
            # name -> (createdAt, completionTime, spec.status, build-image started?)
            FIXTURES = {{
                "older-pre-build": ("2026-01-01T00:00:00Z", "", "", False),
                "older-past-build": ("2026-01-01T00:00:00Z", "", "", True),
                "newer-pr": ("2026-01-03T00:00:00Z", "", "", False),
                "completed-pr": ("2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", "", False),
                "already-cancelling-pr": ("2026-01-01T00:00:00Z", "", "Cancelled", False),
            }}

            if verb == "get" and resource == "pipelinerun":
                if len(args) > 2 and not args[2].startswith("-"):
                    name = args[2]
                    joined = " ".join(args)
                    if name == "self-pr":
                        if "creationTimestamp" in joined:
                            print(SELF_CREATED)
                        sys.exit(0)
                    info = FIXTURES.get(name)
                    if info is None:
                        sys.exit(1)
                    _created, _completed, status, build_started = info
                    if "spec.status" in joined:
                        print(status)
                    elif "childReferences" in joined:
                        if build_started:
                            print("build-image")
                    sys.exit(0)
                else:
                    # bulk listing: NAME CREATED COMPLETED, one per line
                    print("self-pr " + SELF_CREATED + " ")
                    for name, (created, completed, _status, _build) in FIXTURES.items():
                        print(name + " " + created + " " + completed)
                    sys.exit(0)
            elif verb == "patch" and resource == "pipelinerun":
                name = args[2] if len(args) > 2 else ""
                log("patch " + name)
                sys.exit(0)
            sys.exit(0)
        """))
        fake_oc.chmod(0o755)

        script = self._script().replace(
            "{{ .Release.Namespace }}", "test-ns"
        ).replace(
            "$(context.pipelineRun.name)", "self-pr"
        )
        result = subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"supersede script exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        patched = log_file.read_text().splitlines() if log_file.exists() else []
        return result, patched

    def test_cancels_older_run_not_yet_at_build_image(self, tmp_path):
        _result, patched = self._run_supersede_script(tmp_path)
        assert any("older-pre-build" in line for line in patched), (
            f"an older, still-active PipelineRun that hasn't reached "
            f"build-image must be superseded; patches: {patched}"
        )

    def test_does_not_cancel_older_run_already_past_build_image(self, tmp_path):
        _result, patched = self._run_supersede_script(tmp_path)
        assert not any("older-past-build" in line for line in patched), (
            f"a PipelineRun that already started build-image must never be "
            f"cancelled -- it may be about to promote a live image; patches: {patched}"
        )

    def test_does_not_cancel_a_newer_run(self, tmp_path):
        """Directionality guard: only strictly OLDER runs are ever
        superseded -- this run must never cancel one created after it."""
        _result, patched = self._run_supersede_script(tmp_path)
        assert not any("newer-pr" in line for line in patched), (
            f"a PipelineRun newer than self must never be cancelled; patches: {patched}"
        )

    def test_does_not_cancel_an_already_completed_run(self, tmp_path):
        _result, patched = self._run_supersede_script(tmp_path)
        assert not any("completed-pr" in line for line in patched), (
            f"a PipelineRun that already finished must never be touched; patches: {patched}"
        )

    def test_does_not_repatch_an_already_cancelling_run(self, tmp_path):
        _result, patched = self._run_supersede_script(tmp_path)
        assert not any("already-cancelling-pr" in line for line in patched), (
            f"a PipelineRun already being cancelled must not be re-patched; patches: {patched}"
        )

    def test_never_cancels_self(self, tmp_path):
        _result, patched = self._run_supersede_script(tmp_path)
        assert not any("self-pr" in line for line in patched)

    def test_reports_exactly_one_supersede_in_output(self, tmp_path):
        result, _patched = self._run_supersede_script(tmp_path)
        assert "Superseded 1 older PipelineRun(s)." in result.stdout

    def test_rbac_grants_patch_on_pipelineruns_to_the_pipeline_sa(self):
        """The supersede task runs as the `pipeline` ServiceAccount (the
        Pipeline's default, same as every other task here) and needs
        `patch` on `pipelineruns.tekton.dev` to cancel a superseded run --
        must come from this chart's own namespace-scoped Role, matching
        the self-containment bar the rest of tekton/rbac.yaml already
        holds itself to."""
        docs = [d for d in yaml.safe_load_all(_render(CHART_DIR / "tekton" / "rbac.yaml")) if d]
        role = next(d for d in docs if d.get("kind") == "Role" and d["metadata"]["name"] == "agentit-ci-cleanup")
        assert role["metadata"].get("namespace"), "must stay a namespaced Role, not a ClusterRole"
        tekton_rule = next(r for r in role["rules"] if r.get("apiGroups") == ["tekton.dev"])
        assert set(tekton_rule["resources"]) >= {"pipelineruns"}
        assert "patch" in tekton_rule["verbs"]


# ---------------------------------------------------------------------------
# Tekton PipelineRun trigger template: chart/templates/tekton/trigger.yaml
# ---------------------------------------------------------------------------

class TestTektonTrigger:
    TEMPLATE = CHART_DIR / "tekton" / "trigger.yaml"

    def _docs(self):
        rendered = _render(self.TEMPLATE)
        return list(yaml.safe_load_all(rendered))

    def test_parseable(self):
        docs = self._docs()
        kinds = {d["kind"] for d in docs if d}
        assert {"TriggerTemplate", "EventListener", "Route"} <= kinds

    def _task_run_specs(self) -> list[dict]:
        docs = self._docs()
        tt = next(d for d in docs if d and d["kind"] == "TriggerTemplate")
        pr_spec = tt["spec"]["resourcetemplates"][0]["spec"]
        return pr_spec["taskRunSpecs"]

    def test_notify_argocd_tolerates_the_control_plane_taint(self):
        """2026-07-17 incident: notify-argocd's pod hit "0/6 nodes
        available: PV node-affinity mismatches + untolerated taints" --
        the `source` workspace's dynamically-provisioned EBS PVC ties the
        whole PipelineRun to a single AWS zone (1 schedulable worker each
        on this cluster), and that one same-zone worker being briefly
        saturated leaves zero viable nodes. Tolerating the control-plane
        taint for this one lightweight, short-lived task gives it a
        same-zone fallback node without touching cluster taints/labels."""
        specs = {s["pipelineTaskName"]: s for s in self._task_run_specs()}
        assert "notify-argocd" in specs, "notify-argocd needs its own taskRunSpecs entry"
        tolerations = specs["notify-argocd"]["podTemplate"]["tolerations"]
        master_toleration = next(
            (t for t in tolerations if t["key"] == "node-role.kubernetes.io/master"), None,
        )
        assert master_toleration is not None
        assert master_toleration["effect"] == "NoSchedule"
        assert master_toleration["operator"] == "Exists"

    def test_build_image_task_run_spec_untouched(self):
        """The notify-argocd podTemplate entry must be additive -- build-image's
        existing stepSpecs compute-resource override (already tuned live,
        see the comment above it) must survive alongside it."""
        specs = {s["pipelineTaskName"]: s for s in self._task_run_specs()}
        assert specs["build-image"]["stepSpecs"][0]["name"] == "build"

    def test_pipelinerun_labeled_for_supersede_selection(self):
        """The Pipeline's supersede-stale-runs task (pipeline.yaml) selects
        other in-flight agentit-ci PipelineRuns via `-l
        tekton.dev/pipeline=agentit-ci` -- every PipelineRun this
        TriggerTemplate creates must carry that label explicitly (not rely
        on Tekton's own implicit labeling, which the supersede task's own
        selector should not have to assume)."""
        docs = self._docs()
        tt = next(d for d in docs if d and d["kind"] == "TriggerTemplate")
        pr_meta = tt["spec"]["resourcetemplates"][0]["metadata"]
        assert pr_meta["labels"]["tekton.dev/pipeline"] == "agentit-ci"

    def _github_push_trigger(self) -> dict:
        docs = self._docs()
        el = next(d for d in docs if d and d["kind"] == "EventListener")
        return next(t for t in el["spec"]["triggers"] if t["name"] == "github-push")

    def test_github_push_trigger_requires_hmac_secret(self):
        """Regression guard: this is AgentIT's *other* GitHub push webhook --
        entirely separate infrastructure from the portal-native
        `/api/webhook/github-push` FastAPI route (see
        TestOauthProxyHealthzBypass above). This one is fronted by its own
        Route (`agentit-ci-webhook`) straight to `el-agentit-ci-listener`,
        never through the app's oauth-proxy sidecar at all -- so its *only*
        auth boundary is this EventListener's `github` ClusterInterceptor
        secretRef, verifying GitHub's HMAC signature against
        `github-webhook-secret`. Without it, anyone who discovers the Route
        URL could POST a fake push event and trigger an arbitrary
        `agentit-ci` PipelineRun (build + image push + notify-argocd) with
        no authentication at all -- confirmed as a real, previously-required
        config (commit `fe69a89`, "Harden CI/CD pipeline: authenticate
        webhook...") that a future refactor of this template could silently
        drop with nothing else to catch it (docs/deployment.md's 2026-07-13
        incident already shows this Secret going missing once, unnoticed
        for hours, from an operational gap -- this guards the template side
        of the same risk)."""
        trigger = self._github_push_trigger()
        github_interceptor = next(
            i for i in trigger["interceptors"] if i["ref"]["name"] == "github"
        )
        secret_ref_param = next(
            p for p in github_interceptor["params"] if p["name"] == "secretRef"
        )
        assert secret_ref_param["value"] == {
            "secretName": "github-webhook-secret",
            "secretKey": "secret",
        }

    def test_github_push_trigger_filters_to_default_branch(self):
        """The `cel` interceptor's `body.ref == 'refs/heads/main'` filter is
        this trigger's only defense against firing a full CI PipelineRun for
        every branch push -- must survive alongside the secretRef guard
        above, not just one or the other."""
        trigger = self._github_push_trigger()
        cel_interceptor = next(i for i in trigger["interceptors"] if i["ref"]["name"] == "cel")
        filter_param = next(p for p in cel_interceptor["params"] if p["name"] == "filter")
        assert filter_param["value"] == "body.ref == 'refs/heads/main'"

    def test_ci_webhook_route_targets_the_listener_service_directly(self):
        """The `agentit-ci-webhook` Route must keep targeting
        `el-agentit-ci-listener` (Tekton Triggers' own EventListener
        Service) -- never the app's own Service/Route, which would put this
        webhook behind the oauth-proxy sidecar and reproduce the exact
        github-push 302 bug `TestOauthProxyHealthzBypass` guards against,
        just for a different webhook."""
        docs = self._docs()
        route = next(d for d in docs if d and d["kind"] == "Route" and d["metadata"]["name"] == "agentit-ci-webhook")
        assert route["spec"]["to"] == {"kind": "Service", "name": "el-agentit-ci-listener"}


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

    def _cleanup_script(self) -> str:
        doc = _load(self.TEMPLATE)
        return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]

    def test_no_multi_field_for_loop_over_command_substitution(self):
        """Regression guard for the real 2026-07-18 incident bug: `for x in
        $(oc get ... -o jsonpath='...{name}{" "}{value}...')` word-splits on
        every space/newline, so each loop iteration only ever sees one
        token -- `awk '{print $2}'` on that single token is always empty,
        so every `[ -z "$VALUE" ] && continue` guard fired on every
        iteration and none of these loops ever deleted anything (confirmed
        live: 100+ un-GC'd PipelineRuns/TaskRuns and dozens of stale Failed
        pods). Every jsonpath template below emits two space-separated
        fields per item, so none of them may be looped over with a bare
        `for x in $(...)` -- must redirect to a file and `while read`."""
        script = self._cleanup_script()
        two_field_jsonpaths = [
            line for line in script.splitlines()
            if "jsonpath=" in line and '{" "}' in line and not line.lstrip().startswith("#")
        ]
        assert len(two_field_jsonpaths) >= 4, (
            "expected to find the failed-pods/orphaned-affinity-pods/"
            "orphaned-affinity-statefulsets/orphaned-jobs jsonpath queries"
        )
        for line in two_field_jsonpaths:
            assert "for " not in line, (
                f"multi-field jsonpath query must not be consumed by a bare "
                f"`for x in $(...)` (word-splits apart from the pairing): {line!r}"
            )

    def _run_cleanup_script(self, tmp_path, namespace: str = "test-ns"):
        """Actually execute the rendered cleanup script (real shell logic,
        not just a string/YAML assertion) against a fake `oc` on PATH that
        serves canned `get` responses and records every `delete` call --
        the strongest regression check that the fixed multi-field loops
        really do delete old-and-only-old resources end to end."""
        import os
        import subprocess
        import textwrap

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_file = tmp_path / "oc-deletes.log"
        fake_oc = bin_dir / "oc"
        fake_oc.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import sys

            args = sys.argv[1:]
            verb = args[0] if args else ""
            resource = args[1] if len(args) > 1 else ""
            log_path = {str(log_file)!r}

            def log(line):
                with open(log_path, "a") as f:
                    f.write(line + "\\n")

            OLD, NEW = "2020-01-01T00:00:00Z", "2099-01-01T00:00:00Z"

            if verb == "get":
                if resource == "pods":
                    joined = " ".join(args)
                    if "phase=Succeeded" in joined:
                        pass  # no succeeded pods in this fixture
                    elif "phase=Failed" in joined:
                        print("old-failed-pod " + OLD)
                        print("new-failed-pod " + NEW)
                    elif "managed-by=tekton-pipelines" in joined:
                        print("orphan-aa-pod orphaned-pr")
                        print("live-aa-pod live-pr")
                elif resource == "pipelinerun":
                    if len(args) > 2 and not args[2].startswith("-"):
                        # existence check: `oc get pipelinerun NAME -n NS -o name`
                        name = args[2]
                        if name == "live-pr":
                            print("pipelinerun.tekton.dev/" + name)
                            sys.exit(0)
                        sys.exit(1)
                    else:
                        print("old-pr " + OLD)
                        print("new-pr " + NEW)
                elif resource == "statefulset":
                    print("orphan-aa-ss orphaned-pr")
                    print("live-aa-ss live-pr")
                elif resource == "pvc":
                    pass  # no orphaned PVCs in this fixture
                elif resource == "jobs":
                    print("old-job " + OLD)
                    print("new-job " + NEW)
                elif resource == "taskrun":
                    # NAME COMPLETED OWNER_PR (OWNER_PR empty => standalone,
                    # i.e. never part of a Pipeline at all)
                    print("old-standalone-tr " + OLD + " ")
                    print("new-standalone-tr " + NEW + " ")
                    print("old-orphaned-tr " + OLD + " gone-pr")
                    print("old-owned-tr " + OLD + " live-pr")
                elif resource == "imagestream":
                    # CREATED TAG -- 10 "recent" tags (within the keep window)
                    # and 2 much older ones that must be pruned once the
                    # ImageStream has more than IMAGE_TAG_KEEP tags.
                    for i in range(1, 11):
                        print("2025-01-%02dT00:00:00Z keep-tag-%02d" % (i, i))
                    print("2020-01-02T00:00:00Z prune-tag-02")
                    print("2020-01-01T00:00:00Z prune-tag-01")
                sys.exit(0)
            elif verb == "delete":
                if resource.startswith(("pod/", "pipelinerun/")):
                    log("delete " + resource)
                else:
                    name = args[2] if len(args) > 2 else ""
                    log("delete " + resource + " " + name)
                sys.exit(0)
            sys.exit(0)
        """))
        fake_oc.chmod(0o755)

        script = self._cleanup_script().replace("{{ .Release.Namespace }}", namespace)
        result = subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"cleanup script exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        deleted = log_file.read_text().splitlines() if log_file.exists() else []
        return result, deleted

    def test_deletes_failed_pods_older_than_one_hour_only(self, tmp_path):
        _result, deleted = self._run_cleanup_script(tmp_path)
        assert any("old-failed-pod" in line for line in deleted), (
            f"expected the >1h-old Failed pod to be deleted; deletes: {deleted}"
        )
        assert not any("new-failed-pod" in line for line in deleted), (
            f"a Failed pod from the far future must never be deleted; deletes: {deleted}"
        )

    def test_deletes_orphaned_affinity_assistant_pods_only(self, tmp_path):
        _result, deleted = self._run_cleanup_script(tmp_path)
        assert any("orphan-aa-pod" in line for line in deleted), (
            f"affinity-assistant pod whose PipelineRun no longer exists must be deleted; deletes: {deleted}"
        )
        assert not any("live-aa-pod" in line for line in deleted), (
            f"affinity-assistant pod for a still-existing PipelineRun must not be deleted; deletes: {deleted}"
        )

    def test_deletes_orphaned_affinity_assistant_statefulsets_only(self, tmp_path):
        _result, deleted = self._run_cleanup_script(tmp_path)
        assert any("orphan-aa-ss" in line for line in deleted)
        assert not any("live-aa-ss" in line for line in deleted)

    def test_deletes_old_pipelineruns_only(self, tmp_path):
        _result, deleted = self._run_cleanup_script(tmp_path)
        assert any("old-pr" in line for line in deleted), (
            f"PipelineRun completed >24h ago must be deleted; deletes: {deleted}"
        )
        assert not any("new-pr" in line for line in deleted)

    def test_deletes_standalone_taskrun_older_than_cutoff(self, tmp_path):
        """Self-containment gap this loop closes: a TaskRun that was never
        part of a Pipeline (no owning PipelineRun at all) has no owner-ref
        GC to rely on -- a cluster-wide TektonConfig pruner covering only
        `pipelinerun` wouldn't touch it either. AgentIT's own CronJob must
        delete it directly once it's past retention."""
        _result, deleted = self._run_cleanup_script(tmp_path)
        assert any("old-standalone-tr" in line for line in deleted), (
            f"standalone TaskRun completed >24h ago must be deleted; deletes: {deleted}"
        )

    def test_does_not_delete_recent_standalone_taskrun(self, tmp_path):
        _result, deleted = self._run_cleanup_script(tmp_path)
        assert not any("new-standalone-tr" in line for line in deleted), (
            f"a standalone TaskRun from the far future must never be deleted; deletes: {deleted}"
        )

    def test_deletes_orphaned_taskrun_whose_owner_pipelinerun_is_gone(self, tmp_path):
        """The other half of the same gap: a TaskRun whose owning
        PipelineRun *reference* still exists in its labels, but that
        PipelineRun object itself is already gone (owner-ref GC delayed or
        stuck -- the exact etcd/control-plane-pressure scenario this
        incident already involved). Must be deleted directly, not left
        waiting on Kubernetes GC to eventually catch up."""
        _result, deleted = self._run_cleanup_script(tmp_path)
        assert any("old-orphaned-tr" in line for line in deleted), (
            f"TaskRun whose owning PipelineRun no longer exists must be deleted; deletes: {deleted}"
        )

    def test_does_not_delete_taskrun_still_owned_by_a_live_pipelinerun(self, tmp_path):
        """A TaskRun still owned by a PipelineRun that has NOT been pruned
        yet is deliberately left alone by this loop -- it cascades away
        naturally once its owning PipelineRun's own turn comes (via the
        loop above, or a later run of this CronJob), so this loop must not
        race ahead and delete task history for a still-tracked run."""
        _result, deleted = self._run_cleanup_script(tmp_path)
        assert not any("old-owned-tr" in line for line in deleted), (
            f"TaskRun owned by a still-existing PipelineRun must not be deleted; deletes: {deleted}"
        )

    def _taskrun_cleanup_section(self) -> str:
        """Extract just the standalone/orphaned-TaskRun loop's own shell
        text, verbatim, from the full rendered script -- used to prove this
        loop is fully self-contained (computes its own cutoff, needs
        nothing set up by any earlier loop) rather than merely passing when
        run as part of the whole script."""
        script = self._cleanup_script()
        start_marker = 'echo "Cleaning standalone/orphaned TaskRuns'
        end_marker = 'echo "Deleted $TR_DELETED standalone/orphaned TaskRuns"'
        start = script.index(start_marker)
        end = script.index(end_marker) + len(end_marker)
        return "set -eu\n" + script[start:end]

    def test_taskrun_cleanup_is_self_contained_independent_of_pipelinerun_loop(self, tmp_path):
        """Regression guard for the actual task requirement: this loop must
        not depend on the PipelineRun-cleanup loop (above it in the script)
        having already run. Extracts and executes *only* the TaskRun
        section in isolation, against a fake `oc` that hard-fails on the
        PipelineRun loop's bulk-listing query shape (`-o jsonpath=...`) --
        proving the section never needs that query to have run, and still
        correctly cleans up standalone/orphaned TaskRuns purely from its own
        per-TaskRun owner-existence check."""
        import os
        import subprocess
        import textwrap

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_file = tmp_path / "oc-deletes.log"
        fake_oc = bin_dir / "oc"
        fake_oc.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import sys

            args = sys.argv[1:]
            verb = args[0] if args else ""
            resource = args[1] if len(args) > 1 else ""
            log_path = {str(log_file)!r}

            def log(line):
                with open(log_path, "a") as f:
                    f.write(line + "\\n")

            OLD = "2020-01-01T00:00:00Z"

            if verb == "get" and resource == "pipelinerun":
                if any("jsonpath=" in a for a in args):
                    # The PipelineRun loop's own bulk-listing query shape --
                    # the isolated TaskRun section must never issue this.
                    sys.stderr.write("unexpected bulk pipelinerun listing call\\n")
                    sys.exit(2)
                name = args[2] if len(args) > 2 else ""
                if name == "live-pr":
                    print("pipelinerun.tekton.dev/" + name)
                    sys.exit(0)
                sys.exit(1)
            elif verb == "get" and resource == "taskrun":
                print("old-standalone-tr " + OLD + " ")
                print("old-orphaned-tr " + OLD + " gone-pr")
                print("old-owned-tr " + OLD + " live-pr")
                sys.exit(0)
            elif verb == "delete":
                name = args[2] if len(args) > 2 else ""
                log("delete " + resource + " " + name)
                sys.exit(0)
            sys.exit(0)
        """))
        fake_oc.chmod(0o755)

        section = self._taskrun_cleanup_section().replace("{{ .Release.Namespace }}", "test-ns")
        result = subprocess.run(
            ["bash", "-c", section],
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"isolated TaskRun-cleanup section exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        deleted = log_file.read_text().splitlines() if log_file.exists() else []
        assert any("old-standalone-tr" in line for line in deleted)
        assert any("old-orphaned-tr" in line for line in deleted)
        assert not any("old-owned-tr" in line for line in deleted)

    def test_deletes_old_orphaned_jobs_only(self, tmp_path):
        _result, deleted = self._run_cleanup_script(tmp_path)
        assert any("old-job" in line for line in deleted), (
            f"agent Job older than 1h must be deleted; deletes: {deleted}"
        )
        assert not any("new-job" in line for line in deleted)

    def test_reports_nonzero_deleted_counts_in_output(self, tmp_path):
        """Regression: before the fix, every 'Deleted N ...' summary line
        printed 0 (or, for failed pods, only counted the unrelated
        succeeded-pods loop) even when stale resources existed."""
        result, _deleted = self._run_cleanup_script(tmp_path)
        assert "Deleted 1 orphaned agent Jobs" in result.stdout
        assert "Deleted 1 orphaned affinity-assistant pods" in result.stdout
        assert "Deleted 1 orphaned affinity-assistant StatefulSets" in result.stdout
        assert "Deleted 1 old PipelineRuns" in result.stdout
        assert "Deleted 2 standalone/orphaned TaskRuns" in result.stdout
        assert "Deleted 2 old image tags" in result.stdout

    def test_prunes_image_tags_beyond_the_keep_window(self, tmp_path):
        """Self-containment gap: `build-image` pushes a new, uniquely-named
        tag to the `agentit` ImageStream on every CI run -- the same
        unbounded-accumulation shape as PipelineRuns/TaskRuns, normally
        bounded on a real OpenShift cluster by a cluster-admin-run `oc adm
        prune images` that a customer's cluster is no more guaranteed to
        have scheduled than the TektonConfig pruner. This loop must prune
        the oldest tags itself once there are more than IMAGE_TAG_KEEP."""
        _result, deleted = self._run_cleanup_script(tmp_path)
        assert any("imagestreamtag agentit:prune-tag-01" in line for line in deleted), (
            f"oldest image tag beyond the keep window must be pruned; deletes: {deleted}"
        )
        assert any("imagestreamtag agentit:prune-tag-02" in line for line in deleted)

    def test_keeps_the_most_recent_image_tags(self, tmp_path):
        _result, deleted = self._run_cleanup_script(tmp_path)
        assert not any("keep-tag" in line for line in deleted), (
            f"the 10 most recent image tags must never be pruned; deletes: {deleted}"
        )

    def test_taskrun_rbac_is_namespaced_and_shipped_by_the_chart_itself(self):
        """The new TaskRun-cleanup loop needs `get`/`list`/`delete` on
        `taskruns.tekton.dev`. This must come from the same namespace-scoped
        Role AgentIT's own chart already creates for the `pipeline`
        ServiceAccount -- not a cluster-scoped grant, and not something a
        customer's cluster-admin has to add by hand -- so the loop's RBAC
        is exactly as self-contained as the loop itself."""
        docs = [d for d in yaml.safe_load_all(_render(CHART_DIR / "tekton" / "rbac.yaml")) if d]
        role = next(d for d in docs if d.get("kind") == "Role" and d["metadata"]["name"] == "agentit-ci-cleanup")
        assert role["metadata"].get("namespace"), "must stay a namespaced Role, not a ClusterRole"
        tekton_rule = next(r for r in role["rules"] if r.get("apiGroups") == ["tekton.dev"])
        assert set(tekton_rule["resources"]) >= {"pipelineruns", "taskruns"}
        assert set(tekton_rule["verbs"]) >= {"get", "list", "delete"}
        assert not any(d.get("kind") == "ClusterRole" for d in docs), (
            "TaskRun cleanup RBAC must stay namespace-scoped, shipped entirely "
            "by this chart -- never a cluster-scoped grant"
        )

    def test_image_tag_pruning_rbac_is_namespaced_and_shipped_by_the_chart_itself(self):
        """Same self-containment requirement for the image-tag-pruning loop:
        `imagestreams` (get) + `imagestreamtags` (delete) must be granted by
        this same namespaced Role, not require a customer's cluster-admin to
        add anything (e.g. the `system:image-pruner` ClusterRole `oc adm
        prune images` needs -- see the doc's note on why that broader,
        cluster-wide blob-GC action is structurally out of reach here)."""
        docs = [d for d in yaml.safe_load_all(_render(CHART_DIR / "tekton" / "rbac.yaml")) if d]
        role = next(d for d in docs if d.get("kind") == "Role" and d["metadata"]["name"] == "agentit-ci-cleanup")
        image_rules = [r for r in role["rules"] if r.get("apiGroups") == ["image.openshift.io"]]
        resources_covered = {res for r in image_rules for res in r["resources"]}
        assert {"imagestreams", "imagestreamtags"} <= resources_covered
        for r in image_rules:
            if r["resources"] == ["imagestreamtags"]:
                assert "delete" in r["verbs"]
            if r["resources"] == ["imagestreams"]:
                assert "get" in r["verbs"]


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

    def test_skips_auth_for_github_push_webhook(self):
        """Regression guard for a live bug: GitHub's own `github-push`
        webhook delivery is external (GitHub can't reach the in-cluster
        Service the other /api/webhook/* routes use) and must go through
        this same oauth-proxy-fronted Route -- without this flag, every
        single delivery attempt got redirected to the OAuth login page
        (302) instead of reaching the app (confirmed live via `gh api
        repos/.../hooks/{id}/deliveries`: 100% failure rate), silently
        starving `check_pending_delivery_verifications()` of the push
        events it needs and leaving deliveries stuck showing "Awaiting
        verification" forever. `/api/webhook/github-push` already has its
        own HMAC-SHA256 signature check as its real auth boundary
        (`_verify_github_signature` in routes/webhooks.py), same as every
        other /api/webhook/* route relies on an internal token instead of
        this proxy -- see docs/deployment.md's "Internal webhook token"
        section."""
        raw = self.TEMPLATE.read_text()
        assert "--skip-auth-regex=^/api/webhook/" in raw

    def test_templates_webhook_insecure_ssl_env_var_from_values(self):
        """Regression guard for the other half of the same live bug:
        `github_pr.py::ensure_webhook()`'s `AGENTIT_WEBHOOK_INSECURE_SSL`
        opt-in override (for clusters, like this one, whose Route serves a
        self-signed ingress cert) only ever reaches the running pod's
        `os.environ` if this Deployment actually templates it from
        `values.yaml` -- `test_portal_pr.py`'s `ensure_webhook` tests set
        the env var directly and so can't catch this template wiring being
        dropped or pointed at the wrong values.yaml key on its own."""
        raw = self.TEMPLATE.read_text()
        assert "- name: AGENTIT_WEBHOOK_INSECURE_SSL" in raw
        assert "value: {{ .Values.env.AGENTIT_WEBHOOK_INSECURE_SSL | default \"\" | quote }}" in raw


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

        # Regression guard: kube.apply_yaml() uses server-side-apply PATCH
        # (even to create an object that doesn't exist yet) -- verified live
        # against the real cluster, "create"+"get" alone still 403s with
        # "cannot patch resource \"namespaces\"". Every resource this
        # ClusterRole creates via that path needs "patch", not just "create".
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

    def test_parseable_and_covers_all_seven_watchers(self):
        by_name = self._by_name()
        assert set(by_name) == {
            "agentit-vuln-watcher",
            "agentit-slo-tracker",
            "agentit-drift-detector",
            "agentit-skill-learner",
            "agentit-capability-scout",
            "agentit-reassess-scheduler",
            "agentit-self-health-check",
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
            "agentit-reassess-scheduler": "agentit-reassess-scheduler",
        }
        by_name = self._by_name()
        for name, app_label in expected.items():
            assert by_name[name]["spec"]["podSelector"]["matchLabels"]["app"] == app_label

    def test_capability_scout_has_no_portal_or_kube_api_egress(self):
        """Unlike the other watchers, capability-scout never calls
        kube.py and has no cross-pod draft-push mechanism -- it opens a PR
        directly against the git remote instead."""
        policy = self._by_name()["agentit-capability-scout"]
        for rule in policy["spec"]["egress"]:
            ports = {p.get("port") for p in rule.get("ports", [])}
            assert 8080 not in ports
            assert 6443 not in ports

    def test_reassess_scheduler_has_portal_egress_but_no_kube_api_egress(self):
        """reassess-scheduler only ever calls back into the portal's own
        /api/webhook/assess -- unlike vuln-watcher/slo-tracker/drift-detector
        it never calls kube.py, so it has no Kubernetes API server egress
        rule (but, unlike capability-scout, it does need the portal Service
        rule)."""
        policy = self._by_name()["agentit-reassess-scheduler"]
        egress_ports = set()
        for rule in policy["spec"]["egress"]:
            for p in rule.get("ports", []):
                egress_ports.add(p.get("port"))
        assert 8080 in egress_ports
        assert 6443 not in egress_ports

    def test_egress_allows_dns_and_api_server(self):
        """capability-scout and reassess-scheduler are the two exceptions
        -- see their own dedicated tests above: neither calls kube.py, so
        neither has a Kubernetes API server egress rule."""
        for name, policy in self._by_name().items():
            if name in ("agentit-capability-scout", "agentit-reassess-scheduler"):
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
        CHART_DIR / "agents" / "reassess-scheduler.yaml",
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

    def _scout_container(self):
        doc = _load(self.TEMPLATE)
        containers = doc["spec"]["template"]["spec"]["containers"]
        by_name = {c["name"]: c for c in containers}
        assert "capability-scout" in by_name
        return by_name["capability-scout"], by_name

    def test_wires_dedicated_test_pg_dsn_not_fleet_db(self):
        """tests-pass must use AGENTIT_TEST_PG_DSN (throwaway), never the
        fleet AGENTIT_DB_DSN — fixtures TRUNCATE every table."""
        scout, _ = self._scout_container()
        env_by_name = {e["name"]: e.get("value") for e in scout["env"] if "name" in e}
        assert "AGENTIT_TEST_PG_DSN" in env_by_name
        dsn = env_by_name["AGENTIT_TEST_PG_DSN"]
        assert "agentit_test" in dsn
        assert "127.0.0.1" in dsn or "localhost" in dsn
        assert "postgres-bundled" not in dsn

    def test_test_postgres_sidecar_present(self):
        """Ephemeral sidecar mirrors Tekton run-tests / GHA services so the
        in-cluster gate can actually execute pytest (no podman in image)."""
        _, by_name = self._scout_container()
        assert "test-postgres" in by_name
        pg = by_name["test-postgres"]
        env_names = {e["name"] for e in pg["env"]}
        assert {"POSTGRESQL_USER", "POSTGRESQL_PASSWORD", "POSTGRESQL_DATABASE"} <= env_names
        assert pg["env"][0]["value"] == "agentit_test" or any(
            e.get("value") == "agentit_test" for e in pg["env"] if e.get("name") == "POSTGRESQL_USER"
        )
        doc = _load(self.TEMPLATE)
        volumes = {v["name"]: v for v in (doc["spec"]["template"]["spec"].get("volumes") or [])}
        assert "scout-test-pg-data" in volumes
        assert "emptyDir" in volumes["scout-test-pg-data"]

    def test_test_postgres_defaults_enabled_in_values(self):
        values = yaml.safe_load((CHART_DIR.parent / "values.yaml").read_text())
        assert values["agents"]["capabilityScout"]["testPostgres"]["enabled"] is True


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


class TestReassessSchedulerDeployment:
    """reassess-scheduler Deployment (chart/templates/agents/reassess-scheduler.yaml)
    -- follows the exact same long-lived-Deployment-watcher pattern as
    drift-detector/skill-learner, not a new mechanism."""

    TEMPLATE = CHART_DIR / "agents" / "reassess-scheduler.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "Deployment"
        assert doc["metadata"]["name"] == "agentit-reassess-scheduler"

    def test_invokes_reassess_watch_with_configured_interval(self):
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == ["python", "-m", "agentit", "reassess-watch"]
        assert "--interval" in container["args"]

    def test_calls_back_into_the_portal_via_internal_webhook_token(self):
        """Every re-assessment goes through the portal's own
        /api/webhook/assess -- not a second, parallel assess pipeline --
        so this Deployment needs the same AGENTIT_PORTAL_URL/
        AGENTIT_INTERNAL_WEBHOOK_TOKEN wiring SkillLearner already uses."""
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        env_by_name = {e["name"]: e for e in container["env"] if "name" in e}
        assert "AGENTIT_PORTAL_URL" in env_by_name
        assert env_by_name["AGENTIT_INTERNAL_WEBHOOK_TOKEN"]["valueFrom"]["secretKeyRef"]["optional"] is True

    def test_has_liveness_probe_via_heartbeat_file(self):
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        assert "heartbeat" in container["livenessProbe"]["exec"]["command"][-1]

    def test_has_restrictive_security_context(self):
        doc = _load(self.TEMPLATE)
        pod_spec = doc["spec"]["template"]["spec"]
        assert pod_spec["securityContext"]["runAsNonRoot"] is True
        container = pod_spec["containers"][0]
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]

    def test_defaults_to_disabled_in_values(self):
        """Off by default like every other agent flag -- an operator must
        opt in before any app's saved cadence actually causes an automatic
        re-assessment."""
        values_path = CHART_DIR.parent / "values.yaml"
        values = yaml.safe_load(values_path.read_text())
        assert values["agents"]["reassessScheduler"]["enabled"] is False
        assert values["agents"]["reassessScheduler"]["interval"] == 3600


class TestSelfHealthCheckDeployment:
    """self-health-check Deployment (chart/templates/agents/self-health-check.yaml)
    -- follows the exact same long-lived-Deployment-watcher pattern as
    drift-detector/reassess-scheduler, not a new mechanism. See
    docs/self-health-check-backlog.md for the design rationale."""

    TEMPLATE = CHART_DIR / "agents" / "self-health-check.yaml"

    def test_parseable(self):
        doc = _load(self.TEMPLATE)
        assert doc["kind"] == "Deployment"
        assert doc["metadata"]["name"] == "agentit-self-health-check"

    def test_invokes_self_health_watch_with_configured_interval(self):
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == ["python", "-m", "agentit", "self-health-watch"]
        assert "--interval" in container["args"]

    def test_reuses_the_shared_service_account_no_new_rbac(self):
        """This watcher only needs the RBAC every other watcher's SA
        already has (-argocd-read + the namespace-scoped edit ClusterRole)
        -- must not introduce a dedicated ServiceAccount/Role."""
        doc = _load(self.TEMPLATE)
        pod_spec = doc["spec"]["template"]["spec"]
        # HELM_VARS substitutes `{{ .Release.Name }}` -> "agentit" for
        # rendering purposes -- same value every other agent template test
        # in this file resolves to.
        assert pod_spec["serviceAccountName"] == "agentit"

    def test_github_token_is_optional(self):
        """Needed for the webhook-reachability check, but must degrade
        gracefully (that one check reports inconclusive) rather than
        block the whole watcher when unset."""
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        env_by_name = {e["name"]: e for e in container["env"] if "name" in e}
        assert env_by_name["GITHUB_TOKEN"]["valueFrom"]["secretKeyRef"]["optional"] is True

    def test_has_liveness_probe_via_heartbeat_file(self):
        doc = _load(self.TEMPLATE)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        assert "heartbeat" in container["livenessProbe"]["exec"]["command"][-1]

    def test_has_restrictive_security_context(self):
        doc = _load(self.TEMPLATE)
        pod_spec = doc["spec"]["template"]["spec"]
        assert pod_spec["securityContext"]["runAsNonRoot"] is True
        container = pod_spec["containers"][0]
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]

    def test_defaults_to_disabled_in_values(self):
        """Off by default like every other agent flag -- enabled on the
        live deployment via argocd/application.yaml instead."""
        values_path = CHART_DIR.parent / "values.yaml"
        values = yaml.safe_load(values_path.read_text())
        assert values["agents"]["selfHealthCheck"]["enabled"] is False
        assert values["agents"]["selfHealthCheck"]["interval"] == 900


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
