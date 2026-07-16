"""capability-scout's research/propose/gate logic — the counterpart to
``learning_agent.py``, but aimed at AgentIT's own repository instead of the
skills catalog AgentIT generates for the apps it onboards. See
docs/self-improvement-for-agentit.md for the full design.

**Scope boundary (read this before extending).** Two build modes:

- ``docs`` (default / safe fallback): the LLM proposes and documents a
  change as ``docs/proposals/<slug>.md`` — never auto-applies source.
- ``source`` / ``auto`` (L3 dogfood): when every ``target_files`` entry sits
  under ``skills/``, ``checks/``, ``tests/``, or ``src/agentit/``, the LLM
  is asked for full file contents for those paths only (current text fed
  in). Paths outside that set, LLM failures, or empty patches fall back to
  the docs artifact rather than inventing an out-of-scope edit.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

CAPABILITY_RUN_ACTION = "capability-run"

# Direct, mechanical enforcement of this project's own "keep changes
# minimal" convention (see llm.py's system prompt) — not a new philosophy,
# just making an existing rule machine-checked for once.
MAX_DIFF_FILES = 3
MAX_DIFF_LINES = 150

SCOPE_ALLOWED_PREFIXES = ("src/agentit/", "skills/", "checks/", "tests/", "docs/")
SCOPE_DENY_SUBSTRINGS = ("chart/", "argocd/", ".github/workflows/", "dockerfile", "secret", "rbac")

# L3 source-mode allowlist — matches SCOPE_ALLOWED_PREFIXES minus docs/
# (docs mode already owns the proposal-markdown path).
SOURCE_ALLOWED_PREFIXES = ("skills/", "checks/", "tests/", "src/agentit/")

# Full-file LLM rewrites of huge modules truncate / fail to parse. Prefer
# new small siblings; when reading an existing target, cap what we send.
MAX_SOURCE_FILE_CHARS = 6000

# The single highest-precision signal source per the design doc — explicit,
# human-written admissions of missing functionality in this repo's own docs.
_DOC_GAP_ANCHORS = ("Known gap", "Deliberately deferred", "Documented future idea", "not built")

# Reused rather than duplicated with any future Trivy/secret-scan
# unification — see the design doc's "no secrets, ever" gate for why a
# short hardcoded list is an acceptable v1.
_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"sk-ant-[a-zA-Z0-9\-_]{20,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
    re.compile(r"xox[baprs]-[a-zA-Z0-9\-]{10,}"),
]

# "a fresh dev cluster with < 5 recorded outcomes anywhere" per the design
# doc — below this, a no-op is the honest outcome, not a fabricated proposal.
MIN_SIGNAL_ROWS = 5


def scan_doc_gaps(docs_dir: Path | None = None) -> list[dict]:
    """Grep this repo's own ``docs/*.md`` for explicit, human-written
    admissions of missing functionality. Returns a list of
    ``{"file", "line_no", "anchor", "text"}`` dicts, one per matching line —
    never fabricates a gap that isn't literally present in the doc text.
    """
    docs_dir = docs_dir or Path("docs")
    if not docs_dir.is_dir():
        return []
    gaps: list[dict] = []
    for md_file in sorted(docs_dir.glob("*.md")):
        try:
            lines = md_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            for anchor in _DOC_GAP_ANCHORS:
                if anchor.lower() in line.lower():
                    gaps.append({
                        "file": str(md_file),
                        "line_no": i,
                        "anchor": anchor,
                        "text": line.strip(),
                    })
                    break
    return gaps


async def _safe_call(store: object, method_name: str, *args, default=None, **kwargs):
    """Best-effort store call — a missing method or a query failure must
    never block the rest of evidence-gathering, mirroring every other
    ``hasattr(...)``-guarded store call already used throughout this repo
    (e.g. ``routes/capabilities.py``'s ``_get_learning_run_history``)."""
    if not hasattr(store, method_name):
        return default if default is not None else []
    try:
        return await getattr(store, method_name)(*args, **kwargs)
    except Exception:
        logger.warning("capability-scout: failed to call store.%s", method_name, exc_info=True)
        return default if default is not None else []


async def gather_evidence(store: object | None) -> dict:
    """Collect every real signal source the design doc specifies — nothing
    here is invented; every field comes straight from a real store query or
    a real grep of this repo's own docs. ``signal_count`` is how the caller
    decides whether there's enough real data to ground a proposal at all.
    """
    doc_gaps = scan_doc_gaps()

    if store is None:
        return {
            "doc_gaps": doc_gaps,
            "rejection_stats": [],
            "agent_stats": [],
            "check_compliance": [],
            "skill_effectiveness": {},
            "low_effectiveness_skills": [],
            "loop_health": {},
            "tick_failures": [],
            "signal_count": len(doc_gaps),
        }

    rejection_stats = await _safe_call(store, "get_fleet_wide_rejection_stats")
    agent_stats = await _safe_call(store, "get_agent_stats")
    check_compliance = await _safe_call(store, "get_check_compliance")
    skill_effectiveness = await _safe_call(store, "get_skill_effectiveness", default={})
    low_effectiveness_skills = await _safe_call(store, "get_low_effectiveness_skills")
    loop_health = await _safe_call(store, "get_loop_health", default={})
    tick_failures = await _safe_call(store, "list_events_by_action", "tick-failed", limit=20)

    signal_count = (
        len(doc_gaps) + len(rejection_stats) + len(agent_stats)
        + len(check_compliance) + len(low_effectiveness_skills) + len(tick_failures)
    )

    return {
        "doc_gaps": doc_gaps,
        "rejection_stats": rejection_stats,
        "agent_stats": agent_stats,
        "check_compliance": check_compliance,
        "skill_effectiveness": skill_effectiveness,
        "low_effectiveness_skills": low_effectiveness_skills,
        "loop_health": loop_health,
        "tick_failures": tick_failures,
        "signal_count": signal_count,
    }


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] or "proposal"


