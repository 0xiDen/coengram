"""Real PostgreSQL behavior checks for the Control Store Adapter."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_memory_service.auth import AuthenticationError, TokenService
from agent_memory_service.control import ControlConflict, ControlModule, ControlNotFound
from agent_memory_service.models import PrincipalKind
from agent_memory_service.stores.postgres_control import PostgresControlStore

CONTROL_DATABASE_URL = os.environ.get("CONTROL_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not CONTROL_DATABASE_URL,
    reason="CONTROL_DATABASE_URL is required for PostgreSQL integration tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_control_store() -> None:
    if not CONTROL_DATABASE_URL:
        return
    from alembic import command
    from alembic.config import Config

    repository_root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(repository_root / "alembic-control.ini"), "head")


def test_control_module_persists_tenant_tokens_and_delegation() -> None:
    assert CONTROL_DATABASE_URL is not None
    suffix = uuid4().hex
    first_tenant_id = f"tenant-{suffix}"
    second_tenant_id = f"tenant-other-{suffix}"
    user_id = f"user-{suffix}"
    agent_id = f"agent-{suffix}"
    delegation_id = f"delegation-{suffix}"

    store = PostgresControlStore(CONTROL_DATABASE_URL)
    control = ControlModule(store, TokenService(store))

    control.create_tenant(first_tenant_id, "Backend")
    control.create_tenant(second_tenant_id, "Other Backend")
    control.create_principal(user_id, "Alice", PrincipalKind.USER.value)
    control.create_principal(agent_id, "Synthesis", PrincipalKind.AGENT.value)
    control.grant_membership(first_tenant_id, user_id, "tenant_member")
    control.grant_membership(first_tenant_id, user_id, "knowledge_curator")
    control.grant_membership(second_tenant_id, user_id, "tenant_member")
    control.grant_membership(first_tenant_id, agent_id, "tenant_member")

    with pytest.raises(ControlConflict, match="Tenant already exists"):
        control.create_tenant(first_tenant_id, "Duplicate")

    first_credential = control.issue_access_token(first_tenant_id, user_id)
    second_credential = control.issue_access_token(second_tenant_id, user_id)

    first_session = control.authenticate(first_credential.access_token)
    second_session = control.authenticate(second_credential.access_token)
    assert first_session.tenant_id == first_tenant_id
    assert first_session.actor_id == user_id
    assert first_session.roles == frozenset({"tenant_member", "knowledge_curator"})
    assert second_session.tenant_id == second_tenant_id
    assert second_session.roles == frozenset({"tenant_member"})
    assert control.list_tokens(first_tenant_id, user_id)[0].last_used_at is not None

    control.create_delegation(
        delegation_id,
        tenant_id=first_tenant_id,
        agent_id=agent_id,
        subject_user_id=user_id,
    )
    delegated_credential = control.issue_delegated_access_token(delegation_id)
    delegated_session = control.authenticate(delegated_credential.access_token)
    assert delegated_session.actor_id == agent_id
    assert delegated_session.actor_kind is PrincipalKind.AGENT
    assert delegated_session.tenant_id == first_tenant_id
    assert delegated_session.subject_user_id == user_id
    assert delegated_session.delegation_id == delegation_id

    binding_id = f"telegram-binding-{suffix}"
    external_id = str(int(suffix[:12], 16))
    binding = control.create_channel_binding(
        binding_id,
        channel="telegram",
        external_id=external_id,
        delegation_id=delegation_id,
    )
    channel_session = control.resolve_channel_session("telegram", external_id)
    assert binding.tenant_id == first_tenant_id
    assert binding.user_id == user_id
    assert channel_session.actor_id == agent_id
    assert channel_session.subject_user_id == user_id
    assert channel_session.delegation_id == delegation_id

    with pytest.raises(ControlConflict, match="Channel Binding already exists"):
        control.create_channel_binding(
            f"duplicate-binding-{suffix}",
            channel="telegram",
            external_id=external_id,
            delegation_id=delegation_id,
        )
    disabled = control.disable_channel_binding("telegram", external_id)
    assert not disabled.active
    with pytest.raises(ControlNotFound, match="Active Channel Binding"):
        control.resolve_channel_session("telegram", external_id)
    assert control.remove_channel_binding("telegram", external_id)
    assert not control.remove_channel_binding("telegram", external_id)

    records = control.list_tokens(first_tenant_id, agent_id)
    assert [record.token_id for record in records] == [delegated_credential.token_id]
    assert records[0].verifier not in delegated_credential.access_token.encode("utf-8")

    assert control.revoke_access_token(delegated_credential.token_id)
    assert control.revoke_access_token(delegated_credential.token_id)
    with pytest.raises(AuthenticationError, match="Expired or revoked"):
        control.authenticate(delegated_credential.access_token)

    expired = TokenService(store).issue(
        first_session,
        lifetime=timedelta(seconds=-1),
    )
    with pytest.raises(AuthenticationError, match="Expired or revoked"):
        control.authenticate(expired.access_token)

    assert not control.revoke_access_token(f"missing-{suffix}")


def test_single_human_administrator_is_derived_from_current_control_records() -> None:
    assert CONTROL_DATABASE_URL is not None
    suffix = uuid4().hex
    tenant_id = f"tenant-self-approval-{suffix}"
    first_admin = f"admin-first-{suffix}"
    second_admin = f"admin-second-{suffix}"
    store = PostgresControlStore(CONTROL_DATABASE_URL)
    control = ControlModule(store, TokenService(store))
    control.create_tenant(tenant_id, "Self approval test")
    control.create_principal(first_admin, "First Admin", PrincipalKind.USER.value)
    control.create_principal(second_admin, "Second Admin", PrincipalKind.USER.value)

    control.grant_membership(tenant_id, first_admin, "tenant_administrator")
    assert control.permits_self_approval(tenant_id, first_admin)

    control.grant_membership(tenant_id, second_admin, "tenant_member")
    assert not control.permits_self_approval(tenant_id, first_admin)
    assert not control.permits_self_approval(tenant_id, second_admin)
