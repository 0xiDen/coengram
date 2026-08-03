"""Add operator-approved external Channel Bindings."""

from __future__ import annotations

from alembic import op

revision = "0002_channel_bindings"
down_revision = "0001_control_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE control.channel_bindings (
            binding_id text PRIMARY KEY,
            channel text NOT NULL CHECK (channel = 'telegram'),
            external_id text NOT NULL CHECK (external_id ~ '^[0-9]+$'),
            tenant_id text NOT NULL,
            user_id text NOT NULL,
            agent_id text NOT NULL,
            delegation_id text NOT NULL,
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (channel, external_id),
            FOREIGN KEY (delegation_id, tenant_id, agent_id, user_id)
                REFERENCES control.delegations (
                    delegation_id,
                    tenant_id,
                    agent_id,
                    subject_user_id
                )
        );

        CREATE INDEX channel_bindings_tenant_idx
            ON control.channel_bindings (tenant_id, active);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE control.channel_bindings")
