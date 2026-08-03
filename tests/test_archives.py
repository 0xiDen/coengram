from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi.testclient import TestClient

from agent_memory_service.archive import ArchiveScope, decode_archive, encode_archive
from agent_memory_service.auth import InMemoryTokenStore, TokenService
from agent_memory_service.durable_memory import PrivateMemoryCommandStore
from agent_memory_service.governance import (
    CandidateStatus,
    InMemoryGovernanceStore,
    ProposeKnowledge,
    ReviewDecision,
    ReviewKnowledge,
)
from agent_memory_service.http import create_http_app
from agent_memory_service.lifecycle import (
    CorrectMemory,
    ErasureDecision,
    ErasureTombstone,
    InMemoryErasureStore,
    RequestErasure,
    ReviewErasure,
)
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import (
    MemoryItem,
    MemoryKind,
    MemoryScope,
    MemoryState,
    PrincipalKind,
    Provenance,
    RecallQuery,
    RetainMemory,
    TenantSession,
)
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter


def _session(tenant_id: str, principal_id: str, *roles: str) -> TenantSession:
    return TenantSession(
        tenant_id=tenant_id,
        actor_id=principal_id,
        actor_kind=PrincipalKind.USER,
        roles=frozenset(roles or ("tenant_member",)),
    )


class _AuthoritativeExportCommands:
    def __init__(self, item: MemoryItem) -> None:
        self._item = item

    async def list_private_memory_items(
        self,
        tenant_id: str,
        owner_principal_id: str,
    ) -> tuple[MemoryItem, ...]:
        assert tenant_id == "tenant-source"
        assert owner_principal_id == "user-alice"
        return (self._item,)


