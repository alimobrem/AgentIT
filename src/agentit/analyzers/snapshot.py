"""Single-pass in-memory repo snapshot for assessment.

Assessment used to re-walk and re-read the tree once per analyzer (plus
architecture / check_engine / LLM file-list passes). ``RepoSnapshot.build``
reads each text file at most once (with a size cap), then
``iter_text_files`` / helpers prefer the active snapshot via a ContextVar
so analyzers need no signature changes.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path

from agentit.analyzers.base import TEXT_EXTENSIONS, is_ignored

# Default per-file cap — large enough for real manifests/source, small
# enough that a vendored multi-MB blob cannot blow assessment memory.
DEFAULT_MAX_FILE_BYTES = 512_000

_active_snapshot: ContextVar[RepoSnapshot | None] = ContextVar(
    "agentit_repo_snapshot", default=None,
)


@dataclass(frozen=True)
class RepoSnapshot:
    """In-memory map of relative posix path → text for one assessment."""

    root: Path
    files: dict[str, str] = field(default_factory=dict)
    skipped_oversized: int = 0

    @classmethod
    def build(
        cls,
        repo_path: Path,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        extensions: set[str] | None = None,
    ) -> RepoSnapshot:
        # Union analyzer text set with stack-detector language extensions and
        # common infra/CI filenames so one pass serves every consumer.
        from agentit.analyzers.stack_detector import LANG_EXTENSIONS

        exts = set(extensions or TEXT_EXTENSIONS) | set(LANG_EXTENSIONS)
        exts.add(".tf")
        special_names = {
            "Jenkinsfile", "Dockerfile", "Makefile", "go.mod", "go.sum",
            "Gemfile", "pom.xml", "build.gradle", "Cargo.toml",
            "requirements.txt", "Pipfile", "pyproject.toml", "package.json",
            "yarn.lock", "pnpm-lock.yaml", "composer.json",
        }
        root = repo_path.resolve()
        files: dict[str, str] = {}
        skipped = 0
        for fp in repo_path.rglob("*"):
            if not fp.is_file() or is_ignored(fp, repo_path):
                continue
            try:
                resolved = fp.resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(root):
                continue
            name = fp.name
            suffix = fp.suffix.lower()
            is_compose = name.startswith("docker-compose") and suffix in {".yml", ".yaml"}
            if suffix not in exts and name not in special_names and not is_compose:
                continue
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            if size > max_file_bytes:
                skipped += 1
                continue
            try:
                text = fp.read_text(errors="ignore")
            except OSError:
                continue
            rel = fp.relative_to(repo_path).as_posix()
            files[rel] = text
        return cls(root=root, files=files, skipped_oversized=skipped)

    def path_for(self, rel: str) -> Path:
        return self.root / rel

    def iter_text(
        self,
        extensions: set[str] | None = None,
    ) -> Iterator[tuple[Path, str]]:
        exts = extensions or TEXT_EXTENSIONS
        for rel, text in sorted(self.files.items()):
            path = self.path_for(rel)
            if path.suffix.lower() in exts or path.name in {"Jenkinsfile"}:
                yield path, text

    def file_paths(self) -> list[str]:
        return sorted(self.files)

    def any_suffix(self, suffix: str) -> bool:
        suffix = suffix.lower()
        return any(rel.lower().endswith(suffix) for rel in self.files)

    def glob_suffixes(self, *suffixes: str) -> list[str]:
        lowered = tuple(s.lower() for s in suffixes)
        return [rel for rel in sorted(self.files) if rel.lower().endswith(lowered)]


def get_active_snapshot() -> RepoSnapshot | None:
    return _active_snapshot.get()


@contextmanager
def use_snapshot(snapshot: RepoSnapshot | None):
    """Activate ``snapshot`` for the current context (and copied worker contexts)."""
    token: Token = _active_snapshot.set(snapshot)
    try:
        yield snapshot
    finally:
        _active_snapshot.reset(token)
