# ADR 0017: Fence cross-store Tenant backup cuts

- Status: accepted
- Date: 2026-08-03

## Context

A Tenant recovery point spans shared Control PostgreSQL, its isolated PostgreSQL
Operations Store, and its isolated Neo4j Community database. Sequential dumps without a
write fence can capture a PostgreSQL state from before a mutation and a graph state from
after its projection, yet incorrectly label the set complete.

## Decision

Before collecting expectations, CoEngram writes an exact durable backup-barrier record,
temporarily marks the Tenant inactive, and acquires an exclusive session advisory lock
derived from the Tenant database name. Governance, Agent Run snapshot, and native
ActiveGraph mutations acquire the matching shared transaction lock. Cached routers
re-resolve the Control route on every operation, so new work fails closed while the
Tenant is suspended and stale work drains behind the lock.

Under the exclusive lock the backup rejects pending outbox/projection work, active Agent
leases, and snapshot/native ActiveGraph divergence. It then captures all three plaintext
stores. The fence is released and the Tenant reactivated before encryption, checksums,
and atomic publication. A complete manifest therefore denotes one quiescent cross-store
cut, while keeping the write outage bounded to snapshot capture.

If a process crashes, PostgreSQL releases the session lock but the durable row and
inactive Tenant remain. An exact-confirmation operator command may recover only the
matching barrier after proving the exclusive lock is no longer held. Decommissioning
cannot suspend a Tenant while a backup barrier exists, and isolated restore removes the
source barrier record before activating its restored route.

## Consequences

- Backup creation briefly pauses reads/writes routed through the affected Tenant.
- Pending projections abort the attempt; the scheduler retries after workers drain them.
- Plaintext snapshots remain protected in a mode-`0700` staging directory and are
  encrypted only after Tenant availability is restored.
- The pinned offline Neo4j admin runs as UID `7474` with no network or capabilities;
  its exact data volume is writable only because Neo4j must manage its store lock while
  the service is stopped and the cross-store write fence is held.
- A crash requires explicit, auditable barrier recovery rather than automatic unsafe
  reactivation.
