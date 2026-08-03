"""Name and retain durable duplicate/conflict Tenant Memory findings."""

from __future__ import annotations

from alembic import op

revision = "0002_candidate_memory_findings"
down_revision = "0001_tenant_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory.knowledge_candidates
            RENAME COLUMN duplicate_candidate_ids TO duplicate_memory_ids;
        ALTER TABLE memory.knowledge_candidates
            RENAME COLUMN conflict_candidate_ids TO conflicting_memory_ids;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory.knowledge_candidates
            RENAME COLUMN duplicate_memory_ids TO duplicate_candidate_ids;
        ALTER TABLE memory.knowledge_candidates
            RENAME COLUMN conflicting_memory_ids TO conflict_candidate_ids;
        """
    )
