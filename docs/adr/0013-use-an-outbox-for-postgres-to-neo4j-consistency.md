# Use a transactional outbox across PostgreSQL and Neo4j

PostgreSQL records command state, governance decisions, audit, and an outbox event in one transaction; an AMQP relay and worker then apply the change idempotently to the Tenant Memory Store and mark it complete. Promotion is recallable only after reaching `published`, and failed or interrupted work remains visible and retryable, avoiding distributed transactions and duplicate memory writes.
