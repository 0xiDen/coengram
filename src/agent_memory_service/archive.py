"""Versioned, independently checksummed JSON Lines Memory Archives."""

from __future__ import annotations

import hashlib
import json
import math
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_memory_service.governance import CandidateStatus, KnowledgeCandidateView
from agent_memory_service.lifecycle import ErasureTombstone
from agent_memory_service.models import MemoryItem

_ARCHIVE_FORMAT = "memory-archive"
_ARCHIVE_VERSION = 2
_CHECKSUM_ALGORITHM = "sha256"


class ArchiveScope(StrEnum):
    PRIVATE = "private"
    TENANT = "tenant"


class ArchiveRecordType(StrEnum):
    MEMORY_ITEM = "memory_item"
    GOVERNANCE_CANDIDATE = "governance_candidate"
    ERASURE_TOMBSTONE = "erasure_tombstone"


class DecodedArchive(BaseModel):
    """A completely validated archive, safe to hand to an import workflow."""

    model_config = ConfigDict(frozen=True)

    memory_items: tuple[MemoryItem, ...] = ()
    governance_candidates: tuple[KnowledgeCandidateView, ...] = ()
    erasure_tombstones: tuple[ErasureTombstone, ...] = ()


class ArchiveImportResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    imported: int
    skipped: int
    candidates: tuple[KnowledgeCandidateView, ...] = ()
    erasure_tombstones: int = 0
    governance_records: int = 0


def encode_archive(
    scope: ArchiveScope,
    records: list[dict[str, Any]],
    *,
    governance_records: list[dict[str, Any]] | None = None,
    erasure_records: list[dict[str, Any]] | None = None,
) -> bytes:
    """Encode an archive whose records and aggregate are independently verifiable."""

    documents: list[dict[str, Any]] = [
        {
            "type": "manifest",
            "format": _ARCHIVE_FORMAT,
            "version": _ARCHIVE_VERSION,
            "scope": scope.value,
        }
    ]
    documents.extend(_record(ArchiveRecordType.MEMORY_ITEM, record) for record in records)
    documents.extend(
        _record(ArchiveRecordType.GOVERNANCE_CANDIDATE, record)
        for record in governance_records or []
    )
    documents.extend(
        _record(ArchiveRecordType.ERASURE_TOMBSTONE, record) for record in erasure_records or []
    )
    lines = [_canonical(document) for document in documents]
    payload = b"\n".join(lines) + b"\n"
    archive_checksum = hashlib.sha256(payload).hexdigest()
    footer = {
        "type": "archive_checksum",
        "algorithm": _CHECKSUM_ALGORITHM,
        "record_count": len(documents) - 1,
        "value": archive_checksum,
    }
    return payload + _canonical(footer) + b"\n"


def decode_archive(data: bytes, expected_scope: ArchiveScope) -> DecodedArchive:
    """Decode and validate every record before returning any importable value."""

    lines = data.splitlines()
    if len(lines) < 2:
        raise ValueError("Memory Archive is incomplete")
    try:
        documents = [json.loads(line) for line in lines]
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Memory Archive is not valid JSON Lines") from exc
    if not all(isinstance(document, dict) for document in documents):
        raise ValueError("Memory Archive lines must be JSON objects")

    manifest = documents[0]
    if manifest != {
        "type": "manifest",
        "format": _ARCHIVE_FORMAT,
        "version": _ARCHIVE_VERSION,
        "scope": expected_scope.value,
    }:
        raise ValueError("Memory Archive manifest or scope is not supported")

    footer = documents[-1]
    payload = b"\n".join(lines[:-1]) + b"\n"
    if footer != {
        "type": "archive_checksum",
        "algorithm": _CHECKSUM_ALGORITHM,
        "record_count": len(lines) - 2,
        "value": hashlib.sha256(payload).hexdigest(),
    }:
        raise ValueError("Memory Archive checksum does not match")

    memory_items: list[MemoryItem] = []
    governance_candidates: list[KnowledgeCandidateView] = []
    erasure_tombstones: list[ErasureTombstone] = []
    for document in documents[1:-1]:
        record_type, record_payload = _validate_record(document)
        if record_type is ArchiveRecordType.MEMORY_ITEM:
            _require_exact_fields(record_payload, MemoryItem)
            item = MemoryItem.model_validate(record_payload)
            _validate_memory_item(item)
            memory_items.append(item)
        elif record_type is ArchiveRecordType.GOVERNANCE_CANDIDATE:
            _require_exact_fields(record_payload, KnowledgeCandidateView)
            candidate = KnowledgeCandidateView.model_validate(record_payload)
            _validate_governance_candidate(candidate)
            governance_candidates.append(candidate)
        elif record_type is ArchiveRecordType.ERASURE_TOMBSTONE:
            _require_exact_fields(record_payload, ErasureTombstone)
            tombstone = ErasureTombstone.model_validate(record_payload)
            _validate_erasure_tombstone(tombstone)
            erasure_tombstones.append(tombstone)

    _validate_unique_ids(memory_items, governance_candidates, erasure_tombstones)
    return DecodedArchive(
        memory_items=tuple(memory_items),
        governance_candidates=tuple(governance_candidates),
        erasure_tombstones=tuple(erasure_tombstones),
    )


