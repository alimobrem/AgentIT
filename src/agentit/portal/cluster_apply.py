from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_SKIP_EXTENSIONS = frozenset({".sh", ".md", ".json", ".txt", ".toml", ".cfg", ".ini"})


def _find_cli() -> str:
    """Return 'oc' if available, else 'kubectl', else raise."""
    for cmd in ("oc", "kubectl"):
        if shutil.which(cmd):
            return cmd
    msg = "Neither oc nor kubectl found on PATH"
    raise FileNotFoundError(msg)


def apply_manifests_to_cluster(
    files: list[dict],
    namespace: str = "default",
    dry_run: bool = False,
) -> dict:
    """Apply manifests to the cluster via oc/kubectl apply.

    Parameters
    ----------
    files:
        List of dicts with keys: category, path, content, description.
    namespace:
        Target namespace for ``-n``.
    dry_run:
        If True, pass ``--dry-run=client``.

    Returns
    -------
    dict with keys ``applied``, ``skipped``, ``errors`` (each a list of str).
    """
    cli = _find_cli()
    applied: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    tmpdir = tempfile.mkdtemp(prefix="agentit-apply-")
    try:
        for entry in files:
            fpath = entry["path"]
            suffix = Path(fpath).suffix.lower()

            if suffix in _SKIP_EXTENSIONS or suffix not in (".yaml", ".yml"):
                skipped.append(fpath)
                continue

            tmp_file = Path(tmpdir) / Path(fpath).name
            tmp_file.write_text(entry["content"])

            cmd = [cli, "apply", "-f", str(tmp_file), "-n", namespace]
            if dry_run:
                cmd.append("--dry-run=client")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                applied.append(fpath)
                logger.info("Applied %s: %s", fpath, result.stdout.strip())
            else:
                errors.append(f"{fpath}: {result.stderr.strip()}")
                logger.error("Failed %s: %s", fpath, result.stderr.strip())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return {"applied": applied, "skipped": skipped, "errors": errors}
