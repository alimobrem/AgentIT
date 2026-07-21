"""Regression tests for .github/workflows/*.yml -- specifically the
image-smoke-test job that builds the real Containerfile image and asserts
its contents, mirroring chart/templates/tekton/pipeline.yaml's
smoke-test-image task for the live-deploy path (see test_helm_templates.py's
TestTektonPipeline for that one)."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name: str) -> dict:
    raw = (WORKFLOWS_DIR / name).read_text()
    doc = yaml.safe_load(raw)
    assert doc is not None, f"{name} rendered to empty YAML"
    return doc


class TestImageSmokeTestJob:
    def test_parseable(self):
        doc = _load("tests.yml")
        # PyYAML parses the bare `on:` key as boolean True -- harmless here,
        # we only need "jobs" to exist and parse cleanly.
        assert "jobs" in doc

    def test_job_present(self):
        doc = _load("tests.yml")
        assert "image-smoke-test" in doc["jobs"]

    def test_builds_the_real_containerfile(self):
        doc = _load("tests.yml")
        steps = doc["jobs"]["image-smoke-test"]["steps"]
        build_step = next(s for s in steps if "docker build" in s.get("run", ""))
        assert "-f Containerfile" in build_step["run"]

    def test_checks_every_regressed_tool(self):
        """Each of these was discovered missing from the deployed image one
        at a time, live: a real .git checkout, pytest, tests/, chart/, and
        (now) importable agentit.kube / github_pr — not the gh CLI.
        Keep in sync with Tekton smoke-test-image (test_helm_templates)."""
        doc = _load("tests.yml")
        steps = doc["jobs"]["image-smoke-test"]["steps"]
        smoke_step = next(s for s in steps if "docker run" in s.get("run", ""))
        script = smoke_step["run"]
        for expected in (
            "python -m pytest --version",
            "test -d tests",
            "test -d chart",
            "git --version",
            "from agentit import kube",
            "safe.directory",
            "git -C /opt/app-root/src status",
        ):
            assert expected in script, f"image-smoke-test script missing check: {expected!r}"
        assert "gh --version" not in script, "runtime must not require the gh CLI"

    def test_smoke_test_step_runs_after_build(self):
        doc = _load("tests.yml")
        steps = doc["jobs"]["image-smoke-test"]["steps"]
        run_steps = [s.get("run", "") for s in steps if "run" in s]
        build_idx = next(i for i, s in enumerate(run_steps) if "docker build" in s)
        smoke_idx = next(i for i, s in enumerate(run_steps) if "docker run" in s)
        assert build_idx < smoke_idx


class TestContainerfileSmokeToolingDrift:
    """Fail GHA if tip smoke checks and Containerfile tooling diverge.

    2026-07-21 incident: #125 removed ``gh`` from the Containerfile while the
    *live* Tekton Pipeline still ran ``gh --version`` in smoke-test-image.
    Tip chart (no gh check) never reached the cluster because smoke failed
    before notify-argocd — portal stuck on the last good image. Tip GHA/Tekton
    smoke must stay aligned and must not require gh; Containerfile still
    installs gh as break-glass/bootstrap for lagging live Pipelines.
    """

    _ROOT = Path(__file__).resolve().parent.parent

    def _containerfile(self) -> str:
        return (self._ROOT / "Containerfile").read_text(encoding="utf-8")

    def _gha_smoke_script(self) -> str:
        doc = _load("tests.yml")
        steps = doc["jobs"]["image-smoke-test"]["steps"]
        return next(s["run"] for s in steps if "docker run" in s.get("run", ""))

    def _tekton_smoke_script(self) -> str:
        # Text slice is enough for drift asserts; full Helm render lives in
        # test_helm_templates.TestTektonPipeline.
        raw = (self._ROOT / "chart/templates/tekton/pipeline.yaml").read_text()
        start = raw.index("name: smoke-test-image")
        end = raw.index("name: notify-argocd", start)
        return raw[start:end]

    def test_tip_smokes_do_not_require_gh(self):
        assert "gh --version" not in self._gha_smoke_script()
        assert "gh --version" not in self._tekton_smoke_script()

    def test_if_any_tip_smoke_requires_gh_containerfile_installs_it(self):
        """Generic drift: tip ``gh --version`` smoke implies Containerfile installs gh."""
        scripts = self._gha_smoke_script() + "\n" + self._tekton_smoke_script()
        cf = self._containerfile()
        if "gh --version" in scripts:
            assert "dnf install -y gh" in cf or "install -y gh" in cf, (
                "Tip smoke requires gh --version but Containerfile does not "
                "install gh — smoke-test-image will fail before notify-argocd"
            )

    def test_containerfile_keeps_gh_for_live_pipeline_bootstrap(self):
        """Bootstrap pin: image must satisfy live ``gh --version`` smoke.

        Tip smoke must not require gh (see test_tip_smokes_do_not_require_gh),
        but a lagging live Pipeline that still runs ``gh --version`` needs
        something at /usr/local/bin/gh (real CLI or shim) or promotion never
        lands. Prefer the shim (no cli.github.com egress during buildah).
        """
        cf = self._containerfile()
        has_shim = "agentit-shim" in cf and "/usr/local/bin/gh" in cf
        has_rpm = "dnf install -y gh" in cf or "install -y gh" in cf
        assert has_shim or has_rpm, (
            "Containerfile must ship gh --version (shim or RPM) for live "
            "Tekton smoke lag (chicken-and-egg after #125). See README "
            "'Image promotion / Tekton CI'."
        )
