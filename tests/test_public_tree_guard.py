from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _guard() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "verify-public-tree.py"
    spec = importlib.util.spec_from_file_location("verify_public_tree", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secret_patterns_detect_high_confidence_credentials_without_echoing_them() -> None:
    guard = _guard()
    candidates = (
        "-----BEGIN " + "PRIVATE KEY-----",
        "AKIA" + ("A" * 16),
        "gh" + "p_" + ("a" * 24),
        "sk-" + "ant-" + ("a" * 24),
        "12345678:" + ("a" * 35),
        "password=" + repr("Aa1" + ("a" * 29)),
    )

    for candidate in candidates:
        assert any(pattern.search(candidate) for _label, pattern in guard.SECRET_PATTERNS)


def test_secret_patterns_allow_placeholders_and_file_references() -> None:
    guard = _guard()
    examples = (
        "CLOUDFLARE_API_TOKEN_FILE=/run/secrets/cloudflare_api_token",
        "password = read_secret(password_file)",
        "token = ${TOKEN:?Set TOKEN}",
        "AGE-SECRET-KEY-1",
    )

    for example in examples:
        assert not any(pattern.search(example) for _label, pattern in guard.SECRET_PATTERNS)


def test_public_dependency_hosts_are_explicitly_allowlisted() -> None:
    guard = _guard()

    assert "huggingface.co" in guard.ALLOWED_HOSTS
    assert "apt.postgresql.org" in guard.ALLOWED_HOSTS
    assert ("private-deployment" + "." + "online") not in guard.ALLOWED_HOSTS
