from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from agentit.models import (
    Database, DimensionScore, Finding, Framework, Language, Runtime, Severity, StackInfo,
)

LANG_EXTENSIONS: dict[str, str] = {
    ".py": "python", ".go": "go", ".java": "java", ".kt": "kotlin",
    ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".rb": "ruby", ".rs": "rust", ".cs": "csharp", ".php": "php",
    ".swift": "swift", ".scala": "scala", ".cpp": "cpp", ".c": "c",
}

FRAMEWORK_PATTERNS: dict[str, dict] = {
    "flask": {"files": ["requirements.txt", "Pipfile", "pyproject.toml"], "pattern": r"flask", "language": "python"},
    "django": {"files": ["requirements.txt", "Pipfile", "pyproject.toml", "manage.py"], "pattern": r"django", "language": "python"},
    "fastapi": {"files": ["requirements.txt", "Pipfile", "pyproject.toml"], "pattern": r"fastapi", "language": "python"},
    "spring boot": {"files": ["pom.xml", "build.gradle"], "pattern": r"spring-boot", "language": "java"},
    "gin": {"files": ["go.mod", "go.sum"], "pattern": r"github\.com/gin-gonic/gin", "language": "go"},
    "echo": {"files": ["go.mod", "go.sum"], "pattern": r"github\.com/labstack/echo", "language": "go"},
    "fiber": {"files": ["go.mod", "go.sum"], "pattern": r"github\.com/gofiber/fiber", "language": "go"},
    "next.js": {"files": ["package.json"], "pattern": r'"next"', "language": "typescript"},
    "react": {"files": ["package.json"], "pattern": r'"react"', "language": "typescript"},
    "express": {"files": ["package.json"], "pattern": r'"express"', "language": "javascript"},
    "rails": {"files": ["Gemfile"], "pattern": r"rails", "language": "ruby"},
}

DB_PATTERNS: dict[str, list[str]] = {
    "postgresql": ["psycopg", "pg ", '"pg"', "postgres", "postgresql", "pq"],
    "mysql": ["mysql-connector", "mysqlclient", "pymysql", "mysql"],
    "mongodb": ["pymongo", "mongoose", "mongodb", "mongoclient"],
    "redis": ["redis", "ioredis"],
    "sqlite": ["sqlite3", "sqlite", "better-sqlite"],
    "elasticsearch": ["elasticsearch", "elastic"],
}

PACKAGE_MANAGER_FILES: dict[str, str] = {
    "go.mod": "go mod",
    "go.sum": "go mod",
    "requirements.txt": "pip",
    "Pipfile": "pipenv",
    "pyproject.toml": "pip",
    "package.json": "npm",
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "Gemfile": "bundler",
    "Cargo.toml": "cargo",
    "composer.json": "composer",
}


