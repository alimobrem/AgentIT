"""Durable rejection/pre-merge-edit capture (pr_outcomes.py) -- the data-
capture requirement folded into the gates-removal work: a PR's real
rejection reason and any pre-merge edit must be captured as real, queryable
DB rows, not thrown away after being displayed once."""
from __future__ import annotations

from unittest.mock import MagicMock

from agentit.portal import pr_outcomes
from agentit.portal.github_pr import get_pr_extra_commits

from conftest import make_report, make_store


class TestGetPrExtraCommits:
    def test_single_commit_returns_empty(self, monkeypatch):
        import requests

        def fake_get(url, headers=None, timeout=None, params=None):
            resp = MagicMock()
            resp.raise_for_status = lambda: None
            resp.json = lambda: [{"sha": "abc123", "commit": {"message": "feat: agentit", "author": {"name": "agentit-bot"}}}]
            return resp

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        assert get_pr_extra_commits("https://github.com/o/r/pull/1") == []

    def test_extra_commit_returns_its_own_diff(self, monkeypatch):
        import requests

        calls = []

        def fake_get(url, headers=None, timeout=None, params=None):
            calls.append(url)
            resp = MagicMock()
            resp.raise_for_status = lambda: None
            if url.endswith("/commits") and params is not None:
                resp.json = lambda: [
                    {"sha": "abc123", "commit": {"message": "feat: agentit", "author": {"name": "agentit-bot"}}},
                    {"sha": "def456", "commit": {"message": "human tweak", "author": {"name": "a-human"}}},
                ]
            else:
                resp.json = lambda: {
                    "files": [{"filename": "app.yaml", "additions": 2, "deletions": 1, "patch": "@@ -1 +1,2 @@"}],
                }
            return resp

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        extra = get_pr_extra_commits("https://github.com/o/r/pull/1")
        assert len(extra) == 1
        assert extra[0]["sha"] == "def456"
        assert extra[0]["author"] == "a-human"
        assert extra[0]["files"][0]["filename"] == "app.yaml"

    def test_non_pr_url_returns_empty(self):
        assert get_pr_extra_commits("https://github.com/o/r/compare/some-branch") == []

    def test_failure_returns_empty_never_raises(self, monkeypatch):
        import requests

        def fake_get(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        assert get_pr_extra_commits("https://github.com/o/r/pull/1") == []


class TestSyncPrOutcomesRejection:
    async def test_closed_pr_records_reject_reason_from_comments(self):
        store = await make_store()
        aid = await store.save(make_report(repo_name="reject-app"))
        await store.save_onboarding(aid, [
            {"category": "skills", "path": "reject-app-add-networkpolicy.yaml", "content": "x", "description": "d"},
        ])

        record = {
            "pr_url": "https://github.com/org/reject-app-gitops/pull/5",
            "state": "closed", "assessment_id": aid, "app_name": "reject-app",
            "category": "cluster_config",
        }
        newly = await pr_outcomes.sync_pr_outcomes(
            store, [record],
            get_status=lambda url: {"labels": [], "body": ""},
            get_extra_commits=lambda url: [],
            get_comments=lambda url: ["Closing -- this duplicates existing functionality."],
        )
        assert len(newly) == 1
        assert newly[0]["outcome"] == pr_outcomes.OUTCOME_REJECTED
        assert newly[0]["reject_reason"] == "duplicate"

        stored = await store.get_pr_outcome(record["pr_url"])
        assert stored is not None
        assert stored["outcome"] == "rejected"
        assert stored["reject_reason"] == "duplicate"
        assert stored["app_name"] == "reject-app"
        assert stored["category"] == "cluster_config"
        assert stored["skill_names"] == ["add-networkpolicy"]

    async def test_rejection_writes_agent_feedback_for_get_rejection_count(self):
        """webhooks.py's auto-fixable dispatch loop still reads
        get_rejection_count() (agent_feedback) to back off a category
        rejected 3+ times -- this must keep receiving real signal now that
        the rejection source is a real closed PR, not a gate-reject click."""
        store = await make_store()
        aid = await store.save(make_report(repo_name="feedback-app"))
        record = {
            "pr_url": "https://github.com/org/feedback-app-gitops/pull/9",
            "state": "closed", "assessment_id": aid, "app_name": "feedback-app",
            "category": "cluster_config",
        }
        await pr_outcomes.sync_pr_outcomes(
            store, [record],
            get_status=lambda url: {"labels": [], "body": ""},
            get_extra_commits=lambda url: [],
            get_comments=lambda url: ["wontfix for now"],
        )
        assert await store.get_rejection_count("feedback-app", "cluster_config") == 1

    async def test_already_recorded_pr_is_never_re_detected(self):
        """The one batched pr_outcomes_recorded_for() check must stop a
        second sync pass from re-running the real GitHub calls (comments/
        extra-commits) for a PR whose outcome is already durably recorded."""
        store = await make_store()
        aid = await store.save(make_report(repo_name="dedup-app"))
        record = {
            "pr_url": "https://github.com/org/dedup-app-gitops/pull/2",
            "state": "closed", "assessment_id": aid, "app_name": "dedup-app",
            "category": "cluster_config",
        }
        comments_calls = []

        def get_comments(url):
            comments_calls.append(url)
            return ["wontfix"]

        first = await pr_outcomes.sync_pr_outcomes(
            store, [record], get_status=lambda url: {"labels": [], "body": ""},
            get_extra_commits=lambda url: [], get_comments=get_comments,
        )
        second = await pr_outcomes.sync_pr_outcomes(
            store, [record], get_status=lambda url: {"labels": [], "body": ""},
            get_extra_commits=lambda url: [], get_comments=get_comments,
        )
        assert len(first) == 1
        assert second == []
        assert len(comments_calls) == 1


class TestSyncPrOutcomesEditedBeforeMerge:
    async def test_merged_pr_with_extra_commits_records_edit(self):
        store = await make_store()
        aid = await store.save(make_report(repo_name="edited-app"))
        record = {
            "pr_url": "https://github.com/org/edited-app-gitops/pull/3",
            "state": "merged", "assessment_id": aid, "app_name": "edited-app",
            "category": "cluster_config",
        }
        extra_commit = {
            "sha": "abcd", "message": "tighten resource limits", "author": "a-human",
            "files": [{"filename": "deploy.yaml", "additions": 1, "deletions": 1, "patch": "@@ -1 +1 @@"}],
        }
        newly = await pr_outcomes.sync_pr_outcomes(
            store, [record],
            get_status=lambda url: {},
            get_extra_commits=lambda url: [extra_commit],
            get_comments=lambda url: [],
        )
        assert len(newly) == 1
        assert newly[0]["outcome"] == pr_outcomes.OUTCOME_EDITED_BEFORE_MERGE

        stored = await store.get_pr_outcome(record["pr_url"])
        assert stored["outcome"] == "edited_before_merge"
        assert stored["edit_diff"][0]["sha"] == "abcd"

    async def test_merged_pr_with_no_extra_commits_records_nothing(self):
        """Shipped exactly as proposed -- not an outcome worth recording."""
        store = await make_store()
        aid = await store.save(make_report(repo_name="clean-merge-app"))
        record = {
            "pr_url": "https://github.com/org/clean-merge-app-gitops/pull/4",
            "state": "merged", "assessment_id": aid, "app_name": "clean-merge-app",
            "category": "cluster_config",
        }
        newly = await pr_outcomes.sync_pr_outcomes(
            store, [record], get_status=lambda url: {},
            get_extra_commits=lambda url: [], get_comments=lambda url: [],
        )
        assert newly == []
        assert await store.get_pr_outcome(record["pr_url"]) is None


class TestSyncPrOutcomesSkipsOpen:
    async def test_open_pr_is_never_synced(self):
        store = await make_store()
        record = {"pr_url": "https://github.com/org/open-app-gitops/pull/1", "state": "open", "app_name": "open-app"}
        newly = await pr_outcomes.sync_pr_outcomes(
            store, [record],
            get_status=lambda url: (_ for _ in ()).throw(AssertionError("must not be called for an open PR")),
            get_extra_commits=lambda url: (_ for _ in ()).throw(AssertionError("must not be called for an open PR")),
            get_comments=lambda url: (_ for _ in ()).throw(AssertionError("must not be called for an open PR")),
        )
        assert newly == []


class TestListPrOutcomes:
    async def test_list_pr_outcomes_filters_by_app_and_finding_category(self):
        store = await make_store()
        await store.record_pr_outcome(
            "https://github.com/o/r/pull/1", "app-a", "rejected",
            category="cluster_config", finding_category="security", reject_reason="wontfix",
        )
        await store.record_pr_outcome(
            "https://github.com/o/r/pull/2", "app-b", "edited_before_merge",
            category="cluster_config", finding_category="cost", skill_names=["add-hpa"],
        )
        assert len(await store.list_pr_outcomes(app_name="app-a")) == 1
        assert len(await store.list_pr_outcomes(finding_category="cost")) == 1
        assert len(await store.list_pr_outcomes(skill_name="add-hpa")) == 1
        assert len(await store.list_pr_outcomes()) == 2

    async def test_record_pr_outcome_is_idempotent_per_pr_url(self):
        store = await make_store()
        first_id = await store.record_pr_outcome("https://github.com/o/r/pull/9", "app", "rejected")
        second_id = await store.record_pr_outcome("https://github.com/o/r/pull/9", "app", "edited_before_merge")
        assert first_id is not None
        assert second_id is None
        stored = await store.get_pr_outcome("https://github.com/o/r/pull/9")
        assert stored["outcome"] == "rejected"
