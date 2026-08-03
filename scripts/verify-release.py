#!/usr/bin/env python3
"""Execute the provider-neutral release gate."""

from agent_memory_service.release_verification import main

if __name__ == "__main__":
    raise SystemExit(main())
