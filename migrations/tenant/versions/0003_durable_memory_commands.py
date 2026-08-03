"""Add the authoritative private-memory command ledger and graph outbox."""

from __future__ import annotations

from alembic import op

revision = "0003_durable_memory_commands"
down_revision = "0002_candidate_memory_findings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE memory.private_memory_items (
            memory_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            owner_principal_id text NOT NULL,
            state text NOT NULL CHECK (
                state IN ('active', 'superseded', 'erasure_pending', 'erased')
            ),
            item jsonb,
            source_command_id text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                (state = 'erased' AND item IS NULL)
                OR (state <> 'erased' AND jsonb_typeof(item) = 'object')
            )
        );

        CREATE INDEX private_memory_items_owner_state_idx
            ON memory.private_memory_items (tenant_id, owner_principal_id, state, created_at);

        CREATE TABLE memory.private_memory_commands (
            command_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            actor_id text NOT NULL,
            owner_principal_id text NOT NULL,
            command_type text NOT NULL CHECK (
                command_type IN ('retain', 'correct', 'import', 'erase')
            ),
            idempotency_key text NOT NULL,
            target_memory_id text,
            result_memory_id text,
            erasure_request_id text REFERENCES memory.erasure_requests (request_id),
            payload jsonb NOT NULL,
            result_item jsonb,
            redacted_at timestamptz,
            state text NOT NULL DEFAULT 'accepted' CHECK (
                state IN ('accepted', 'applied', 'failed')
            ),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            applied_at timestamptz,
            last_error_code text,
            CHECK (jsonb_typeof(payload) = 'object'),
            CHECK (result_item IS NULL OR jsonb_typeof(result_item) = 'object'),
            CHECK (
                (command_type = 'erase' AND target_memory_id IS NOT NULL
                    AND result_memory_id IS NULL AND result_item IS NULL)
                OR
                (command_type <> 'erase' AND result_memory_id IS NOT NULL
                    AND (result_item IS NOT NULL OR redacted_at IS NOT NULL))
            ),
            UNIQUE (tenant_id, actor_id, command_type, idempotency_key),
            UNIQUE (erasure_request_id)
        );

        ALTER TABLE memory.private_memory_items
            ADD CONSTRAINT private_memory_items_source_command_fk
            FOREIGN KEY (source_command_id)
            REFERENCES memory.private_memory_commands (command_id);

        CREATE INDEX private_memory_commands_state_idx
            ON memory.private_memory_commands (tenant_id, state, created_at);
        CREATE INDEX private_memory_commands_result_idx
            ON memory.private_memory_commands (tenant_id, result_memory_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory.private_memory_items
            DROP CONSTRAINT private_memory_items_source_command_fk;
        DROP TABLE memory.private_memory_items;
        DROP TABLE memory.private_memory_commands;
        """
    )
