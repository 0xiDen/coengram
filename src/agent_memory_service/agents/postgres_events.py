"""Bounded, lifecycle-owned PostgreSQL pools for ActiveGraph event projections."""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Any

from activegraph.store.postgres import PostgresEventStore  # type: ignore[import-untyped]
from psycopg_pool import ConnectionPool

from agent_memory_service.database_barrier import acquire_backup_shared_lock
from agent_memory_service.models import TenantSession


class _BarrierAwareActiveGraphStore:
    """Apply the Tenant backup write fence to native ActiveGraph mutations."""

    def __init__(self, pool: Any, run_id: str) -> None:
        self._pool = pool
        self.run_id = run_id

    def append(self, event: Any) -> None:
        self._mutate("append", event)

    def truncate_after(self, event_id: str) -> None:
        self._mutate("truncate_after", event_id)

    def upsert_run(self, **values: Any) -> None:
        self._mutate("upsert_run", **values)

    def iter_events(self, after: str | None = None, until: str | None = None) -> Any:
        # Materialize before the short-lived delegate is released.
        return iter(tuple(self._delegate().iter_events(after=after, until=until)))

    def get_event(self, event_id: str) -> Any:
        return self._delegate().get_event(event_id)

    def count(self) -> int:
        return int(self._delegate().count())

    def get_run(self) -> Any:
        return self._delegate().get_run()

    def close(self) -> None:
        # Pool ownership belongs to ManagedActiveGraphStoreFactory.
        return None

    def _delegate(self) -> Any:
        return PostgresEventStore(self._pool, self.run_id)

    def _mutate(self, method: str, *arguments: Any, **values: Any) -> None:
        with self._pool.connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    acquire_backup_shared_lock(cursor)
                operation = getattr(PostgresEventStore(connection, self.run_id), method)
                operation(*arguments, **values)


class ManagedActiveGraphStoreFactory:
    """Route ActiveGraph stores through one bounded connection pool per Tenant."""

    def __init__(
        self,
        database_url_for_tenant: Callable[[str], str],
        *,
        min_size: int = 0,
        max_size: int = 4,
        timeout_seconds: float = 10.0,
    ) -> None:
        if min_size < 0 or max_size < 1 or min_size > max_size:
            raise ValueError("ActiveGraph pool bounds are invalid")
        if timeout_seconds <= 0:
            raise ValueError("ActiveGraph pool timeout must be positive")
        self._database_url_for_tenant = database_url_for_tenant
        self._min_size = min_size
        self._max_size = max_size
        self._timeout_seconds = timeout_seconds
        self._pools: dict[str, Any] = {}
        self._database_urls: dict[str, str] = {}
        self._closed = False
        self._lock = RLock()

    def __call__(self, session: TenantSession, run_id: str) -> Any:
        if not run_id:
            raise ValueError("ActiveGraph Agent Run identifier cannot be empty")
        with self._lock:
            if self._closed:
                raise RuntimeError("ActiveGraph store factory is closed")
            database_url = self._database_url_for_tenant(session.tenant_id)
            pool = self._pools.get(session.tenant_id)
            if pool is None:
                pool = ConnectionPool(
                    conninfo=database_url,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    timeout=self._timeout_seconds,
                    open=False,
                )
                pool.open(wait=True, timeout=self._timeout_seconds)
                self._pools[session.tenant_id] = pool
                self._database_urls[session.tenant_id] = database_url
            elif self._database_urls[session.tenant_id] != database_url:
                raise RuntimeError("Tenant database route changed; restart is required")
            return _BarrierAwareActiveGraphStore(pool, run_id)

    def close(self) -> None:
        """Close every owned pool once and reject subsequent store creation."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            pools = tuple(self._pools.values())
            self._pools.clear()
            self._database_urls.clear()
        for pool in pools:
            pool.close(timeout=self._timeout_seconds)
