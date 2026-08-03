# Backup, restore, and recovery drill runbook

## Objective and boundaries

Iteration 1 targets an RPO of at most 24 hours and an RTO of at most four hours.
The recovery unit is one tenant plus the control metadata needed to reconstruct
its database role, route, and isolated Neo4j service. A complete recovery point
contains exactly:

- one shared control PostgreSQL artifact;
- one PostgreSQL artifact for the tenant Operations Store; and
- one Neo4j Community Edition artifact for that tenant.

Remote storage provisioning and its credentials are managed outside this
repository. Plaintext backup data must never enter remote storage or retention.

## Nightly backup procedure

1. Resolve the active tenant list and each tenant's exact database, database
   role, route, Compose project, Neo4j service, and Neo4j volume from the control
   store. Do not discover targets through a wildcard.
2. Insert an exact `control.backup_barriers` record, mark only that Tenant inactive,
   and acquire the Tenant database's exclusive backup advisory lock. Every governance,
   Agent Run, and native ActiveGraph mutation takes the matching shared transaction
   lock, so the exclusive lock drains in-flight writes and blocks stale routed writers.
   Other tenants remain available.
3. While the fence is held, fail closed unless Private Memory commands, Tenant Knowledge
   publications, and the transactional outbox are fully projected; no Agent Run has an
   active worker lease; and every Agent Run snapshot exactly matches its application
   events in native ActiveGraph. Capture the content-free restore expectations only
   after these checks pass.
4. Still under the same fence, dump the control PostgreSQL database with role/routing
   metadata and the Tenant PostgreSQL database. Then stop only that Tenant's Neo4j
   service and run the pinned Community image's
   offline `neo4j-admin database dump neo4j` as the image's numeric service UID `7474`.
   Neo4j requires write access to the exact data volume solely to acquire and release
   its offline store lock; the service stop and Tenant write fence make this the narrow
   trusted-tool exception. The one-shot container has no network, a read-only root
   filesystem, no capabilities, `no-new-privileges`, bounded temporary filesystems,
   and a group-scoped handoff nested beneath the private staging directory. Restart the
   same Neo4j service in `finally`, even when dump creation fails, and remove the handoff
   on every outcome.
5. Release the exclusive lock and reactivate the exact Tenant in `finally` after all
   three plaintext captures finish. The Tenant write outage covers expectation capture
   and the three store snapshots only; age encryption, checksums, and publication happen
   after reactivation. Caddy, observability, the gateway, and other tenants stay running.
6. Encrypt each local dump with age X25519. The recipient/identity files are
   mounted read-only with mode `0600`; key contents never appear in argv, logs,
   metrics, or manifests.
7. The content-free restore expectations include the
   SHA-256 identities of published Tenant Knowledge; the candidate count/set digest;
   complete count/set digests and optional representatives for active Private Memory,
   correction chains, and completed erasure tombstones; and the Agent Run count plus
   one representative snapshot bound to its exact native ActiveGraph event count and
   digest. At least one published Tenant Knowledge item is required so a later public
   recall cannot pass on an empty graph. Principal identifiers, claims, rationales,
   Agent output, and Memory content never enter the manifest.
8. Calculate SHA-256 and byte length over the ciphertext, not plaintext. Create a
   version-1 `BackupArtifactManifest` containing only identifiers, tenant scope,
   store/schema versions, creation time, checksum, size, key ID, and completeness.
9. Publish encrypted artifacts first and publish the complete version-2
   `BackupManifest`, including only those digests/counts, last. An interrupted set
   remains incomplete and is never a valid recovery point.
10. Emit one structured completion/failure event per tenant. Emit the latest valid
   backup age as a metric and alert when there is no complete point or it is more
   than 24 hours old.

Plaintext exists only in the mode-`0700` host staging directory. Remove each plaintext
immediately after its encryption attempt and remove every remaining plaintext on any
failure; do not put it in a shared Compose volume.

Run one exact Tenant backup and inspect its RPO result with:

