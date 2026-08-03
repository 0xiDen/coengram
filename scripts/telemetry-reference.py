#!/usr/bin/env python3
"""Print one safe Tenant telemetry reference without exposing the shared key."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agent_memory_service.pseudonyms import TelemetryPseudonymizer


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: telemetry-reference.py TENANT_ID")
    key_path = os.getenv("MEMORY_TELEMETRY_HMAC_KEY_FILE", "").strip()
    if not key_path:
        raise SystemExit("MEMORY_TELEMETRY_HMAC_KEY_FILE is required")
    pseudonymizer = TelemetryPseudonymizer.from_file(Path(key_path))
    print(pseudonymizer.reference("tenant", sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
