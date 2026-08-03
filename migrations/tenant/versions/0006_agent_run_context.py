"""Bind Agent Run identity and access to the complete execution context."""

from __future__ import annotations

from alembic import op

revision = "0006_agent_run_context"
down_revision = "0005_agent_run_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory.agent_runs
            ADD COLUMN actor_kind text,
            ADD COLUMN subject_user_id text,
            ADD COLUMN delegation_id text;

        UPDATE memory.agent_runs
        SET actor_kind = COALESCE(snapshot ->> 'actor_kind', 'user'),
            subject_user_id = COALESCE(snapshot ->> 'subject_user_id', ''),
            delegation_id = COALESCE(snapshot ->> 'delegation_id', '');

        ALTER TABLE memory.agent_runs
            ALTER COLUMN actor_kind SET NOT NULL,
            ALTER COLUMN subject_user_id SET NOT NULL,
            ALTER COLUMN delegation_id SET NOT NULL,
            ADD CONSTRAINT agent_runs_actor_kind_check
                CHECK (actor_kind IN ('user', 'agent')),
            ADD CONSTRAINT agent_runs_delegation_shape_check CHECK (
                (subject_user_id = '' AND delegation_id = '')
                OR (
                    actor_kind = 'agent'
                    AND subject_user_id <> ''
                    AND delegation_id <> ''
                )
            ),
            DROP CONSTRAINT agent_runs_tenant_id_actor_id_idempotency_key_key,
            ADD CONSTRAINT agent_runs_context_idempotency_key
                UNIQUE (
                    tenant_id,
                    actor_id,
                    actor_kind,
                    subject_user_id,
                    delegation_id,
                    idempotency_key
                );
        """
    )


def downgrade() -> None:
    # Reinstating the old constraint fails transactionally when distinct delegated
    # contexts legitimately reused a key; no run is deleted to make downgrade fit.
    op.execute(
        """
        ALTER TABLE memory.agent_runs
            DROP CONSTRAINT agent_runs_context_idempotency_key,
            ADD CONSTRAINT agent_runs_tenant_id_actor_id_idempotency_key_key
                UNIQUE (tenant_id, actor_id, idempotency_key),
            DROP CONSTRAINT agent_runs_delegation_shape_check,
            DROP CONSTRAINT agent_runs_actor_kind_check,
            DROP COLUMN delegation_id,
            DROP COLUMN subject_user_id,
            DROP COLUMN actor_kind;
        """
    )