```console
coengramctl backup plan --tenant-id tenant-product-a-backend
coengramctl backup create --tenant-id tenant-product-a-backend
coengramctl backup status --tenant-id tenant-product-a-backend
```

### Recovering an abandoned backup fence

A host/process crash releases PostgreSQL advisory locks automatically but intentionally
leaves the durable barrier row and Tenant inactive. This is fail-closed: do not edit the
Control Store or reactivate the Tenant by hand. Inspect the content-free barrier identity:

```console
coengramctl backup barrier-status --tenant-id tenant-product-a-backend
```

Confirm that no backup process is still running and copy the exact `barrier_id` returned
by that command. Recovery first proves it can acquire the exclusive Tenant database lock;
therefore it refuses a live backup, a wrong ID, an unrelated Tenant, or an arbitrary
pre-existing inactive Tenant. Repeat both immutable identifiers to recover:

```console
coengramctl backup recover-barrier \
  --tenant-id tenant-product-a-backend \
  --barrier-id BARRIER_ID \
  --confirm tenant-product-a-backend:BARRIER_ID
```

Run `barrier-status` again and verify normal authenticated Tenant access. If recovery
fails, preserve the barrier and investigate rather than changing `control.tenants`.

`create` writes the encrypted store objects before atomically exposing
`manifest.json`. It also writes a content-free, checksummed protection receipt beneath
`MEMORY_RECOVERY_EVIDENCE_DIR`; decommission confirmation verifies both the receipt and
the referenced manifest. Active Tenant schema migration uses the same protected receipt
as a mandatory first step. It accepts only a backup for the exact Tenant and active
route whose manifest checksum still matches, whose artifacts are complete, and whose
creation time is within the 24-hour RPO. A missing, stale, corrupt, wrong-Tenant, or
route-mismatched receipt fails before either PostgreSQL or Neo4j migration runs.

## Retention

Run retention only over complete, verified manifests. `plan_retention` keeps the
newest complete recovery point unconditionally, the newest point for seven UTC
days, and the newest point for four ISO weeks. It also keeps incomplete sets for
operator investigation instead of silently deleting them.

Apply only the exact IDs in `delete_backup_ids`. Every object-store deletion
failure appears in `RetentionExecution.failed_backup_ids` and must alert/retry.
Never reinterpret a prefix as permission to recursively delete a bucket.

Preview and apply local retention separately. Apply requires the immutable Tenant ID
twice and deletes only canonical backup directories containing exactly one manifest and
the three expected ciphertext files:

```console
coengramctl backup retention-plan --tenant-id tenant-product-a-backend
coengramctl backup retention-apply --tenant-id tenant-product-a-backend \
  --confirm tenant-product-a-backend
```

Retention also reads durable decommission state before planning and immediately before
each deletion. A backup referenced by a suspended, grace-period, or finalizing request
is reported in `plan.pins` with `status`, `reason`, and its grace deadline and cannot be
deleted while that request is open, including at the exact day-30 boundary. Successful
destruction atomically preserves a content-free recovery pin next to the decommission
tombstone for a further 30 days. The post-destruction pin expires at that second
day-30 boundary; ordinary seven-daily/four-weekly retention then applies. Accepted
exports do not pin a backup because their artifact is outside the backup set.

## Isolated restore

An ordinary restore can never overwrite an active tenant. Create a disposable
Compose project and route namespace whose target ID differs from the source
tenant ID and which is not addressable by Caddy or an active tenant token.

Before restoring:

1. parse the manifest with `extra=forbid` and require all three complete artifacts;
2. require a local ciphertext path for each artifact;
3. verify exact byte length and SHA-256 before attempting decryption;
4. select the age identity by non-secret key ID and treat any wrong-key result as
   a failed restore; and
5. reject any unsupported PostgreSQL, Neo4j, or schema version.

Restore control metadata into an isolated namespace, then the tenant PostgreSQL
database and tenant Neo4j volume. Do not register an active route. Exercise the
same public interfaces used in production and record whether all checks pass:

