# tests/test_real_repos.py
"""
Integration tests against real open source repos.
Run with: pytest tests/test_real_repos.py --run-real-repos -v -s
"""
import shutil

import pytest

from agentit.cloner import clone_repo
from agentit.reporter import render_terminal_report
from agentit.runner import run_assessment

REPOS = [
    {
        "url": "https://github.com/go-gitea/gitea.git",
        "expected_lang": "go",
        "expected_fw": None,
        "min_security_score": 0,
        "max_security_score": 60,
    },
    {
        "url": "https://github.com/makeplane/plane.git",
        "expected_lang": "typescript",
        "expected_fw": None,
        "min_security_score": 0,
        "max_security_score": 80,
    },
    {
        "url": "https://github.com/spring-projects/spring-petclinic.git",
        "expected_lang": "java",
        "expected_fw": "spring boot",
        "min_security_score": 0,
        "max_security_score": 80,
    },
]


@pytest.mark.real_repo
@pytest.mark.parametrize(
    "repo_config",
    REPOS,
    ids=[r["url"].split("/")[-1].replace(".git", "") for r in REPOS],
)
def test_real_repo_assessment(repo_config, tmp_path):
    repo_path = clone_repo(repo_config["url"], target_dir=tmp_path / "repo")
    try:
        report = run_assessment(repo_path, repo_url=repo_config["url"], criticality="high")

        lang_names = [l.name for l in report.stack.languages]
        assert repo_config["expected_lang"] in lang_names, (
            f"Expected {repo_config['expected_lang']} in {lang_names}"
        )

        if repo_config["expected_fw"]:
            fw_names = [f.name for f in report.stack.frameworks]
            assert repo_config["expected_fw"] in fw_names, (
                f"Expected {repo_config['expected_fw']} in {fw_names}"
            )

        assert len(report.scores) == 7

        security_score = next(s for s in report.scores if s.dimension == "security")
        assert repo_config["min_security_score"] <= security_score.score <= repo_config["max_security_score"]

        terminal_output = render_terminal_report(report)
        assert "ENTERPRISE READINESS ASSESSMENT" in terminal_output

        print(terminal_output)
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)
