"""Add bounded, fenced worker leases to durable Agent Runs."""

from __future__ import annotations

from alembic import op

revision = "0005_agent_run_leases"
down_revision = "0004_erasure_redaction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory.agent_runs
            ADD COLUMN lease_token text,
            ADD COLUMN lease_version bigint NOT NULL DEFAULT 0
                CHECK (lease_version >= 0),
            ADD COLUMN lease_expires_at timestamptz,
            ADD CONSTRAINT agent_runs_lease_shape_check CHECK (
                (lease_token IS NULL AND lease_expires_at IS NULL)
                OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
            );

        CREATE INDEX agent_runs_claimable_idx
            ON memory.agent_runs (tenant_id, created_at, run_id, lease_expires_at)
            WHERE state IN ('queued', 'running', 'cancel_requested');
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX memory.agent_runs_claimable_idx;
        ALTER TABLE memory.agent_runs
            DROP CONSTRAINT agent_runs_lease_shape_check,
            DROP COLUMN lease_expires_at,
            DROP COLUMN lease_version,
            DROP COLUMN lease_token;
        """
    )