class StackDetector:
    dimension = "stack"

    def detect(self, repo_path: Path) -> StackInfo:
        languages = self._detect_languages(repo_path)
        frameworks = self._detect_frameworks(repo_path)
        databases = self._detect_databases(repo_path)
        runtimes = self._detect_runtimes(repo_path, languages)
        package_managers = self._detect_package_managers(repo_path)
        return StackInfo(
            languages=languages,
            frameworks=frameworks,
            databases=databases,
            runtimes=runtimes,
            package_managers=package_managers,
        )

    def analyze(self, repo_path: Path) -> DimensionScore:
        self.detect(repo_path)
        return DimensionScore(dimension="stack", score=0, max_score=0, findings=[])

    def _detect_languages(self, repo_path: Path) -> list[Language]:
        counts: Counter[str] = Counter()
        versions: dict[str, str | None] = {}

        for file_path in repo_path.rglob("*"):
            if file_path.is_file() and not _is_ignored(file_path, repo_path):
                ext = file_path.suffix.lower()
                if ext in LANG_EXTENSIONS:
                    lang = LANG_EXTENSIONS[ext]
                    counts[lang] += 1

        total = sum(counts.values())
        if total == 0:
            return []

        versions = self._detect_language_versions(repo_path)

        return [
            Language(
                name=lang,
                version=versions.get(lang),
                file_count=count,
                percentage=round(count / total * 100, 1),
            )
            for lang, count in counts.most_common()
        ]

    def _detect_language_versions(self, repo_path: Path) -> dict[str, str | None]:
        versions: dict[str, str | None] = {}

        go_mod = repo_path / "go.mod"
        if go_mod.exists():
            content = go_mod.read_text()
            match = re.search(r"^go\s+(\d+\.\d+)", content, re.MULTILINE)
            if match:
                versions["go"] = match.group(1)

        for pyfile in ["pyproject.toml", ".python-version", "runtime.txt"]:
            p = repo_path / pyfile
            if p.exists():
                content = p.read_text()
                match = re.search(r"python.*?(\d+\.\d+)", content, re.IGNORECASE)
                if match:
                    versions["python"] = match.group(1)
                    break

        pkg_json = repo_path / "package.json"
        if pkg_json.exists():
            try:
                data = json.loads(pkg_json.read_text())
                engines = data.get("engines", {})
                if "node" in engines:
                    match = re.search(r"(\d+\.\d+)", engines["node"])
                    if match:
                        versions.setdefault("javascript", match.group(1))
                        versions.setdefault("typescript", match.group(1))
            except (json.JSONDecodeError, KeyError):
                pass

        pom = repo_path / "pom.xml"
        if pom.exists():
            content = pom.read_text()
            match = re.search(r"<java\.version>(\d+)</java\.version>", content)
            if match:
                versions["java"] = match.group(1)

        return versions

    def _detect_frameworks(self, repo_path: Path) -> list[Framework]:
        found: list[Framework] = []
        file_contents: dict[str, str] = {}

        for name, info in FRAMEWORK_PATTERNS.items():
            for dep_file in info["files"]:
                dep_path = repo_path / dep_file
                if dep_path.exists():
                    if dep_file not in file_contents:
                        file_contents[dep_file] = dep_path.read_text()
                    content = file_contents[dep_file]
                    if re.search(info["pattern"], content, re.IGNORECASE):
                        version = self._extract_version(content, name, dep_file)
                        found.append(Framework(name=name, version=version, language=info["language"]))
                        break

        return found

    def _extract_version(self, content: str, name: str, dep_file: str) -> str | None:
        if dep_file == "package.json":
            try:
                data = json.loads(content)
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                for key, ver in deps.items():
                    if name.replace(".", "").replace(" ", "").lower() in key.lower().replace(".", "").replace(" ", ""):
                        return ver.lstrip("^~>=<")
                    return None
            except (json.JSONDecodeError, KeyError):
                pass
        if dep_file in ("requirements.txt", "Pipfile"):
            pattern = rf"{re.escape(name)}[=<>~!]*=*(\d+[\d.]*)"
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                return match.group(1)
        if dep_file == "pom.xml":
            match = re.search(r"<version>(\d+[\d.]*)</version>", content)
            if match:
                return match.group(1)
        return None

    def _detect_databases(self, repo_path: Path) -> list[Database]:
        found: list[Database] = []
        all_content = ""

        for candidate in [
            "requirements.txt", "Pipfile", "pyproject.toml",
            "package.json", "go.mod", "go.sum",
            "pom.xml", "build.gradle", "Gemfile", "Cargo.toml",
            "docker-compose.yml", "docker-compose.yaml",
        ]:
            p = repo_path / candidate
            if p.exists():
                all_content += " " + p.read_text()

        seen: set[str] = set()
        for db_name, patterns in DB_PATTERNS.items():
            for pattern in patterns:
                if pattern.lower() in all_content.lower() and db_name not in seen:
                    seen.add(db_name)
                    found.append(Database(name=db_name))
                    break

        return found

    def _detect_runtimes(self, repo_path: Path, languages: list[Language]) -> list[Runtime]:
        runtimes: list[Runtime] = []
        lang_names = {l.name for l in languages}

        if "python" in lang_names:
            py_lang = next(l for l in languages if l.name == "python")
            runtimes.append(Runtime(name="cpython", version=py_lang.version))
        if "javascript" in lang_names or "typescript" in lang_names:
            runtimes.append(Runtime(name="node.js", version=None))
        if "java" in lang_names:
            java_lang = next(l for l in languages if l.name == "java")
            runtimes.append(Runtime(name="jvm", version=java_lang.version))
        if "go" in lang_names:
            go_lang = next(l for l in languages if l.name == "go")
            runtimes.append(Runtime(name="go", version=go_lang.version))

        return runtimes

    def _detect_package_managers(self, repo_path: Path) -> list[str]:
        found: set[str] = set()
        for filename, pm in PACKAGE_MANAGER_FILES.items():
            if (repo_path / filename).exists():
                found.add(pm)
        return sorted(found)


def _is_ignored(file_path: Path, repo_root: Path) -> bool:
    relative = file_path.relative_to(repo_root)
    parts = relative.parts
    ignored_dirs = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "vendor", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
        "target", ".idea", ".vscode",
    }
    return bool(ignored_dirs & set(parts))
