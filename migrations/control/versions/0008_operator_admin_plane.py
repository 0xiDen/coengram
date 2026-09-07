"""Add Operator admin plane tables."""

from __future__ import annotations

from alembic import op

revision = "0008_operator_admin_plane"
down_revision = "0007_backup_barriers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE control.operators (
            operator_id text PRIMARY KEY,
            name text NOT NULL CHECK (length(btrim(name)) > 0),
            roles jsonb NOT NULL DEFAULT '[]'::jsonb,
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (jsonb_typeof(roles) = 'array'),
            CHECK (
                roles <@ '[
                    "operator_admin",
                    "identity_admin",
                    "tenant_provisioner",
                    "tenant_support",
                    "knowledge_admin",
                    "token_admin",
                    "audit_viewer"
                ]'::jsonb
            )
        );

        CREATE TABLE control.operator_access_tokens (
            token_id text PRIMARY KEY,
            operator_id text NOT NULL REFERENCES control.operators (operator_id),
            roles jsonb NOT NULL,
            verifier bytea NOT NULL CHECK (octet_length(verifier) = 32),
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at timestamptz,
            CHECK (jsonb_typeof(roles) = 'array'),
            CHECK (jsonb_array_length(roles) > 0),
            CHECK (
                roles <@ '[
                    "operator_admin",
                    "identity_admin",
                    "tenant_provisioner",
                    "tenant_support",
                    "knowledge_admin",
                    "token_admin",
                    "audit_viewer"
                ]'::jsonb
            )
        );

        CREATE INDEX operator_access_tokens_operator_idx
            ON control.operator_access_tokens (operator_id, created_at);
        CREATE INDEX operator_access_tokens_expiry_idx
            ON control.operator_access_tokens (expires_at)
            WHERE revoked_at IS NULL;

        CREATE TABLE control.admin_sessions (
            session_id text PRIMARY KEY,
            operator_id text NOT NULL REFERENCES control.operators (operator_id),
            token_id text REFERENCES control.operator_access_tokens (token_id),
            roles jsonb NOT NULL,
            verifier bytea NOT NULL CHECK (octet_length(verifier) = 32),
            csrf_verifier bytea NOT NULL CHECK (octet_length(csrf_verifier) = 32),
            absolute_expires_at timestamptz NOT NULL,
            idle_expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at timestamptz,
            CHECK (jsonb_typeof(roles) = 'array'),
            CHECK (jsonb_array_length(roles) > 0),
            CHECK (
                roles <@ '[
                    "operator_admin",
                    "identity_admin",
                    "tenant_provisioner",
                    "tenant_support",
                    "knowledge_admin",
                    "token_admin",
                    "audit_viewer"
                ]'::jsonb
            )
        );

        CREATE INDEX admin_sessions_operator_idx
            ON control.admin_sessions (operator_id, created_at);
        CREATE INDEX admin_sessions_expiry_idx
            ON control.admin_sessions (absolute_expires_at, idle_expires_at)
            WHERE revoked_at IS NULL;

        CREATE TABLE control.operator_audit_events (
            event_id text PRIMARY KEY,
            operator_id text REFERENCES control.operators (operator_id),
            roles jsonb NOT NULL DEFAULT '[]'::jsonb,
            action text NOT NULL CHECK (length(btrim(action)) > 0),
            target_type text NOT NULL CHECK (length(btrim(target_type)) > 0),
            target_ids jsonb NOT NULL DEFAULT '{}'::jsonb,
            request_ref text,
            outcome text NOT NULL CHECK (length(btrim(outcome)) > 0),
            before_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            after_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (jsonb_typeof(roles) = 'array'),
            CHECK (jsonb_typeof(target_ids) = 'object'),
            CHECK (jsonb_typeof(before_metadata) = 'object'),
            CHECK (jsonb_typeof(after_metadata) = 'object')
        );

        CREATE INDEX operator_audit_events_operator_time_idx
            ON control.operator_audit_events (operator_id, created_at);
        CREATE INDEX operator_audit_events_action_time_idx
            ON control.operator_audit_events (action, created_at);

        CREATE TABLE control.provisioning_jobs (
            job_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            manifest_fingerprint text NOT NULL,
            manifest jsonb NOT NULL,
            idempotency_key text NOT NULL,
            requested_by_operator_id text NOT NULL REFERENCES control.operators (operator_id),
            state text NOT NULL CHECK (
                state IN (
                    'queued',
                    'running',
                    'succeeded',
                    'failed',
                    'cancel_requested',
                    'canceled',
                    'cleanup_requested',
                    'cleaned_up'
                )
            ),
            claimed_by text,
            claimed_at timestamptz,
            heartbeat_at timestamptz,
            attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
            completed_steps jsonb NOT NULL DEFAULT '[]'::jsonb,
            failed_step text,
            failure_code text,
            cancel_requested_at timestamptz,
            cleanup_requested_at timestamptz,
            cleanup_completed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (jsonb_typeof(manifest) = 'object'),
            CHECK (jsonb_typeof(completed_steps) = 'array'),
            UNIQUE (requested_by_operator_id, idempotency_key)
        );

        CREATE UNIQUE INDEX provisioning_jobs_active_manifest_idx
            ON control.provisioning_jobs (tenant_id, manifest_fingerprint)
            WHERE state IN (
                'queued',
                'running',
                'failed',
                'cancel_requested',
                'canceled',
                'cleanup_requested'
            );
        CREATE INDEX provisioning_jobs_state_time_idx
            ON control.provisioning_jobs (state, created_at);

        CREATE TABLE control.provisioning_job_attempts (
            job_id text NOT NULL REFERENCES control.provisioning_jobs (job_id)
                ON DELETE CASCADE,
            attempt integer NOT NULL CHECK (attempt > 0),
            state text NOT NULL CHECK (
                state IN ('running', 'succeeded', 'failed', 'canceled', 'cleaned_up')
            ),
            started_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at timestamptz,
            failed_step text,
            failure_code text,
            PRIMARY KEY (job_id, attempt)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE control.provisioning_job_attempts;
        DROP TABLE control.provisioning_jobs;
        DROP TABLE control.operator_audit_events;
        DROP TABLE control.admin_sessions;
        DROP TABLE control.operator_access_tokens;
        DROP TABLE control.operators;
        """
    )
