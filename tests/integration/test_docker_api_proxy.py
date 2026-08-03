from __future__ import annotations

import http.client
import json
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PROXY_CONFIG = ROOT / "deploy" / "docker-api-proxy" / "Caddyfile"
PROXY_IMAGE = (
    "caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d"
)


def _docker(*arguments: str, capture: bool = False) -> str:
    result = subprocess.run(
        ("docker", *arguments),
        check=True,
        capture_output=capture,
        text=True,
    )
    return result.stdout.strip() if capture else ""


@contextmanager
def _running_proxy() -> Iterator[tuple[str, str]]:
    suffix = uuid4().hex[:12]
    proxy = f"coengram-docker-api-proxy-test-{suffix}"
    fixture = f"coengram-docker-api-fixture-{suffix}"
    marker = f"coengram-proxy-log-{suffix}"
    try:
        _docker(
            "run",
            "--detach",
            "--name",
            fixture,
            "--label",
            "memory.telemetry=true",
            "python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df",
            "sh",
            "-c",
            f"printf '{marker}\\n'; exec sleep 300",
        )
        fixture_id = _docker("inspect", "--format", "{{.Id}}", fixture, capture=True)
        _docker(
            "run",
            "--detach",
            "--name",
            proxy,
            "--publish",
            "127.0.0.1::2375",
            "--volume",
            "/var/run/docker.sock:/var/run/docker.sock:ro",
            "--volume",
            f"{PROXY_CONFIG}:/etc/caddy/docker-api-proxy.Caddyfile:ro",
            PROXY_IMAGE,
            "caddy",
            "run",
            "--config",
            "/etc/caddy/docker-api-proxy.Caddyfile",
            "--adapter",
            "caddyfile",
        )
        published = _docker("port", proxy, "2375/tcp", capture=True)
        port = published.rsplit(":", maxsplit=1)[1]
        for _ in range(50):
            try:
                status, body = _request(port, "GET", "/_ping")
                if status == 200 and body.strip() == b"OK":
                    break
            except (http.client.HTTPException, OSError):
                pass
            time.sleep(0.1)
        else:
            raise AssertionError("Docker API proxy did not become ready")
        yield port, fixture_id
    finally:
        subprocess.run(
            ("docker", "rm", "--force", proxy, fixture), check=False, capture_output=True
        )


def _request(port: str, method: str, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_proxy_allows_only_docker_discovery_and_log_reads() -> None:
    with _running_proxy() as (port, fixture_id):
        status, version_body = _request(port, "GET", "/version")
        assert status == 200
        api_version = str(json.loads(version_body)["ApiVersion"])
        prefix = f"/v{api_version}"

        allowed = (
            f"{prefix}/containers/json?filters=%7B%22label%22%3A%5B%22memory.telemetry%3Dtrue%22%5D%7D",
            f"{prefix}/networks",
            f"{prefix}/containers/{fixture_id}/json",
            f"{prefix}/containers/{fixture_id}/logs?stdout=true&stderr=true",
        )
        responses = [_request(port, "GET", path) for path in allowed]

        assert all(status == 200 for status, _body in responses)
        assert fixture_id.encode() in responses[0][1]
        assert responses[1][1].startswith(b"[")
        assert json.loads(responses[2][1])["Id"] == fixture_id
        assert b"coengram-proxy-log-" in responses[3][1]

        forbidden = (
            ("GET", f"{prefix}/containers/{fixture_id}/archive?path=/etc/hostname"),
            ("GET", f"{prefix}/containers/{fixture_id}/stats?stream=false"),
            ("GET", f"{prefix}/events"),
            ("GET", f"{prefix}/images/json"),
            ("GET", f"{prefix}/secrets"),
            ("POST", f"{prefix}/containers/{fixture_id}/exec"),
            ("DELETE", f"{prefix}/containers/{fixture_id}"),
            ("POST", f"{prefix}/containers/json"),
            ("PUT", f"{prefix}/containers/json"),
            ("PATCH", f"{prefix}/containers/json"),
        )
        assert [_request(port, method, path)[0] for method, path in forbidden] == [403] * len(
            forbidden
        )
