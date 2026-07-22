"""RepoSnapshot single-pass + concurrent analyzer regression tests."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agentit.analyzers.base import iter_text_files
from agentit.analyzers.snapshot import RepoSnapshot, get_active_snapshot, use_snapshot
from agentit.runner import run_assessment


def _tiny_repo(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (tmp_path / "deploy.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: demo\n"
    )
    (tmp_path / "big.bin").write_bytes(b"x" * 10)
    return tmp_path


def test_snapshot_build_indexes_text_and_skips_oversized(tmp_path):
    repo = _tiny_repo(tmp_path)
    huge = repo / "vendor_blob.py"
    huge.write_text("x" * 600_000)
    snap = RepoSnapshot.build(repo, max_file_bytes=512_000)
    assert "app.py" in snap.files
    assert "deploy.yaml" in snap.files
    assert "vendor_blob.py" not in snap.files
    assert snap.skipped_oversized == 1


def test_iter_text_files_uses_active_snapshot(tmp_path):
    repo = _tiny_repo(tmp_path)
    snap = RepoSnapshot.build(repo)
    with use_snapshot(snap):
        assert get_active_snapshot() is snap
        pairs = list(iter_text_files(repo, {".py"}))
    assert any(p.name == "app.py" for p, _ in pairs)
    # Without snapshot, still works.
    assert list(iter_text_files(repo, {".py"}))


def test_run_assessment_uses_single_build_and_stable_dimensions(tmp_path):
    repo = _tiny_repo(tmp_path)
    builds: list[Path] = []
    real_build = RepoSnapshot.build

    def _counting_build(path, **kwargs):
        builds.append(path)
        return real_build(path, **kwargs)

    with patch("agentit.runner.RepoSnapshot.build", side_effect=_counting_build):
        report = run_assessment(repo, "https://github.com/t/demo", criticality="low")
    assert len(builds) == 1
    dims = [s.dimension for s in report.scores]
    assert dims == [
        "security", "observability", "cicd", "infrastructure",
        "compliance", "data_governance", "ha_dr",
    ]
    # Same repo twice → same overall score (determinism under concurrency).
    report2 = run_assessment(repo, "https://github.com/t/demo", criticality="low")
    assert report.overall_score == report2.overall_score
