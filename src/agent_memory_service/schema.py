"""Fail-closed database schema compatibility checks for runtime processes."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from agent_memory_service.database_url import (
    psycopg_database_url as _psycopg_database_url,
)

CONTROL_SCHEMA_REVISION = "0007_backup_barriers"
TENANT_SCHEMA_REVISION = "0006_agent_run_context"


class SchemaCompatibilityError(RuntimeError):
    """Raised when a required database is absent, unreachable, or incompatible."""


@dataclass(frozen=True, slots=True)
class SchemaRequirement:
    database_url: str
    expected_revision: str
    store_name: str


def require_schema(requirement: SchemaRequirement) -> None:
    """Require the database's single Alembic head to exactly match the runtime."""

    database_url = _psycopg_database_url(requirement.database_url)
    try:
        with psycopg.connect(database_url, connect_timeout=5) as connection:
            rows = connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            ).fetchall()
    except Exception as exc:
        raise SchemaCompatibilityError(
            f"{requirement.store_name} schema could not be verified"
        ) from exc
    revisions = tuple(str(row[0]) for row in rows)
    if revisions != (requirement.expected_revision,):
        raise SchemaCompatibilityError(
            f"{requirement.store_name} schema revision is incompatible; "
            f"expected {requirement.expected_revision}"
        )
