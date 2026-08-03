"""Contained reader for operator-provisioned per-Tenant credential files."""

from __future__ import annotations

import stat
from pathlib import Path

_SAFE_TENANT_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_SAFE_SECRET_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
_TENANT_SECRET_MODES = frozenset({0o600, 0o640})


def read_tenant_credential(root: Path, tenant_id: str, name: str) -> str:
    """Read one exact 0600/0640 credential without following an escape symlink."""

    if not tenant_id or any(character not in _SAFE_TENANT_CHARACTERS for character in tenant_id):
        raise ValueError("Tenant credential identifier is invalid")
    if not name or any(character not in _SAFE_SECRET_CHARACTERS for character in name):
        raise ValueError("Tenant credential name is invalid")
    resolved_root = root.resolve()
    if root.is_symlink() or not resolved_root.is_dir():
        raise ValueError("Tenant credentials root must be a real directory")
    tenant_directory = root / tenant_id
    if tenant_directory.is_symlink() or not tenant_directory.is_dir():
        raise ValueError("Tenant credential directory must be a real directory")
    candidate = tenant_directory / name
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("Tenant credential must be a regular file")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Tenant credential escapes its protected root") from exc
    if stat.S_IMODE(resolved.stat().st_mode) not in _TENANT_SECRET_MODES:
        raise ValueError("Tenant credential mode must be exactly 0600 or 0640")
    value = resolved.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("Tenant credential is empty")
    return value
