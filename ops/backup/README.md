# Backup operations boundary

`backup-policy.example.json` records the iteration-1 scheduling and safety
defaults. Copy it into the server's external operations configuration and keep
the copy outside the repository if it contains environment-specific object keys.

The application contract is in `agent_memory_service.backup`. A production
adapter must:

1. create a control PostgreSQL dump and a PostgreSQL dump for every active
   tenant database;
2. stop only the selected tenant's Neo4j Compose service, snapshot its tenant
   volume, and restart that service in a `finally` path;
3. encrypt every dump locally through `AgeEncryptionCommandAdapter` before any
   artifact is handed to a remote/retention adapter;
4. calculate the ciphertext size and SHA-256, then publish the complete manifest
   last; and
5. emit `BackupHealth.overdue` and every `RetentionExecution.failed_backup_ids`
   result to the central observability pipeline.

The adapter must not invoke a shell with interpolated identifiers. It must pass
an argv vector and exact tenant resource names. Remote object-store provisioning,
Cloudflare DNS, and retention-bucket policy are intentionally external to this
repository.

No automated restore may target an active tenant route. Quarterly restore drills
use disposable Compose projects whose names start with the configured prefix.
See `docs/runbooks/backup-restore.md` for the tested procedure and evidence.
