"""Opaque Access Token issuance and authentication."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from agent_memory_service.models import TenantSession


class AuthenticationError(Exception):
    """Raised when a credential cannot produce a valid Tenant Session."""


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    token_id: str
    access_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RotatedCredential:
    """One-time replacement credential and the old token's bounded overlap."""

    credential: IssuedCredential
    previous_token_id: str
    previous_valid_until: datetime


@dataclass(frozen=True, slots=True)
class TokenRecord:
    token_id: str
    verifier: bytes
    session: TenantSession
    expires_at: datetime
    issued_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class TokenStore(Protocol):
    def save(self, record: TokenRecord) -> None:
        """Persist a token verifier and its fixed authorization context."""

    def get(self, token_id: str) -> TokenRecord | None:
        """Return the token record without revealing credential material."""

    def revoke(self, token_id: str, revoked_at: datetime) -> bool:
        """Revoke a token immediately and idempotently."""

    def mark_used(self, token_id: str, used_at: datetime) -> None:
        """Record content-free credential activity for operator signals."""

    def rotate(
        self,
        previous_token_id: str,
        replacement: TokenRecord,
        *,
        previous_valid_until: datetime,
        rotated_at: datetime,
    ) -> bool:
        """Atomically save replacement and shorten the previous token's validity."""


class InMemoryTokenStore:
    def __init__(self) -> None:
        self._records: dict[str, TokenRecord] = {}

    def save(self, record: TokenRecord) -> None:
        self._records[record.token_id] = record

    def get(self, token_id: str) -> TokenRecord | None:
        return self._records.get(token_id)

    def revoke(self, token_id: str, revoked_at: datetime) -> bool:
        existing = self._records.get(token_id)
        if existing is None:
            return False
        if existing.revoked_at is not None:
            return True
        self._records[token_id] = TokenRecord(
            token_id=existing.token_id,
            verifier=existing.verifier,
            session=existing.session,
            expires_at=existing.expires_at,
            issued_at=existing.issued_at,
            revoked_at=revoked_at,
            last_used_at=existing.last_used_at,
        )
        return True

    def mark_used(self, token_id: str, used_at: datetime) -> None:
        existing = self._records.get(token_id)
        if existing is None:
            return
        self._records[token_id] = TokenRecord(
            token_id=existing.token_id,
            verifier=existing.verifier,
            session=existing.session,
            expires_at=existing.expires_at,
            issued_at=existing.issued_at,
            revoked_at=existing.revoked_at,
            last_used_at=used_at,
        )

    def rotate(
        self,
        previous_token_id: str,
        replacement: TokenRecord,
        *,
        previous_valid_until: datetime,
        rotated_at: datetime,
    ) -> bool:
        previous = self._records.get(previous_token_id)
        if (
            previous is None
            or previous.revoked_at is not None
            or previous.expires_at <= rotated_at
            or replacement.token_id in self._records
        ):
            return False
        self._records[previous_token_id] = TokenRecord(
            token_id=previous.token_id,
            verifier=previous.verifier,
            session=previous.session,
            expires_at=min(previous.expires_at, previous_valid_until),
            issued_at=previous.issued_at,
            revoked_at=previous.revoked_at,
            last_used_at=previous.last_used_at,
        )
        self._records[replacement.token_id] = replacement
        return True


class TokenService:
    """Create high-entropy tokens and derive immutable Tenant Sessions from them."""

    _PREFIX = "mem1"

    def __init__(self, store: TokenStore) -> None:
        self._store = store

    def issue(
        self,
        session: TenantSession,
        *,
        lifetime: timedelta,
        now: datetime | None = None,
    ) -> IssuedCredential:
        issued_at = now or datetime.now(UTC)
        credential, record = self._build_record(session, lifetime=lifetime, issued_at=issued_at)
        self._store.save(record)
        return credential

    def rotate(
        self,
        previous_token_id: str,
        session: TenantSession,
        *,
        lifetime: timedelta,
        overlap: timedelta,
        now: datetime | None = None,
    ) -> RotatedCredential:
        rotated_at = now or datetime.now(UTC)
        if overlap <= timedelta(0):
            raise ValueError("Token rotation overlap must be positive")
        credential, replacement = self._build_record(
            session,
            lifetime=lifetime,
            issued_at=rotated_at,
        )
        previous_valid_until = rotated_at + overlap
        if not self._store.rotate(
            previous_token_id,
            replacement,
            previous_valid_until=previous_valid_until,
            rotated_at=rotated_at,
        ):
            raise AuthenticationError("Active Access Token not found for rotation")
        return RotatedCredential(
            credential=credential,
            previous_token_id=previous_token_id,
            previous_valid_until=previous_valid_until,
        )

    def _build_record(
        self,
        session: TenantSession,
        *,
        lifetime: timedelta,
        issued_at: datetime,
    ) -> tuple[IssuedCredential, TokenRecord]:
        token_id = str(uuid4())
        secret = secrets.token_urlsafe(32)
        access_token = f"{self._PREFIX}.{token_id}.{secret}"
        expires_at = issued_at + lifetime
        record = TokenRecord(
            token_id=token_id,
            verifier=_verifier(secret),
            session=TenantSession(
                tenant_id=session.tenant_id,
                actor_id=session.actor_id,
                actor_kind=session.actor_kind,
                roles=session.roles,
                subject_user_id=session.subject_user_id,
                delegation_id=session.delegation_id,
                token_id=token_id,
            ),
            expires_at=expires_at,
            issued_at=issued_at,
        )
        return IssuedCredential(token_id, access_token, expires_at), record

    def authenticate(self, access_token: str, *, now: datetime | None = None) -> TenantSession:
        try:
            prefix, token_id, secret = access_token.split(".", maxsplit=2)
        except ValueError as exc:
            raise AuthenticationError("Invalid Access Token") from exc
        if prefix != self._PREFIX or not secret:
            raise AuthenticationError("Invalid Access Token")
        record = self._store.get(token_id)
        if record is None or not hmac.compare_digest(record.verifier, _verifier(secret)):
            raise AuthenticationError("Invalid Access Token")
        checked_at = now or datetime.now(UTC)
        if record.revoked_at is not None or checked_at >= record.expires_at:
            raise AuthenticationError("Expired or revoked Access Token")
        self._store.mark_used(record.token_id, checked_at)
        return record.session

    def revoke(self, token_id: str, *, now: datetime | None = None) -> bool:
        return self._store.revoke(token_id, now or datetime.now(UTC))


def _verifier(secret: str) -> bytes:
    # The secret has 256 bits of generated entropy; a fast digest is appropriate for
    # verification and avoids storing any usable credential in the Control Store.
    return hashlib.sha256(secret.encode("utf-8")).digest()
