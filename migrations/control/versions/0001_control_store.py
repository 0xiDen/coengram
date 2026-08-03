"""Create the content-free Control Store schema."""

from __future__ import annotations

from alembic import op

revision = "0001_control_store"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA control;

        CREATE TABLE control.tenants (
            tenant_id text PRIMARY KEY,
            name text NOT NULL CHECK (length(btrim(name)) > 0),
            active boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE control.principals (
            principal_id text PRIMARY KEY,
            name text NOT NULL CHECK (length(btrim(name)) > 0),
            kind text NOT NULL CHECK (kind IN ('user', 'agent')),
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE control.memberships (
            tenant_id text NOT NULL REFERENCES control.tenants (tenant_id),
            principal_id text NOT NULL REFERENCES control.principals (principal_id),
            roles jsonb NOT NULL,
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, principal_id),
            CHECK (jsonb_typeof(roles) = 'array'),
            CHECK (jsonb_array_length(roles) > 0),
            CHECK (
                roles <@ '["tenant_administrator", "knowledge_curator", "tenant_member"]'::jsonb
            )
        );

        CREATE TABLE control.delegations (
            delegation_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            agent_id text NOT NULL,
            subject_user_id text NOT NULL,
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (tenant_id, agent_id)
                REFERENCES control.memberships (tenant_id, principal_id),
            FOREIGN KEY (tenant_id, subject_user_id)
                REFERENCES control.memberships (tenant_id, principal_id),
            UNIQUE (delegation_id, tenant_id, agent_id, subject_user_id),
            CHECK (agent_id <> subject_user_id)
        );

        CREATE TABLE control.access_tokens (
            token_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            principal_id text NOT NULL,
            actor_kind text NOT NULL CHECK (actor_kind IN ('user', 'agent')),
            roles jsonb NOT NULL,
            verifier bytea NOT NULL CHECK (octet_length(verifier) = 32),
            subject_user_id text,
            delegation_id text,
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at timestamptz,
            FOREIGN KEY (tenant_id, principal_id)
                REFERENCES control.memberships (tenant_id, principal_id),
            FOREIGN KEY (delegation_id, tenant_id, principal_id, subject_user_id)
                REFERENCES control.delegations (
                    delegation_id,
                    tenant_id,
                    agent_id,
                    subject_user_id
                ),
            CHECK (jsonb_typeof(roles) = 'array'),
            CHECK (jsonb_array_length(roles) > 0),
            CHECK (
                roles <@ '["tenant_administrator", "knowledge_curator", "tenant_member"]'::jsonb
            ),
            CHECK ((subject_user_id IS NULL) = (delegation_id IS NULL)),
            CHECK (subject_user_id IS NULL OR actor_kind = 'agent')
        );

        CREATE INDEX access_tokens_membership_idx
            ON control.access_tokens (tenant_id, principal_id, created_at);
        CREATE INDEX access_tokens_expiry_idx
            ON control.access_tokens (expires_at)
            WHERE revoked_at IS NULL;

        CREATE TABLE control.routing (
            tenant_id text PRIMARY KEY REFERENCES control.tenants (tenant_id),
            neo4j_service_address text NOT NULL,
            neo4j_secret_name text NOT NULL,
            tenant_database_name text NOT NULL UNIQUE,
            tenant_database_role text NOT NULL UNIQUE,
            healthy boolean NOT NULL DEFAULT false,
            checked_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE control.provisioning (
            tenant_id text PRIMARY KEY REFERENCES control.tenants (tenant_id),
            manifest_version integer NOT NULL CHECK (manifest_version > 0),
            manifest_checksum text NOT NULL,
            desired_state text NOT NULL CHECK (desired_state IN ('active', 'suspended')),
            state text NOT NULL CHECK (
                state IN ('planned', 'provisioning', 'active', 'failed', 'suspended')
            ),
            current_step text,
            completed_steps jsonb NOT NULL DEFAULT '[]'::jsonb,
            last_error_code text,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (jsonb_typeof(completed_steps) = 'array')
        );

        CREATE TABLE control.operator_audit (
            audit_id text PRIMARY KEY,
            operator_id text NOT NULL,
            action text NOT NULL,
            target_type text NOT NULL,
            target_id text NOT NULL,
            tenant_id text REFERENCES control.tenants (tenant_id),
            outcome text NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (jsonb_typeof(metadata) = 'object')
        );

        CREATE INDEX operator_audit_tenant_time_idx
            ON control.operator_audit (tenant_id, created_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA control CASCADE")
