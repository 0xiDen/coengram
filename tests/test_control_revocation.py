from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest

from agent_memory_service.auth import AuthenticationError, TokenService
from agent_memory_service.cli import run_cli
from agent_memory_service.control import ControlModule, ControlNotFound, InMemoryControlStore


def control_plane() -> tuple[ControlModule, InMemoryControlStore]:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A")
    control.create_tenant("tenant-b", "Product B")
    control.create_principal("user-alice", "Alice", "user")
    control.create_principal("agent-helper", "Helper", "agent")
    for tenant_id in ("tenant-a", "tenant-b"):
        control.grant_membership(tenant_id, "user-alice", "tenant_member")
    control.grant_membership("tenant-a", "user-alice", "knowledge_curator")
    control.grant_membership("tenant-a", "agent-helper", "tenant_member")
    return control, store


def test_principal_disable_revokes_direct_and_delegated_access_fail_closed() -> None:
    control, _ = control_plane()
    direct = control.issue_access_token("tenant-a", "user-alice")
    delegation = control.create_delegation(
        "delegation-alice-helper",
        tenant_id="tenant-a",
        agent_id="agent-helper",
        subject_user_id="user-alice",
    )
    delegated = control.issue_delegated_access_token(delegation.delegation_id)
    control.create_channel_binding(
        "binding-alice-helper",
        channel="telegram",
        external_id="12345",
        delegation_id=delegation.delegation_id,
    )

    disabled = control.disable_principal("user-alice")

    assert not disabled.active
    for token in (direct, delegated):
        with pytest.raises(AuthenticationError):
            control.authenticate(token.access_token)
    with pytest.raises(ControlNotFound, match="Active Channel Binding"):
        control.resolve_channel_session("telegram", "12345")
    with pytest.raises(ControlNotFound, match="Active Principal"):
        control.issue_access_token("tenant-b", "user-alice")


def test_membership_disable_is_tenant_scoped_and_revokes_subject_delegations() -> None:
    control, _ = control_plane()
    tenant_a = control.issue_access_token("tenant-a", "user-alice")
    tenant_b = control.issue_access_token("tenant-b", "user-alice")
    delegation = control.create_delegation(
        "delegation-alice-helper",
        tenant_id="tenant-a",
        agent_id="agent-helper",
        subject_user_id="user-alice",
    )
    delegated = control.issue_delegated_access_token(delegation.delegation_id)

    disabled = control.disable_membership("tenant-a", "user-alice")

    assert not disabled.active
    with pytest.raises(AuthenticationError):
        control.authenticate(tenant_a.access_token)
    with pytest.raises(AuthenticationError):
        control.authenticate(delegated.access_token)
    assert control.authenticate(tenant_b.access_token).tenant_id == "tenant-b"


def test_role_revoke_invalidates_fixed_role_tokens_and_new_token_has_current_roles() -> None:
    control, _ = control_plane()
    old = control.issue_access_token("tenant-a", "user-alice")
    assert "knowledge_curator" in control.authenticate(old.access_token).roles

    membership = control.revoke_membership_role("tenant-a", "user-alice", "knowledge_curator")

    assert membership.roles == frozenset({"tenant_member"})
    with pytest.raises(AuthenticationError):
        control.authenticate(old.access_token)
    replacement = control.issue_access_token("tenant-a", "user-alice")
    assert control.authenticate(replacement.access_token).roles == frozenset({"tenant_member"})


def test_delegation_revoke_disables_channel_and_delegated_token() -> None:
    control, _ = control_plane()
    delegation = control.create_delegation(
        "delegation-alice-helper",
        tenant_id="tenant-a",
        agent_id="agent-helper",
        subject_user_id="user-alice",
    )
    credential = control.issue_delegated_access_token(delegation.delegation_id)
    session = control.authenticate(credential.access_token)
    control.create_channel_binding(
        "binding-alice-helper",
        channel="telegram",
        external_id="12345",
        delegation_id=delegation.delegation_id,
    )

    assert control.reauthorize_session(session) == session
    assert not control.revoke_delegation(delegation.delegation_id).active
    with pytest.raises(ControlNotFound, match="Active Delegation"):
        control.reauthorize_session(session)
    with pytest.raises(AuthenticationError):
        control.authenticate(credential.access_token)
    with pytest.raises(ControlNotFound):
        control.resolve_channel_session("telegram", "12345")


def test_token_rotation_atomically_shortens_old_token_and_preserves_overlap() -> None:
    control, store = control_plane()
    old = control.issue_access_token("tenant-a", "user-alice")

    rotated = control.rotate_access_token(
        old.token_id,
        overlap=timedelta(minutes=15),
        lifetime=timedelta(days=2),
    )

    assert control.authenticate(old.access_token).actor_id == "user-alice"
    assert control.authenticate(rotated.credential.access_token).actor_id == "user-alice"
    old_record = next(
        record
        for record in store.list_token_records("tenant-a", "user-alice")
        if record.token_id == old.token_id
    )
    assert old_record.expires_at <= rotated.previous_valid_until
    with pytest.raises(AuthenticationError, match="Expired"):
        TokenService(store).authenticate(old.access_token, now=old_record.expires_at)
    with pytest.raises(ValueError, match="24 hours"):
        control.rotate_access_token(
            rotated.credential.token_id,
            overlap=timedelta(hours=25),
        )


def test_memoryctl_exposes_revocation_and_rotation_operations() -> None:
    control, _ = control_plane()
    credential = control.issue_access_token("tenant-a", "user-alice")
    output = io.StringIO()

    run_cli(
        [
            "membership",
            "revoke-role",
            "--tenant-id",
            "tenant-a",
            "--principal-id",
            "user-alice",
            "--role",
            "knowledge_curator",
        ],
        control,
        output,
    )
    replacement = control.issue_access_token("tenant-a", "user-alice")
    run_cli(
        [
            "token",
            "rotate",
            "--token-id",
            replacement.token_id,
            "--overlap-minutes",
            "10",
            "--lifetime-days",
            "1",
        ],
        control,
        output,
    )
    run_cli(
        [
            "membership",
            "disable",
            "--tenant-id",
            "tenant-b",
            "--principal-id",
            "user-alice",
        ],
        control,
        output,
    )
    run_cli(["principal", "disable", "--id", "agent-helper"], control, output)

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    assert documents[0]["roles"] == ["tenant_member"]
    assert documents[1]["previous_token_id"] == replacement.token_id
    assert documents[1]["access_token"].startswith("mem1.")
    assert documents[2]["active"] is False
    assert documents[3] == {"active": False, "principal_id": "agent-helper"}
    with pytest.raises(AuthenticationError):
        control.authenticate(credential.access_token)
