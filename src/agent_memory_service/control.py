"""Control Store domain model and operator-facing control Module."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from agent_memory_service.auth import (
    IssuedCredential,
    RotatedCredential,
    TokenRecord,
    TokenService,
)
from agent_memory_service.models import PrincipalKind, TenantSession

VALID_ROLES = frozenset({"tenant_administrator", "knowledge_curator", "tenant_member"})
UNUSED_TOKEN_AGE = timedelta(days=30)


class ControlConflict(ValueError):
    pass


class ControlNotFound(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TenantRecord:
    tenant_id: str
    name: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class PrincipalRecord:
    principal_id: str
    name: str
    kind: PrincipalKind
    active: bool = True


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    tenant_id: str
    principal_id: str
    roles: frozenset[str]
    active: bool = True


@dataclass(frozen=True, slots=True)
class DelegationRecord:
    delegation_id: str
    tenant_id: str
    agent_id: str
    subject_user_id: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class ChannelBindingRecord:
    binding_id: str
    channel: str
    external_id: str
    tenant_id: str
    user_id: str
    agent_id: str
    delegation_id: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class TenantRouteRecord:
    tenant_id: str
    neo4j_service_address: str
    neo4j_secret_name: str
    tenant_database_name: str
    tenant_database_role: str
    healthy: bool = False


class ControlStore(Protocol):
    def add_tenant(self, record: TenantRecord) -> None: ...

    def get_tenant(self, tenant_id: str) -> TenantRecord | None: ...

    def add_principal(self, record: PrincipalRecord) -> None: ...

    def get_principal(self, principal_id: str) -> PrincipalRecord | None: ...

    def list_principals(self) -> tuple[PrincipalRecord, ...]: ...

    def update_principal(
        self, principal_id: str, *, name: str, active: bool, changed_at: datetime
    ) -> PrincipalRecord: ...

    def disable_principal(self, principal_id: str, disabled_at: datetime) -> PrincipalRecord: ...

    def save_membership(self, record: MembershipRecord) -> None: ...

    def get_membership(self, tenant_id: str, principal_id: str) -> MembershipRecord | None: ...

    def list_memberships(self, tenant_id: str) -> tuple[MembershipRecord, ...]: ...

    def update_membership(
        self, record: MembershipRecord, changed_at: datetime
    ) -> MembershipRecord: ...

    def disable_membership(
        self, tenant_id: str, principal_id: str, disabled_at: datetime
    ) -> MembershipRecord: ...

    def revoke_membership_role(
        self, tenant_id: str, principal_id: str, role: str, revoked_at: datetime
    ) -> MembershipRecord: ...

    def list_active_human_member_ids(
        self,
        tenant_id: str,
    ) -> tuple[str, ...]: ...

    def save_delegation(self, record: DelegationRecord) -> None: ...

    def get_delegation(self, delegation_id: str) -> DelegationRecord | None: ...

    def list_delegations(self, tenant_id: str) -> tuple[DelegationRecord, ...]: ...

    def update_delegation_active(
        self, delegation_id: str, *, active: bool, changed_at: datetime
    ) -> DelegationRecord: ...

    def revoke_delegation(self, delegation_id: str, revoked_at: datetime) -> DelegationRecord: ...

    def save_channel_binding(self, record: ChannelBindingRecord) -> None: ...

    def get_channel_binding(
        self, channel: str, external_id: str
    ) -> ChannelBindingRecord | None: ...

    def update_channel_binding(self, record: ChannelBindingRecord) -> None: ...

    def delete_channel_binding(self, channel: str, external_id: str) -> bool: ...

    def save_tenant_route(self, record: TenantRouteRecord) -> None: ...

    def get_tenant_route(self, tenant_id: str) -> TenantRouteRecord | None: ...

    def list_tenant_routes(self) -> tuple[TenantRouteRecord, ...]: ...

    def token_lifetime_days(
        self,
        tenant_id: str,
        actor_kind: PrincipalKind,
        *,
        delegated: bool,
    ) -> int | None: ...

    def list_token_records(self, tenant_id: str, principal_id: str) -> tuple[TokenRecord, ...]: ...

    def get(self, token_id: str) -> TokenRecord | None: ...


class InMemoryControlStore:
    """Deterministic Control Store Adapter used by contract tests and local demos."""

    def __init__(self) -> None:
        self._tenants: dict[str, TenantRecord] = {}
        self._principals: dict[str, PrincipalRecord] = {}
        self._memberships: dict[tuple[str, str], MembershipRecord] = {}
        self._delegations: dict[str, DelegationRecord] = {}
        self._channel_bindings: dict[tuple[str, str], ChannelBindingRecord] = {}
        self._routes: dict[str, TenantRouteRecord] = {}
        self._tokens: dict[str, TokenRecord] = {}
        self._token_lifetime_days: dict[tuple[str, PrincipalKind, bool], int] = {}

    def add_tenant(self, record: TenantRecord) -> None:
        if record.tenant_id in self._tenants:
            raise ControlConflict("Tenant already exists")
        self._tenants[record.tenant_id] = record

    def get_tenant(self, tenant_id: str) -> TenantRecord | None:
        return self._tenants.get(tenant_id)

    def add_principal(self, record: PrincipalRecord) -> None:
        if record.principal_id in self._principals:
            raise ControlConflict("Principal already exists")
        self._principals[record.principal_id] = record

    def get_principal(self, principal_id: str) -> PrincipalRecord | None:
        return self._principals.get(principal_id)

    def list_principals(self) -> tuple[PrincipalRecord, ...]:
        return tuple(self._principals[key] for key in sorted(self._principals))

    def update_principal(
        self, principal_id: str, *, name: str, active: bool, changed_at: datetime
    ) -> PrincipalRecord:
        current = self._principals.get(principal_id)
        if current is None:
            raise ControlNotFound("Principal not found")
        if not active:
            current = self.disable_principal(principal_id, changed_at)
        updated = replace(current, name=name, active=active)
        self._principals[principal_id] = updated
        return updated

    def disable_principal(self, principal_id: str, disabled_at: datetime) -> PrincipalRecord:
        current = self._principals.get(principal_id)
        if current is None:
            raise ControlNotFound("Principal not found")
        disabled = replace(current, active=False)
        self._principals[principal_id] = disabled
        for key, membership in tuple(self._memberships.items()):
            if membership.principal_id == principal_id:
                self._memberships[key] = replace(membership, active=False)
        affected_delegations = {
            delegation_id
            for delegation_id, delegation in self._delegations.items()
            if delegation.agent_id == principal_id or delegation.subject_user_id == principal_id
        }
        for delegation_id in affected_delegations:
            self._delegations[delegation_id] = replace(
                self._delegations[delegation_id], active=False
            )
        for key, binding in tuple(self._channel_bindings.items()):
            if (
                binding.agent_id == principal_id
                or binding.user_id == principal_id
                or binding.delegation_id in affected_delegations
            ):
                self._channel_bindings[key] = replace(binding, active=False)
        self._revoke_matching_tokens(
            lambda record: record.session.actor_id == principal_id
            or record.session.subject_user_id == principal_id,
            disabled_at,
        )
        return disabled

    def save_membership(self, record: MembershipRecord) -> None:
        self._memberships[(record.tenant_id, record.principal_id)] = record

    def get_membership(self, tenant_id: str, principal_id: str) -> MembershipRecord | None:
        return self._memberships.get((tenant_id, principal_id))

    def list_memberships(self, tenant_id: str) -> tuple[MembershipRecord, ...]:
        return tuple(
            sorted(
                (record for record in self._memberships.values() if record.tenant_id == tenant_id),
                key=lambda record: record.principal_id,
            )
        )

    def update_membership(self, record: MembershipRecord, changed_at: datetime) -> MembershipRecord:
        key = (record.tenant_id, record.principal_id)
        current = self._memberships.get(key)
        if current is None:
            raise ControlNotFound("Tenant Membership not found")
        if not record.active:
            disabled = self.disable_membership(record.tenant_id, record.principal_id, changed_at)
            updated = replace(disabled, roles=record.roles)
        else:
            updated = record
        self._memberships[key] = updated
        if updated.roles != current.roles or updated.active != current.active:
            self._revoke_matching_tokens(
                lambda token: token.session.tenant_id == record.tenant_id
                and token.session.actor_id == record.principal_id,
                changed_at,
            )
        return updated

    def disable_membership(
        self, tenant_id: str, principal_id: str, disabled_at: datetime
    ) -> MembershipRecord:
        key = (tenant_id, principal_id)
        current = self._memberships.get(key)
        if current is None:
            raise ControlNotFound("Tenant Membership not found")
        disabled = replace(current, active=False)
        self._memberships[key] = disabled
        affected_delegations = {
            delegation_id
            for delegation_id, delegation in self._delegations.items()
            if delegation.tenant_id == tenant_id
            and (delegation.agent_id == principal_id or delegation.subject_user_id == principal_id)
        }
        for delegation_id in affected_delegations:
            self._delegations[delegation_id] = replace(
                self._delegations[delegation_id], active=False
            )
        for binding_key, binding in tuple(self._channel_bindings.items()):
            if binding.delegation_id in affected_delegations:
                self._channel_bindings[binding_key] = replace(binding, active=False)
        self._revoke_matching_tokens(
            lambda record: record.session.tenant_id == tenant_id
            and (
                record.session.actor_id == principal_id
                or record.session.subject_user_id == principal_id
            ),
            disabled_at,
        )
        return disabled

    def revoke_membership_role(
        self, tenant_id: str, principal_id: str, role: str, revoked_at: datetime
    ) -> MembershipRecord:
        current = self._memberships.get((tenant_id, principal_id))
        if current is None:
            raise ControlNotFound("Tenant Membership not found")
        if role not in current.roles:
            raise ControlNotFound("Tenant Membership role not found")
        remaining = current.roles - {role}
        if not remaining:
            return self.disable_membership(tenant_id, principal_id, revoked_at)
        updated = replace(current, roles=remaining)
        self._memberships[(tenant_id, principal_id)] = updated
        self._revoke_matching_tokens(
            lambda record: record.session.tenant_id == tenant_id
            and record.session.actor_id == principal_id,
            revoked_at,
        )
        return updated

    def list_active_human_member_ids(
        self,
        tenant_id: str,
    ) -> tuple[str, ...]:
        tenant = self._tenants.get(tenant_id)
        if tenant is None or not tenant.active:
            return ()
        return tuple(
            sorted(
                membership.principal_id
                for membership in self._memberships.values()
                if membership.tenant_id == tenant_id
                and membership.active
                and ((principal := self._principals.get(membership.principal_id)) is not None)
                and principal.active
                and principal.kind is PrincipalKind.USER
            )
        )

    def save_delegation(self, record: DelegationRecord) -> None:
        if record.delegation_id in self._delegations:
            raise ControlConflict("Delegation already exists")
        self._delegations[record.delegation_id] = record

    def get_delegation(self, delegation_id: str) -> DelegationRecord | None:
        return self._delegations.get(delegation_id)

    def list_delegations(self, tenant_id: str) -> tuple[DelegationRecord, ...]:
        return tuple(
            sorted(
                (record for record in self._delegations.values() if record.tenant_id == tenant_id),
                key=lambda record: record.delegation_id,
            )
        )

    def update_delegation_active(
        self, delegation_id: str, *, active: bool, changed_at: datetime
    ) -> DelegationRecord:
        current = self._delegations.get(delegation_id)
        if current is None:
            raise ControlNotFound("Delegation not found")
        if not active:
            return self.revoke_delegation(delegation_id, changed_at)
        updated = replace(current, active=True)
        self._delegations[delegation_id] = updated
        return updated

    def revoke_delegation(self, delegation_id: str, revoked_at: datetime) -> DelegationRecord:
        current = self._delegations.get(delegation_id)
        if current is None:
            raise ControlNotFound("Delegation not found")
        revoked = replace(current, active=False)
        self._delegations[delegation_id] = revoked
        for key, binding in tuple(self._channel_bindings.items()):
            if binding.delegation_id == delegation_id:
                self._channel_bindings[key] = replace(binding, active=False)
        self._revoke_matching_tokens(
            lambda record: record.session.delegation_id == delegation_id,
            revoked_at,
        )
        return revoked

    def save_channel_binding(self, record: ChannelBindingRecord) -> None:
        key = (record.channel, record.external_id)
        if key in self._channel_bindings:
            raise ControlConflict("Channel Binding already exists")
        self._channel_bindings[key] = record

    def get_channel_binding(self, channel: str, external_id: str) -> ChannelBindingRecord | None:
        return self._channel_bindings.get((channel, external_id))

    def update_channel_binding(self, record: ChannelBindingRecord) -> None:
        key = (record.channel, record.external_id)
        if key not in self._channel_bindings:
            raise ControlNotFound("Channel Binding not found")
        self._channel_bindings[key] = record

    def delete_channel_binding(self, channel: str, external_id: str) -> bool:
        return self._channel_bindings.pop((channel, external_id), None) is not None

    def save_tenant_route(self, record: TenantRouteRecord) -> None:
        self._routes[record.tenant_id] = record

    def get_tenant_route(self, tenant_id: str) -> TenantRouteRecord | None:
        return self._routes.get(tenant_id)

    def list_tenant_routes(self) -> tuple[TenantRouteRecord, ...]:
        return tuple(self._routes[tenant_id] for tenant_id in sorted(self._routes))

    def configure_token_lifetime_days(
        self,
        tenant_id: str,
        actor_kind: PrincipalKind,
        days: int,
        *,
        delegated: bool = False,
    ) -> None:
        self._token_lifetime_days[(tenant_id, actor_kind, delegated)] = days

    def token_lifetime_days(
        self,
        tenant_id: str,
        actor_kind: PrincipalKind,
        *,
        delegated: bool,
    ) -> int | None:
        return self._token_lifetime_days.get((tenant_id, actor_kind, delegated))

    def save(self, record: TokenRecord) -> None:
        self._tokens[record.token_id] = record

    def get(self, token_id: str) -> TokenRecord | None:
        record = self._tokens.get(token_id)
        if record is None:
            return None
        session = record.session
        tenant = self._tenants.get(session.tenant_id)
        principal = self._principals.get(session.actor_id)
        membership = self._memberships.get((session.tenant_id, session.actor_id))
        if (
            tenant is None
            or not tenant.active
            or principal is None
            or not principal.active
            or membership is None
            or not membership.active
        ):
            return None
        if session.delegation_id is not None:
            delegation = self._delegations.get(session.delegation_id)
            if delegation is None or not delegation.active:
                return None
        return record

    def revoke(self, token_id: str, revoked_at: datetime) -> bool:
        existing = self._tokens.get(token_id)
        if existing is None:
            return False
        if existing.revoked_at is None:
            self._tokens[token_id] = TokenRecord(
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
        existing = self._tokens.get(token_id)
        if existing is None:
            return
        self._tokens[token_id] = TokenRecord(
            token_id=existing.token_id,
            verifier=existing.verifier,
            session=existing.session,
            expires_at=existing.expires_at,
            issued_at=existing.issued_at,
            revoked_at=existing.revoked_at,
            last_used_at=used_at,
        )

    def list_token_records(self, tenant_id: str, principal_id: str) -> tuple[TokenRecord, ...]:
        return tuple(
            record
            for record in self._tokens.values()
            if record.session.tenant_id == tenant_id and record.session.actor_id == principal_id
        )

    def rotate(
        self,
        previous_token_id: str,
        replacement: TokenRecord,
        *,
        previous_valid_until: datetime,
        rotated_at: datetime,
    ) -> bool:
        previous = self.get(previous_token_id)
        if (
            previous is None
            or previous.revoked_at is not None
            or previous.expires_at <= rotated_at
            or replacement.token_id in self._tokens
        ):
            return False
        self._tokens[previous_token_id] = replace(
            previous, expires_at=min(previous.expires_at, previous_valid_until)
        )
        self._tokens[replacement.token_id] = replacement
        return True

    def _revoke_matching_tokens(
        self, predicate: Callable[[TokenRecord], bool], revoked_at: datetime
    ) -> None:
        for token_id, record in tuple(self._tokens.items()):
            if predicate(record) and record.revoked_at is None:
                self._tokens[token_id] = replace(record, revoked_at=revoked_at)


def is_token_unused_for_30_days(record: TokenRecord, *, now: datetime) -> bool:
    """Derive an inventory warning without treating it as an enforcement policy."""
    if now.tzinfo is None:
        raise ValueError("Token inventory time must be timezone-aware")
    last_activity = record.last_used_at or record.issued_at
    return (
        record.revoked_at is None
        and record.expires_at > now
        and last_activity <= now - UNUSED_TOKEN_AGE
    )


class ControlModule:
    """Deep control-plane Interface; storage and CLI details remain behind Adapters."""

    def __init__(self, store: ControlStore, tokens: TokenService) -> None:
        self._store = store
        self._tokens = tokens

    def create_tenant(self, tenant_id: str, name: str) -> TenantRecord:
        record = TenantRecord(tenant_id=tenant_id, name=name)
        self._store.add_tenant(record)
        return record

    def create_principal(self, principal_id: str, name: str, kind: str) -> PrincipalRecord:
        record = PrincipalRecord(
            principal_id=principal_id,
            name=name,
            kind=PrincipalKind(kind),
        )
        self._store.add_principal(record)
        return record

    def inspect_principal(self, principal_id: str) -> PrincipalRecord:
        record = self._store.get_principal(principal_id)
        if record is None:
            raise ControlNotFound("Principal not found")
        return record

    def list_principals(self) -> tuple[PrincipalRecord, ...]:
        return self._store.list_principals()

    def update_principal(
        self,
        principal_id: str,
        *,
        name: str | None = None,
        active: bool | None = None,
    ) -> PrincipalRecord:
        current = self.inspect_principal(principal_id)
        if name is None and active is None:
            raise ValueError("Principal update requires a name or active state")
        normalized_name = current.name if name is None else name.strip()
        if not normalized_name:
            raise ValueError("Principal name cannot be empty")
        return self._store.update_principal(
            principal_id,
            name=normalized_name,
            active=current.active if active is None else active,
            changed_at=datetime.now(UTC),
        )

    def disable_principal(self, principal_id: str) -> PrincipalRecord:
        return self._store.disable_principal(principal_id, datetime.now(UTC))

    def grant_membership(
        self,
        tenant_id: str,
        principal_id: str,
        role: str,
    ) -> MembershipRecord:
        if role not in VALID_ROLES:
            raise ValueError(f"Unknown role: {role}")
        tenant = self._store.get_tenant(tenant_id)
        principal = self._store.get_principal(principal_id)
        if tenant is None or not tenant.active:
            raise ControlNotFound("Active Tenant not found")
        if principal is None or not principal.active:
            raise ControlNotFound("Active Principal not found")
        if principal.kind is PrincipalKind.AGENT and role != "tenant_member":
            raise ValueError("Administrative and Curator roles require a human User")
        existing = self._store.get_membership(tenant_id, principal_id)
        roles = frozenset({role}) if existing is None else existing.roles | {role}
        membership = MembershipRecord(tenant_id, principal_id, roles)
        self._store.save_membership(membership)
        return membership

    def inspect_membership(self, tenant_id: str, principal_id: str) -> MembershipRecord:
        membership = self._store.get_membership(tenant_id, principal_id)
        if membership is None:
            raise ControlNotFound("Tenant Membership not found")
        return membership

    def list_memberships(self, tenant_id: str) -> tuple[MembershipRecord, ...]:
        if self._store.get_tenant(tenant_id) is None:
            raise ControlNotFound("Tenant not found")
        return self._store.list_memberships(tenant_id)

    def update_membership(
        self,
        tenant_id: str,
        principal_id: str,
        *,
        roles: frozenset[str] | None = None,
        active: bool | None = None,
    ) -> MembershipRecord:
        current = self.inspect_membership(tenant_id, principal_id)
        if roles is None and active is None:
            raise ValueError("Tenant Membership update requires roles or active state")
        selected_roles = current.roles if roles is None else frozenset(roles)
        if not selected_roles:
            raise ValueError("Tenant Membership must retain at least one role")
        unknown_roles = selected_roles - VALID_ROLES
        if unknown_roles:
            raise ValueError(f"Unknown roles: {', '.join(sorted(unknown_roles))}")
        selected_active = current.active if active is None else active
        principal = self._store.get_principal(principal_id)
        tenant = self._store.get_tenant(tenant_id)
        if principal is None:
            raise ControlNotFound("Principal not found")
        if principal.kind is PrincipalKind.AGENT and selected_roles != {"tenant_member"}:
            raise ValueError("Agents may only have the tenant_member role")
        if selected_active and (tenant is None or not tenant.active):
            raise ControlNotFound("Active Tenant not found")
        if selected_active and not principal.active:
            raise ControlNotFound("Active Principal not found")
        return self._store.update_membership(
            replace(current, roles=selected_roles, active=selected_active),
            datetime.now(UTC),
        )

    def permits_self_approval(self, tenant_id: str, principal_id: str) -> bool:
        """Return true only while this Administrator is the Tenant's sole human."""
        membership = self._store.get_membership(tenant_id, principal_id)
        if membership is None or "tenant_administrator" not in membership.roles:
            return False
        return self._store.list_active_human_member_ids(tenant_id) == (principal_id,)

    def disable_membership(self, tenant_id: str, principal_id: str) -> MembershipRecord:
        return self._store.disable_membership(tenant_id, principal_id, datetime.now(UTC))

    def revoke_membership_role(
        self, tenant_id: str, principal_id: str, role: str
    ) -> MembershipRecord:
        if role not in VALID_ROLES:
            raise ValueError(f"Unknown role: {role}")
        return self._store.revoke_membership_role(tenant_id, principal_id, role, datetime.now(UTC))

    def issue_access_token(
        self,
        tenant_id: str,
        principal_id: str,
        *,
        lifetime: timedelta | None = None,
    ) -> IssuedCredential:
        tenant = self._store.get_tenant(tenant_id)
        principal = self._store.get_principal(principal_id)
        membership = self._store.get_membership(tenant_id, principal_id)
        if tenant is None or not tenant.active:
            raise ControlNotFound("Active Tenant not found")
        if principal is None or not principal.active:
            raise ControlNotFound("Active Principal not found")
        if membership is None or not membership.active:
            raise ControlNotFound("Active Tenant Membership not found")
        platform_days = 90 if principal.kind is PrincipalKind.USER else 30
        configured_days = self._store.token_lifetime_days(
            tenant_id,
            principal.kind,
            delegated=False,
        )
        maximum_lifetime = timedelta(days=configured_days or platform_days)
        requested_lifetime = maximum_lifetime if lifetime is None else lifetime
        if requested_lifetime <= timedelta(0) or requested_lifetime > maximum_lifetime:
            raise ValueError("Access Token lifetime exceeds the Principal policy maximum")
        return self._tokens.issue(
            TenantSession(
                tenant_id=tenant_id,
                actor_id=principal_id,
                actor_kind=principal.kind,
                roles=membership.roles,
            ),
            lifetime=requested_lifetime,
        )

    def reauthorize_session(self, session: TenantSession) -> TenantSession:
        """Rehydrate current authority for delayed work; revocation is immediate."""
        tenant = self._store.get_tenant(session.tenant_id)
        principal = self._store.get_principal(session.actor_id)
        membership = self._store.get_membership(session.tenant_id, session.actor_id)
        if tenant is None or not tenant.active:
            raise ControlNotFound("Active Tenant not found")
        if principal is None or not principal.active or principal.kind is not session.actor_kind:
            raise ControlNotFound("Active Principal not found")
        if membership is None or not membership.active:
            raise ControlNotFound("Active Tenant Membership not found")
        if session.delegation_id is not None:
            delegation = self._store.get_delegation(session.delegation_id)
            subject = (
                None
                if session.subject_user_id is None
                else self._store.get_principal(session.subject_user_id)
            )
            subject_membership = (
                None
                if session.subject_user_id is None
                else self._store.get_membership(session.tenant_id, session.subject_user_id)
            )
            if (
                delegation is None
                or not delegation.active
                or delegation.tenant_id != session.tenant_id
                or delegation.agent_id != session.actor_id
                or delegation.subject_user_id != session.subject_user_id
            ):
                raise ControlNotFound("Active Delegation not found")
            if subject is None or not subject.active or subject.kind is not PrincipalKind.USER:
                raise ControlNotFound("Active Subject User not found")
            if subject_membership is None or not subject_membership.active:
                raise ControlNotFound("Active Subject User Tenant Membership not found")
        return TenantSession(
            tenant_id=session.tenant_id,
            actor_id=session.actor_id,
            actor_kind=session.actor_kind,
            roles=membership.roles,
            subject_user_id=session.subject_user_id,
            delegation_id=session.delegation_id,
            token_id=session.token_id,
        )

    def create_delegation(
        self,
        delegation_id: str,
        *,
        tenant_id: str,
        agent_id: str,
        subject_user_id: str,
    ) -> DelegationRecord:
        self._require_active_delegation_bindings(tenant_id, agent_id, subject_user_id)
        record = DelegationRecord(
            delegation_id=delegation_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            subject_user_id=subject_user_id,
        )
        self._store.save_delegation(record)
        return record

    def inspect_delegation(self, delegation_id: str) -> DelegationRecord:
        record = self._store.get_delegation(delegation_id)
        if record is None:
            raise ControlNotFound("Delegation not found")
        return record

    def list_delegations(self, tenant_id: str) -> tuple[DelegationRecord, ...]:
        if self._store.get_tenant(tenant_id) is None:
            raise ControlNotFound("Tenant not found")
        return self._store.list_delegations(tenant_id)

    def update_delegation(self, delegation_id: str, *, active: bool) -> DelegationRecord:
        current = self.inspect_delegation(delegation_id)
        if active:
            self._require_active_delegation_bindings(
                current.tenant_id, current.agent_id, current.subject_user_id
            )
        return self._store.update_delegation_active(
            delegation_id,
            active=active,
            changed_at=datetime.now(UTC),
        )

    def _require_active_delegation_bindings(
        self, tenant_id: str, agent_id: str, subject_user_id: str
    ) -> None:
        tenant = self._store.get_tenant(tenant_id)
        agent = self._store.get_principal(agent_id)
        subject = self._store.get_principal(subject_user_id)
        agent_membership = self._store.get_membership(tenant_id, agent_id)
        subject_membership = self._store.get_membership(tenant_id, subject_user_id)
        if tenant is None or not tenant.active:
            raise ControlNotFound("Active Tenant not found")
        if agent is None or agent.kind is not PrincipalKind.AGENT or not agent.active:
            raise ControlNotFound("Active Agent not found")
        if subject is None or subject.kind is not PrincipalKind.USER or not subject.active:
            raise ControlNotFound("Active Subject User not found")
        if agent_membership is None or not agent_membership.active:
            raise ControlNotFound("Active Agent Tenant Membership not found")
        if subject_membership is None or not subject_membership.active:
            raise ControlNotFound("Active Subject User Tenant Membership not found")

    def issue_delegated_access_token(
        self,
        delegation_id: str,
        *,
        lifetime: timedelta | None = None,
    ) -> IssuedCredential:
        delegation = self._store.get_delegation(delegation_id)
        configured_days = (
            None
            if delegation is None
            else self._store.token_lifetime_days(
                delegation.tenant_id,
                PrincipalKind.AGENT,
                delegated=True,
            )
        )
        maximum_lifetime = timedelta(days=configured_days or 90)
        requested_lifetime = maximum_lifetime if lifetime is None else lifetime
        if requested_lifetime <= timedelta(0) or requested_lifetime > maximum_lifetime:
            raise ValueError("Delegated Access Token lifetime exceeds the Tenant policy maximum")
        if delegation is None or not delegation.active:
            raise ControlNotFound("Active Delegation not found")
        membership = self._store.get_membership(delegation.tenant_id, delegation.agent_id)
        if membership is None or not membership.active:
            raise ControlNotFound("Active Agent Tenant Membership not found")
        return self._tokens.issue(
            TenantSession(
                tenant_id=delegation.tenant_id,
                actor_id=delegation.agent_id,
                actor_kind=PrincipalKind.AGENT,
                roles=membership.roles,
                subject_user_id=delegation.subject_user_id,
                delegation_id=delegation.delegation_id,
            ),
            lifetime=requested_lifetime,
        )

    def revoke_delegation(self, delegation_id: str) -> DelegationRecord:
        return self._store.revoke_delegation(delegation_id, datetime.now(UTC))

    def create_channel_binding(
        self,
        binding_id: str,
        *,
        channel: str,
        external_id: str,
        delegation_id: str,
    ) -> ChannelBindingRecord:
        if channel != "telegram":
            raise ValueError("Iteration 1 supports only Telegram Channel Bindings")
        if not external_id.isdecimal():
            raise ValueError("Telegram Channel Binding requires a numeric external identity")
        delegation = self._store.get_delegation(delegation_id)
        if delegation is None or not delegation.active:
            raise ControlNotFound("Active Delegation not found")
        record = ChannelBindingRecord(
            binding_id=binding_id,
            channel=channel,
            external_id=external_id,
            tenant_id=delegation.tenant_id,
            user_id=delegation.subject_user_id,
            agent_id=delegation.agent_id,
            delegation_id=delegation.delegation_id,
        )
        self._store.save_channel_binding(record)
        return record

    def resolve_channel_session(self, channel: str, external_id: str) -> TenantSession:
        binding = self._store.get_channel_binding(channel, external_id)
        if binding is None or not binding.active:
            raise ControlNotFound("Active Channel Binding not found")
        delegation = self._store.get_delegation(binding.delegation_id)
        membership = self._store.get_membership(binding.tenant_id, binding.agent_id)
        if delegation is None or not delegation.active:
            raise ControlNotFound("Active Delegation not found")
        if (
            delegation.tenant_id != binding.tenant_id
            or delegation.agent_id != binding.agent_id
            or delegation.subject_user_id != binding.user_id
        ):
            raise ControlNotFound("Channel Binding no longer matches its Delegation")
        if membership is None or not membership.active:
            raise ControlNotFound("Active Agent Tenant Membership not found")
        return TenantSession(
            tenant_id=binding.tenant_id,
            actor_id=binding.agent_id,
            actor_kind=PrincipalKind.AGENT,
            roles=membership.roles,
            subject_user_id=binding.user_id,
            delegation_id=binding.delegation_id,
        )

    def inspect_channel_binding(self, channel: str, external_id: str) -> ChannelBindingRecord:
        binding = self._store.get_channel_binding(channel, external_id)
        if binding is None:
            raise ControlNotFound("Channel Binding not found")
        return binding

    def disable_channel_binding(self, channel: str, external_id: str) -> ChannelBindingRecord:
        binding = self.inspect_channel_binding(channel, external_id)
        disabled = replace(binding, active=False)
        self._store.update_channel_binding(disabled)
        return disabled

    def remove_channel_binding(self, channel: str, external_id: str) -> bool:
        return self._store.delete_channel_binding(channel, external_id)

    def register_tenant_route(
        self,
        tenant_id: str,
        *,
        neo4j_service_address: str,
        neo4j_secret_name: str,
        tenant_database_name: str,
        tenant_database_role: str,
        healthy: bool,
    ) -> TenantRouteRecord:
        tenant = self._store.get_tenant(tenant_id)
        if tenant is None or not tenant.active:
            raise ControlNotFound("Active Tenant not found")
        normalized = tenant_id.replace("-", "_")
        expected = (
            f"neo4j-{tenant_id}:7687",
            f"{tenant_id}/neo4j_password",
            f"tenant_{normalized}",
            f"tenant_{normalized}_rw",
        )
        supplied = (
            neo4j_service_address,
            neo4j_secret_name,
            tenant_database_name,
            tenant_database_role,
        )
        if supplied != expected:
            raise ValueError("Tenant route values must derive from the immutable Tenant ID")
        route = TenantRouteRecord(
            tenant_id=tenant_id,
            neo4j_service_address=neo4j_service_address,
            neo4j_secret_name=neo4j_secret_name,
            tenant_database_name=tenant_database_name,
            tenant_database_role=tenant_database_role,
            healthy=healthy,
        )
        self._store.save_tenant_route(route)
        return route

    def resolve_tenant_route(self, tenant_id: str) -> TenantRouteRecord:
        tenant = self._store.get_tenant(tenant_id)
        route = self._store.get_tenant_route(tenant_id)
        if tenant is None or not tenant.active or route is None or not route.healthy:
            raise ControlNotFound("Healthy Tenant route not found")
        return route

    def list_tenant_routes(self) -> tuple[TenantRouteRecord, ...]:
        return tuple(
            route
            for route in self._store.list_tenant_routes()
            if route.healthy
            and (tenant := self._store.get_tenant(route.tenant_id)) is not None
            and tenant.active
        )

    def authenticate(self, access_token: str) -> TenantSession:
        return self._tokens.authenticate(access_token)

    def revoke_access_token(self, token_id: str) -> bool:
        return self._tokens.revoke(token_id)

    def rotate_access_token(
        self,
        token_id: str,
        *,
        overlap: timedelta,
        lifetime: timedelta | None = None,
    ) -> RotatedCredential:
        if overlap <= timedelta(0) or overlap > timedelta(hours=24):
            raise ValueError("Token rotation overlap must be between 1 second and 24 hours")
        previous = self._store.get(token_id)
        now = datetime.now(UTC)
        if previous is None or previous.revoked_at is not None or previous.expires_at <= now:
            raise ControlNotFound("Active Access Token not found")
        session = previous.session
        principal = self._store.get_principal(session.actor_id)
        membership = self._store.get_membership(session.tenant_id, session.actor_id)
        if principal is None or not principal.active:
            raise ControlNotFound("Active Principal not found")
        if membership is None or not membership.active:
            raise ControlNotFound("Active Tenant Membership not found")
        delegated = session.delegation_id is not None
        if delegated:
            delegation = self._store.get_delegation(session.delegation_id or "")
            subject = (
                None
                if session.subject_user_id is None
                else self._store.get_principal(session.subject_user_id)
            )
            subject_membership = (
                None
                if session.subject_user_id is None
                else self._store.get_membership(session.tenant_id, session.subject_user_id)
            )
            if delegation is None or not delegation.active:
                raise ControlNotFound("Active Delegation not found")
            if subject is None or not subject.active:
                raise ControlNotFound("Active Subject User not found")
            if subject_membership is None or not subject_membership.active:
                raise ControlNotFound("Active Subject User Tenant Membership not found")
        platform_days = 90 if principal.kind is PrincipalKind.USER else 30
        configured_days = self._store.token_lifetime_days(
            session.tenant_id,
            principal.kind,
            delegated=delegated,
        )
        maximum_lifetime = timedelta(days=configured_days or (90 if delegated else platform_days))
        requested_lifetime = maximum_lifetime if lifetime is None else lifetime
        if requested_lifetime <= timedelta(0) or requested_lifetime > maximum_lifetime:
            raise ValueError("Replacement Access Token lifetime exceeds policy maximum")
        bounded_overlap = min(overlap, previous.expires_at - now)
        return self._tokens.rotate(
            token_id,
            TenantSession(
                tenant_id=session.tenant_id,
                actor_id=session.actor_id,
                actor_kind=principal.kind,
                roles=membership.roles,
                subject_user_id=session.subject_user_id,
                delegation_id=session.delegation_id,
            ),
            lifetime=requested_lifetime,
            overlap=bounded_overlap,
            now=now,
        )

    def list_tokens(self, tenant_id: str, principal_id: str) -> tuple[TokenRecord, ...]:
        return self._store.list_token_records(tenant_id, principal_id)
