"""Database URL normalization shared by PostgreSQL adapters."""

from __future__ import annotations


def psycopg_database_url(value: str) -> str:
    """Accept a native PostgreSQL DSN or SQLAlchemy's explicit psycopg URL."""

    if value.startswith("postgresql+psycopg://"):
        return value.replace("postgresql+psycopg://", "postgresql://", 1)
    return value
