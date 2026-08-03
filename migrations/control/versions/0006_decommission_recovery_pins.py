"""Retain decommission recovery points after Tenant destruction."""

from __future__ import annotations

from alembic import op

revision = "0006_decommission_recovery_pins"
down_revision = "0005_active_tenant_migrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE control.decommission_recovery_pins (
            request_id text PRIMARY KEY REFERENCES control.decommission_tombstones (request_id)
                ON DELETE CASCADE,
            tenant_id text NOT NULL,
            backup_id text NOT NULL,
            protected_until timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX decommission_recovery_pins_tenant_deadline_idx
            ON control.decommission_recovery_pins (tenant_id, protected_until);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE control.decommission_recovery_pins")
