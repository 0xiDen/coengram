from __future__ import annotations

from typing import Any

import pytest

from agent_memory_service.schema import (
    SchemaCompatibilityError,
    SchemaRequirement,
    require_schema,
)


class _Rows:
    def __init__(self, revisions: tuple[str, ...]) -> None:
        self._revisions = revisions

    def fetchall(self) -> list[tuple[str]]:
        return [(revision,) for revision in self._revisions]


class _Connection:
    def __init__(self, revisions: tuple[str, ...]) -> None:
        self._revisions = revisions

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str) -> _Rows:
        assert query == "SELECT version_num FROM alembic_version ORDER BY version_num"
        return _Rows(self._revisions)


def test_schema_requirement_accepts_only_the_exact_single_head(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "agent_memory_service.schema.psycopg.connect",
        lambda *_args, **_kwargs: _Connection(("expected",)),
    )

    require_schema(SchemaRequirement("postgresql://db", "expected", "Tenant Store"))


@pytest.mark.parametrize("revisions", [(), ("old",), ("expected", "branch")])
def test_schema_requirement_rejects_missing_old_or_branched_heads(
    monkeypatch: Any,
    revisions: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        "agent_memory_service.schema.psycopg.connect",
        lambda *_args, **_kwargs: _Connection(revisions),
    )

    with pytest.raises(SchemaCompatibilityError, match="schema revision is incompatible"):
        require_schema(SchemaRequirement("postgresql://db", "expected", "Tenant Store"))


def test_schema_requirement_hides_connection_details(monkeypatch: Any) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("postgresql://user:secret@example.invalid/db")

    monkeypatch.setattr("agent_memory_service.schema.psycopg.connect", fail)

    with pytest.raises(SchemaCompatibilityError) as caught:
        require_schema(SchemaRequirement("postgresql://db", "expected", "Control Store"))
    assert "secret" not in str(caught.value)
