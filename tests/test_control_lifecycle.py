from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agent_memory_service.auth import AuthenticationError, TokenRecord, TokenService
from agent_memory_service.cli import run_cli
from agent_memory_service.control import (
    ControlModule,
    ControlNotFound,
    InMemoryControlStore,
    is_token_unused_for_30_days,
)
from agent_memory_service.models import PrincipalKind, TenantSession


def _control() -> tuple[ControlModule, InMemoryControlStore]:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A")
    control.create_principal("user-alice", "Alice", "user")
    control.create_principal("agent-helper", "Helper", "agent")
    control.grant_membership("tenant-a", "user-alice", "tenant_member")
    control.grant_membership("tenant-a", "agent-helper", "tenant_member")
    return control, store


def test_principal_and_membership_lifecycle_preserves_immutable_bindings() -> None:
    control, _ = _control()
    old_token = control.issue_access_token("tenant-a", "user-alice")

    principal = control.update_principal("user-alice", name="Alice Cooper")
    membership = control.update_membership(
        "tenant-a",
        "user-alice",
        roles=frozenset({"tenant_member", "knowledge_curator"}),
    )

    assert principal.principal_id == "user-alice"
    assert principal.kind is PrincipalKind.USER
    assert principal.name == "Alice Cooper"
    assert membership.tenant_id == "tenant-a"
    assert membership.principal_id == "user-alice"
    assert membership.roles == frozenset({"tenant_member", "knowledge_curator"})
    assert control.inspect_principal("user-alice") == principal
    assert control.inspect_membership("tenant-a", "user-alice") == membership
    assert [item.principal_id for item in control.list_principals()] == [
        "agent-helper",
        "user-alice",
    ]
    assert [item.principal_id for item in control.list_memberships("tenant-a")] == [
        "agent-helper",
        "user-alice",
    ]
    with pytest.raises(AuthenticationError):
        control.authenticate(old_token.access_token)

    with pytest.raises(ValueError, match="only have"):
        control.update_membership(
            "tenant-a",
            "agent-helper",
            roles=frozenset({"knowledge_curator"}),
        )


def test_reactivation_never_restores_cascaded_access() -> None:
    control, _ = _control()
    delegation = control.create_delegation(
        "delegation-a",
        tenant_id="tenant-a",
        agent_id="agent-helper",
        subject_user_id="user-alice",
    )
    control.create_channel_binding(
        "binding-a",
        channel="telegram",
        external_id="123",
        delegation_id=delegation.delegation_id,
    )
    delegated = control.issue_delegated_access_token(delegation.delegation_id)

    disabled = control.update_delegation(delegation.delegation_id, active=False)
    reactivated = control.update_delegation(delegation.delegation_id, active=True)

    assert not disabled.active and reactivated.active
    assert reactivated.tenant_id == "tenant-a"
    assert reactivated.agent_id == "agent-helper"
    assert reactivated.subject_user_id == "user-alice"
    assert control.list_delegations("tenant-a") == (reactivated,)
    with pytest.raises(AuthenticationError):
        control.authenticate(delegated.access_token)
    with pytest.raises(ControlNotFound, match="Channel Binding"):
        control.resolve_channel_session("telegram", "123")

    control.update_membership("tenant-a", "user-alice", active=False)
    with pytest.raises(ControlNotFound, match="Membership"):
        control.update_delegation(delegation.delegation_id, active=True)

    control.update_principal("user-alice", active=False)
    control.update_principal("user-alice", active=True)
    assert not control.inspect_membership("tenant-a", "user-alice").active


def test_memoryctl_lists_inspects_and_safely_updates_control_records() -> None:
    control, _ = _control()
    control.create_delegation(
        "delegation-a",
        tenant_id="tenant-a",
        agent_id="agent-helper",
        subject_user_id="user-alice",
    )
    output = io.StringIO()

    commands = (
        ["principal", "list"],
        ["principal", "inspect", "--id", "user-alice"],
        ["principal", "update", "--id", "user-alice", "--name", "Alice C"],
        ["membership", "list", "--tenant-id", "tenant-a"],
        [
            "membership",
            "inspect",
            "--tenant-id",
            "tenant-a",
            "--principal-id",
            "user-alice",
        ],
        [
            "membership",
            "update",
            "--tenant-id",
            "tenant-a",
            "--principal-id",
            "user-alice",
            "--role",
            "tenant_member",
            "--role",
            "knowledge_curator",
        ],
        ["delegation", "list", "--tenant-id", "tenant-a"],
        ["delegation", "inspect", "--id", "delegation-a"],
        ["delegation", "update", "--id", "delegation-a", "--active", "false"],
    )
    for command in commands:
        assert run_cli(command, control, output) == 0

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    assert documents[0]["principals"][0]["principal_id"] == "agent-helper"
    assert documents[1]["kind"] == "user"
    assert documents[2]["name"] == "Alice C"
    assert documents[3]["memberships"][0]["tenant_id"] == "tenant-a"
    assert documents[4]["principal_id"] == "user-alice"
    assert documents[5]["roles"] == ["knowledge_curator", "tenant_member"]
    assert documents[6]["delegations"][0]["agent_id"] == "agent-helper"
    assert documents[7]["subject_user_id"] == "user-alice"
    assert documents[8]["active"] is False


def test_unused_token_signal_uses_issue_time_until_first_use() -> None:
    checked_at = datetime(2026, 8, 2, tzinfo=UTC)
    record = TokenRecord(
        token_id="tok-a",
        verifier=b"verifier",
        session=TenantSession(
            tenant_id="tenant-a",
            actor_id="user-alice",
            actor_kind=PrincipalKind.USER,
            roles=frozenset({"tenant_member"}),
        ),
        issued_at=checked_at - timedelta(days=31),
        expires_at=checked_at + timedelta(days=1),
    )

    assert is_token_unused_for_30_days(record, now=checked_at)
    assert not is_token_unused_for_30_days(
        replace(record, last_used_at=checked_at - timedelta(days=1)), now=checked_at
    )
    assert not is_token_unused_for_30_days(
        replace(record, revoked_at=checked_at - timedelta(hours=1)), now=checked_at
    )
    assert not is_token_unused_for_30_days(replace(record, expires_at=checked_at), now=checked_at)

    control, store = _control()
    credential = control.issue_access_token("tenant-a", "user-alice")
    persisted = store.get(credential.token_id)
    assert persisted is not None
    store.save(replace(persisted, issued_at=datetime.now(UTC) - timedelta(days=31)))
    output = io.StringIO()
    run_cli(
        [
            "token",
            "list",
            "--tenant-id",
            "tenant-a",
            "--principal-id",
            "user-alice",
        ],
        control,
        output,
    )
    assert json.loads(output.getvalue())["tokens"][0]["unused_for_30_days"] is True
