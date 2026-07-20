"""Assessment Detail's own manual "Scan" trigger -- previously the only way
to re-assess an app was Fleet's row action; a concurrent worker building the
recurring-assessment scheduler flagged that Assessment Detail itself had
Onboard/Re-onboard but no standalone re-assess button.

Same underlying `POST /assess` (repo_url + criticality) Fleet's own row
action already uses, mirroring Fleet's exact ever_onboarded split: a never-
onboarded app gets a plain submit button (no confirm dialog); an already-
onboarded app gets a confirm dialog first (since re-assessing there also
auto-chains into onboard, regenerating and re-delivering real manifests).
Both cases render the same "Scan" label -- 2026-07-20 fix dropped the
"Re-scan" wording once "Scan" was the only button either way, since a
separate "Re-onboard" button removed the same day was the only other place
that distinction mattered.
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
        assert "title: 'Scan'" not in resp.text

    async def test_reassess_form_posts_repo_url_and_criticality(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert f'value="{report.repo_url}"' in resp.text
        assert f'value="{report.criticality}"' in resp.text

    async def test_onboarded_app_gets_confirm_dialog_scan_button(self, portal_client):
        # `portal_client`'s own seeded app is already onboarded (conftest.py).
        client, store, aid = portal_client
        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Scan" in resp.text
        # Onboarded -> the more consequential re-assess+auto-onboard path
        # gets a confirm dialog first, same "Scan" label either way.
        assert "title: 'Scan'" in resp.text
        assert "regenerate onboard manifests" in resp.text
