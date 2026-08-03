"""PostgreSQL integration checks for revocable administration and rotation."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_memory_service.auth import AuthenticationError, TokenService
from agent_memory_service.control import ControlModule
from agent_memory_service.stores.postgres_control import PostgresControlStore

CONTROL_DATABASE_URL = os.environ.get("CONTROL_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not CONTROL_DATABASE_URL,
    reason="CONTROL_DATABASE_URL is required for PostgreSQL administration tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_control_store() -> None:
    if not CONTROL_DATABASE_URL:
        return
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(root / "alembic-control.ini"), "head")


def test_rotation_and_principal_disable_are_atomic_and_fail_closed() -> None:
    assert CONTROL_DATABASE_URL is not None
    suffix = uuid4().hex[:16]
    tenant_id = f"tenant-{suffix}"
    user_id = f"user-{suffix}"
    agent_id = f"agent-{suffix}"
    delegation_id = f"delegation-{suffix}"
    store = PostgresControlStore(CONTROL_DATABASE_URL)
    control = ControlModule(store, TokenService(store))
    control.create_tenant(tenant_id, "Administration integration")
    control.create_principal(user_id, "Alice", "user")
    control.create_principal(agent_id, "Helper", "agent")
    control.grant_membership(tenant_id, user_id, "tenant_member")
    control.grant_membership(tenant_id, agent_id, "tenant_member")
    direct = control.issue_access_token(tenant_id, user_id)
    control.create_delegation(
        delegation_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        subject_user_id=user_id,
    )
    delegated = control.issue_delegated_access_token(delegation_id)
    control.create_channel_binding(
        f"binding-{suffix}",
        channel="telegram",
        external_id=str(int(suffix, 16)),
        delegation_id=delegation_id,
    )

    rotated = control.rotate_access_token(
        direct.token_id,
        overlap=timedelta(minutes=10),
        lifetime=timedelta(days=1),
    )
    assert control.authenticate(direct.access_token).actor_id == user_id
    assert control.authenticate(rotated.credential.access_token).actor_id == user_id

    assert not control.disable_principal(user_id).active
    for credential in (direct, rotated.credential, delegated):
        with pytest.raises(AuthenticationError):
            control.authenticate(credential.access_token)
    with pytest.raises(Exception, match="Active Channel Binding"):
        control.resolve_channel_session("telegram", str(int(suffix, 16)))


def test_role_membership_and_delegation_revocation_persist() -> None:
    assert CONTROL_DATABASE_URL is not None
    suffix = uuid4().hex[:16]
    tenant_id = f"tenant-{suffix}"
    user_id = f"user-{suffix}"
    agent_id = f"agent-{suffix}"
    store = PostgresControlStore(CONTROL_DATABASE_URL)
    control = ControlModule(store, TokenService(store))
    control.create_tenant(tenant_id, "Administration integration")
    control.create_principal(user_id, "Alice", "user")
    control.create_principal(agent_id, "Helper", "agent")
    control.grant_membership(tenant_id, user_id, "tenant_member")
    control.grant_membership(tenant_id, user_id, "knowledge_curator")
    control.grant_membership(tenant_id, agent_id, "tenant_member")
    old = control.issue_access_token(tenant_id, user_id)

    membership = control.revoke_membership_role(tenant_id, user_id, "knowledge_curator")
    assert membership.roles == frozenset({"tenant_member"})
    with pytest.raises(AuthenticationError):
        control.authenticate(old.access_token)

    delegation = control.create_delegation(
        f"delegation-{suffix}",
        tenant_id=tenant_id,
        agent_id=agent_id,
        subject_user_id=user_id,
    )
    delegated = control.issue_delegated_access_token(delegation.delegation_id)
    assert not control.revoke_delegation(delegation.delegation_id).active
    with pytest.raises(AuthenticationError):
        control.authenticate(delegated.access_token)

    replacement = control.issue_access_token(tenant_id, user_id)
    assert not control.disable_membership(tenant_id, user_id).active
    with pytest.raises(AuthenticationError):
        control.authenticate(replacement.access_token)


def test_control_record_lifecycle_updates_only_safe_mutable_fields() -> None:
    assert CONTROL_DATABASE_URL is not None
    suffix = uuid4().hex[:16]
    tenant_id = f"tenant-{suffix}"
    user_id = f"user-{suffix}"
    agent_id = f"agent-{suffix}"
    delegation_id = f"delegation-{suffix}"
    store = PostgresControlStore(CONTROL_DATABASE_URL)
    control = ControlModule(store, TokenService(store))
    control.create_tenant(tenant_id, "Lifecycle integration")
    control.create_principal(user_id, "Alice", "user")
    control.create_principal(agent_id, "Helper", "agent")
    control.grant_membership(tenant_id, user_id, "tenant_member")
    control.grant_membership(tenant_id, agent_id, "tenant_member")
    delegation = control.create_delegation(
        delegation_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        subject_user_id=user_id,
    )
    old = control.issue_access_token(tenant_id, user_id)

    principal = control.update_principal(user_id, name="Alice Updated")
    membership = control.update_membership(
        tenant_id,
        user_id,
        roles=frozenset({"tenant_member", "knowledge_curator"}),
    )
    revoked = control.update_delegation(delegation_id, active=False)
    reactivated = control.update_delegation(delegation_id, active=True)

    assert principal.principal_id == user_id
    assert principal.kind.value == "user"
    assert membership.tenant_id == tenant_id
    assert membership.principal_id == user_id
    assert revoked.agent_id == agent_id
    assert reactivated == delegation
    assert control.inspect_principal(user_id) == principal
    assert control.inspect_membership(tenant_id, user_id) == membership
    assert control.inspect_delegation(delegation_id) == reactivated
    assert user_id in {item.principal_id for item in control.list_principals()}
    assert {item.principal_id for item in control.list_memberships(tenant_id)} == {
        agent_id,
        user_id,
    }
    assert control.list_delegations(tenant_id) == (reactivated,)
    with pytest.raises(AuthenticationError):
        control.authenticate(old.access_token)
