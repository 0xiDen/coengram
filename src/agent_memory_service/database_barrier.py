"""PostgreSQL advisory-lock primitives for a coherent Tenant backup cut."""

from __future__ import annotations

from typing import Any

# One Tenant owns one PostgreSQL database, so the database name is the isolation key.
# The fixed seed keeps this lock namespace independent from application idempotency locks.
_BACKUP_LOCK_SEED = 0x434F454E4752414D
_BACKUP_LOCK_KEY_SQL = f"hashtextextended(current_database(), {_BACKUP_LOCK_SEED})"

ACQUIRE_BACKUP_SHARED_LOCK_SQL = f"SELECT pg_advisory_xact_lock_shared({_BACKUP_LOCK_KEY_SQL})"
ACQUIRE_BACKUP_EXCLUSIVE_LOCK_SQL = f"SELECT pg_advisory_lock({_BACKUP_LOCK_KEY_SQL})"
TRY_ACQUIRE_BACKUP_EXCLUSIVE_LOCK_SQL = f"SELECT pg_try_advisory_lock({_BACKUP_LOCK_KEY_SQL})"
RELEASE_BACKUP_EXCLUSIVE_LOCK_SQL = f"SELECT pg_advisory_unlock({_BACKUP_LOCK_KEY_SQL})"


def acquire_backup_shared_lock(cursor: Any) -> None:
    """Fence one synchronous Tenant mutation inside its current transaction."""

    cursor.execute(ACQUIRE_BACKUP_SHARED_LOCK_SQL)


async def acquire_backup_shared_lock_async(cursor: Any) -> None:
    """Fence one asynchronous Tenant mutation inside its current transaction."""

    await cursor.execute(ACQUIRE_BACKUP_SHARED_LOCK_SQL)