def render_proposal_doc(proposal: dict) -> str:
    """Render the LLM's structured proposal into the one artifact this
    loop's PR actually commits — see this module's docstring for why v1
    documents a proposed change rather than mechanically generating the
    source diff those target files would need."""
    target_files = proposal.get("target_files") or []
    lines = [
        f"# Proposal: {proposal.get('title', 'Untitled')}",
        "",
        "> Proposed by AgentIT's capability-scout — see docs/self-improvement-for-agentit.md",
        "",
        f"**Risk:** {proposal.get('risk', 'unknown')}",
        "",
        "## Gap",
        "",
        proposal.get("gap_description", ""),
        "",
        "## Evidence",
        "",
        proposal.get("evidence", ""),
        "",
        "## Suggested target files",
        "",
        "\n".join(f"- `{f}`" for f in target_files) or "- (none specified)",
        "",
        "## Suggested change",
        "",
        proposal.get("change_summary", ""),
        "",
        "## Test plan",
        "",
        proposal.get("test_plan", ""),
        "",
    ]
    return "\n".join(lines)


def build_diff(
    proposal: dict,
    *,
    mode: str = "docs",
    repo_dir: Path | None = None,
    llm_client: object | None = None,
) -> dict[str, str]:
    """File changes this cycle would commit.

    ``mode``:
    - ``docs``: always ``docs/proposals/<slug>.md`` (v1 behavior).
    - ``source`` / ``auto``: attempt skill/check/test/src file generation when
      targets are eligible. On generation failure return ``{}`` (caller must
      not open a docs-only PR labeled as source — that burned the open-PR
      cap during L3 dogfood). When targets are ineligible, ``auto``/``source``
      still fall back to the docs artifact via ``resolve_build_mode`` → docs.
    """
    resolved = resolve_build_mode(proposal, mode)
    if resolved == "source" and repo_dir is not None and llm_client is not None:
        source = build_source_diff(proposal, Path(repo_dir), llm_client)
        if source:
            return source
        logger.warning(
            "capability-scout source mode produced no files for %r — not falling back to docs",
            proposal.get("title"),
        )
        return {}
    if resolved == "source":
        # Eligible targets but no llm/repo — cannot invent source; fail closed.
        return {}
    return build_docs_diff(proposal)


def build_docs_diff(proposal: dict) -> dict[str, str]:
    """The literal docs/proposals artifact (v1 / fallback)."""
    slug = slugify(proposal.get("title", "proposal"))
    path = f"docs/proposals/{slug}.md"
    return {path: render_proposal_doc(proposal)}


def paths_eligible_for_source(target_files: list[str] | None) -> bool:
    """True when every target path is under skills/, checks/, tests/, or
    src/agentit/ and none hit the denylist — the L3 source-mode allowlist."""
    if not target_files:
        return False
    for path in target_files:
        normalized = path.replace("\\", "/")
        lowered = normalized.lower()
        if any(bad in lowered for bad in SCOPE_DENY_SUBSTRINGS):
            return False
        if not any(normalized.startswith(prefix) for prefix in SOURCE_ALLOWED_PREFIXES):
            return False
    return True


