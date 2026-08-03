# 13. Encrypted backup and verified restore

Status: resolved

Blocked by: 02. Declarative Tenant lifecycle and hard isolation; 04. Reviewed knowledge
publication; 08. ActiveGraph runtime compatibility tracer.

## What to build

Create a self-hosted backup and restore workflow that protects the Control Store, each
Tenant Operations Store, and each Tenant Memory Store. Nightly encrypted artifacts meet
the accepted retention and recovery targets; restores happen into isolated targets and
are verified through public memory, governance, and Agent Run Interfaces rather than
raw storage inspection.

## Acceptance criteria

- [x] Nightly PostgreSQL backup covers the Control Store and every Tenant Operations
      Store with enough metadata to restore roles and immutable Tenant routing safely.
- [x] Nightly per-Tenant Neo4j Community backup performs the documented brief Tenant
      stop required for a consistent dump and does not stop unrelated Tenants.
- [x] All backup artifacts are encrypted before remote transfer or long-term retention,
      with encryption and remote-storage credentials supplied through protected files.
- [x] Backup manifests record version, Tenant identifier, store/schema versions,
      creation time, checksum, encryption metadata, and completeness without domain
      content.
- [x] Retention preserves seven daily and four weekly generations and reports deletion
      or retention failures without silently losing the newest valid recovery point.
- [x] Backup status demonstrates a target recovery-point objective of no more than 24
      hours and alerts when a Tenant exceeds it.
- [x] Restore creates isolated PostgreSQL and Neo4j targets and cannot overwrite an
      active Tenant without a separate, explicit operator workflow.
- [x] Restored routing, schemas, graph indexes, Promotion state, private/shared memory,
      corrections, erasure tombstones, and Agent Run events remain coherent.
- [x] Verification uses authenticated public recall, governance audit, and Agent Run
      inspection and proves no data is routed into a different Tenant.
- [x] A timed restore drill demonstrates or reports variance from the accepted four-hour
      per-Tenant recovery-time objective.
- [x] A quarterly drill procedure records artifact identifiers, timings, verification
      results, and operator outcome without recording memory content or credentials.
- [x] Integration tests create representative two-store Tenant data, back it up, restore
      it into isolation, verify it through public Interfaces, and detect corrupted,
      incomplete, wrong-key, and incompatible-version artifacts.
- [x] Documentation leaves remote backup storage provisioning and credential creation
      to the operator while defining the required Adapter contract and validation.

## Comments

_No comments yet._
