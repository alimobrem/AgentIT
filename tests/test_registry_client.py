"""Container registry client -- real digest resolution for Dockerfile
pinning (see source_patches.pin_dockerfile_with_real_digests()).

All tests mock ``requests`` -- no live network calls, no dependency on
any specific registry being reachable during CI.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.remediation.registry_client import (
    _anonymous_bearer_token,
    parse_image_ref,
    resolve_image_digest,
)


class TestParseImageRef:
    def test_registry_and_repository_and_tag(self) -> None:
        assert parse_image_ref("registry.access.redhat.com/ubi9/python-312:latest") == (
            "registry.access.redhat.com", "ubi9/python-312", "latest",
        )

    def test_defaults_to_latest_when_no_tag(self) -> None:
        assert parse_image_ref("registry.access.redhat.com/ubi9/python-312") == (
            "registry.access.redhat.com", "ubi9/python-312", "latest",
        )

    def test_bare_image_name_implies_docker_hub_library(self) -> None:
        registry, repository, tag = parse_image_ref("nginx:latest")
        assert registry == "registry-1.docker.io"
        assert repository == "library/nginx"
        assert tag == "latest"

    def test_docker_hub_org_repo_without_registry_host(self) -> None:
        registry, repository, tag = parse_image_ref("someorg/someapp:1.2.3")
        assert registry == "registry-1.docker.io"
        assert repository == "someorg/someapp"
        assert tag == "1.2.3"

    def test_registry_with_port_is_recognized_as_a_host(self) -> None:
        registry, repository, tag = parse_image_ref("localhost:5000/myapp:latest")
        assert registry == "localhost:5000"
        assert repository == "myapp"
        assert tag == "latest"

    def test_already_digest_pinned_returns_empty(self) -> None:
        assert parse_image_ref(
            "registry.access.redhat.com/ubi9/python-312@sha256:" + "a" * 64,
        ) == ("", "", "")

    def test_empty_input_returns_empty(self) -> None:
        assert parse_image_ref("") == ("", "", "")


class TestAnonymousBearerToken:
    def test_returns_none_for_non_bearer_challenge(self) -> None:
        assert _anonymous_bearer_token("Basic realm=x", timeout=1.0) is None

    def test_returns_none_when_realm_missing(self) -> None:
        assert _anonymous_bearer_token('Bearer service="x"', timeout=1.0) is None

    def test_fetches_token_from_parsed_realm(self) -> None:
        challenge = (
            'Bearer realm="https://auth.example.com/token",'
            'service="registry.example.com",scope="repository:x:pull"'
        )
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"token": "abc123"}
        with patch("requests.get", return_value=mock_resp) as mock_get:
            token = _anonymous_bearer_token(challenge, timeout=1.0)
        assert token == "abc123"
        called_url = mock_get.call_args[0][0]
        assert called_url == "https://auth.example.com/token"
        called_params = mock_get.call_args[1]["params"]
        assert "realm" not in called_params
        assert called_params["service"] == "registry.example.com"

    def test_falls_back_to_access_token_key(self) -> None:
        challenge = 'Bearer realm="https://auth.example.com/token"'
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"access_token": "xyz789"}
        with patch("requests.get", return_value=mock_resp):
            token = _anonymous_bearer_token(challenge, timeout=1.0)
        assert token == "xyz789"

    def test_returns_none_on_non_200(self) -> None:
        mock_resp = MagicMock(status_code=403)
        with patch("requests.get", return_value=mock_resp):
            token = _anonymous_bearer_token('Bearer realm="https://x/token"', timeout=1.0)
        assert token is None


class TestResolveImageDigest:
    def test_returns_digest_on_successful_anonymous_get(self) -> None:
        mock_resp = MagicMock(status_code=200)
        mock_resp.headers = {"Docker-Content-Digest": "sha256:" + "a" * 64}
        with patch("requests.get", return_value=mock_resp) as mock_get:
            digest = resolve_image_digest("registry.access.redhat.com/ubi9/python-312:latest")
        assert digest == "sha256:" + "a" * 64
        called_url = mock_get.call_args[0][0]
        assert called_url == (
            "https://registry.access.redhat.com/v2/ubi9/python-312/manifests/latest"
        )

    def test_follows_bearer_challenge_then_succeeds(self) -> None:
        challenge_resp = MagicMock(status_code=401)
        challenge_resp.headers = {
            "WWW-Authenticate": 'Bearer realm="https://auth.example.com/token",service="x"',
        }
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        success_resp = MagicMock(status_code=200)
        success_resp.headers = {"Docker-Content-Digest": "sha256:" + "b" * 64}

        with patch("requests.get", side_effect=[challenge_resp, token_resp, success_resp]):
            digest = resolve_image_digest("quay.io/org/app:latest")
        assert digest == "sha256:" + "b" * 64

    def test_returns_none_when_bearer_challenge_yields_no_token(self) -> None:
        challenge_resp = MagicMock(status_code=401)
        challenge_resp.headers = {"WWW-Authenticate": "Basic realm=x"}
        with patch("requests.get", return_value=challenge_resp):
            digest = resolve_image_digest("quay.io/org/private:latest")
        assert digest is None

    def test_returns_none_on_network_error(self) -> None:
        import requests

        with patch("requests.get", side_effect=requests.RequestException("boom")):
            digest = resolve_image_digest("registry.access.redhat.com/ubi9/python-312:latest")
        assert digest is None

    def test_returns_none_when_digest_header_missing(self) -> None:
        mock_resp = MagicMock(status_code=200, headers={})
        with patch("requests.get", return_value=mock_resp):
            digest = resolve_image_digest("registry.access.redhat.com/ubi9/python-312:latest")
        assert digest is None

    def test_returns_none_when_digest_header_is_not_sha256(self) -> None:
        """Defensive: only trust a real sha256 digest, never pass through
        something malformed/unexpected as if it were a real pin."""
        mock_resp = MagicMock(status_code=200)
        mock_resp.headers = {"Docker-Content-Digest": "not-a-real-digest"}
        with patch("requests.get", return_value=mock_resp):
            digest = resolve_image_digest("registry.access.redhat.com/ubi9/python-312:latest")
        assert digest is None

    def test_returns_none_for_already_digest_pinned_reference(self) -> None:
        with patch("requests.get") as mock_get:
            digest = resolve_image_digest(
                "registry.access.redhat.com/ubi9/python-312@sha256:" + "c" * 64,
            )
        assert digest is None
        mock_get.assert_not_called()