def resolve_build_mode(proposal: dict, mode: str) -> str:
    """Map requested mode + proposal targets to ``docs`` or ``source``."""
    requested = (mode or "docs").strip().lower()
    if requested not in ("docs", "source", "auto"):
        requested = "docs"
    if requested == "docs":
        return "docs"
    eligible = paths_eligible_for_source(proposal.get("target_files") or [])
    if requested == "source":
        return "source" if eligible else "docs"
    # auto
    return "source" if eligible else "docs"


def load_target_file_contents(repo_dir: Path, target_files: list[str]) -> dict[str, str]:
    """Read current file text for each target (empty string if creating new).

    Oversized existing files are truncated with a note so the LLM is steered
    toward a small additive change or a new sibling module rather than a
    full rewrite that will not fit in the generation token budget.
    """
    out: dict[str, str] = {}
    for path in target_files:
        normalized = path.replace("\\", "/")
        full = repo_dir / normalized
        if full.is_file():
            try:
                text = full.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if len(text) > MAX_SOURCE_FILE_CHARS:
                text = (
                    text[:MAX_SOURCE_FILE_CHARS]
                    + "\n\n# ... truncated for LLM context — do not rewrite this whole file; "
                    + "add a small new sibling module or a minimal additive change instead.\n"
                )
            out[normalized] = text
        else:
            out[normalized] = ""
    return out


def sibling_module_path(title: str) -> str:
    """Derive a short ``src/agentit/<feature>.py`` path from a proposal title.

    Used when an oversized existing module is stripped from ``target_files`` so
    source mode can still land a small new module instead of a full rewrite.
    """
    slug = slugify(title or "feature").replace("-", "_")
    for prefix in ("add_", "fix_", "implement_", "create_", "track_", "build_"):
        if slug.startswith(prefix):
            slug = slug[len(prefix) :]
            break
    slug = (slug[:48].rstrip("_") or "feature")
    return f"src/agentit/{slug}.py"


def rewrite_oversized_source_targets(
    target_files: list[str],
    repo_dir: Path,
    *,
    title: str = "",
) -> list[str]:
    """Drop existing targets that already exceed ``MAX_DIFF_LINES``.

    Full-file LLM rewrites of those modules always fail the size gate. Keep
    small/new files; when any oversized path was dropped, insert one new
    sibling module under ``src/agentit/`` so the cycle can still produce a
    reviewable executable diff.
    """
    kept: list[str] = []
    dropped_oversized = False
    for path in target_files:
        normalized = path.replace("\\", "/")
        full = repo_dir / normalized
        if full.is_file():
            try:
                n_lines = full.read_text(encoding="utf-8").count("\n") + 1
            except OSError:
                n_lines = 0
            if n_lines > MAX_DIFF_LINES:
                logger.info(
                    "Rewriting oversized source target %s (%d lines > %d) → new sibling",
                    normalized,
                    n_lines,
                    MAX_DIFF_LINES,
                )
                dropped_oversized = True
                continue
        kept.append(normalized)
    if dropped_oversized:
        sibling = sibling_module_path(title)
        if sibling not in kept:
            kept.insert(0, sibling)
    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for path in kept:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def build_source_diff(proposal: dict, repo_dir: Path, llm_client: object) -> dict[str, str]:
    """Ask the LLM for full-file replacements for eligible target_files only.

    Drops any path the LLM returns that wasn't in the proposal's target set
    or isn't source-allowlisted — never widen scope mid-cycle.
    """
    raw_targets = [p.replace("\\", "/") for p in (proposal.get("target_files") or [])]
    targets = rewrite_oversized_source_targets(
        raw_targets, Path(repo_dir), title=str(proposal.get("title") or ""),
    )
    # Keep proposal metadata aligned with what we actually ask the LLM to write.
    if targets != raw_targets:
        proposal["target_files"] = targets
    if not paths_eligible_for_source(targets):
        return {}
    current = load_target_file_contents(repo_dir, targets)
    generate = getattr(llm_client, "generate_capability_files", None)
    if generate is None:
        return {}
    generated = generate(proposal, current)
    if not generated or not isinstance(generated, dict):
        return {}
    allowed = set(targets)
    cleaned: dict[str, str] = {}
    for path, content in generated.items():
        normalized = str(path).replace("\\", "/")
        if normalized not in allowed:
            logger.warning("Dropping LLM path outside proposal targets: %s", normalized)
            continue
        if not paths_eligible_for_source([normalized]):
            logger.warning("Dropping LLM path outside source allowlist: %s", normalized)
            continue
        cleaned[normalized] = str(content)
    # Soft reject oversize patches here so we fail closed before gates burn a cycle
    # on a known-bad full-file rewrite (caller treats {} as source-generation-failed).
    total_lines = sum(c.count("\n") + 1 for c in cleaned.values())
    if cleaned and (len(cleaned) > MAX_DIFF_FILES or total_lines > MAX_DIFF_LINES):
        logger.warning(
            "capability-scout source generation over size cap (%d files, %d lines) — discarding",
            len(cleaned),
            total_lines,
        )
        return {}
    return cleaned


