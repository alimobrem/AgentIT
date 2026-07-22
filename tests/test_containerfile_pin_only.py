"""Container skill: pin-only FROM — never gut an existing Dockerfile (#165)."""
from __future__ import annotations

from agentit.remediation.source_patches import (
    apply_containerfile_pin_only,
    is_destructive_dockerfile_rewrite,
    pin_dockerfile_from_lines,
)


_REAL_CONTAINERFILE = """\
FROM registry.access.redhat.com/ubi9/python-312:latest

USER 0
RUN curl -sfL https://example.com/oc | tar -xz -C /usr/local/bin oc
USER 1001

WORKDIR /opt/app-root/src
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[dev]"
COPY src/ src/
COPY skills/ skills/
COPY tests/ tests/
COPY chart/ chart/

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \\
  CMD curl -f http://localhost:8080/healthz || exit 1
"""


class TestPinDockerfileFromLines:
    def test_pins_latest_to_floating_one(self) -> None:
        out = pin_dockerfile_from_lines(_REAL_CONTAINERFILE)
        assert "python-312:1" in out
        assert ":latest" not in out
        assert "pip install" in out
        assert "COPY skills/" in out

    def test_digest_pin_when_requested(self) -> None:
        out = pin_dockerfile_from_lines(
            "FROM registry.access.redhat.com/ubi9/python-312:latest\nUSER 1001\n",
            floating_tag="sha256:abc123",
        )
        assert "python-312@sha256:abc123" in out
        assert ":latest" not in out


class TestDestructiveRewrite:
    def test_stub_is_destructive(self) -> None:
        stub = (
            "FROM registry.access.redhat.com/ubi9/python-312:1\n"
            "WORKDIR /app\nCOPY . .\nUSER 1001\nEXPOSE 8080\n"
            "HEALTHCHECK CMD true\n"
        )
        bad, reason = is_destructive_dockerfile_rewrite(_REAL_CONTAINERFILE, stub)
        assert bad
        assert "guts" in reason or "drops" in reason or "rewrites" in reason

    def test_pin_only_not_destructive(self) -> None:
        pinned = pin_dockerfile_from_lines(_REAL_CONTAINERFILE)
        bad, reason = is_destructive_dockerfile_rewrite(_REAL_CONTAINERFILE, pinned)
        assert not bad, reason


class TestApplyContainerfilePinOnly:
    def test_replaces_stub_with_pin_of_existing(self) -> None:
        stub = (
            "# agentit-pin-only: delivery will pin FROM on existing Containerfile\n"
            "FROM registry.access.redhat.com/ubi9/ubi-minimal:1\n"
        )
        files = [{
            "target_path": "Containerfile",
            "content": stub,
            "skill_name": "containerfile",
            "description": "pin-only marker",
        }]
        out = apply_containerfile_pin_only(
            files, read_file=lambda _p: _REAL_CONTAINERFILE,
        )
        assert len(out) == 1
        assert out[0]["base_content"] == _REAL_CONTAINERFILE
        assert ":latest" not in out[0]["content"]
        assert "pip install" in out[0]["content"]
        assert "COPY skills/" in out[0]["content"]

    def test_greenfield_keeps_stub_when_missing(self) -> None:
        stub = (
            "FROM registry.access.redhat.com/ubi9/python-312:1\n"
            "WORKDIR /app\nCOPY . .\nUSER 1001\n"
        )
        files = [{
            "target_path": "Dockerfile",
            "content": stub,
            "skill_name": "containerfile",
        }]
        out = apply_containerfile_pin_only(files, read_file=lambda _p: None)
        assert out[0]["content"] == stub
        assert "base_content" not in out[0] or not out[0].get("base_content")
