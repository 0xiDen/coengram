"""Allow completed erasures to scrub all free-text request and review fields."""

from __future__ import annotations

from alembic import op

revision = "0004_erasure_redaction"
down_revision = "0003_durable_memory_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory.erasure_requests
            ALTER COLUMN reason DROP NOT NULL;

        ALTER TABLE memory.erasure_reviews
            DROP CONSTRAINT erasure_reviews_rationale_check;
        ALTER TABLE memory.erasure_reviews
            ALTER COLUMN rationale DROP NOT NULL;

        UPDATE memory.erasure_requests
        SET reason = NULL,
            review_rationale = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE status = 'completed';

        UPDATE memory.erasure_reviews AS v
        SET rationale = NULL
        FROM memory.erasure_requests AS r
        WHERE r.request_id = v.request_id
          AND r.status = 'completed';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE memory.erasure_requests
        SET reason = ''
        WHERE reason IS NULL;
        ALTER TABLE memory.erasure_requests
            ALTER COLUMN reason SET NOT NULL;

        UPDATE memory.erasure_reviews
        SET rationale = 'redacted'
        WHERE rationale IS NULL;
        ALTER TABLE memory.erasure_reviews
            ALTER COLUMN rationale SET NOT NULL;
        ALTER TABLE memory.erasure_reviews
            ADD CONSTRAINT erasure_reviews_rationale_check
            CHECK (length(btrim(rationale)) > 0);
        """
    )
