from __future__ import annotations

from agentit.dependency_manager import classify_update, evaluate_pr, DependencyUpdate


class TestDependencyManager:
    def test_classify_patch(self):
        utype, risk = classify_update("lodash", "4.17.20", "4.17.21")
        assert utype == "patch"
        assert risk == "low"

    def test_classify_minor(self):
        utype, risk = classify_update("react", "18.2.0", "18.3.0")
        assert utype == "minor"
        assert risk == "medium"

    def test_classify_major(self):
        utype, risk = classify_update("express", "4.18.2", "5.0.0")
        assert utype == "major"
        assert risk == "high"

    def test_classify_with_v_prefix(self):
        utype, risk = classify_update("pkg", "v1.2.3", "v1.2.4")
        assert utype == "patch"

    def test_evaluate_renovate_pr(self):
        update = evaluate_pr(
            "Update dependency lodash to v4.17.21",
            "Updates lodash from 4.17.20 to 4.17.21",
            "https://github.com/org/repo/pull/42",
        )
        assert update is not None
        assert update.name == "lodash"
        assert update.update_type == "patch"
        assert update.auto_mergeable is True

    def test_evaluate_bump_pr(self):
        update = evaluate_pr(
            "Bump express from 4.18.2 to 5.0.0",
            "",
            "https://github.com/org/repo/pull/43",
        )
        assert update is not None
        assert update.update_type == "major"
        assert update.auto_mergeable is False

    def test_evaluate_non_dep_pr(self):
        update = evaluate_pr("Add new feature", "", "https://github.com/org/repo/pull/1")
        assert update is None

    def test_evaluate_chore_deps_pr(self):
        update = evaluate_pr(
            "chore(deps): update typescript to v5.3.0",
            "",
            "https://github.com/org/repo/pull/44",
        )
        assert update is not None
        assert update.name == "typescript"
