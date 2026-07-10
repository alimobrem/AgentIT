from pathlib import Path

import pytest


@pytest.fixture
def create_mock_repo(tmp_path: Path):
    """Create a mock repo directory with specified files and contents."""
    def _create(files: dict[str, str]) -> Path:
        repo_dir = tmp_path / "mock_repo"
        repo_dir.mkdir(exist_ok=True)
        for filepath, content in files.items():
            full_path = repo_dir / filepath
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
        return repo_dir
    return _create
