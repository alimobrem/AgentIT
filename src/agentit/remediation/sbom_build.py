"""Build a meaningful CycloneDX SBOM for ``sbom-artifact`` source patches.

Prefer Syft when available on PATH; otherwise inventory components from
lockfiles / package manifests already present in the assessed repo
(``requirements.txt``, ``package.json``, ``go.mod``, …). Empty
``components: []`` shells are theater — clear-evidence refuses them.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Basename (or suffix) patterns we will read for inventory.
_MANIFEST_BASENAMES = frozenset({
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "Pipfile",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "pom.xml",
    "composer.json",
})

_REQ_LINE = re.compile(
    r"^\s*([A-Za-z0-9_.\-]+)\s*(?:[=<>!~]=?\s*([^\s;#]+))?",
)
_GO_REQUIRE = re.compile(
    r"^\s*([^\s]+)\s+v?([0-9][^\s]*)\s*(?://.*)?$",
)
_GEM_LINE = re.compile(
    r"""^\s*gem\s+['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?""",
)
_CARGO_DEP = re.compile(
    r'^\s*([A-Za-z0-9_\-]+)\s*=\s*(?:"([^"]+)"|\{[^}]*version\s*=\s*"([^"]+)")',
)
_POM_DEP = re.compile(
    r"<dependency>\s*"
    r"<groupId>([^<]+)</groupId>\s*"
    r"<artifactId>([^<]+)</artifactId>\s*"
    r"(?:<version>([^<]+)</version>\s*)?",
    re.IGNORECASE | re.DOTALL,
)
def manifest_paths_from_tree(tree_paths: list[str] | None) -> list[str]:
    """Return relative paths in ``tree_paths`` that look like dep manifests."""
    if not tree_paths:
        return []
    out: list[str] = []
    for path in tree_paths:
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        if name in _MANIFEST_BASENAMES:
            out.append(path)
    return sorted(out)


def _purl(eco: str, name: str, version: str | None) -> str | None:
    if not name:
        return None
    n = name.strip()
    v = (version or "").strip().lstrip("v")
    if not v or v.startswith("$") or v.startswith("{"):
        return f"pkg:{eco}/{n}"
    return f"pkg:{eco}/{n}@{v}"


def _component(
    *,
    name: str,
    version: str | None,
    purl: str | None,
    typ: str = "library",
) -> dict[str, Any]:
    c: dict[str, Any] = {"type": typ, "name": name}
    if version and not version.startswith(("$", "{")):
        c["version"] = version.lstrip("v")
    if purl:
        c["purl"] = purl
    return c


