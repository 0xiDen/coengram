"""Keyed, content-safe references shared by runtime and host operators."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from pathlib import Path

_PSEUDONYM_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PSEUDONYM_DOMAIN = b"coengram-telemetry-v1\0"
_MINIMUM_HMAC_KEY_BYTES = 32


@dataclass(frozen=True, slots=True)
class TelemetryPseudonymizer:
    """Derive stable, namespaced telemetry references with a shared secret key."""

    _key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self._key) < _MINIMUM_HMAC_KEY_BYTES:
            raise ValueError("Telemetry HMAC key must contain at least 32 bytes")

    @classmethod
    def from_file(cls, path: Path) -> TelemetryPseudonymizer:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Telemetry HMAC key is not a regular file")
        key = path.read_bytes().strip()
        if not key:
            raise RuntimeError("Telemetry HMAC key file is empty")
        try:
            return cls(key)
        except ValueError as exc:
            raise RuntimeError("Telemetry HMAC key must contain at least 32 bytes") from exc

    def reference(self, namespace: str, value: str) -> str:
        if _PSEUDONYM_NAMESPACE.fullmatch(namespace) is None:
            raise ValueError("Telemetry pseudonym namespace is invalid")
        digest = hmac.new(
            self._key,
            _PSEUDONYM_DOMAIN + namespace.encode("ascii") + b"\0" + value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return "o_" + digest[:16]
