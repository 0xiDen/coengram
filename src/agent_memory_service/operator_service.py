"""Run the host-side Operator Service for queued Tenant provisioning jobs."""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from urllib.parse import quote

from agent_memory_service.host_provisioning import create_host_tenant_provisioner
from agent_memory_service.operator_provisioning import OperatorProvisioningService
from agent_memory_service.platform import PlatformConfig
from agent_memory_service.stores.postgres_control import PostgresControlStore


def main() -> None:
    config = PlatformConfig.from_env()
    store = PostgresControlStore(config.control_database_url)
    repository_root = Path(os.getenv("MEMORY_REPOSITORY_ROOT", Path.cwd()))
    admin_user = os.getenv("MEMORY_POSTGRES_ADMIN_USER", "postgres")
    admin_password = _secret_from_environment("MEMORY_POSTGRES_ADMIN_PASSWORD_FILE")
    admin_database_url = (
        f"postgresql://{quote(admin_user, safe='')}:{quote(admin_password, safe='')}@"
        f"{config.tenant_postgres_host}:{config.tenant_postgres_port}/postgres"
    )
    provisioner = create_host_tenant_provisioner(
        control_database_url=config.control_database_url,
        postgres_admin_url=admin_database_url,
        postgres_host=config.tenant_postgres_host,
        postgres_port=config.tenant_postgres_port,
        secrets_root=config.tenant_credentials_dir,
        tenant_secrets_group_id=_required_positive_integer("MEMORY_TENANT_SECRETS_GID"),
        repository_root=repository_root,
        telemetry_pseudonymizer=config.telemetry_pseudonymizer,
    )
    service = OperatorProvisioningService(
        store,
        provisioner,
        worker_id=os.getenv("MEMORY_OPERATOR_WORKER_ID", "").strip() or _default_worker_id(),
    )
    if _boolean("MEMORY_OPERATOR_RUN_ONCE", default=False):
        service.run_once()
        return
    poll_seconds = _positive_float("MEMORY_OPERATOR_POLL_SECONDS", default=5.0)
    while True:
        if service.run_once() is None:
            time.sleep(poll_seconds)


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _secret_from_environment(name: str) -> str:
    configured = os.getenv(name, "").strip()
    if not configured:
        raise RuntimeError(f"Required secret file variable {name} is not set")
    path = Path(configured)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Secret file configured by {name} is not a regular file")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Secret file configured by {name} is empty")
    return value


def _required_positive_integer(name: str) -> int:
    value = os.getenv(name, "").strip()
    try:
        integer = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if integer < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return integer


def _positive_float(name: str, *, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return parsed


def _boolean(name: str, *, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


if __name__ == "__main__":
    main()
