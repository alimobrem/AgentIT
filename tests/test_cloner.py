# tests/test_cloner.py
import subprocess
from pathlib import Path

import pytest

from agentit.cloner import clone_repo, CloneError


@pytest.fixture
def local_git_repo(tmp_path: Path) -> str:
    """Create a minimal local git repo to clone from."""
    repo_dir = tmp_path / "source_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    return str(repo_dir)


def test_clone_repo_creates_directory(local_git_repo: str, tmp_path: Path):
    target = tmp_path / "cloned"
    result = clone_repo(local_git_repo, target_dir=target)
    assert result.exists()
    assert (result / "README.md").exists()


def test_clone_repo_auto_target(local_git_repo: str):
    result = clone_repo(local_git_repo)
    try:
        assert result.exists()
        assert (result / "README.md").exists()
    finally:
        import shutil
        shutil.rmtree(result, ignore_errors=True)


def test_clone_repo_invalid_url_raises(tmp_path: Path):
    with pytest.raises(CloneError):
        clone_repo("https://invalid.example.com/no/repo.git", target_dir=tmp_path / "bad")
