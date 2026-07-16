# Portal crawl matrix (live OAuth + in-cluster)

**When:** 2026-07-16 (initial crawl) · **Updated:** post-#65 / #62 / #66 gap pass  
**Portal:** https://agentit-agentit.apps.aws-jb-acsacm-1.dev05.red-chesterfield.com  
**Session:** Playwright MCP as `kube:admin` (founder OAuth session available)  
**Deployed commit during initial crawl:** mixed (`99763e4` → `49d6f02`); main advanced mid-crawl.  
**Live re-check (gap pass):** pinky still on older image (`72d8b28…`); Argo reported Deploy failed / Degraded — **deploy lag**, not missing main fixes.

Feedback columns follow EDL §3 (busy / success / error / empty / confirm).

| Page | Control | Result | Class | Fix |
| ---- | ------- | ------ | ----- | --- |
| Fleet (`/`, live still Fleet-home until deploy) | Assess New Repo modal | **Pass** | — | — |
| Fleet | Delete × → typed confirm | **Pass** (Cancel focused, confirm disabled until name) | — | — |
| Fleet | Re-assess | **Pass*** (spinner markup present; not long-run timed) | — | — |
| Fleet | Needs Action column | **Pass** on main (#55); **Fail** on live deploy lag | wrong page / IA | #55 on main |
| Masthead | Cmd+K vs primary nav | **Pass** on main — search in **right** cluster (#64, restored #65). #61 briefly centered; #56 closed as superseded. | a11y / layout | #64/#65 |
| Assessment Detail | Register for GitOps → confirm → Register | **Pass** on main (#62): flash banner + URL toasts after htmx-boost + infra URL field | silent fail | #62 |
| Assessment Detail | Findings Fix → confirm → Generate Fix | **Pass** (landed onboard-results `fix_generated=1`) | — | #59 |
| Assessment Detail | Remediation Plan Fix (bare submit) | **Pass** on main | dead click / missing confirm | #59 |
| Assessment Detail | Onboard This App | **Pass*** (htmx-indicator present) | — | — |
| Onboard Results | Dry Run / Apply / Per-Agent PRs / Download | **Pass*** | feedback (EDL) | #45 |
| Admin Review | Approve & Deliver | **Pass** (empty queue; empty copy OK) | — | — |
| Health | Summary cards (Platform/Pods/…) | **Pass** on main (`a.stat-card` links, #54); **Fail** on live (still `div`s) | dead click | #54 — **needs deploy** |
| Health | Pod / pipeline row links | **Pass** | — | — |
| Capabilities | Skill Activity rows | **Pass** on main (#57 field map + skip incomplete); **Fail** on live (blank Skill/Outcome) | empty data | #57 — **needs deploy** |
| Capabilities | Activate / Research Skills | **Pass** (busy via global submit + indicators) | — | — |
| Events | Filter form | **Pass** on main (`.filter-bar`, #58) | layout | #58 |
| Events | DLQ link | **Pass** | — | — |
| Decisions | Page load / filters | **Pass*** | — | — |
| Settings | Page load | **Pass*** | — | — |
| Ledger | Nav link / Needs You | **Pass** | — | — |
| Insights | Stat cards / agent / skill rows | **Pass** after gap pass — deep links to Fleet, remediations, Ledger Needs You, Events, `/agents/{name}`, skill history | dead click | this PR |
| Insights | Rate bars (charts) | **Pass*** (visual only; no interactive filters on this page) | — | — |
| Schedules | Create / Save / Disable | **Pass*** | — | — |
| Cmd+K | Command palette | **Pass** (opens; nav + apps listed); right-cluster placement on main | — | #65 |
| Mobile hamburger | Toggle primary + secondary | **Pass** (live + browser test): Ledger…Insights + Events + account | — | covered |
| Events drawer | Esc, focus trap, severity badge | **Pass** (live Esc/trap/badges; trap also handles Esc; `_badgeClass` for warning/unknown) | a11y | this PR |

\*Pass\* = control opens/submits with expected confirm or navigation; long async busy/success not fully timed in this pass.

## Resolved since initial crawl (on `main`)

| Gap | PRs |
| --- | --- |
| Register for GitOps silent `?error=` | #62 |
| Masthead Cmd+K overlap / placement | #64 right → #61 center (brief) → **#65 right** (founder intent). #56 closed superseded. |
| Skill Activity blank columns | #57 |
| Health summary cards not links | #54 |
| Events filter-bar layout | #58 |
| Findings Fix during onboarding | #59 |
| Insights dead rollups / row links | this PR |
| Crawl matrix doc | #66 + this PR |

## Still needs human eyes / deploy

1. **Pinky deploy** — Argo Degraded; live still lacks #54/#57/#62/#65 UI. Re-crawl Register toast, Health `a.stat-card`, Skill Activity after sync.
2. Mid-width masthead (~1024–1100px) with **right-side** search: confirm Ledger…Insights fully clickable.
3. Full Dry Run → Apply → Approve & Deliver on a real gate (destructive).
4. Capabilities Activate busy/success; Self-Improvement scan end-to-end.
5. Insights has **no chart filters** by design (aggregate tables + rate bars only) — do not expect filter controls.
