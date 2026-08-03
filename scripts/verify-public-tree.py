#!/usr/bin/env python3
"""Reject private deployment identifiers and workstation residue before release."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?\.)+"
    r"(?:ai|app|cloud|co|com|de|dev|io|me|net|online|org|tech|uk|us|xyz|internal|invalid|test)"
    r"(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
ALLOWED_HOSTS = frozenset(
    {
        "activegraph.ai",
        "api.telegram.org",
        "apt.postgresql.org",
        "download.pytorch.org",
        "example.com",
        "example.invalid",
        "ghcr.io",
        "github.com",
        "huggingface.co",
        "img.shields.io",
        "neo4j.com",
        "proton.me",
        "pypi.org",
        "quay.io",
        "raw.githubusercontent.com",
        "www.w3.org",
    }
)
ALLOWED_SUFFIXES = (".example.com", ".invalid", ".internal", ".test")
TEXT_SUFFIXES = frozenset(
    {
        "",
        ".caddy",
        ".cfg",
        ".css",
        ".env",
        ".example",
        ".html",
        ".ini",
        ".json",
        ".md",
        ".py",
        ".sh",
        ".sql",
        ".svg",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key material", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])")),
    (
        "GitHub access token",
        re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ),
    ("Anthropic API key", re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{16,}")),
    ("OpenAI API key", re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{24,}")),
    ("Slack access token", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{16,}")),
    ("Telegram bot token", re.compile(r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{30,}")),
    ("age identity", re.compile(r"AGE-SECRET-KEY-1[A-Z0-9]{40,}")),
    (
        "assigned credential",
        re.compile(
            r"(?i:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
            r"\s*[:=]\s*['\"]"
            r"(?=[A-Za-z0-9+/=_-]*[a-z])"
            r"(?=[A-Za-z0-9+/=_-]*[A-Z])"
            r"(?=[A-Za-z0-9+/=_-]*[0-9])"
            r"[A-Za-z0-9+/=_-]{24,}['\"]"
        ),
    ),
)


def publishable_paths() -> tuple[Path, ...]:
    result = subprocess.run(
        ("git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"),
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(
        ROOT / item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item and (ROOT / item.decode("utf-8")).suffix.casefold() in TEXT_SUFFIXES
    )


def main() -> int:
    failures: list[str] = []
    for path in publishable_paths():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(ROOT)
        home_pattern = r"/" + r"home/[^/$ `{]+"
        if ("/" + "Users/") in text or re.search(home_pattern, text):
            failures.append(f"{relative}: contains an absolute workstation path")
        for line_number, line in enumerate(text.splitlines(), 1):
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    failures.append(f"{relative}:{line_number}: contains suspected {label}")
            for match in DOMAIN.finditer(line):
                host = match.group(0).casefold().rstrip(".")
                if host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES):
                    continue
                failures.append(f"{relative}:{line_number}: contains an unapproved hostname")

    if failures:
        print("Public-tree verification failed:")
        print("\n".join(f"- {failure}" for failure in sorted(set(failures))))
        return 1
    print(
        "Public tree contains no recognized credentials, private workstation paths, "
        "or unapproved hostnames."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