# ── Safety gates ─────────────────────────────────────────────────────────
# Every gate below is a real, executable check over the real diff/proposal
# — none of these are stubs that always return True.


def check_diff_size(diff: dict[str, str]) -> tuple[bool, str]:
    if len(diff) > MAX_DIFF_FILES:
        return False, f"{len(diff)} file(s) touched — over the {MAX_DIFF_FILES}-file cap"
    total_lines = sum(content.count("\n") + 1 for content in diff.values())
    if total_lines > MAX_DIFF_LINES:
        return False, f"{total_lines} line(s) — over the {MAX_DIFF_LINES}-line cap"
    return True, f"{len(diff)} file(s), {total_lines} line(s) — within cap"


def check_scope_allowlist(diff: dict[str, str]) -> tuple[bool, str]:
    for path in diff:
        normalized = path.replace("\\", "/")
        lowered = normalized.lower()
        if any(bad in lowered for bad in SCOPE_DENY_SUBSTRINGS):
            return False, f"'{path}' is outside the scope allowlist (denylisted path segment)"
        if not any(normalized.startswith(prefix) for prefix in SCOPE_ALLOWED_PREFIXES):
            return False, f"'{path}' is outside the scope allowlist ({', '.join(SCOPE_ALLOWED_PREFIXES)})"
    return True, "all paths within src/agentit/, skills/, checks/, tests/, or docs/"


def check_no_secrets(diff: dict[str, str]) -> tuple[bool, str]:
    for path, content in diff.items():
        for pattern in _SECRET_PATTERNS:
            if pattern.search(content):
                return False, f"potential secret pattern matched in '{path}'"
    return True, "no secret patterns matched"


def check_has_test_plan(proposal: dict) -> tuple[bool, str]:
    test_plan = (proposal.get("test_plan") or "").strip()
    if not test_plan:
        return False, "proposal has no test_plan — rejected (no test coverage described)"
    return True, f"test plan present: {test_plan[:100]}"


def check_syntax(diff: dict[str, str]) -> tuple[bool, str]:
    """`python -m py_compile` on every touched `.py` file — the bare-minimum
    structural validator the design doc's "genuinely new engineering"
    section calls out (a source diff has no equivalent to
    `load_skill()`/`verify_skill()`'s structural validation today)."""
    import py_compile
    import tempfile

    for path, content in diff.items():
        if not path.endswith(".py"):
            continue
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            py_compile.compile(tmp_path, doraise=True)
        except py_compile.PyCompileError as exc:
            return False, f"'{path}' failed to compile: {exc}"
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
    return True, "all .py files compile cleanly"


