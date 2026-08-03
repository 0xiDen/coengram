"""Complete durable Tenant provisioning progress fields."""

from __future__ import annotations

from alembic import op

revision = "0003_provisioning_state"
down_revision = "0002_channel_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE control.provisioning
            ADD COLUMN policies jsonb NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN attempt integer NOT NULL DEFAULT 0;

        ALTER TABLE control.provisioning
            ADD CONSTRAINT provisioning_policies_object
                CHECK (jsonb_typeof(policies) = 'object'),
            ADD CONSTRAINT provisioning_attempt_nonnegative
                CHECK (attempt >= 0);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE control.provisioning
            DROP CONSTRAINT provisioning_attempt_nonnegative,
            DROP CONSTRAINT provisioning_policies_object,
            DROP COLUMN attempt,
            DROP COLUMN policies;
        """
    )
