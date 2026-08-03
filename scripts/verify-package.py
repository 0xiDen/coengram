#!/usr/bin/env python3
"""Reject publishable archives that omit identity metadata or contain local secrets."""

from __future__ import annotations

import email
import tarfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

DIST = Path("dist")
EXPECTED_AUTHOR = "0xiDen <0xiden@proton.me>"
FORBIDDEN_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "dev-secrets",
}


def _assert_publishable_path(name: str) -> None:
    path = PurePosixPath(name)
    if FORBIDDEN_PARTS.intersection(path.parts):
        raise RuntimeError(f"Publishable archive contains forbidden path: {name}")
    if path.suffix in {".age", ".pyc"}:
        raise RuntimeError(f"Publishable archive contains forbidden file: {name}")


def _single(pattern: str) -> Path:
    matches = sorted(DIST.glob(pattern))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {pattern} artifact, found {len(matches)}")
    return matches[0]


def _verify_wheel(path: Path) -> None:
    with ZipFile(path) as archive:
        names = archive.namelist()
        for name in names:
            _assert_publishable_path(name)
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        license_names = {
            PurePosixPath(name).name for name in names if ".dist-info/licenses/" in name
        }
        if len(metadata_names) != 1 or license_names != {"LICENSE", "THIRD_PARTY_NOTICES.md"}:
            raise RuntimeError("Wheel must contain CoEngram and third-party license notices")
        metadata = email.message_from_bytes(archive.read(metadata_names[0]))
        if metadata["Name"] != "coengram" or metadata["Author-email"] != EXPECTED_AUTHOR:
            raise RuntimeError("Wheel package name or author identity is incorrect")
        if metadata["License-Expression"] != "MIT":
            raise RuntimeError("Wheel license expression is not MIT")


def _verify_sdist(path: Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        names = archive.getnames()
        for name in names:
            _assert_publishable_path(name)
        required_suffixes = {
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "README.md",
            "deploy/caddy/Caddyfile",
            "deploy/secrets/README.md",
            "pyproject.toml",
        }
        for suffix in required_suffixes:
            if not any(name.endswith(suffix) for name in names):
                raise RuntimeError(f"Source distribution is missing {suffix}")


def main() -> None:
    _verify_wheel(_single("coengram-*.whl"))
    _verify_sdist(_single("coengram-*.tar.gz"))
    print("Publishable archives contain only expected CoEngram source and metadata.")


if __name__ == "__main__":
    main()
