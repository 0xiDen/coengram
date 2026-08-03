"""Add durable progress for controlled active-Tenant schema migrations."""

from __future__ import annotations

from alembic import op

revision = "0005_active_tenant_migrations"
down_revision = "0004_decommission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE control.active_tenant_migrations (
            tenant_id text NOT NULL REFERENCES control.tenants (tenant_id) ON DELETE CASCADE,
            migration_id text NOT NULL,
            postgres_target_version text NOT NULL,
            neo4j_target_version text NOT NULL,
            state text NOT NULL CHECK (state IN ('applying', 'failed', 'completed')),
            completed_steps jsonb NOT NULL DEFAULT '[]'::jsonb,
            failed_step text,
            failure_code text,
            attempt integer NOT NULL CHECK (attempt > 0),
            revision integer NOT NULL CHECK (revision > 0),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at timestamptz,
            PRIMARY KEY (tenant_id, migration_id),
            CHECK (jsonb_typeof(completed_steps) = 'array'),
            CHECK ((failed_step IS NULL) = (failure_code IS NULL)),
            CHECK ((state = 'completed') = (completed_at IS NOT NULL))
        );

        CREATE INDEX active_tenant_migrations_state_idx
            ON control.active_tenant_migrations (state, updated_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE control.active_tenant_migrations")
