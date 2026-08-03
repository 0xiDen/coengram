from __future__ import annotations

from pathlib import Path

import pytest

from agent_memory_service.backup_barrier import PostgresBackupConsistencyBarrier
from agent_memory_service.tenant_credentials import read_tenant_credential


def _barrier(secrets: Path) -> PostgresBackupConsistencyBarrier:
    return PostgresBackupConsistencyBarrier(
        control_database_url="postgresql://control:secret@postgres/control",
        tenant_secrets_directory=secrets,
        postgres_host="postgres",
        postgres_port=5432,
    )


def test_backup_barrier_rejects_password_through_symlinked_tenant_directory(
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "secrets"
    outside = tmp_path / "outside"
    secrets.mkdir()
    outside.mkdir()
    password = outside / "postgres_password"
    password.write_text("secret\n", encoding="utf-8")
    password.chmod(0o600)
    (secrets / "tenant-a").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory"):
        read_tenant_credential(secrets, "tenant-a", "postgres_password")


def test_backup_barrier_rejects_in_root_symlinked_tenant_directory(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    actual = secrets / "actual"
    actual.mkdir(parents=True)
    password = actual / "postgres_password"
    password.write_text("secret\n", encoding="utf-8")
    password.chmod(0o600)
    (secrets / "tenant-a").symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory"):
        read_tenant_credential(secrets, "tenant-a", "postgres_password")


def test_backup_barrier_rejects_group_or_world_accessible_password(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    tenant = secrets / "tenant-a"
    tenant.mkdir(parents=True)
    password = tenant / "postgres_password"
    password.write_text("secret\n", encoding="utf-8")
    password.chmod(0o644)

    with pytest.raises(ValueError, match="0600 or 0640"):
        read_tenant_credential(secrets, "tenant-a", "postgres_password")


def test_backup_barrier_accepts_provisioned_group_readable_password(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    tenant = secrets / "tenant-a"
    tenant.mkdir(parents=True)
    password = tenant / "postgres_password"
    password.write_text("secret\n", encoding="utf-8")
    password.chmod(0o640)

    assert _barrier(secrets)._read_tenant_password("tenant-a") == "secret"
