# Portal crawl matrix (live OAuth + in-cluster)

**When:** 2026-07-16  
**Portal:** https://agentit-agentit.apps.aws-jb-acsacm-1.dev05.red-chesterfield.com  
**Session:** Playwright MCP as `kube:admin` (founder OAuth session available)  
**Deployed commit during crawl:** mixed (`99763e4` → `49d6f02`); main advanced mid-crawl.

Feedback columns follow EDL §3 (busy / success / error / empty / confirm).

| Page | Control | Result | Class | Fix |
| ---- | ------- | ------ | ----- | --- |
| Fleet (`/`, live still Fleet-home) | Assess New Repo modal | **Pass** | — | — |
| Fleet | Delete × → typed confirm | **Pass** (Cancel focused, confirm disabled until name) | — | — |
| Fleet | Re-assess | **Pass*** (spinner markup present; not long-run timed) | — | — |
| Fleet | Needs Action column | **Fail** (IA: competes with Ledger; fixed in #55, deploy lag) | wrong page / IA | #55 on main |
| Masthead | Cmd+K search vs Fleet/Admin/Ledger links | **Fail** (search overlaps primary nav — screenshot) | a11y / layout | [#56](https://github.com/alimobrem/AgentIT/pull/56) open |
| Assessment Detail | Register for GitOps → confirm → Register | **Fail** (POST returns `?error=…` but **no banner/toast**; button unchanged) | silent fail / missing feedback | **this PR** |
| Assessment Detail | Findings Fix → confirm → Generate Fix | **Pass** (landed onboard-results `fix_generated=1`) | — | busy indicator still weak on live; #59 merged |
| Assessment Detail | Remediation Plan Fix (bare submit) | **Fail** on older deploy (no confirm) | dead click / missing confirm | #59 merged |
| Assessment Detail | Onboard This App | **Pass*** (htmx-indicator present) | — | — |
| Onboard Results | Dry Run / Apply / Per-Agent PRs / Download | **Pass*** (controls present; Apply still showed status-in-button on old deploy) | feedback (EDL) | #45 on main |
| Onboard Results | Register for GitOps | N/A (not shown when already in onboard flow) | — | — |
| Admin Review | Approve & Deliver | **Pass** (empty queue; empty copy OK) | — | — |
| Health | Summary cards (Platform/Pods/…) | **Fail** on live (divs, not links) | dead click | #54 on main (deploy lag) |
| Health | Pod / pipeline row links | **Pass** | — | — |
| Capabilities | Skill Activity rows | **Fail** (App+Timestamp only; Skill/Outcome blank) | empty data / wrong fields | #57 merged (deploy lag) |
| Capabilities | Activate / Research CVEs | **Pass*** (present; Activate lacks busy) | missing busy (medium) | follow-up |
| Events | Filter form | **Fail** on live (`action-bar` not `.filter-bar`) | layout | #58 merged |
| Events | DLQ link | **Pass** | — | — |
| Decisions | Page load / filters | **Pass*** | — | — |
| Settings | Page load | **Pass*** | — | — |
| Ledger | Nav link | **Pass** | — | — |
| Insights | Page load | Not fully click-tested charts | needs human eyes | — |
| Schedules | Mutating toggles | Not fully click-tested | needs human eyes | — |
| Mobile hamburger | Nav drawer | Not tested at mobile viewport | needs human eyes | — |
| Events drawer | Bell → slide-over | Not fully exercised | needs human eyes | — |

\*Pass\* = control opens/submits with expected confirm or navigation; long async busy/success not fully timed in this pass.

## High-severity feedback gaps (this PR)

1. **Register for GitOps silent failure** — live POST to Spoon-Knife returned  
   `?error=Could+not+auto-create+a+GitOps+infra+repo…` with **zero** visible alert/toast.  
   Root causes: (a) htmx-boost body swap does not re-fire `alpine:initialized` for URL toasts;  
   (b) Assessment Detail had no server-rendered flash banners;  
   (c) auto-create failed for third-party owners (`octocat/…`) without reuse of token-user `agentit-gitops`.
2. **Masthead overlap** — tracked in #56 (do not duplicate).
3. **Skill Activity blank** — fixed in #57; pinky still shows blanks until deploy catches main.

## Needs human OAuth eyes (not claimed done)

- Mid-width masthead after #56 merges (1024–1100px): click Fleet/Admin/Ledger under search.
- Full Dry Run → Apply → Approve & Deliver on a real gate (destructive; confirm copy).
- Events drawer focus trap + Esc; mobile hamburger.
- Capabilities Activate busy/success; Self-Improvement scan end-to-end.
- Insights chart filters and deep links.
- Post-deploy re-crawl of Register for GitOps with optional infra URL field + error banner.
