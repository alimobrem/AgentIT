"""Assessment Detail's own manual "Scan"/"Re-scan" trigger -- previously the
only way to re-assess an app was Fleet's row action; a concurrent worker
building the recurring-assessment scheduler flagged that Assessment Detail
itself had Onboard/Re-onboard but no standalone re-assess button.

Same underlying `POST /assess` (repo_url + criticality) Fleet's own row
action already uses, mirroring Fleet's exact ever_onboarded split: a never-
onboarded app gets a plain submit button (no confirm dialog, labeled
"Scan"); an already-onboarded app gets a confirm dialog first (labeled
"Re-scan", since re-assessing there also auto-chains into onboard). Both
labels are copy-only -- the underlying route/behavior is identical.
"""
from __future__ import annotations

from conftest import make_report


class TestAssessmentDetailReassessButton:
    async def test_never_onboarded_app_gets_plain_scan_button(self, portal_client):
        # `portal_client`'s own seeded app is already onboarded by
        # construction (see conftest.py) -- a genuinely never-onboarded app
        # needs its own fresh assessment with no onboarding_results row.
        client, store, _seed_aid = portal_client
        aid = await store.save(make_report(repo_name="never-onboarded-app"))
        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Scan" in resp.text
        # Never onboarded -> no confirm dialog wrapper, direct submit.
        assert "title: 'Re-scan'" not in resp.text

    async def test_reassess_form_posts_repo_url_and_criticality(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert f'value="{report.repo_url}"' in resp.text
        assert f'value="{report.criticality}"' in resp.text

    async def test_onboarded_app_gets_confirm_dialog_rescan_button(self, portal_client):
        # `portal_client`'s own seeded app is already onboarded (conftest.py).
        client, store, aid = portal_client
        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Re-scan" in resp.text
        # Onboarded -> the more consequential re-assess+auto-onboard path
        # gets a confirm dialog, labeled "Re-scan".
        assert "title: 'Re-scan'" in resp.text
        assert "regenerate onboard manifests" in resp.text