def run_test_suite(repo_dir: Path) -> tuple[bool, str]:
    """The exact same invocation `.github/workflows/tests.yml` uses, same
    `KUBECONFIG` env var per CLAUDE.md's Testing section — a red suite is
    an automatic discard, never a PR with a note saying tests are failing.

    Unlike CI (which runs outside any cluster), this gate can itself run
    from inside a real pod on a real cluster (capability-scout's own
    watcher deployment). The `kubernetes` client's config loader tries
    ``load_incluster_config()`` *before* ``load_kube_config()`` (see
    ``kube.py``), and in-cluster loading only looks at the
    ``KUBERNETES_SERVICE_HOST``/``KUBERNETES_SERVICE_PORT`` env vars and
    the mounted service-account token -- it never consults ``KUBECONFIG``
    at all. So overriding ``KUBECONFIG`` alone silently does nothing here:
    every kube-touching test still succeeds against the real cluster
    instead of failing fast, each one taking real network round-trips
    against real fleet data instead of <300ms. Confirmed live against the
    real agentit-capability-scout pod: with these two vars left in place,
    the same suite that takes ~190s outside a cluster ran for ~900s+ and
    was ultimately killed twice, at both 256Mi/250m and 2Gi/1-core limits.
    Stripping them forces the same clean, fast ConfigException-driven
    fallback CI already gets.

    The same problem, same shape, hits ``AGENTIT_DB_DSN`` (set on this
    watcher's own Deployment so it can talk to the real fleet's shared
    Postgres instance): ``store.create_store()`` reads that env var
    directly, so a test run that doesn't strip it would connect to the
    *real* shared Postgres instead of the test suite's own dedicated
    ``AGENTIT_TEST_PG_DSN``-backed instance (see
    ``tests/conftest.py``/``docs/postgres-migration-plan.md``'s testing
    section). Confirmed live, before this stripping existed:
    ``test_watch_rescan_iterates_the_fleet`` saw the real fleet's actual
    apps instead of its one fixture app, and a concurrent real-Postgres-
    pool contention error (``asyncpg... cannot perform operation: another
    operation is in progress``) hit other tests sharing that same live
    pool under full-suite load. Stripped for the same reason as the kube
    vars above -- the test suite's own fixtures set up their isolated
    Postgres instance independently of these vars, so stripping them here
    only removes the risk of accidentally reaching production, it doesn't
    remove the test suite's ability to run.

    **Known follow-up gap, not fixed here**: this pod has neither
    podman/docker (so ``tests/conftest.py``'s auto-start fallback can't
    help) nor an ``AGENTIT_TEST_PG_DSN`` of its own wired into its
    Deployment env -- unlike CI (a GitHub Actions ``services:`` block / a
    Tekton ``Sidecar``, see ``.github/workflows/tests.yml``/
    ``chart/templates/tekton/pipeline.yaml``), so this safety gate's test
    run will currently fail to acquire a Postgres instance at all inside
    the live capability-scout pod. Deliberately NOT fixed by pointing this
    at the live bundled instance's own database -- test fixtures `TRUNCATE`
    every table, which would be a real, severe data-loss bug if aimed at
    production data. The correct fix is a small, dedicated Postgres
    instance (or a distinct, human-provisioned database on the bundled
    one) wired into this watcher's own Deployment via a new, explicit
    ``AGENTIT_TEST_PG_DSN`` value -- flagged here for whoever picks it up
    next rather than guessed at under this pass's time constraints.
    """
    import os

    env = {**os.environ, "KUBECONFIG": "/tmp/nonexistent-path"}
    for var in (
        "KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT",
        "AGENTIT_DB_BACKEND", "AGENTIT_DB_DSN", "PGUSER", "PGPASSWORD",
    ):
        env.pop(var, None)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q",
             "--ignore=tests/test_real_repos.py",
             "--ignore=tests/test_browser.py",
             "--ignore=tests/test_live_cluster_e2e.py"],
            cwd=repo_dir, capture_output=True, text=True, timeout=900, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pytest failed to run: {exc}"
    if result.returncode != 0:
        # A bare invocation failure (e.g. pytest itself isn't importable, or
        # `tests/` doesn't exist in this environment) writes to stderr, not
        # stdout -- stdout alone silently produced an empty, undiagnosable
        # "pytest exited 1: " detail for exactly that failure mode. Surface
        # both so the real cause is visible from the gate result itself.
        tail = (result.stdout[-500:] + "\n" + result.stderr[-500:]).strip()
        return False, f"pytest exited {result.returncode}: {tail}"
    return True, "pytest passed"


_SELF_IMPROVE_BRANCH_PREFIX = "agentit/self-improve/"


