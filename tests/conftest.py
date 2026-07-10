from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-real-repos", action="store_true", default=False, help="Run tests against real repos")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-real-repos"):
        skip = pytest.mark.skip(reason="needs --run-real-repos flag")
        for item in items:
            if "real_repo" in item.keywords:
                item.add_marker(skip)


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
