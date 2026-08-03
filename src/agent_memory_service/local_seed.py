"""Idempotently seed the explicit one-Tenant local Compose tracer."""

from __future__ import annotations

import json
import os

import psycopg

from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule
from agent_memory_service.platform import PlatformConfig
from agent_memory_service.stores.postgres_control import PostgresControlStore


def main() -> None:
    config = PlatformConfig.from_env()
    tenant_id = os.getenv("MEMORY_SEED_TENANT_ID", "tenant-a")
    admin_id = "user-local-admin"
    normalized = tenant_id.replace("-", "_")
    with psycopg.connect(config.control_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO control.tenants (tenant_id, name, active)
                VALUES (%s, %s, true)
                ON CONFLICT (tenant_id) DO UPDATE
                SET active = true, updated_at = CURRENT_TIMESTAMP
                """,
                (tenant_id, "Local Product A Backend"),
            )
            cursor.execute(
                """
                INSERT INTO control.routing (
                    tenant_id,
                    neo4j_service_address,
                    neo4j_secret_name,
                    tenant_database_name,
                    tenant_database_role,
                    healthy,
                    checked_at
                )
                VALUES (%s, %s, %s, %s, %s, true, CURRENT_TIMESTAMP)
                ON CONFLICT (tenant_id) DO UPDATE
                SET healthy = true,
                    checked_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    tenant_id,
                    f"neo4j-{tenant_id}:7687",
                    f"{tenant_id}/neo4j_password",
                    f"tenant_{normalized}",
                    f"tenant_{normalized}_rw",
                ),
            )
            cursor.execute(
                """
                INSERT INTO control.principals (principal_id, name, kind, active)
                VALUES (%s, 'Local Administrator', 'user', true)
                ON CONFLICT (principal_id) DO UPDATE
                SET active = true, updated_at = CURRENT_TIMESTAMP
                """,
                (admin_id,),
            )
            cursor.execute(
                """
                INSERT INTO control.memberships (tenant_id, principal_id, roles, active)
                VALUES (
                    %s,
                    %s,
                    '["tenant_administrator", "knowledge_curator", "tenant_member"]'::jsonb,
                    true
                )
                ON CONFLICT (tenant_id, principal_id) DO UPDATE
                SET roles = EXCLUDED.roles,
                    active = true,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (tenant_id, admin_id),
            )

    store = PostgresControlStore(config.control_database_url)
    credential = ControlModule(store, TokenService(store)).issue_access_token(
        tenant_id,
        admin_id,
    )
    print(
        json.dumps(
            {
                "tenant_id": tenant_id,
                "principal_id": admin_id,
                "token_id": credential.token_id,
                "access_token": credential.access_token,
                "expires_at": credential.expires_at.isoformat(),
                "warning": "Local bootstrap token is shown once; store it securely.",
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
