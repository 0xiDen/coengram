"""Create Tenant governance, outbox, erasure, and ownership schemas."""

from __future__ import annotations

from alembic import op

revision = "0001_tenant_governance"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA memory;
        CREATE SCHEMA activegraph;

        CREATE TABLE activegraph.schema_ownership (
            component text PRIMARY KEY,
            managed_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        INSERT INTO activegraph.schema_ownership (component, managed_by)
        VALUES ('activegraph', 'activegraph-runtime');

        CREATE TABLE memory.knowledge_candidates (
            candidate_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            claim text NOT NULL CHECK (length(btrim(claim)) > 0),
            confidence double precision NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
            proposer_id text NOT NULL,
            source_memory_ids jsonb NOT NULL,
            duplicate_candidate_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            conflict_candidate_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            status text NOT NULL CHECK (
                status IN ('submitted', 'approved', 'rejected', 'publishing', 'published', 'failed')
            ),
            proposal_idempotency_key text,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_by text,
            review_rationale text,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (jsonb_typeof(source_memory_ids) = 'array'),
            CHECK (jsonb_typeof(duplicate_candidate_ids) = 'array'),
            CHECK (jsonb_typeof(conflict_candidate_ids) = 'array'),
            UNIQUE (tenant_id, proposer_id, proposal_idempotency_key)
        );

        CREATE INDEX knowledge_candidates_tenant_status_idx
            ON memory.knowledge_candidates (tenant_id, status, created_at);

        CREATE TABLE memory.knowledge_reviews (
            review_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            candidate_id text NOT NULL REFERENCES memory.knowledge_candidates (candidate_id),
            reviewer_id text NOT NULL,
            decision text NOT NULL CHECK (decision IN ('approve', 'reject')),
            rationale text NOT NULL CHECK (length(btrim(rationale)) > 0),
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (tenant_id, reviewer_id, idempotency_key),
            UNIQUE (candidate_id)
        );

        CREATE TABLE memory.erasure_requests (
            request_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            memory_id text NOT NULL,
            requester_id text NOT NULL,
            owner_principal_id text NOT NULL,
            reason text NOT NULL,
            status text NOT NULL CHECK (
                status IN ('requested', 'approved', 'rejected', 'completed', 'failed')
            ),
            request_idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_by text,
            review_rationale text,
            completed_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (tenant_id, requester_id, request_idempotency_key)
        );

        CREATE INDEX erasure_requests_tenant_status_idx
            ON memory.erasure_requests (tenant_id, status, created_at);

        CREATE TABLE memory.erasure_reviews (
            review_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            request_id text NOT NULL REFERENCES memory.erasure_requests (request_id),
            reviewer_id text NOT NULL,
            decision text NOT NULL CHECK (decision IN ('approve', 'reject')),
            rationale text NOT NULL CHECK (length(btrim(rationale)) > 0),
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (tenant_id, reviewer_id, idempotency_key),
            UNIQUE (request_id)
        );

        CREATE TABLE memory.governance_audit (
            audit_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            actor_id text NOT NULL,
            event_type text NOT NULL,
            target_type text NOT NULL,
            target_id text NOT NULL,
            outcome text NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (jsonb_typeof(metadata) = 'object')
        );

        CREATE INDEX governance_audit_tenant_time_idx
            ON memory.governance_audit (tenant_id, created_at);

        CREATE TABLE memory.outbox (
            event_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            aggregate_type text NOT NULL,
            aggregate_id text NOT NULL,
            event_type text NOT NULL,
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            available_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            locked_at timestamptz,
            lock_id text,
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            last_error_code text,
            dispatched_at timestamptz,
            processed_at timestamptz,
            CHECK (jsonb_typeof(payload) = 'object'),
            CHECK ((locked_at IS NULL) = (lock_id IS NULL)),
            UNIQUE (event_type, aggregate_id)
        );

        CREATE INDEX outbox_dispatch_idx
            ON memory.outbox (available_at, created_at)
            WHERE dispatched_at IS NULL AND processed_at IS NULL;

        CREATE TABLE memory.agent_runs (
            run_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            actor_id text NOT NULL,
            idempotency_key text NOT NULL,
            capability text NOT NULL,
            state text NOT NULL CHECK (
                state IN (
                    'queued', 'running', 'cancel_requested', 'cancelled',
                    'completed', 'budget_exhausted', 'failed'
                )
            ),
            snapshot jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (jsonb_typeof(snapshot) = 'object'),
            UNIQUE (tenant_id, actor_id, idempotency_key)
        );

        CREATE INDEX agent_runs_runnable_idx
            ON memory.agent_runs (tenant_id, state, created_at)
            WHERE state IN ('queued', 'running', 'cancel_requested');
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA activegraph CASCADE; DROP SCHEMA memory CASCADE")
