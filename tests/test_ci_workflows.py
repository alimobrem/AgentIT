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
        at a time, live: gh, a real .git checkout, pytest, tests/, chart/."""
        doc = _load("tests.yml")
        steps = doc["jobs"]["image-smoke-test"]["steps"]
        smoke_step = next(s for s in steps if "docker run" in s.get("run", ""))
        script = smoke_step["run"]
        for expected in (
            "python -m pytest --version",
            "test -d tests",
            "test -d chart",
            "git --version",
            "gh --version",
            "safe.directory",
            "git -C /opt/app-root/src status",
        ):
            assert expected in script, f"image-smoke-test script missing check: {expected!r}"

    def test_smoke_test_step_runs_after_build(self):
        doc = _load("tests.yml")
        steps = doc["jobs"]["image-smoke-test"]["steps"]
        run_steps = [s.get("run", "") for s in steps if "run" in s]
        build_idx = next(i for i, s in enumerate(run_steps) if "docker build" in s)
        smoke_idx = next(i for i, s in enumerate(run_steps) if "docker run" in s)
        assert build_idx < smoke_idx