def test_canonical_archive_fixture_has_stable_record_and_aggregate_checksums() -> None:
    item = MemoryItem(
        id="fixture-memory",
        owner_principal_id="fixture-user",
        scope=MemoryScope.PRIVATE,
        content="Canonical archive fixture.",
        kind=MemoryKind.EXPLICIT,
        confidence=0.75,
        provenance=Provenance(actor_id="fixture-user", source="explicit"),
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    archive = encode_archive(ArchiveScope.PRIVATE, [item.model_dump(mode="json")])
    lines = archive.splitlines()

    assert hashlib.sha256(archive).hexdigest() == (
        "05d8ae766d43ecc5e28fee14914afb007764af5a41c5f5f7bc68995abcbe79f3"
    )
    assert json.loads(lines[1])["checksum"]["value"] == (
        "529921f154f9ae52d13d5e32ecca752ea358e480feb069d43d2dc247a9978cb2"
    )
    assert json.loads(lines[-1])["value"] == (
        "354376c5fb271fa9d1c307b7bd290e6f0d1060d28aa4e20af4b1c9729ea1cc12"
    )


@pytest.mark.asyncio
async def test_private_archive_round_trip_preserves_scope_checksum_and_stable_id() -> None:
    source_memory = MemoryModule(InMemoryTenantMemoryRouter(["tenant-source"]))
    alice = _session("tenant-source", "user-alice")
    bob = _session("tenant-source", "user-bob")
    alice_item = await source_memory.retain(
        alice,
        RetainMemory(
            content="Alice uses compact incident summaries.",
            kind=MemoryKind.PREFERENCE,
            idempotency_key="alice-summary-style",
        ),
    )
    await source_memory.retain(
        bob,
        RetainMemory(
            content="Bob's private deploy preference.",
            kind=MemoryKind.PREFERENCE,
            idempotency_key="bob-deploy-style",
        ),
    )

    archive = await source_memory.export_private(alice)

    assert b"Alice uses compact incident summaries" in archive
    assert b"Bob's private deploy preference" not in archive
    assert b'"checksum"' in archive

    target_memory = MemoryModule(InMemoryTenantMemoryRouter(["tenant-target"]))
    imported_alice = _session("tenant-target", "user-imported-alice")
    result = await target_memory.import_private(imported_alice, archive)
    recalled = await target_memory.recall(imported_alice, RecallQuery(query="incident summaries"))

    assert result.imported == 1
    assert recalled.items[0].id == alice_item.id
    assert recalled.items[0].owner_principal_id == "user-imported-alice"


@pytest.mark.asyncio
async def test_private_export_includes_durable_item_before_graph_projection() -> None:
    item = MemoryItem(
        id="pending-memory",
        owner_principal_id="user-alice",
        scope=MemoryScope.PRIVATE,
        content="Accepted durably before graph projection.",
        kind=MemoryKind.EXPLICIT,
        confidence=1.0,
        provenance=Provenance(actor_id="user-alice", source="explicit"),
    )
    memory = MemoryModule(
        InMemoryTenantMemoryRouter(["tenant-source"]),
        commands=cast(PrivateMemoryCommandStore, _AuthoritativeExportCommands(item)),
    )

    archive = await memory.export_private(_session("tenant-source", "user-alice"))

    decoded = decode_archive(archive, ArchiveScope.PRIVATE)
    assert decoded.memory_items == (item,)


@pytest.mark.asyncio
async def test_tenant_archive_excludes_private_memory_and_imports_knowledge_as_candidate() -> None:
    source_router = InMemoryTenantMemoryRouter(["tenant-source"])
    source_governance = InMemoryGovernanceStore()
    source_memory = MemoryModule(source_router, source_governance)
    alice = _session("tenant-source", "user-alice")
    admin = _session(
        "tenant-source",
        "user-admin",
        "tenant_member",
        "tenant_administrator",
    )
    await source_memory.retain(
        alice,
        RetainMemory(
            content="Alice private source must never enter a Tenant archive.",
            idempotency_key="alice-private-archive-check",
        ),
    )
    await source_router.for_tenant("tenant-source").publish_tenant_knowledge(
        "candidate-existing",
        "Product A uses canary deployments.",
        0.9,
        "user-curator",
    )
    candidate = await source_governance.propose(
        "tenant-source",
        "user-alice",
        ProposeKnowledge(
            claim="Reviews retain safe governance history.",
            source_memory_ids=("private-source-must-not-export",),
            idempotency_key="archive-governance-candidate",
        ),
    )
    await source_governance.review(
        "tenant-source",
        "user-admin",
        ReviewKnowledge(
            candidate_id=candidate.id,
            decision=ReviewDecision.REJECT,
            rationale="Use a more concrete claim.",
            idempotency_key="archive-governance-review",
        ),
    )

    archive = await source_memory.export_tenant(admin)

    assert b"Product A uses canary deployments" in archive
    assert b"Alice private source" not in archive
    assert b"Reviews retain safe governance history" in archive
    assert b"Use a more concrete claim" in archive
    assert b"private-source-must-not-export" not in archive

    target_governance = InMemoryGovernanceStore()
    target_memory = MemoryModule(
        InMemoryTenantMemoryRouter(["tenant-target"]),
        target_governance,
    )
    target_admin = _session(
        "tenant-target",
        "user-target-admin",
        "tenant_member",
        "tenant_administrator",
    )
    imported = await target_memory.import_tenant(target_admin, archive)

    assert imported.imported == 1
    assert imported.candidates[0].status is CandidateStatus.SUBMITTED
    assert (
        await target_memory.recall(target_admin, RecallQuery(query="canary deployments"))
    ).items == ()


@pytest.mark.asyncio
async def test_archive_checksum_tampering_is_rejected() -> None:
    memory = MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"]))
    alice = _session("tenant-a", "user-alice")
    await memory.retain(
        alice,
        RetainMemory(content="Original memory.", idempotency_key="original-memory"),
    )
    archive = await memory.export_private(alice)

    with pytest.raises(ValueError, match="checksum"):
        await memory.import_private(alice, archive.replace(b"Original", b"Changed!"))


@pytest.mark.asyncio
async def test_private_archive_round_trips_corrections_and_content_free_erasure_tombstones() -> (
    None
):
    source_router = InMemoryTenantMemoryRouter(["tenant-source"])
    source_erasures = InMemoryErasureStore()
    source = MemoryModule(source_router, erasures=source_erasures)
    alice = _session("tenant-source", "user-alice")
    admin = _session("tenant-source", "user-admin", "tenant_member", "tenant_administrator")
    original = await source.retain(
        alice,
        RetainMemory(content="Deploy on Thursday.", idempotency_key="deploy-day"),
    )
    corrected = await source.correct(
        alice,
        CorrectMemory(
            memory_id=original.id,
            replacement_content="Deploy on Tuesday.",
            reason="The release schedule changed.",
            idempotency_key="correct-deploy-day",
        ),
    )
    secret = await source.retain(
        alice,
        RetainMemory(content="Temporary bridge secret 1234.", idempotency_key="bridge-secret"),
    )
    request = await source.request_erasure(
        alice,
        RequestErasure(
            memory_id=secret.id,
            reason="This free-text reason must be scrubbed.",
            idempotency_key="erase-bridge-secret",
        ),
    )
    await source.review_erasure(
        admin,
        ReviewErasure(
            request_id=request.id,
            decision=ErasureDecision.APPROVE,
            rationale="This free-text review rationale must be scrubbed.",
            idempotency_key="approve-bridge-secret",
        ),
    )
    await source.erase_next("tenant-source")

    completed = await source_erasures.get_request("tenant-source", request.id)
    assert completed is not None
    assert completed.reason is None
    assert completed.review_rationale is None

    archive = await source.export_private(alice)
    assert b"bridge secret 1234" not in archive.lower()
    assert b"free-text reason" not in archive
    assert b"free-text review rationale" not in archive

    target_erasures = InMemoryErasureStore()
    target = MemoryModule(
        InMemoryTenantMemoryRouter(["tenant-target"]),
        erasures=target_erasures,
    )
    imported_alice = _session("tenant-target", "user-imported-alice")
    first = await target.import_private(imported_alice, archive)
    second = await target.import_private(imported_alice, archive)
    imported_items = await target.inspect_private(imported_alice)
    imported_by_id = {item.id: item for item in imported_items}
    decoded_round_trip = decode_archive(
        await target.export_private(imported_alice), ArchiveScope.PRIVATE
    )

    assert first.imported == 3
    assert first.erasure_tombstones == 1
    assert second.imported == 0
    assert second.skipped == 3
    assert imported_by_id[original.id].state is MemoryState.SUPERSEDED
    assert imported_by_id[corrected.id].supersedes_id == original.id
    assert decoded_round_trip.erasure_tombstones[0].memory_id == secret.id
    assert decoded_round_trip.erasure_tombstones[0].reviewed_by == "user-admin"


@pytest.mark.asyncio
async def test_all_archive_records_are_validated_before_any_private_import_mutation() -> None:
    valid = MemoryItem(
        id="valid-memory",
        owner_principal_id="user-source",
        scope=MemoryScope.PRIVATE,
        content="Valid content.",
        kind=MemoryKind.EXPLICIT,
        confidence=1.0,
        provenance=Provenance(actor_id="user-source", source="explicit"),
    ).model_dump(mode="json")
    malformed = {**valid, "id": "malformed-memory"}
    malformed.pop("content")
    archive = encode_archive(ArchiveScope.PRIVATE, [valid, malformed])
    target = MemoryModule(InMemoryTenantMemoryRouter(["tenant-target"]))
    alice = _session("tenant-target", "user-alice")

    with pytest.raises(ValueError, match="declared schema"):
        await target.import_private(alice, archive)

    assert await target.inspect_private(alice) == ()


@pytest.mark.asyncio
async def test_archive_conflicts_are_preflighted_before_any_private_import_mutation() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-target"])
    erasures = InMemoryErasureStore()
    target = MemoryModule(router, erasures=erasures)
    alice = _session("tenant-target", "user-alice")
    existing = MemoryItem(
        id="existing-memory",
        owner_principal_id="user-alice",
        scope=MemoryScope.PRIVATE,
        content="Existing destination memory.",
        kind=MemoryKind.EXPLICIT,
        confidence=1.0,
        provenance=Provenance(actor_id="user-alice", source="explicit"),
    )
    await router.for_tenant("tenant-target").import_private("user-alice", existing)
    new_item = existing.model_copy(
        update={"id": "new-memory", "content": "This must not import partially."}
    )
    tombstone = ErasureTombstone(
        id="conflicting-erasure",
        memory_id=existing.id,
        requester_id="user-source",
        owner_principal_id="user-source",
        created_at=datetime.now(UTC),
        reviewed_by="user-source-admin",
        completed_at=datetime.now(UTC),
    )
    archive = encode_archive(
        ArchiveScope.PRIVATE,
        [new_item.model_dump(mode="json")],
        erasure_records=[tombstone.model_dump(mode="json")],
    )

    with pytest.raises(ValueError, match="conflicts with an active Memory Item"):
        await target.import_private(alice, archive)

    assert [item.id for item in await target.inspect_private(alice)] == [existing.id]


@pytest.mark.asyncio
async def test_malformed_governance_history_rejects_tenant_archive_before_candidates() -> None:
    shared = MemoryItem(
        id="shared-memory",
        owner_principal_id=None,
        scope=MemoryScope.TENANT_KNOWLEDGE,
        content="Product A uses canary deployments.",
        kind=MemoryKind.EXPLICIT,
        confidence=0.9,
        provenance=Provenance(actor_id="user-curator", source="candidate:source"),
    )
    archive = encode_archive(
        ArchiveScope.TENANT,
        [shared.model_dump(mode="json")],
        governance_records=[{"id": "malformed-governance"}],
    )
    governance = InMemoryGovernanceStore()
    target = MemoryModule(InMemoryTenantMemoryRouter(["tenant-target"]), governance)
    admin = _session("tenant-target", "user-admin", "tenant_member", "tenant_administrator")

    with pytest.raises(ValueError, match="declared schema"):
        await target.import_tenant(admin, archive)

    assert await governance.list_candidates("tenant-target") == ()


@pytest.mark.asyncio
async def test_record_checksum_detects_tampering_even_with_recomputed_archive_checksum() -> None:
    item = MemoryItem(
        id="memory-one",
        owner_principal_id="user-alice",
        scope=MemoryScope.PRIVATE,
        content="Original content.",
        kind=MemoryKind.EXPLICIT,
        confidence=1.0,
        provenance=Provenance(actor_id="user-alice", source="explicit"),
    )
    lines = encode_archive(ArchiveScope.PRIVATE, [item.model_dump(mode="json")]).splitlines()
    record = json.loads(lines[1])
    record["payload"]["content"] = "Tampered content."
    lines[1] = _canonical(record)
    payload = b"\n".join(lines[:-1]) + b"\n"
    footer = json.loads(lines[-1])
    footer["value"] = hashlib.sha256(payload).hexdigest()
    lines[-1] = _canonical(footer)

    with pytest.raises(ValueError, match="record checksum"):
        decode_archive(b"\n".join(lines) + b"\n", ArchiveScope.PRIVATE)


def test_private_archive_http_import_is_bad_request_and_idempotent() -> None:
    tokens = TokenService(InMemoryTokenStore())
    alice = _session("tenant-a", "user-alice")
    token = tokens.issue(alice, lifetime=timedelta(days=1)).access_token
    memory = MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"]))
    client = TestClient(create_http_app(memory, tokens))
    auth_headers = {"Authorization": f"Bearer {token}"}
    archive_headers = {**auth_headers, "Content-Type": "application/x-ndjson"}
    retained = client.post(
        "/api/v1/memories",
        headers=auth_headers,
        json={"content": "Portable memory.", "idempotency_key": "portable-memory"},
    )
    assert retained.status_code == 202
    archive = client.get("/api/v1/memory-archives/private", headers=auth_headers).content

    invalid = client.post(
        "/api/v1/memory-archives/private/import",
        headers=archive_headers,
        content=archive.replace(b"Portable", b"Tampered"),
    )
    first = client.post(
        "/api/v1/memory-archives/private/import", headers=archive_headers, content=archive
    )
    second = client.post(
        "/api/v1/memory-archives/private/import", headers=archive_headers, content=archive
    )

    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_request"
    assert first.json() == {
        "imported": 0,
        "skipped": 1,
        "candidates": [],
        "erasure_tombstones": 0,
        "governance_records": 0,
    }
    assert second.json() == first.json()


def test_tenant_archive_http_rejects_malformed_governance_and_retries_idempotently() -> None:
    tokens = TokenService(InMemoryTokenStore())
    admin = _session("tenant-a", "user-admin", "tenant_member", "tenant_administrator")
    token = tokens.issue(admin, lifetime=timedelta(days=1)).access_token
    governance = InMemoryGovernanceStore()
    memory = MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"]), governance)
    client = TestClient(create_http_app(memory, tokens))
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-ndjson",
    }
    shared = MemoryItem(
        id="http-shared-memory",
        owner_principal_id=None,
        scope=MemoryScope.TENANT_KNOWLEDGE,
        content="HTTP archives produce reviewable candidates.",
        kind=MemoryKind.EXPLICIT,
        confidence=0.9,
        provenance=Provenance(actor_id="source-curator", source="candidate:source"),
    )
    valid = encode_archive(ArchiveScope.TENANT, [shared.model_dump(mode="json")])
    malformed = encode_archive(
        ArchiveScope.TENANT,
        [shared.model_dump(mode="json")],
        governance_records=[{"id": "missing-required-fields"}],
    )

    rejected = client.post(
        "/api/v1/memory-archives/tenant/import", headers=headers, content=malformed
    )
    first = client.post("/api/v1/memory-archives/tenant/import", headers=headers, content=valid)
    second = client.post("/api/v1/memory-archives/tenant/import", headers=headers, content=valid)

    assert rejected.status_code == 400
    assert first.status_code == 200
    assert first.json()["imported"] == 1
    assert first.json()["candidates"][0]["status"] == "submitted"
    assert second.status_code == 200
    assert second.json()["imported"] == 0
    assert second.json()["skipped"] == 1


def _canonical(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