def check_no_open_self_improve_pr(max_open_prs: int = 1) -> tuple[bool, str]:
    """Weekly-cap / not-daily-spam gate: only open a new PR if fewer than
    ``max_open_prs`` ``agentit/self-improve/*`` PRs are already open —
    checked via ``gh pr list`` per the design doc, so a proposal never
    piles up unreviewed.

    ``gh pr list --head`` does an *exact* branch-name match (confirmed live
    against the real repo), never a prefix -- but every real branch this
    loop creates is ``agentit/self-improve/<slug>-<unix-timestamp>``
    (see ``_open_pr``), which never equals the literal string
    ``agentit/self-improve``. Filtering with ``--head`` that way always
    returned zero results regardless of how many self-improve PRs were
    actually open, silently disabling this cap entirely. List every open
    PR's ``headRefName`` instead and filter by prefix ourselves."""
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--limit", "100", "--json", "url,headRefName"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not check for open PRs (gh unavailable): {exc}"
    if result.returncode != 0:
        return False, f"gh pr list failed: {result.stderr[:200]}"
    import json as _json
    try:
        all_open_prs = _json.loads(result.stdout or "[]")
    except _json.JSONDecodeError:
        return False, "could not parse 'gh pr list' output"
    open_prs = [pr for pr in all_open_prs if pr.get("headRefName", "").startswith(_SELF_IMPROVE_BRANCH_PREFIX)]
    if len(open_prs) >= max_open_prs:
        return False, f"{len(open_prs)} open agentit/self-improve/* PR(s) already outstanding (cap: {max_open_prs})"
    return True, f"{len(open_prs)} open agentit/self-improve/* PR(s) — under the {max_open_prs} cap"


def run_safety_gates(proposal: dict, diff: dict[str, str], repo_dir: Path, max_open_prs: int = 1) -> dict:
    """Run every gate in order, fail-closed — no PR opens if any gate fails.
    Returns ``{"passed": bool, "gates": [{"name", "passed", "detail"}, ...]}``.
    """
    gate_defs = [
        ("diff-size", lambda: check_diff_size(diff)),
        ("scope-allowlist", lambda: check_scope_allowlist(diff)),
        ("no-secrets", lambda: check_no_secrets(diff)),
        ("test-plan-required", lambda: check_has_test_plan(proposal)),
        ("syntax", lambda: check_syntax(diff)),
        ("no-open-pr", lambda: check_no_open_self_improve_pr(max_open_prs)),
        ("tests-pass", lambda: run_test_suite(repo_dir)),
    ]
    results = []
    all_passed = True
    for name, fn in gate_defs:
        try:
            passed, detail = fn()
        except Exception as exc:
            passed, detail = False, f"gate raised an exception: {exc}"
            logger.warning("Safety gate '%s' raised an exception", name, exc_info=True)
        results.append({"name": name, "passed": passed, "detail": detail})
        if not passed:
            all_passed = False
    return {"passed": all_passed, "gates": results}


def describe_capability_run(
    evidence: dict,
    proposal: dict | None,
    gate_result: dict | None,
    pr_url: str | None,
    error: str | None = None,
) -> tuple[str, str, dict]:
    """Build the ``(severity, summary, details)`` for one durable
    ``capability-run`` event — mirrors ``learning_agent.describe_learning_run``'s
    convention exactly: one action for every outcome (proposed / gate-blocked
    / no-signal / error), not a separate success-only event, so every cycle
    is queryable via ``list_events_by_action(CAPABILITY_RUN_ACTION)``.
    """
    doc_anchor = None
    doc_gaps = evidence.get("doc_gaps") or []
    if doc_gaps:
        g = doc_gaps[0]
        doc_anchor = f"{g['file']}:{g['line_no']}"

    details: dict = {
        "trigger": "watcher",
        "title": (proposal or {}).get("title", ""),
        "evidence": (proposal or {}).get("evidence", ""),
        "risk": (proposal or {}).get("risk", ""),
        "doc_anchor": doc_anchor,
        "gate_results": (gate_result or {}).get("gates", []),
        "pr_url": pr_url,
    }
    if error:
        details["error"] = error
        return "error", f"capability-scout run failed: {error}", details
    if pr_url:
        return "info", f"Opened proposal PR: {proposal['title']} ({pr_url})", details
    if proposal and proposal.get("has_proposal") and gate_result and not gate_result["passed"]:
        failed = [g["name"] for g in gate_result["gates"] if not g["passed"]]
        return "warning", f"Proposal '{proposal['title']}' gate-blocked: {', '.join(failed)}", details
    if evidence.get("signal_count", 0) < MIN_SIGNAL_ROWS:
        return "warning", (
            "No proposal this cycle — insufficient real signal "
            f"({evidence.get('signal_count', 0)} data point(s) across doc gaps and store queries, need {MIN_SIGNAL_ROWS})."
        ), details
    return "warning", "No proposal this cycle — LLM found no evidence-grounded gap worth proposing.", details
