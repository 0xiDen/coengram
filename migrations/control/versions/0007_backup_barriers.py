"""Fence Tenant writes while all backup stores share one coherent cut."""

from __future__ import annotations

from alembic import op

revision = "0007_backup_barriers"
down_revision = "0006_decommission_recovery_pins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE control.backup_barriers (
            tenant_id text PRIMARY KEY REFERENCES control.tenants (tenant_id)
                ON DELETE CASCADE,
            barrier_id text NOT NULL UNIQUE,
            started_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE control.backup_barriers")
