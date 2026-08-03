"""PostgreSQL Adapter for the content-free Control Store."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from agent_memory_service.auth import TokenRecord
from agent_memory_service.control import (
    ChannelBindingRecord,
    ControlConflict,
    ControlNotFound,
    DelegationRecord,
    MembershipRecord,
    PrincipalRecord,
    TenantRecord,
    TenantRouteRecord,
)
from agent_memory_service.database_url import (
    psycopg_database_url as _psycopg_database_url,
)
from agent_memory_service.models import PrincipalKind, TenantSession


class PostgresControlStore:
    """Persist ControlStore and TokenStore records in PostgreSQL.

    A connection is opened for each public operation. Psycopg's connection context
    commits successful writes and rolls back exceptions, so callers never observe a
    partially persisted record.
    """

    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("Control Store database URL cannot be empty")
        self._database_url = _psycopg_database_url(database_url)

    def add_tenant(self, record: TenantRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.tenants (tenant_id, name, active)
            VALUES (%s, %s, %s)
            """,
            (record.tenant_id, record.name, record.active),
            conflict_message="Tenant already exists",
        )

    def get_tenant(self, tenant_id: str) -> TenantRecord | None:
        row = self._fetch_one(
            """
            SELECT tenant_id, name, active
            FROM control.tenants
            WHERE tenant_id = %s
            """,
            (tenant_id,),
        )
        if row is None:
            return None
        return TenantRecord(tenant_id=str(row[0]), name=str(row[1]), active=bool(row[2]))

    def add_principal(self, record: PrincipalRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.principals (principal_id, name, kind, active)
            VALUES (%s, %s, %s, %s)
            """,
            (record.principal_id, record.name, record.kind.value, record.active),
            conflict_message="Principal already exists",
        )

    def get_principal(self, principal_id: str) -> PrincipalRecord | None:
        row = self._fetch_one(
            """
            SELECT principal_id, name, kind, active
            FROM control.principals
            WHERE principal_id = %s
            """,
            (principal_id,),
        )
        if row is None:
            return None
        return PrincipalRecord(
            principal_id=str(row[0]),
            name=str(row[1]),
            kind=PrincipalKind(str(row[2])),
            active=bool(row[3]),
        )

    def list_principals(self) -> tuple[PrincipalRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT principal_id, name, kind, active
            FROM control.principals
            ORDER BY principal_id
            """,
            (),
        )
        return tuple(_decode_principal(row) for row in rows)

    def update_principal(
        self, principal_id: str, *, name: str, active: bool, changed_at: datetime
    ) -> PrincipalRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.principals
                    SET name = %s, active = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE principal_id = %s
                    RETURNING principal_id, name, kind, active
                    """,
                    (name, active, principal_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ControlNotFound("Principal not found")
                if not active:
                    cursor.execute(
                        """
                        UPDATE control.memberships
                        SET active = false, updated_at = CURRENT_TIMESTAMP
                        WHERE principal_id = %s
                        """,
                        (principal_id,),
                    )
                    cursor.execute(
                        """
                        UPDATE control.delegations
                        SET active = false, updated_at = CURRENT_TIMESTAMP
                        WHERE agent_id = %s OR subject_user_id = %s
                        """,
                        (principal_id, principal_id),
                    )
                    cursor.execute(
                        """
                        UPDATE control.channel_bindings
                        SET active = false, updated_at = CURRENT_TIMESTAMP
                        WHERE agent_id = %s OR user_id = %s
                        """,
                        (principal_id, principal_id),
                    )
                    cursor.execute(
                        """
                        UPDATE control.access_tokens
                        SET revoked_at = COALESCE(revoked_at, %s)
                        WHERE principal_id = %s OR subject_user_id = %s
                        """,
                        (changed_at, principal_id, principal_id),
                    )
        return _decode_principal(row)

    def disable_principal(self, principal_id: str, disabled_at: datetime) -> PrincipalRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.principals
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE principal_id = %s
                    RETURNING principal_id, name, kind, active
                    """,
                    (principal_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ControlNotFound("Principal not found")
                cursor.execute(
                    """
                    UPDATE control.memberships
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE principal_id = %s
                    """,
                    (principal_id,),
                )
                cursor.execute(
                    """
                    UPDATE control.delegations
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE agent_id = %s OR subject_user_id = %s
                    """,
                    (principal_id, principal_id),
                )
                cursor.execute(
                    """
                    UPDATE control.channel_bindings
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE agent_id = %s OR user_id = %s
                    """,
                    (principal_id, principal_id),
                )
                cursor.execute(
                    """
                    UPDATE control.access_tokens
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE principal_id = %s OR subject_user_id = %s
                    """,
                    (disabled_at, principal_id, principal_id),
                )
        return PrincipalRecord(
            principal_id=str(row[0]),
            name=str(row[1]),
            kind=PrincipalKind(str(row[2])),
            active=bool(row[3]),
        )

    def save_membership(self, record: MembershipRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.memberships (tenant_id, principal_id, roles, active)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, principal_id) DO UPDATE
            SET roles = EXCLUDED.roles,
                active = EXCLUDED.active,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                record.tenant_id,
                record.principal_id,
                Jsonb(sorted(record.roles)),
                record.active,
            ),
            conflict_message="Tenant Membership could not be saved",
        )

    def get_membership(self, tenant_id: str, principal_id: str) -> MembershipRecord | None:
        row = self._fetch_one(
            """
            SELECT tenant_id, principal_id, roles, active
            FROM control.memberships
            WHERE tenant_id = %s AND principal_id = %s
            """,
            (tenant_id, principal_id),
        )
        if row is None:
            return None
        return MembershipRecord(
            tenant_id=str(row[0]),
            principal_id=str(row[1]),
            roles=_decode_roles(row[2]),
            active=bool(row[3]),
        )

    def list_memberships(self, tenant_id: str) -> tuple[MembershipRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT tenant_id, principal_id, roles, active
            FROM control.memberships
            WHERE tenant_id = %s
            ORDER BY principal_id
            """,
            (tenant_id,),
        )
        return tuple(_decode_membership(row) for row in rows)

    def update_membership(self, record: MembershipRecord, changed_at: datetime) -> MembershipRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT tenant_id, principal_id, roles, active
                    FROM control.memberships
                    WHERE tenant_id = %s AND principal_id = %s
                    FOR UPDATE
                    """,
                    (record.tenant_id, record.principal_id),
                )
                current = cursor.fetchone()
                if current is None:
                    raise ControlNotFound("Tenant Membership not found")
                if not record.active:
                    _disable_membership(
                        cursor,
                        record.tenant_id,
                        record.principal_id,
                        changed_at,
                    )
                cursor.execute(
                    """
                    UPDATE control.memberships
                    SET roles = %s, active = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND principal_id = %s
                    RETURNING tenant_id, principal_id, roles, active
                    """,
                    (
                        Jsonb(sorted(record.roles)),
                        record.active,
                        record.tenant_id,
                        record.principal_id,
                    ),
                )
                updated = cursor.fetchone()
                assert updated is not None
                if _decode_roles(current[2]) != record.roles or bool(current[3]) != record.active:
                    cursor.execute(
                        """
                        UPDATE control.access_tokens
                        SET revoked_at = COALESCE(revoked_at, %s)
                        WHERE tenant_id = %s AND principal_id = %s
                        """,
                        (changed_at, record.tenant_id, record.principal_id),
                    )
        return _decode_membership(updated)

    def disable_membership(
        self, tenant_id: str, principal_id: str, disabled_at: datetime
    ) -> MembershipRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                return _disable_membership(cursor, tenant_id, principal_id, disabled_at)

    def revoke_membership_role(
        self, tenant_id: str, principal_id: str, role: str, revoked_at: datetime
    ) -> MembershipRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT tenant_id, principal_id, roles, active
                    FROM control.memberships
                    WHERE tenant_id = %s AND principal_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, principal_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ControlNotFound("Tenant Membership not found")
                roles = _decode_roles(row[2])
                if role not in roles:
                    raise ControlNotFound("Tenant Membership role not found")
                remaining = roles - {role}
                if not remaining:
                    return _disable_membership(cursor, tenant_id, principal_id, revoked_at)
                cursor.execute(
                    """
                    UPDATE control.memberships
                    SET roles = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND principal_id = %s
                    RETURNING tenant_id, principal_id, roles, active
                    """,
                    (Jsonb(sorted(remaining)), tenant_id, principal_id),
                )
                updated = cursor.fetchone()
                assert updated is not None
                cursor.execute(
                    """
                    UPDATE control.access_tokens
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE tenant_id = %s AND principal_id = %s
                    """,
                    (revoked_at, tenant_id, principal_id),
                )
                return _decode_membership(updated)

    def list_active_human_member_ids(
        self,
        tenant_id: str,
    ) -> tuple[str, ...]:
        rows = self._fetch_all(
            """
            SELECT m.principal_id
            FROM control.memberships AS m
            JOIN control.principals AS p ON p.principal_id = m.principal_id
            JOIN control.tenants AS t ON t.tenant_id = m.tenant_id
            WHERE m.tenant_id = %s
              AND m.active
              AND p.active
              AND t.active
              AND p.kind = 'user'
            ORDER BY m.principal_id
            """,
            (tenant_id,),
        )
        return tuple(str(row[0]) for row in rows)

    def save_delegation(self, record: DelegationRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.delegations (
                delegation_id,
                tenant_id,
                agent_id,
                subject_user_id,
                active
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                record.delegation_id,
                record.tenant_id,
                record.agent_id,
                record.subject_user_id,
                record.active,
            ),
            conflict_message="Delegation already exists",
        )

    def get_delegation(self, delegation_id: str) -> DelegationRecord | None:
        row = self._fetch_one(
            """
            SELECT delegation_id, tenant_id, agent_id, subject_user_id, active
            FROM control.delegations
            WHERE delegation_id = %s
            """,
            (delegation_id,),
        )
        if row is None:
            return None
        return DelegationRecord(
            delegation_id=str(row[0]),
            tenant_id=str(row[1]),
            agent_id=str(row[2]),
            subject_user_id=str(row[3]),
            active=bool(row[4]),
        )

    def list_delegations(self, tenant_id: str) -> tuple[DelegationRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT delegation_id, tenant_id, agent_id, subject_user_id, active
            FROM control.delegations
            WHERE tenant_id = %s
            ORDER BY delegation_id
            """,
            (tenant_id,),
        )
        return tuple(_decode_delegation(row) for row in rows)

    def update_delegation_active(
        self, delegation_id: str, *, active: bool, changed_at: datetime
    ) -> DelegationRecord:
        if not active:
            return self.revoke_delegation(delegation_id, changed_at)
        row = self._fetch_one(
            """
            UPDATE control.delegations
            SET active = true, updated_at = CURRENT_TIMESTAMP
            WHERE delegation_id = %s
            RETURNING delegation_id, tenant_id, agent_id, subject_user_id, active
            """,
            (delegation_id,),
        )
        if row is None:
            raise ControlNotFound("Delegation not found")
        return _decode_delegation(row)

    def revoke_delegation(self, delegation_id: str, revoked_at: datetime) -> DelegationRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.delegations
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE delegation_id = %s
                    RETURNING delegation_id, tenant_id, agent_id, subject_user_id, active
                    """,
                    (delegation_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ControlNotFound("Delegation not found")
                cursor.execute(
                    """
                    UPDATE control.channel_bindings
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE delegation_id = %s
                    """,
                    (delegation_id,),
                )
                cursor.execute(
                    """
                    UPDATE control.access_tokens
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE delegation_id = %s
                    """,
                    (revoked_at, delegation_id),
                )
        return DelegationRecord(
            delegation_id=str(row[0]),
            tenant_id=str(row[1]),
            agent_id=str(row[2]),
            subject_user_id=str(row[3]),
            active=bool(row[4]),
        )

    def save_channel_binding(self, record: ChannelBindingRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.channel_bindings (
                binding_id,
                channel,
                external_id,
                tenant_id,
                user_id,
                agent_id,
                delegation_id,
                active
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.binding_id,
                record.channel,
                record.external_id,
                record.tenant_id,
                record.user_id,
                record.agent_id,
                record.delegation_id,
                record.active,
            ),
            conflict_message="Channel Binding already exists",
        )

    def get_channel_binding(
        self,
        channel: str,
        external_id: str,
    ) -> ChannelBindingRecord | None:
        row = self._fetch_one(
            """
            SELECT
                binding_id,
                channel,
                external_id,
                tenant_id,
                user_id,
                agent_id,
                delegation_id,
                active
            FROM control.channel_bindings
            WHERE channel = %s AND external_id = %s
            """,
            (channel, external_id),
        )
        if row is None:
            return None
        return ChannelBindingRecord(
            binding_id=str(row[0]),
            channel=str(row[1]),
            external_id=str(row[2]),
            tenant_id=str(row[3]),
            user_id=str(row[4]),
            agent_id=str(row[5]),
            delegation_id=str(row[6]),
            active=bool(row[7]),
        )

    def update_channel_binding(self, record: ChannelBindingRecord) -> None:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE control.channel_bindings
                        SET tenant_id = %s,
                            user_id = %s,
                            agent_id = %s,
                            delegation_id = %s,
                            active = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE binding_id = %s
                          AND channel = %s
                          AND external_id = %s
                        RETURNING binding_id
                        """,
                        (
                            record.tenant_id,
                            record.user_id,
                            record.agent_id,
                            record.delegation_id,
                            record.active,
                            record.binding_id,
                            record.channel,
                            record.external_id,
                        ),
                    )
                    if cursor.fetchone() is None:
                        raise ControlNotFound("Channel Binding not found")
        except psycopg.IntegrityError as exc:
            raise ControlConflict("Channel Binding could not be updated") from exc

    def delete_channel_binding(self, channel: str, external_id: str) -> bool:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM control.channel_bindings
                    WHERE channel = %s AND external_id = %s
                    RETURNING binding_id
                    """,
                    (channel, external_id),
                )
                return cursor.fetchone() is not None

    def save_tenant_route(self, record: TenantRouteRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.routing (
                tenant_id,
                neo4j_service_address,
                neo4j_secret_name,
                tenant_database_name,
                tenant_database_role,
                healthy,
                checked_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (tenant_id) DO UPDATE
            SET neo4j_service_address = EXCLUDED.neo4j_service_address,
                neo4j_secret_name = EXCLUDED.neo4j_secret_name,
                tenant_database_name = EXCLUDED.tenant_database_name,
                tenant_database_role = EXCLUDED.tenant_database_role,
                healthy = EXCLUDED.healthy,
                checked_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                record.tenant_id,
                record.neo4j_service_address,
                record.neo4j_secret_name,
                record.tenant_database_name,
                record.tenant_database_role,
                record.healthy,
            ),
            conflict_message="Tenant route could not be saved",
        )

    def get_tenant_route(self, tenant_id: str) -> TenantRouteRecord | None:
        row = self._fetch_one(
            """
            SELECT tenant_id, neo4j_service_address, neo4j_secret_name,
                   tenant_database_name, tenant_database_role, healthy
            FROM control.routing
            WHERE tenant_id = %s
            """,
            (tenant_id,),
        )
        return None if row is None else _decode_route(row)

    def list_tenant_routes(self) -> tuple[TenantRouteRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT tenant_id, neo4j_service_address, neo4j_secret_name,
                   tenant_database_name, tenant_database_role, healthy
            FROM control.routing
            ORDER BY tenant_id
            """,
            (),
        )
        return tuple(_decode_route(row) for row in rows)

    def token_lifetime_days(
        self,
        tenant_id: str,
        actor_kind: PrincipalKind,
        *,
        delegated: bool,
    ) -> int | None:
        if actor_kind is PrincipalKind.USER:
            policy_name = "user_token_lifetime_days"
        elif delegated:
            policy_name = "delegated_agent_token_lifetime_days"
        else:
            policy_name = "autonomous_agent_token_lifetime_days"
        row = self._fetch_one(
            """
            SELECT policies ->> %s
            FROM control.provisioning
            WHERE tenant_id = %s AND state = 'active'
            """,
            (policy_name, tenant_id),
        )
        if row is None or row[0] is None:
            return None
        days = int(str(row[0]))
        if days < 1:
            raise ValueError("Control Store contains an invalid token lifetime policy")
        return days

    def save(self, record: TokenRecord) -> None:
        """Save only the token's non-reversible verifier and fixed session context."""
        self._write_once(
            """
            INSERT INTO control.access_tokens (
                token_id,
                tenant_id,
                principal_id,
                actor_kind,
                roles,
                verifier,
                subject_user_id,
                delegation_id,
                expires_at,
                revoked_at,
                created_at,
                last_used_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.token_id,
                record.session.tenant_id,
                record.session.actor_id,
                record.session.actor_kind.value,
                Jsonb(sorted(record.session.roles)),
                record.verifier,
                record.session.subject_user_id,
                record.session.delegation_id,
                record.expires_at,
                record.revoked_at,
                record.issued_at,
                record.last_used_at,
            ),
            conflict_message="Access Token could not be saved",
        )

    def get(self, token_id: str) -> TokenRecord | None:
        row = self._fetch_one(
            """
            SELECT
                a.token_id,
                a.verifier,
                a.tenant_id,
                a.principal_id,
                a.actor_kind,
                a.roles,
                a.subject_user_id,
                a.delegation_id,
                a.expires_at,
                a.revoked_at,
                a.last_used_at,
                a.created_at
            FROM control.access_tokens AS a
            JOIN control.tenants AS t ON t.tenant_id = a.tenant_id AND t.active
            JOIN control.principals AS p
              ON p.principal_id = a.principal_id AND p.active
            JOIN control.memberships AS m
              ON m.tenant_id = a.tenant_id
             AND m.principal_id = a.principal_id
             AND m.active
            LEFT JOIN control.delegations AS d
              ON d.delegation_id = a.delegation_id
            WHERE a.token_id = %s
              AND (a.delegation_id IS NULL OR d.active)
            """,
            (token_id,),
        )
        return None if row is None else _decode_token(row)

    def revoke(self, token_id: str, revoked_at: datetime) -> bool:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.access_tokens
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE token_id = %s
                    RETURNING token_id
                    """,
                    (revoked_at, token_id),
                )
                return cursor.fetchone() is not None

    def mark_used(self, token_id: str, used_at: datetime) -> None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.access_tokens
                    SET last_used_at = GREATEST(COALESCE(last_used_at, %s), %s)
                    WHERE token_id = %s
                    """,
                    (used_at, used_at, token_id),
                )

    def rotate(
        self,
        previous_token_id: str,
        replacement: TokenRecord,
        *,
        previous_valid_until: datetime,
        rotated_at: datetime,
    ) -> bool:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT tenant_id, principal_id, subject_user_id, delegation_id
                        FROM control.access_tokens
                        WHERE token_id = %s AND revoked_at IS NULL AND expires_at > %s
                        FOR UPDATE
                        """,
                        (previous_token_id, rotated_at),
                    )
                    previous = cursor.fetchone()
                    expected = (
                        replacement.session.tenant_id,
                        replacement.session.actor_id,
                        replacement.session.subject_user_id,
                        replacement.session.delegation_id,
                    )
                    if previous is None or previous != expected:
                        return False
                    cursor.execute(
                        """
                        SELECT 1
                        FROM control.tenants AS t
                        JOIN control.principals AS p ON p.principal_id = %s
                        JOIN control.memberships AS m
                          ON m.tenant_id = t.tenant_id AND m.principal_id = p.principal_id
                        WHERE t.tenant_id = %s AND t.active AND p.active AND m.active
                        FOR KEY SHARE OF t, p, m
                        """,
                        (replacement.session.actor_id, replacement.session.tenant_id),
                    )
                    if cursor.fetchone() is None:
                        return False
                    if replacement.session.delegation_id is not None:
                        cursor.execute(
                            """
                            SELECT 1
                            FROM control.delegations AS d
                            JOIN control.principals AS subject
                              ON subject.principal_id = d.subject_user_id
                            JOIN control.memberships AS membership
                              ON membership.tenant_id = d.tenant_id
                             AND membership.principal_id = d.subject_user_id
                            WHERE d.delegation_id = %s
                              AND d.tenant_id = %s
                              AND d.agent_id = %s
                              AND d.subject_user_id = %s
                              AND d.active AND subject.active AND membership.active
                            FOR KEY SHARE OF d, subject, membership
                            """,
                            (
                                replacement.session.delegation_id,
                                replacement.session.tenant_id,
                                replacement.session.actor_id,
                                replacement.session.subject_user_id,
                            ),
                        )
                        if cursor.fetchone() is None:
                            return False
                    cursor.execute(
                        """
                        UPDATE control.access_tokens
                        SET expires_at = LEAST(expires_at, %s)
                        WHERE token_id = %s
                        """,
                        (previous_valid_until, previous_token_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO control.access_tokens (
                            token_id, tenant_id, principal_id, actor_kind, roles,
                            verifier, subject_user_id, delegation_id, expires_at,
                            revoked_at, created_at, last_used_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            replacement.token_id,
                            replacement.session.tenant_id,
                            replacement.session.actor_id,
                            replacement.session.actor_kind.value,
                            Jsonb(sorted(replacement.session.roles)),
                            replacement.verifier,
                            replacement.session.subject_user_id,
                            replacement.session.delegation_id,
                            replacement.expires_at,
                            replacement.revoked_at,
                            replacement.issued_at,
                            replacement.last_used_at,
                        ),
                    )
                    return True
        except psycopg.IntegrityError as exc:
            raise ControlConflict("Access Token rotation conflicted") from exc

    def list_token_records(self, tenant_id: str, principal_id: str) -> tuple[TokenRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT
                token_id,
                verifier,
                tenant_id,
                principal_id,
                actor_kind,
                roles,
                subject_user_id,
                delegation_id,
                expires_at,
                revoked_at,
                last_used_at,
                created_at
            FROM control.access_tokens
            WHERE tenant_id = %s AND principal_id = %s
            ORDER BY created_at, token_id
            """,
            (tenant_id, principal_id),
        )
        return tuple(_decode_token(row) for row in rows)

    def token_usage_signals(
        self,
        *,
        checked_at: datetime,
        inactive_before: datetime,
    ) -> tuple[int, int]:
        """Return aggregate active-token signals without identifiers or token material."""

        row = self._fetch_one(
            """
            SELECT
                count(*) FILTER (WHERE last_used_at IS NULL),
                count(*) FILTER (
                    WHERE COALESCE(last_used_at, created_at) < %s
                )
            FROM control.access_tokens
            WHERE revoked_at IS NULL AND expires_at > %s
            """,
            (inactive_before, checked_at),
        )
        if row is None:
            return 0, 0
        return int(row[0]), int(row[1])

    def _write_once(
        self,
        statement: str,
        parameters: Sequence[object],
        *,
        conflict_message: str,
    ) -> None:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement, parameters)
        except psycopg.IntegrityError as exc:
            raise ControlConflict(conflict_message) from exc

    def _fetch_one(
        self,
        statement: str,
        parameters: Sequence[object],
    ) -> tuple[Any, ...] | None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return cursor.fetchone()

    def _fetch_all(
        self,
        statement: str,
        parameters: Sequence[object],
    ) -> list[tuple[Any, ...]]:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return cursor.fetchall()


def _disable_membership(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    tenant_id: str,
    principal_id: str,
    disabled_at: datetime,
) -> MembershipRecord:
    cursor.execute(
        """
        UPDATE control.memberships
        SET active = false, updated_at = CURRENT_TIMESTAMP
        WHERE tenant_id = %s AND principal_id = %s
        RETURNING tenant_id, principal_id, roles, active
        """,
        (tenant_id, principal_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise ControlNotFound("Tenant Membership not found")
    cursor.execute(
        """
        UPDATE control.delegations
        SET active = false, updated_at = CURRENT_TIMESTAMP
        WHERE tenant_id = %s AND (agent_id = %s OR subject_user_id = %s)
        """,
        (tenant_id, principal_id, principal_id),
    )
    cursor.execute(
        """
        UPDATE control.channel_bindings
        SET active = false, updated_at = CURRENT_TIMESTAMP
        WHERE tenant_id = %s AND (agent_id = %s OR user_id = %s)
        """,
        (tenant_id, principal_id, principal_id),
    )
    cursor.execute(
        """
        UPDATE control.access_tokens
        SET revoked_at = COALESCE(revoked_at, %s)
        WHERE tenant_id = %s AND (principal_id = %s OR subject_user_id = %s)
        """,
        (disabled_at, tenant_id, principal_id, principal_id),
    )
    return _decode_membership(row)


def _decode_membership(row: tuple[Any, ...]) -> MembershipRecord:
    return MembershipRecord(
        tenant_id=str(row[0]),
        principal_id=str(row[1]),
        roles=_decode_roles(row[2]),
        active=bool(row[3]),
    )


def _decode_principal(row: tuple[Any, ...]) -> PrincipalRecord:
    return PrincipalRecord(
        principal_id=str(row[0]),
        name=str(row[1]),
        kind=PrincipalKind(str(row[2])),
        active=bool(row[3]),
    )


def _decode_delegation(row: tuple[Any, ...]) -> DelegationRecord:
    return DelegationRecord(
        delegation_id=str(row[0]),
        tenant_id=str(row[1]),
        agent_id=str(row[2]),
        subject_user_id=str(row[3]),
        active=bool(row[4]),
    )


def _decode_roles(value: object) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(role, str) for role in value):
        raise ValueError("Control Store contains invalid role data")
    return frozenset(value)


def _decode_token(row: tuple[Any, ...]) -> TokenRecord:
    verifier = row[1]
    if not isinstance(verifier, bytes | bytearray | memoryview):
        raise ValueError("Control Store contains an invalid Access Token verifier")
    expires_at = row[8]
    revoked_at = row[9]
    last_used_at = row[10]
    issued_at = row[11]
    if not isinstance(expires_at, datetime):
        raise ValueError("Control Store contains an invalid Access Token expiry")
    if revoked_at is not None and not isinstance(revoked_at, datetime):
        raise ValueError("Control Store contains an invalid Access Token revocation time")
    if last_used_at is not None and not isinstance(last_used_at, datetime):
        raise ValueError("Control Store contains an invalid Access Token use time")
    if not isinstance(issued_at, datetime):
        raise ValueError("Control Store contains an invalid Access Token issue time")
    return TokenRecord(
        token_id=str(row[0]),
        verifier=bytes(verifier),
        session=TenantSession(
            tenant_id=str(row[2]),
            actor_id=str(row[3]),
            actor_kind=PrincipalKind(str(row[4])),
            roles=_decode_roles(row[5]),
            subject_user_id=None if row[6] is None else str(row[6]),
            delegation_id=None if row[7] is None else str(row[7]),
            token_id=str(row[0]),
        ),
        expires_at=expires_at,
        issued_at=issued_at,
        revoked_at=revoked_at,
        last_used_at=last_used_at,
    )


def _decode_route(row: tuple[Any, ...]) -> TenantRouteRecord:
    return TenantRouteRecord(
        tenant_id=str(row[0]),
        neo4j_service_address=str(row[1]),
        neo4j_secret_name=str(row[2]),
        tenant_database_name=str(row[3]),
        tenant_database_role=str(row[4]),
        healthy=bool(row[5]),
    )
