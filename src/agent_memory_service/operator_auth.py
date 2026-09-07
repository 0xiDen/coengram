"""Operator Access Token and Admin Session authentication."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from agent_memory_service.auth import AuthenticationError, IssuedCredential, RotatedCredential


@dataclass(frozen=True, slots=True)
class OperatorSession:
    """Server-derived authorization context for one Operator."""

    operator_id: str
    roles: frozenset[str]
    token_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class OperatorTokenRecord:
    token_id: str
    verifier: bytes
    session: OperatorSession
    expires_at: datetime
    issued_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class IssuedAdminSession:
    session_id: str
    session_token: str
    csrf_token: str
    session: OperatorSession
    absolute_expires_at: datetime
    idle_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AdminSessionRecord:
    session_id: str
    verifier: bytes
    csrf_verifier: bytes
    session: OperatorSession
    absolute_expires_at: datetime
    idle_expires_at: datetime
    issued_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class OperatorTokenStore(Protocol):
    def save_operator_token(self, record: OperatorTokenRecord) -> None: ...

    def get_operator_token(self, token_id: str) -> OperatorTokenRecord | None: ...

    def revoke_operator_token(self, token_id: str, revoked_at: datetime) -> bool: ...

    def mark_operator_token_used(self, token_id: str, used_at: datetime) -> None: ...

    def rotate_operator_token(
        self,
        previous_token_id: str,
        replacement: OperatorTokenRecord,
        *,
        previous_valid_until: datetime,
        rotated_at: datetime,
    ) -> bool: ...


class AdminSessionStore(Protocol):
    def save_admin_session(self, record: AdminSessionRecord) -> None: ...

    def get_admin_session(self, session_id: str) -> AdminSessionRecord | None: ...

    def revoke_admin_session(self, session_id: str, revoked_at: datetime) -> bool: ...

    def mark_admin_session_used(
        self,
        session_id: str,
        used_at: datetime,
        idle_expires_at: datetime,
    ) -> None: ...


class OperatorTokenService:
    """Create high-entropy Operator tokens and authenticate Operator Sessions."""

    _PREFIX = "op1"

    def __init__(self, store: OperatorTokenStore) -> None:
        self._store = store

    def issue(
        self,
        session: OperatorSession,
        *,
        lifetime: timedelta,
        now: datetime | None = None,
    ) -> IssuedCredential:
        issued_at = now or datetime.now(UTC)
        credential, record = self._build_record(session, lifetime=lifetime, issued_at=issued_at)
        self._store.save_operator_token(record)
        return credential

    def rotate(
        self,
        previous_token_id: str,
        session: OperatorSession,
        *,
        lifetime: timedelta,
        overlap: timedelta,
        now: datetime | None = None,
    ) -> RotatedCredential:
        rotated_at = now or datetime.now(UTC)
        if overlap <= timedelta(0):
            raise ValueError("Operator Access Token rotation overlap must be positive")
        credential, replacement = self._build_record(
            session,
            lifetime=lifetime,
            issued_at=rotated_at,
        )
        previous_valid_until = rotated_at + overlap
        if not self._store.rotate_operator_token(
            previous_token_id,
            replacement,
            previous_valid_until=previous_valid_until,
            rotated_at=rotated_at,
        ):
            raise AuthenticationError("Active Operator Access Token not found for rotation")
        return RotatedCredential(
            credential=credential,
            previous_token_id=previous_token_id,
            previous_valid_until=previous_valid_until,
        )

    def authenticate(self, access_token: str, *, now: datetime | None = None) -> OperatorSession:
        try:
            prefix, token_id, secret = access_token.split(".", maxsplit=2)
        except ValueError as exc:
            raise AuthenticationError("Invalid Operator Access Token") from exc
        if prefix != self._PREFIX or not secret:
            raise AuthenticationError("Invalid Operator Access Token")
        record = self._store.get_operator_token(token_id)
        if record is None or not hmac.compare_digest(record.verifier, _verifier(secret)):
            raise AuthenticationError("Invalid Operator Access Token")
        checked_at = now or datetime.now(UTC)
        if record.revoked_at is not None or checked_at >= record.expires_at:
            raise AuthenticationError("Expired or revoked Operator Access Token")
        self._store.mark_operator_token_used(record.token_id, checked_at)
        return record.session

    def revoke(self, token_id: str, *, now: datetime | None = None) -> bool:
        return self._store.revoke_operator_token(token_id, now or datetime.now(UTC))

    def _build_record(
        self,
        session: OperatorSession,
        *,
        lifetime: timedelta,
        issued_at: datetime,
    ) -> tuple[IssuedCredential, OperatorTokenRecord]:
        token_id = str(uuid4())
        secret = secrets.token_urlsafe(32)
        access_token = f"{self._PREFIX}.{token_id}.{secret}"
        expires_at = issued_at + lifetime
        record = OperatorTokenRecord(
            token_id=token_id,
            verifier=_verifier(secret),
            session=OperatorSession(
                operator_id=session.operator_id,
                roles=session.roles,
                token_id=token_id,
            ),
            expires_at=expires_at,
            issued_at=issued_at,
        )
        return IssuedCredential(token_id, access_token, expires_at), record


class AdminSessionService:
    """Create and authenticate browser Admin Sessions."""

    _PREFIX = "adm1"
    _IDLE_LIFETIME = timedelta(minutes=30)
    _ABSOLUTE_LIFETIME = timedelta(hours=8)

    def __init__(self, store: AdminSessionStore) -> None:
        self._store = store

    def create(
        self,
        session: OperatorSession,
        *,
        now: datetime | None = None,
    ) -> IssuedAdminSession:
        issued_at = now or datetime.now(UTC)
        session_id = str(uuid4())
        secret = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        session_token = f"{self._PREFIX}.{session_id}.{secret}"
        absolute_expires_at = issued_at + self._ABSOLUTE_LIFETIME
        idle_expires_at = issued_at + self._IDLE_LIFETIME
        self._store.save_admin_session(
            AdminSessionRecord(
                session_id=session_id,
                verifier=_verifier(secret),
                csrf_verifier=_verifier(csrf_token),
                session=OperatorSession(
                    operator_id=session.operator_id,
                    roles=session.roles,
                    token_id=session.token_id,
                    session_id=session_id,
                ),
                absolute_expires_at=absolute_expires_at,
                idle_expires_at=idle_expires_at,
                issued_at=issued_at,
            )
        )
        return IssuedAdminSession(
            session_id=session_id,
            session_token=session_token,
            csrf_token=csrf_token,
            session=OperatorSession(
                operator_id=session.operator_id,
                roles=session.roles,
                token_id=session.token_id,
                session_id=session_id,
            ),
            absolute_expires_at=absolute_expires_at,
            idle_expires_at=idle_expires_at,
        )

    def authenticate(
        self,
        session_token: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
        now: datetime | None = None,
    ) -> OperatorSession:
        try:
            prefix, session_id, secret = session_token.split(".", maxsplit=2)
        except ValueError as exc:
            raise AuthenticationError("Invalid Admin Session") from exc
        if prefix != self._PREFIX or not secret:
            raise AuthenticationError("Invalid Admin Session")
        record = self._store.get_admin_session(session_id)
        if record is None or not hmac.compare_digest(record.verifier, _verifier(secret)):
            raise AuthenticationError("Invalid Admin Session")
        if require_csrf:
            if csrf_token is None or not hmac.compare_digest(
                record.csrf_verifier,
                _verifier(csrf_token),
            ):
                raise AuthenticationError("Invalid Admin Session CSRF token")
        checked_at = now or datetime.now(UTC)
        if (
            record.revoked_at is not None
            or checked_at >= record.absolute_expires_at
            or checked_at >= record.idle_expires_at
        ):
            raise AuthenticationError("Admin Session expired or revoked")
        next_idle_expiry = min(
            checked_at + self._IDLE_LIFETIME,
            record.absolute_expires_at,
        )
        self._store.mark_admin_session_used(record.session_id, checked_at, next_idle_expiry)
        return record.session

    def revoke(self, session_id: str, *, now: datetime | None = None) -> bool:
        return self._store.revoke_admin_session(session_id, now or datetime.now(UTC))


def _verifier(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()