The offline Neo4j loader remains on `--network none` with a deterministic local
hostname. It receives a read-only, mode-`0750` handoff directory containing only the
temporarily group-readable `neo4j.dump`; the surrounding mode-`0700` workspace and all
other decrypted artifacts remain inaccessible. The dump returns to mode `0600` and the
handoff is removed on every outcome.

- authenticated typed HTTP Tenant Knowledge recall, requiring at least one result whose
  stable digest was captured before backup, plus complete graph-listing equivalence;
- Private Memory inspection, archive, and recall through the canonical Memory Module
  and authenticated personal interfaces where the owner remains active, requiring the
  exact expected active/correction/erasure proof sets, field-equivalent PostgreSQL and
  Neo4j state, and absence of completed-erasure IDs from Neo4j;
- governance candidate listing through `MemoryModule`, requiring the exact expected
  count and digest;
- Agent Run lookup through the repository interface, requiring the expected total count
  and representative snapshot digest, followed by an exact pre-reconciliation native
  ActiveGraph event count/digest check and canonical status/replay equivalence; and
- proof that no other tenant route or store is reachable.

Ciphertext size/checksum, supported store/schema versions, and the protected age identity
are validated before Docker resources are created. A wrong key, corrupt ciphertext,
missing expectation, empty recall, mismatched digest, or incompatible schema fails the
drill. Private graph resurrection/corruption, duplicate graph projections, or an
ActiveGraph stream that is missing or diverges from its fenced Agent Run snapshot also
fails closed. Failure preserves only the exact isolated workspace/resources needed for
diagnosis.

A restore with any failed check is a failed drill. Preserve the isolated target
long enough to diagnose it, then remove that exact drill project by the normal
ephemeral-environment procedure.

Restore an explicitly selected manifest, or choose the latest verified recovery point:

```console
coengramctl backup restore-drill \
  --manifest /var/lib/coengram/backups/BACKUP_ID/manifest.json \
  --target-id restore-drill-product-a-manual \
  --operator-id operator-alice \
  --confirm-target restore-drill-product-a-manual

coengramctl backup restore-latest-drill \
  --tenant-id tenant-product-a-backend \
  --target-id restore-drill-product-a-quarterly \
  --operator-id operator-alice \
  --confirm-target restore-drill-product-a-quarterly
```

The executor creates only names derived from the `restore-drill-*` target, never adds a
Control Store route, and rejects collisions with an active Tenant or an existing drill
workspace. A successful drill is removed after its content-free record is written; a
failed drill remains isolated for diagnosis and must be cleaned up by its exact target
identifier before retry.

After capturing diagnosis evidence, remove only that preserved target with a second
exact confirmation. The command refuses active Tenant identifiers, missing workspaces,
symlinks, and paths outside the configured restore root:

```sh
coengramctl backup cleanup-restore-drill \
  --target-id restore-drill-product-a-failed \
  --operator-id operator-alice \
  --confirm-target restore-drill-product-a-failed
```

## Quarterly drill record

Record only `backup_id`, source `tenant_id`, disposable `target_id`, operator ID,
start/end timestamps, elapsed seconds, named passed checks (including Private Memory),
outcome, and whether the four-hour RTO was met. Do not copy memory text, graph values,
prompts, token values, or database rows into the record.

Escalate the drill when an artifact is missing/incomplete/corrupt, a key is wrong,
a version is incompatible, a public check fails, tenant isolation cannot be
proved, or elapsed time exceeds four hours. A new successful drill is required
after remediation; editing the old record is not acceptable evidence.

The checked systemd templates run nightly backup with RPO/retention reporting and a
quarterly latest-valid restore. Enable timer instances only for exact active Tenant IDs
and monitor `systemctl --failed`, timer exit status, and the `overdue` backup health
field. Remote copy jobs must consume only `.age` files and `manifest.json`, never the
staging directory.
