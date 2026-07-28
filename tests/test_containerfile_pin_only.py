"""Container skill: pin-only FROM — never gut an existing Dockerfile (#165)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agentit.remediation.source_patches import (
    apply_containerfile_pin_only,
    is_destructive_dockerfile_rewrite,
    pin_dockerfile_from_lines,
    pin_dockerfile_with_real_digests,
)


@pytest.fixture(autouse=True)
def _no_live_registry_calls():
    """harden_dockerfile_content() (used by apply_containerfile_pin_only)
    now attempts a real registry digest lookup -- this suite must stay
    hermetic/offline, so every test here gets the "registry unreachable"
    fallback (floating :1 tag) unless it explicitly overrides this mock
    to test digest resolution itself (see TestPinDockerfileWithRealDigests)."""
    with patch(
        "agentit.remediation.registry_client.resolve_image_digest", return_value=None,
    ):
        yield


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


class TestPinDockerfileWithRealDigests:
    """A floating tag (:1) is still not a real pin -- confirmed live
    (2026-07-28, pulse-agent/pulse-ui) that it satisfies the naive
    "no :latest" check without providing real image immutability.
    pin_dockerfile_with_real_digests() resolves the actual current
    digest when possible, falling back to :1 per-image when it can't."""

    def test_uses_real_digest_when_resolvable(self) -> None:
        with patch(
            "agentit.remediation.registry_client.resolve_image_digest",
            return_value="sha256:" + "a" * 64,
        ):
            out = pin_dockerfile_with_real_digests(
                "FROM registry.access.redhat.com/ubi9/python-312:latest\nUSER 1001\n",
            )
        assert f"python-312@sha256:{'a' * 64}" in out
        assert ":latest" not in out
        assert ":1" not in out

    def test_falls_back_to_floating_tag_when_digest_unresolvable(self) -> None:
        with patch(
            "agentit.remediation.registry_client.resolve_image_digest", return_value=None,
        ):
            out = pin_dockerfile_with_real_digests(
                "FROM registry.access.redhat.com/ubi9/python-312:latest\nUSER 1001\n",
            )
        assert "python-312:1" in out
        assert ":latest" not in out

    def test_multi_stage_build_resolves_each_distinct_image_independently(self) -> None:
        """Two different base images each on :latest must never share the
        same digest -- a shared tag/digest applied uniformly would be
        actively wrong (node's digest is not nginx's digest)."""
        digests = {
            "node:latest": "sha256:" + "1" * 64,
            "nginx:latest": "sha256:" + "2" * 64,
        }

        def _fake_resolve(image_ref: str) -> str | None:
            return digests.get(image_ref)

        with patch(
            "agentit.remediation.registry_client.resolve_image_digest",
            side_effect=_fake_resolve,
        ):
            out = pin_dockerfile_with_real_digests(
                "FROM node:latest AS build\nRUN npm install\n"
                "FROM nginx:latest\nCOPY --from=build /app /usr/share/nginx/html\n",
            )
        assert f"node@sha256:{'1' * 64}" in out
        assert f"nginx@sha256:{'2' * 64}" in out
        assert ":latest" not in out

    def test_same_image_resolved_only_once(self) -> None:
        """Two FROM lines for the SAME image must reuse one resolved
        digest, not make a redundant registry call per line."""
        with patch(
            "agentit.remediation.registry_client.resolve_image_digest",
            return_value="sha256:" + "b" * 64,
        ) as mock_resolve:
            pin_dockerfile_with_real_digests(
                "FROM python:latest AS build\nRUN pip install -e .\n"
                "FROM python:latest\nCOPY --from=build /app /app\n",
            )
        assert mock_resolve.call_count == 1


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
