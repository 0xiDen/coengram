"""Add durable, concurrency-safe Tenant decommission state."""

from __future__ import annotations

from alembic import op

revision = "0004_decommission"
down_revision = "0003_provisioning_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE control.operator_audit
            DROP CONSTRAINT operator_audit_tenant_id_fkey,
            ADD CONSTRAINT operator_audit_tenant_id_fkey
                FOREIGN KEY (tenant_id)
                REFERENCES control.tenants (tenant_id)
                ON DELETE SET NULL;

        CREATE TABLE control.decommission_requests (
            request_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            compose_project text NOT NULL,
            neo4j_volume text NOT NULL,
            database_name text NOT NULL,
            database_role text NOT NULL,
            route_id text NOT NULL,
            tenant_secret_ref text NOT NULL,
            requested_by text NOT NULL,
            reason text NOT NULL CHECK (length(btrim(reason)) > 0),
            requested_at timestamptz NOT NULL,
            protection_policy text NOT NULL CHECK (
                protection_policy = 'recent-valid-backup-or-accepted-export'
            ),
            state text NOT NULL CHECK (
                state IN (
                    'suspending',
                    'suspended',
                    'grace-period',
                    'finalizing',
                    'cancelled'
                )
            ),
            suspension_steps jsonb NOT NULL DEFAULT '[]'::jsonb,
            confirmed_by text,
            confirmed_at timestamptz,
            grace_ends_at timestamptz,
            protection_evidence jsonb,
            destruction_steps jsonb NOT NULL DEFAULT '[]'::jsonb,
            cancelled_by text,
            cancelled_at timestamptz,
            last_failed_action text,
            revision integer NOT NULL CHECK (revision > 0),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (jsonb_typeof(suspension_steps) = 'array'),
            CHECK (jsonb_typeof(destruction_steps) = 'array'),
            CHECK (
                protection_evidence IS NULL
                OR jsonb_typeof(protection_evidence) = 'object'
            ),
            CHECK (
                (confirmed_by IS NULL AND confirmed_at IS NULL AND grace_ends_at IS NULL)
                OR
                (
                    confirmed_by IS NOT NULL
                    AND confirmed_at IS NOT NULL
                    AND grace_ends_at IS NOT NULL
                )
            ),
            CHECK ((cancelled_by IS NULL) = (cancelled_at IS NULL))
        );

        CREATE UNIQUE INDEX decommission_one_open_request_per_tenant_idx
            ON control.decommission_requests (tenant_id)
            WHERE state <> 'cancelled';
        CREATE INDEX decommission_grace_deadline_idx
            ON control.decommission_requests (grace_ends_at)
            WHERE state = 'grace-period';

        CREATE TABLE control.decommission_tombstones (
            request_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            requested_by text NOT NULL,
            confirmed_by text NOT NULL,
            destroyed_at timestamptz NOT NULL,
            status text NOT NULL CHECK (status = 'destroyed'),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX decommission_tombstones_tenant_idx
            ON control.decommission_tombstones (tenant_id, destroyed_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE control.decommission_tombstones;
        DROP TABLE control.decommission_requests;

        ALTER TABLE control.operator_audit
            DROP CONSTRAINT operator_audit_tenant_id_fkey,
            ADD CONSTRAINT operator_audit_tenant_id_fkey
                FOREIGN KEY (tenant_id)
                REFERENCES control.tenants (tenant_id);
        """
    )