def order_correction_history(items: tuple[MemoryItem, ...]) -> tuple[MemoryItem, ...]:
    """Place correction ancestors before descendants and reject cyclic histories."""

    by_id = {item.id: item for item in items}
    ordered: list[MemoryItem] = []
    permanent: set[str] = set()
    visiting: set[str] = set()

    def visit(item: MemoryItem) -> None:
        if item.id in permanent:
            return
        if item.id in visiting:
            raise ValueError("Memory Archive correction history contains a cycle")
        visiting.add(item.id)
        if item.supersedes_id is not None and item.supersedes_id in by_id:
            visit(by_id[item.supersedes_id])
        visiting.remove(item.id)
        permanent.add(item.id)
        ordered.append(item)

    for item in items:
        visit(item)
    return tuple(ordered)


def _record(record_type: ArchiveRecordType, payload: dict[str, Any]) -> dict[str, Any]:
    checksum_input = {"record_type": record_type.value, "payload": payload}
    return {
        "type": "record",
        **checksum_input,
        "checksum": {
            "algorithm": _CHECKSUM_ALGORITHM,
            "value": hashlib.sha256(_canonical(checksum_input)).hexdigest(),
        },
    }


def _validate_record(document: dict[str, Any]) -> tuple[ArchiveRecordType, dict[str, Any]]:
    if set(document) != {"type", "record_type", "payload", "checksum"}:
        raise ValueError("Memory Archive contains an unsupported record")
    if document.get("type") != "record" or not isinstance(document.get("payload"), dict):
        raise ValueError("Memory Archive contains an unsupported record")
    try:
        record_type = ArchiveRecordType(str(document.get("record_type")))
    except ValueError as exc:
        raise ValueError("Memory Archive contains an unsupported record") from exc
    payload = document["payload"]
    checksum_input = {"record_type": record_type.value, "payload": payload}
    expected_checksum = {
        "algorithm": _CHECKSUM_ALGORITHM,
        "value": hashlib.sha256(_canonical(checksum_input)).hexdigest(),
    }
    if document.get("checksum") != expected_checksum:
        raise ValueError("Memory Archive record checksum does not match")
    return record_type, payload


def _require_exact_fields(payload: dict[str, Any], model: type[BaseModel]) -> None:
    if set(payload) != set(model.model_fields):
        raise ValueError("Memory Archive record does not match its declared schema")


def _validate_memory_item(item: MemoryItem) -> None:
    if (
        not item.id.strip()
        or not item.content.strip()
        or len(item.content) > 16_000
        or not math.isfinite(item.confidence)
        or not 0.0 <= item.confidence <= 1.0
        or not item.provenance.actor_id.strip()
        or not item.provenance.source.strip()
        or item.created_at.tzinfo is None
        or (item.supersedes_id is not None and not item.supersedes_id.strip())
    ):
        raise ValueError("Memory Archive contains an invalid Memory Item")


def _validate_governance_candidate(candidate: KnowledgeCandidateView) -> None:
    if (
        not candidate.id.strip()
        or not candidate.claim.strip()
        or len(candidate.claim) > 16_000
        or not math.isfinite(candidate.confidence)
        or not 0.0 <= candidate.confidence <= 1.0
        or not candidate.proposer_id.strip()
        or candidate.source_count < 0
        or candidate.created_at.tzinfo is None
    ):
        raise ValueError("Memory Archive contains an invalid governance record")
    reviewed = candidate.reviewed_by is not None or candidate.review_rationale is not None
    if reviewed and (
        candidate.reviewed_by is None
        or not candidate.reviewed_by.strip()
        or candidate.review_rationale is None
        or not candidate.review_rationale.strip()
    ):
        raise ValueError("Memory Archive contains an invalid governance review")
    if candidate.status is CandidateStatus.SUBMITTED and reviewed:
        raise ValueError("Memory Archive contains an invalid governance review")
    if (
        candidate.status
        in {
            CandidateStatus.APPROVED,
            CandidateStatus.REJECTED,
            CandidateStatus.PUBLISHING,
            CandidateStatus.PUBLISHED,
        }
        and not reviewed
    ):
        raise ValueError("Memory Archive contains an incomplete governance review")


def _validate_erasure_tombstone(tombstone: ErasureTombstone) -> None:
    if (
        not tombstone.id.strip()
        or not tombstone.memory_id.strip()
        or not tombstone.requester_id.strip()
        or not tombstone.owner_principal_id.strip()
        or not tombstone.reviewed_by.strip()
        or tombstone.created_at.tzinfo is None
        or tombstone.completed_at.tzinfo is None
        or tombstone.completed_at < tombstone.created_at
    ):
        raise ValueError("Memory Archive contains an invalid Erasure Tombstone")


def _validate_unique_ids(
    memory_items: list[MemoryItem],
    governance_candidates: list[KnowledgeCandidateView],
    erasure_tombstones: list[ErasureTombstone],
) -> None:
    identifiers = [item.id for item in memory_items]
    identifiers.extend(tombstone.memory_id for tombstone in erasure_tombstones)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Memory Archive contains duplicate Memory Item identifiers")
    candidate_ids = [candidate.id for candidate in governance_candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Memory Archive contains duplicate governance identifiers")


def _canonical(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
