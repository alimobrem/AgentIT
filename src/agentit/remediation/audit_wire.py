"""Wire application audit logging into a real app package entrypoint.

Orphan ``audit.py`` / ``audit.ts`` at the repo root clears nothing useful:
the compliance analyzer requires import/usage evidence. This module discovers
the FastAPI (or Express) package and injects a middleware call site.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_AUDIT_MODULE_NAMES = frozenset({"audit.py", "audit.ts", "audit.js", "audit.go"})
_IGNORED_PARTS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "vendor", "dist", "build", "target", "tests", "test",
})

_PYTHON_APP_MARKERS = ("FastAPI(", "Flask(", "Starlette(")
_TS_APP_MARKERS = ("express()", "express.Router", "from 'express'", 'from "express"', "fastify(")

_USAGE_PATTERNS = (
    re.compile(r"\baudit_log\s*\("),
    re.compile(r"\bauditLog\s*\("),
    re.compile(r"\baudit\.Log\s*\("),
    re.compile(r"from\s+\.audit\s+import"),
    re.compile(r"from\s+[\w.]+\.audit\s+import"),
    re.compile(r"import\s+.*\baudit_log\b"),
    re.compile(r"import\s*\{[^}]*\bauditLog\b"),
)


def has_audit_usage(content: str) -> bool:
    return any(p.search(content) for p in _USAGE_PATTERNS)


def _is_audit_module_path(path: str) -> bool:
    name = Path(path.replace("\\", "/")).name.lower()
    return name in _AUDIT_MODULE_NAMES


def _packaged_audit_paths(paths: list[str]) -> list[str]:
    """Audit modules under a package dir (not orphan repo-root ``audit.py``)."""
    out: list[str] = []
    for p in paths:
        norm = p.replace("\\", "/").strip("/")
        if not _is_audit_module_path(norm):
            continue
        if "/" in norm:
            out.append(norm)
    return out


def repo_has_wired_audit(repo_path: Path) -> bool:
    """True when a packaged audit module exists and a non-module file uses it."""
    packaged = False
    callsite = False
    for fp in repo_path.rglob("*"):
        if not fp.is_file():
            continue
        if _IGNORED_PARTS & set(fp.parts):
            continue
        name = fp.name.lower()
        if name in _AUDIT_MODULE_NAMES:
            # Root-only orphan does not count as wired.
            try:
                rel = fp.relative_to(repo_path).as_posix()
            except ValueError:
                continue
            if "/" in rel:
                packaged = True
            continue
        if fp.suffix.lower() not in {".py", ".ts", ".js", ".go"}:
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if has_audit_usage(content):
            callsite = True
        if packaged and callsite:
            return True
    return packaged and callsite


def tree_has_wired_audit(
    tree_paths: list[str],
    read_file,
) -> bool:
    """GitHub-tree variant of :func:`repo_has_wired_audit`.

    Reads only source files under packaged-audit directory roots (plus
    common entrypoints) so Scan does not fetch the whole tree.
    """
    packaged = _packaged_audit_paths(tree_paths)
    if not packaged:
        return False
    roots = {str(Path(p).parent).replace("\\", "/") for p in packaged}
    candidates: list[str] = []
    for p in tree_paths:
        norm = p.replace("\\", "/")
        if _IGNORED_PARTS & set(Path(norm).parts):
            continue
        suffix = Path(norm).suffix.lower()
        if suffix not in {".py", ".ts", ".js", ".go"}:
            continue
        if _is_audit_module_path(norm):
            continue
        parent = str(Path(norm).parent).replace("\\", "/")
        if parent == ".":
            parent = ""
        under_pkg = any(
            parent == r or (r and parent.startswith(r + "/"))
            or (parent and r.startswith(parent + "/"))
            for r in roots
        )
        is_entry = Path(norm).name in {
            "app.py", "main.py", "server.ts", "app.ts", "index.ts", "main.ts",
        }
        if under_pkg or is_entry:
            candidates.append(norm)
    for path in candidates:
        text = read_file(path)
        if text and has_audit_usage(text):
            return True
    return False


def discover_python_app_entry(repo_path: Path) -> tuple[str, str] | None:
    """Return ``(package_dir_rel, app_entry_rel)`` for a FastAPI/Flask app."""
    candidates: list[tuple[str, str]] = []
    for app_py in repo_path.glob("**/app.py"):
        if _IGNORED_PARTS & set(app_py.parts):
            continue
        try:
            text = app_py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not any(m in text for m in _PYTHON_APP_MARKERS):
            continue
        pkg = str(app_py.parent.relative_to(repo_path))
        entry = str(app_py.relative_to(repo_path))
        candidates.append((pkg, entry))
    if not candidates:
        return None
    candidates.sort(
        key=lambda c: (
            0 if c[0].startswith("apps/") else 1,
            0 if "/src/" in c[0] else 1,
            len(c[0]),
        ),
    )
    return candidates[0]


def discover_ts_app_entry(repo_path: Path) -> tuple[str, str] | None:
    """Return ``(package_dir_rel, entry_rel)`` for an Express/Fastify app."""
    candidates: list[tuple[str, str]] = []
    for pattern in ("**/app.ts", "**/server.ts", "**/index.ts", "**/main.ts"):
        for entry in repo_path.glob(pattern):
            if _IGNORED_PARTS & set(entry.parts):
                continue
            try:
                text = entry.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not any(m in text for m in _TS_APP_MARKERS):
                continue
            pkg = str(entry.parent.relative_to(repo_path))
            rel = str(entry.relative_to(repo_path))
            candidates.append((pkg, rel))
    if not candidates:
        return None
    candidates.sort(
        key=lambda c: (
            0 if c[0].startswith("apps/") else 1,
            0 if "/src/" in c[0] else 1,
            len(c[0]),
        ),
    )
    return candidates[0]


def wire_python_app(app_content: str) -> str:
    """Inject ``from .audit import audit_log`` + mutating-request middleware."""
    if has_audit_usage(app_content) and "agentit_audit_middleware" in app_content:
        return app_content
    if "agentit_audit_middleware" in app_content:
        return app_content

    import_line = "from .audit import audit_log\n"
    if "from .audit import audit_log" not in app_content:
        app_content = _insert_after_imports(app_content, import_line)

    middleware = textwrap_dedent(
        """
        @app.middleware("http")
        async def agentit_audit_middleware(request, call_next):
            response = await call_next(request)
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                actor = (
                    request.headers.get("x-forwarded-user")
                    or request.headers.get("x-user")
                    or "anonymous"
                )
                outcome = "success" if response.status_code < 400 else "failure"
                audit_log(
                    action=f"{request.method} {request.url.path}",
                    actor=actor,
                    resource=request.url.path,
                    outcome=outcome,
                )
            return response
        """,
    )
    return _insert_after_app_setup(app_content, middleware)


def wire_ts_app(app_content: str) -> str:
    """Inject ``auditLog`` import + Express-style middleware."""
    if "agentitAuditMiddleware" in app_content:
        return app_content

    import_line = 'import { auditLog } from "./audit";\n'
    if "from \"./audit\"" not in app_content and "from './audit'" not in app_content:
        app_content = _insert_after_imports(app_content, import_line)

    middleware = textwrap_dedent(
        """
        app.use(function agentitAuditMiddleware(req, res, next) {
          const start = Date.now();
          res.on("finish", () => {
            if (!["POST", "PUT", "PATCH", "DELETE"].includes(req.method)) return;
            auditLog({
              action: `${req.method} ${req.path}`,
              actor: String(req.headers["x-forwarded-user"] || req.headers["x-user"] || "anonymous"),
              resource: req.path,
              outcome: res.statusCode < 400 ? "success" : "failure",
              metadata: { durationMs: Date.now() - start },
            });
          });
          next();
        });
        """,
    )
    # Prefer after `const app = express()` / `app = express()`
    m = re.search(r"(?:const|let|var)?\s*app\s*=\s*express\s*\(\s*\)\s*;?", app_content)
    if m:
        insert_at = m.end()
        return app_content[:insert_at] + "\n" + middleware + app_content[insert_at:]
    return app_content.rstrip() + "\n" + middleware + "\n"


def textwrap_dedent(s: str) -> str:
    import textwrap
    return textwrap.dedent(s).strip() + "\n"


def _insert_after_imports(content: str, import_line: str) -> str:
    lines = content.splitlines(keepends=True)
    last_import = -1
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("import ", "from ")) or stripped.startswith(
            ("import{", "import {")
        ):
            last_import = i
        elif last_import >= 0 and stripped and not stripped.startswith("#") and not stripped.startswith("//"):
            # past the import block
            break
    if last_import < 0:
        return import_line + content
    lines.insert(last_import + 1, import_line)
    return "".join(lines)


def _insert_after_app_setup(content: str, middleware: str) -> str:
    """Place middleware after FastAPI() construction / add_middleware block."""
    matches = list(re.finditer(r"app\.add_middleware\(\s*[\s\S]*?\)\s*\n", content))
    if matches:
        insert_at = matches[-1].end()
        return content[:insert_at] + "\n" + middleware + content[insert_at:]

    m = re.search(r"app\s*=\s*FastAPI\s*\([\s\S]*?\)\s*\n", content)
    if m:
        return content[: m.end()] + "\n" + middleware + content[m.end():]

    return content.rstrip() + "\n\n" + middleware + "\n"


def _orphan_audit_index(files: list[dict]) -> tuple[int, str] | None:
    for i, f in enumerate(files):
        target = str(f.get("target_path") or f.get("path") or "")
        name = Path(target).name.lower()
        # Root-level only (no directory components)
        if name in _AUDIT_MODULE_NAMES and "/" not in target.replace("\\", "/").strip("/"):
            return i, name
    return None


def _append_wired_entry(out: list[dict], audit_idx: int, entry: str, wired: str) -> None:
    out.append({
        "path": f"patch-{entry.replace('/', '-')}",
        "target_path": entry,
        "content": wired if wired.endswith("\n") else wired + "\n",
        "description": "Wire app-audit-logging middleware into API entrypoint",
        "finding_addressed": out[audit_idx].get("finding_addressed", ""),
        "skill_name": out[audit_idx].get("skill_name", "app-audit-logging"),
        "category": out[audit_idx].get("category", "codechange"),
    })


def _drop_orphan_audit_files(files: list[dict]) -> list[dict]:
    """Remove root-only audit module stubs from staged delivery files."""
    out: list[dict] = []
    for f in files:
        target = str(f.get("target_path") or f.get("path") or "")
        name = Path(target.replace("\\", "/")).name.lower()
        if name in _AUDIT_MODULE_NAMES and "/" not in target.replace("\\", "/").strip("/"):
            continue
        out.append(f)
    return out


def enrich_audit_files_from_repo(
    repo_path: Path,
    files: list[dict],
) -> list[dict]:
    """Relocate root audit modules into the app package and wire the entrypoint.

    ``files`` entries are delivery dicts with ``target_path`` / ``content``.
    When the repo already has packaged audit + usage, orphan root stubs are
    dropped (no theater PR) — re-Assess should clear the finding.
    """
    found = _orphan_audit_index(files)
    if found is None:
        return files
    if repo_has_wired_audit(repo_path):
        logger.info(
            "Repo already has wired audit logging — dropping orphan root audit patch",
        )
        return _drop_orphan_audit_files(files)
    audit_idx, audit_name = found
    out = [dict(f) for f in files]

    if audit_name == "audit.py":
        discovered = discover_python_app_entry(repo_path)
        if not discovered:
            logger.info("No FastAPI/Flask app.py found — leaving audit.py at repo root")
            return out
        pkg_dir, entry = discovered
        out[audit_idx]["target_path"] = f"{pkg_dir}/audit.py"
        out[audit_idx]["path"] = f"patch-{pkg_dir.replace('/', '-')}-audit.py"
        try:
            app_content = (repo_path / entry).read_text(encoding="utf-8")
        except OSError:
            return out
        wired = wire_python_app(app_content)
        if wired != app_content:
            _append_wired_entry(out, audit_idx, entry, wired)
        return out

    if audit_name in ("audit.ts", "audit.js"):
        discovered = discover_ts_app_entry(repo_path)
        if not discovered:
            logger.info("No Express/Fastify entry found — leaving audit module at repo root")
            return out
        pkg_dir, entry = discovered
        out[audit_idx]["target_path"] = f"{pkg_dir}/{audit_name}"
        try:
            app_content = (repo_path / entry).read_text(encoding="utf-8")
        except OSError:
            return out
        wired = wire_ts_app(app_content)
        if wired != app_content:
            _append_wired_entry(out, audit_idx, entry, wired)
    return out


def enrich_audit_files_from_paths(
    files: list[dict],
    *,
    tree_paths: list[str],
    read_file,
) -> list[dict]:
    """GitHub-tree variant of :func:`enrich_audit_files_from_repo`.

    ``read_file(path) -> str | None`` fetches blob text at the default branch.
    """
    found = _orphan_audit_index(files)
    if found is None:
        return files
    if tree_has_wired_audit(tree_paths, read_file):
        logger.info(
            "Default branch already has wired audit logging — "
            "dropping orphan root audit patch",
        )
        return _drop_orphan_audit_files(files)
    audit_idx, audit_name = found
    out = [dict(f) for f in files]

    if audit_name == "audit.py":
        entries = [
            p for p in tree_paths
            if (p.endswith("/app.py") or p == "app.py")
            and not (_IGNORED_PARTS & set(Path(p).parts))
        ]
        # Score like discover_python_app_entry
        ranked: list[tuple[str, str, str]] = []
        for entry in entries:
            text = read_file(entry)
            if not text or not any(m in text for m in _PYTHON_APP_MARKERS):
                continue
            pkg_dir = str(Path(entry).parent) if Path(entry).parent != Path(".") else "."
            if pkg_dir == ".":
                pkg_dir = ""
            ranked.append((pkg_dir, entry, text))
        if not ranked:
            return out
        ranked.sort(
            key=lambda c: (
                0 if c[0].startswith("apps/") else 1,
                0 if "/src/" in c[0] else 1,
                len(c[0]),
            ),
        )
        pkg_dir, entry, app_content = ranked[0]
        target = f"{pkg_dir}/audit.py" if pkg_dir else "audit.py"
        out[audit_idx]["target_path"] = target
        wired = wire_python_app(app_content)
        if wired != app_content:
            _append_wired_entry(out, audit_idx, entry, wired)
        return out

    if audit_name in ("audit.ts", "audit.js"):
        candidates = [
            p for p in tree_paths
            if Path(p).name in ("app.ts", "server.ts", "index.ts", "main.ts")
            and not (_IGNORED_PARTS & set(Path(p).parts))
        ]
        ranked_ts: list[tuple[str, str, str]] = []
        for entry in candidates:
            text = read_file(entry)
            if not text or not any(m in text for m in _TS_APP_MARKERS):
                continue
            pkg_dir = str(Path(entry).parent) if Path(entry).parent != Path(".") else ""
            ranked_ts.append((pkg_dir, entry, text))
        if not ranked_ts:
            return out
        ranked_ts.sort(
            key=lambda c: (
                0 if c[0].startswith("apps/") else 1,
                0 if "/src/" in c[0] else 1,
                len(c[0]),
            ),
        )
        pkg_dir, entry, app_content = ranked_ts[0]
        target = f"{pkg_dir}/{audit_name}" if pkg_dir else audit_name
        out[audit_idx]["target_path"] = target
        wired = wire_ts_app(app_content)
        if wired != app_content:
            _append_wired_entry(out, audit_idx, entry, wired)
    return out


def is_audit_delivery_file(file: dict) -> bool:
    """True when a staged file is an audit module or audit-wiring patch."""
    skill = str(file.get("skill_name") or "")
    if skill in {"app-audit-logging", "audit-policy"}:
        return True
    path = str(file.get("target_path") or file.get("path") or "").replace("\\", "/")
    if _is_audit_module_path(path):
        return True
    desc = str(file.get("description") or "").lower()
    return "audit" in desc and ("wire" in desc or "middleware" in desc)