def _parse_requirements(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-"):
            continue
        m = _REQ_LINE.match(s)
        if not m:
            continue
        name, ver = m.group(1), m.group(2)
        if name.lower() in ("python", "pip"):
            continue
        out.append(_component(
            name=name, version=ver, purl=_purl("pypi", name, ver),
        ))
    return out


def _parse_pipfile(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    in_packages = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_packages = s.lower() in ("[packages]", "[dev-packages]")
            continue
        if not in_packages or not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        name, _, rest = s.partition("=")
        name = name.strip().strip("\"'")
        ver = rest.strip().strip("\"'")
        if ver in ("*", ""):
            ver = None
        out.append(_component(
            name=name, version=ver, purl=_purl("pypi", name, ver),
        ))
    return out


def _parse_pyproject(text: str) -> list[dict[str, Any]]:
    """Best-effort: PEP 621 ``dependencies = [...]`` and poetry tables."""
    out: list[dict[str, Any]] = []
    section = ""
    in_pep621_deps = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s.lower()
            in_pep621_deps = False
            continue
        if section in (
            "[tool.poetry.dependencies]",
            "[tool.poetry.dev-dependencies]",
            "[tool.poetry.group.dev.dependencies]",
        ):
            if "=" not in s or s.startswith("#"):
                continue
            name, _, rest = s.partition("=")
            name = name.strip().strip("\"'")
            if name.lower() == "python":
                continue
            ver = rest.strip().strip("\"'")
            if ver.startswith("{"):
                vm = re.search(r'version\s*=\s*"([^"]+)"', ver)
                ver = vm.group(1) if vm else None
            if ver in ("*", ""):
                ver = None
            out.append(_component(
                name=name, version=ver, purl=_purl("pypi", name, ver),
            ))
            continue
        if re.match(r"^dependencies\s*=", s, re.I):
            in_pep621_deps = True
        if in_pep621_deps:
            m = re.search(
                r"""["']([A-Za-z0-9_.\-]+)(?:\[[^\]]+\])?\s*([=<>!~][^"']*)?["']""",
                s,
            )
            if m:
                name, ver = m.group(1), (m.group(2) or "").lstrip("=<>!~ ")
                if name.lower() != "python":
                    out.append(_component(
                        name=name,
                        version=ver or None,
                        purl=_purl("pypi", name, ver or None),
                    ))
            if "]" in s:
                in_pep621_deps = False
    return out


def _parse_package_json(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        deps = data.get(key) or {}
        if not isinstance(deps, dict):
            continue
        for name, ver in deps.items():
            v = str(ver).lstrip("^~>=<") if ver is not None else None
            if v and (v.startswith("http") or v.startswith("file:") or v.startswith("git")):
                v = None
            out.append(_component(
                name=str(name), version=v, purl=_purl("npm", str(name), v),
            ))
    return out


def _parse_package_lock(text: str) -> list[dict[str, Any]]:
    """Direct deps from lockfile (skip nested ``node_modules`` paths)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    packages = data.get("packages")
    if isinstance(packages, dict):
        for path, meta in packages.items():
            if not path or not isinstance(meta, dict):
                continue  # root ""
            # Only top-level: "node_modules/foo" or "node_modules/@scope/pkg"
            if not path.startswith("node_modules/"):
                continue
            rest = path[len("node_modules/"):]
            if rest.startswith("@"):
                parts = rest.split("/")
                if len(parts) != 2:
                    continue
            elif "/" in rest:
                continue
            name = meta.get("name") or rest
            ver = meta.get("version")
            out.append(_component(
                name=str(name),
                version=str(ver) if ver else None,
                purl=_purl("npm", str(name), str(ver) if ver else None),
            ))
        if out:
            return out
    deps = data.get("dependencies") or {}
    if isinstance(deps, dict):
        for name, meta in deps.items():
            ver = meta.get("version") if isinstance(meta, dict) else None
            out.append(_component(
                name=str(name),
                version=str(ver) if ver else None,
                purl=_purl("npm", str(name), str(ver) if ver else None),
            ))
    return out


def _parse_go_mod(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    in_require = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("require ("):
            in_require = True
            continue
        if in_require:
            if s == ")":
                in_require = False
                continue
            m = _GO_REQUIRE.match(s)
            if m:
                name, ver = m.group(1), m.group(2)
                out.append(_component(
                    name=name, version=ver, purl=_purl("golang", name, ver),
                ))
            continue
        if s.startswith("require "):
            rest = s[len("require "):].strip()
            m = _GO_REQUIRE.match(rest)
            if m:
                name, ver = m.group(1), m.group(2)
                out.append(_component(
                    name=name, version=ver, purl=_purl("golang", name, ver),
                ))
    return out


def _parse_cargo(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    in_deps = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_deps = s.lower() in ("[dependencies]", "[dev-dependencies]")
            continue
        if not in_deps:
            continue
        m = _CARGO_DEP.match(s)
        if m:
            name, ver = m.group(1), m.group(2) or m.group(3)
            out.append(_component(
                name=name, version=ver, purl=_purl("cargo", name, ver),
            ))
    return out


def _parse_gemfile(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = _GEM_LINE.match(line)
        if m:
            name, ver = m.group(1), m.group(2)
            out.append(_component(
                name=name, version=ver, purl=_purl("gem", name, ver),
            ))
    return out


def _parse_pom(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in _POM_DEP.finditer(text):
        group, artifact, ver = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip() or None
        name = f"{group}:{artifact}"
        out.append(_component(
            name=name, version=ver, purl=_purl("maven", name, ver),
        ))
    return out


def _parse_composer(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for key in ("require", "require-dev"):
        deps = data.get(key) or {}
        if not isinstance(deps, dict):
            continue
        for name, ver in deps.items():
            if str(name).startswith("php"):
                continue
            v = str(ver).lstrip("^~>=<") if ver else None
            out.append(_component(
                name=str(name), version=v, purl=_purl("composer", str(name), v),
            ))
    return out


def components_from_manifest_text(path: str, text: str) -> list[dict[str, Any]]:
    """Parse one manifest file into CycloneDX component dicts."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.startswith("requirements") and name.endswith(".txt"):
        return _parse_requirements(text)
    if name == "pipfile":
        return _parse_pipfile(text)
    if name == "pyproject.toml":
        return _parse_pyproject(text)
    if name == "package.json":
        return _parse_package_json(text)
    if name == "package-lock.json":
        return _parse_package_lock(text)
    if name == "go.mod":
        return _parse_go_mod(text)
    if name == "cargo.toml":
        return _parse_cargo(text)
    if name == "gemfile":
        return _parse_gemfile(text)
    if name == "pom.xml":
        return _parse_pom(text)
    if name == "composer.json":
        return _parse_composer(text)
    return []


def components_from_manifests(manifests: dict[str, str]) -> list[dict[str, Any]]:
    """Merge unique components from a path→content map."""
    seen: set[tuple[str, str | None]] = set()
    out: list[dict[str, Any]] = []
    # Prefer lockfiles / richer sources first when both exist.
    prefer = (
        "package-lock.json", "go.mod", "requirements.txt", "pyproject.toml",
        "package.json", "Pipfile", "Cargo.toml", "Gemfile", "pom.xml",
        "composer.json",
    )

    def sort_key(item: tuple[str, str]) -> tuple[int, str]:
        path = item[0].replace("\\", "/").rsplit("/", 1)[-1]
        try:
            return (prefer.index(path), item[0])
        except ValueError:
            return (len(prefer), item[0])

    for path, text in sorted(manifests.items(), key=sort_key):
        if not text:
            continue
        for comp in components_from_manifest_text(path, text):
            key = (comp.get("name") or "", comp.get("version"))
            if key in seen or not key[0]:
                continue
            seen.add(key)
            out.append(comp)
    return out


def try_syft_cyclonedx(repo_path: Path) -> dict[str, Any] | None:
    """Run Syft if installed; return parsed CycloneDX dict or None."""
    if shutil.which("syft") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "syft", str(repo_path),
                "-o", "cyclonedx-json",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("syft unavailable/failed: %s", exc)
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        logger.info(
            "syft exited %s: %s",
            proc.returncode, (proc.stderr or "")[:200],
        )
        return None
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    comps = doc.get("components")
    if not isinstance(comps, list) or not comps:
        return None
    return doc


def build_cyclonedx_document(
    app_name: str,
    components: list[dict[str, Any]],
    *,
    tools: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    app = (app_name or "app").lower().replace("_", "-").replace(".", "-")
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:agentit-{app}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "component": {
                "type": "application",
                "name": app,
                "version": "0.0.0",
            },
            "tools": tools or [
                {"vendor": "AgentIT", "name": "sbom-artifact"},
            ],
        },
        "components": list(components),
    }


def collect_manifests(
    *,
    read_file: Callable[[str], str | None] | None = None,
    tree_paths: list[str] | None = None,
    repo_path: Path | None = None,
    snapshot_files: dict[str, str] | None = None,
) -> dict[str, str]:
    """Gather manifest path→text from snapshot, local path, or GitHub read_file."""
    manifests: dict[str, str] = {}
    if snapshot_files:
        for path, text in snapshot_files.items():
            name = path.replace("\\", "/").rsplit("/", 1)[-1]
            if name in _MANIFEST_BASENAMES and text:
                manifests[path] = text
    if repo_path is not None and repo_path.is_dir():
        for path in repo_path.rglob("*"):
            if not path.is_file():
                continue
            if path.name not in _MANIFEST_BASENAMES:
                continue
            try:
                rel = path.relative_to(repo_path).as_posix()
                manifests[rel] = path.read_text(errors="ignore")
            except OSError:
                continue
    if read_file is not None:
        paths = manifest_paths_from_tree(tree_paths)
        if not paths:
            # Root-level fallbacks when tree listing unavailable.
            paths = sorted(_MANIFEST_BASENAMES)
        for path in paths:
            if path in manifests:
                continue
            try:
                text = read_file(path)
            except Exception:
                text = None
            if text:
                manifests[path] = text
    return manifests


def enrich_sbom_artifact_files(
    files: list[dict],
    *,
    read_file: Callable[[str], str | None] | None = None,
    tree_paths: list[str] | None = None,
    repo_path: Path | None = None,
    snapshot_files: dict[str, str] | None = None,
    app_name: str | None = None,
) -> list[dict]:
    """Populate empty/trivial ``sbom-artifact`` CycloneDX with real components.

    Prefer Syft against ``repo_path``; else inventory from manifests via
    ``read_file`` / tree / snapshot. Leaves non-sbom files untouched.
    """
    out: list[dict] = []
    for f in files:
        skill = (f.get("skill_name") or "").lower().replace("_", "-")
        target = str(f.get("target_path") or f.get("path") or "")
        is_sbom = (
            skill == "sbom-artifact"
            or "sbom" in target.lower()
            or target.lower().endswith(".cdx.json")
        )
        if not is_sbom:
            out.append(f)
            continue

        content = f.get("content") or ""
        existing_comps: list[Any] = []
        try:
            parsed = json.loads(content) if content.strip() else {}
            if isinstance(parsed.get("components"), list):
                existing_comps = parsed["components"]
        except json.JSONDecodeError:
            parsed = {}

        if existing_comps:
            out.append(f)
            continue

        app = app_name or (
            (parsed.get("metadata") or {}).get("component") or {}
        ).get("name") or "app"

        doc: dict[str, Any] | None = None
        if repo_path is not None:
            doc = try_syft_cyclonedx(Path(repo_path))
            if doc is not None:
                # Keep app metadata name when Syft's is generic.
                meta = doc.setdefault("metadata", {})
                comp = meta.setdefault("component", {})
                if isinstance(comp, dict) and not comp.get("name"):
                    comp["name"] = app
                    comp.setdefault("type", "application")

        if doc is None:
            manifests = collect_manifests(
                read_file=read_file,
                tree_paths=tree_paths,
                repo_path=Path(repo_path) if repo_path else None,
                snapshot_files=snapshot_files,
            )
            components = components_from_manifests(manifests)
            if not components:
                logger.info(
                    "sbom enrich: no components for %s (manifests=%s)",
                    target, sorted(manifests),
                )
                out.append(f)
                continue
            tools = [{"vendor": "AgentIT", "name": "sbom-artifact"}]
            if manifests:
                tools.append({"vendor": "AgentIT", "name": "manifest-inventory"})
            doc = build_cyclonedx_document(str(app), components, tools=tools)

        new_f = dict(f)
        new_f["content"] = json.dumps(doc, indent=2) + "\n"
        desc = (f.get("description") or "").rstrip()
        n = len(doc.get("components") or [])
        suffix = f" — {n} component(s) from repo inventory"
        if suffix not in desc:
            new_f["description"] = f"{desc}{suffix}" if desc else suffix.strip(" —")
        out.append(new_f)
    return out
