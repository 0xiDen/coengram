from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from datetime import timedelta
from typing import cast

import pytest

from agent_memory_service.auth import AuthenticationError, TokenService
from agent_memory_service.backup import BackupOperator
from agent_memory_service.cli import run_cli
from agent_memory_service.control import ControlModule, InMemoryControlStore, MembershipRecord
from agent_memory_service.models import PrincipalKind


def test_public_cli_help_does_not_require_production_secrets() -> None:
    executable = shutil.which("coengramctl")
    assert executable is not None
    environment = {key: value for key, value in os.environ.items() if not key.startswith("MEMORY_")}
    result = subprocess.run(
        [executable, "--help"],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith("usage: coengramctl")


def test_restore_cleanup_requires_exact_confirmation_and_reports_operator() -> None:
    class CleanupBackup:
        def __init__(self) -> None:
            self.targets: list[str] = []

        def cleanup_restore_drill(self, target_id: str) -> None:
            self.targets.append(target_id)

    control = ControlModule(InMemoryControlStore(), TokenService(InMemoryControlStore()))
    cleanup = CleanupBackup()
    arguments = [
        "backup",
        "cleanup-restore-drill",
        "--target-id",
        "restore-drill-tenant-a-failed",
        "--operator-id",
        "operator-alice",
        "--confirm-target",
    ]
    with pytest.raises(ValueError, match="confirmation"):
        run_cli(
            [*arguments, "restore-drill-other"],
            control,
            backup=cast(BackupOperator, cleanup),
        )

    output = io.StringIO()
    assert (
        run_cli(
            [*arguments, "restore-drill-tenant-a-failed"],
            control,
            output,
            backup=cast(BackupOperator, cleanup),
        )
        == 0
    )
    assert cleanup.targets == ["restore-drill-tenant-a-failed"]
    assert json.loads(output.getvalue()) == {
        "operation": "cleanup-restore-drill",
        "operator_id": "operator-alice",
        "removed": True,
        "target_id": "restore-drill-tenant-a-failed",
    }


def test_operator_can_create_user_membership_and_one_time_tenant_token() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A Backend")
    output = io.StringIO()

    assert (
        run_cli(
            [
                "principal",
                "create",
                "--id",
                "user-alice",
                "--kind",
                "user",
                "--name",
                "Alice",
            ],
            control,
            output,
        )
        == 0
    )
    assert (
        run_cli(
            [
                "membership",
                "grant",
                "--tenant-id",
                "tenant-a",
                "--principal-id",
                "user-alice",
                "--role",
                "tenant_member",
            ],
            control,
            output,
        )
        == 0
    )
    assert (
        run_cli(
            ["token", "issue", "--tenant-id", "tenant-a", "--principal-id", "user-alice"],
            control,
            output,
        )
        == 0
    )

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    issued = documents[-1]
    assert issued["access_token"].startswith("mem1.")
    assert control.authenticate(issued["access_token"]).tenant_id == "tenant-a"

    listed = control.list_tokens("tenant-a", "user-alice")
    assert [record.token_id for record in listed] == [issued["token_id"]]
    assert not hasattr(listed[0], "access_token")


def test_direct_tenant_create_is_not_an_operator_command() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))

    with pytest.raises(SystemExit) as error:
        run_cli(
            ["tenant", "create", "--id", "tenant-a", "--name", "Product A Backend"],
            control,
            io.StringIO(),
        )

    assert error.value.code == 2
    assert store.get_tenant("tenant-a") is None


def test_token_can_be_revoked_immediately_by_operator() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A Backend")
    control.create_principal("user-alice", "Alice", "user")
    control.grant_membership("tenant-a", "user-alice", "tenant_member")
    credential = control.issue_access_token("tenant-a", "user-alice")

    assert control.revoke_access_token(credential.token_id) is True

    output = io.StringIO()
    assert (
        run_cli(
            ["token", "revoke", "--token-id", credential.token_id],
            control,
            output,
        )
        == 0
    )
    assert json.loads(output.getvalue())["revoked"] is True


def test_token_policy_maxima_and_last_use_signal_are_enforced() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A Backend")
    control.create_principal("user-alice", "Alice", "user")
    control.create_principal("agent-reader", "Reader", "agent")
    control.grant_membership("tenant-a", "user-alice", "tenant_member")
    control.grant_membership("tenant-a", "agent-reader", "tenant_member")
    store.configure_token_lifetime_days(
        "tenant-a",
        PrincipalKind.USER,
        7,
    )

    with pytest.raises(ValueError, match="policy maximum"):
        control.issue_access_token(
            "tenant-a",
            "user-alice",
            lifetime=timedelta(days=8),
        )
    with pytest.raises(ValueError, match="policy maximum"):
        control.issue_access_token(
            "tenant-a",
            "agent-reader",
            lifetime=timedelta(days=31),
        )

    credential = control.issue_access_token("tenant-a", "user-alice")
    assert control.list_tokens("tenant-a", "user-alice")[0].last_used_at is None
    control.authenticate(credential.access_token)
    assert control.list_tokens("tenant-a", "user-alice")[0].last_used_at is not None

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
    listed = json.loads(output.getvalue())
    assert listed["tokens"][0]["last_used_at"] is not None
    assert "access_token" not in output.getvalue()


def test_operator_can_choose_a_stricter_token_lifetime() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A Backend")
    control.create_principal("user-alice", "Alice", "user")
    control.grant_membership("tenant-a", "user-alice", "tenant_member")
    output = io.StringIO()

    run_cli(
        [
            "token",
            "issue",
            "--tenant-id",
            "tenant-a",
            "--principal-id",
            "user-alice",
            "--lifetime-days",
            "7",
        ],
        control,
        output,
    )

    assert json.loads(output.getvalue())["access_token"].startswith("mem1.")


def test_existing_token_fails_closed_when_membership_is_suspended() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A Backend")
    control.create_principal("user-alice", "Alice", "user")
    membership = control.grant_membership("tenant-a", "user-alice", "tenant_member")
    credential = control.issue_access_token("tenant-a", "user-alice")

    store.save_membership(
        MembershipRecord(
            tenant_id=membership.tenant_id,
            principal_id=membership.principal_id,
            roles=membership.roles,
            active=False,
        )
    )

    with pytest.raises(AuthenticationError):
        control.authenticate(credential.access_token)


def test_operator_manages_a_telegram_channel_binding() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A Backend")
    control.create_principal("user-alice", "Alice", "user")
    control.create_principal("agent-telegram", "Telegram Agent", "agent")
    control.grant_membership("tenant-a", "user-alice", "tenant_member")
    control.grant_membership("tenant-a", "agent-telegram", "tenant_member")
    control.create_delegation(
        "delegation-alice-telegram",
        tenant_id="tenant-a",
        agent_id="agent-telegram",
        subject_user_id="user-alice",
    )
    output = io.StringIO()

    run_cli(
        [
            "channel",
            "bind",
            "--id",
            "binding-alice-telegram",
            "--channel",
            "telegram",
            "--external-id",
            "123456789",
            "--delegation-id",
            "delegation-alice-telegram",
        ],
        control,
        output,
    )
    run_cli(
        ["channel", "inspect", "--channel", "telegram", "--external-id", "123456789"],
        control,
        output,
    )
    run_cli(
        ["channel", "disable", "--channel", "telegram", "--external-id", "123456789"],
        control,
        output,
    )

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    assert documents[0]["tenant_id"] == "tenant-a"
    assert documents[0]["user_id"] == "user-alice"
    assert documents[1]["active"] is True
    assert documents[2]["active"] is False

    output = io.StringIO()
    run_cli(
        ["channel", "remove", "--channel", "telegram", "--external-id", "123456789"],
        control,
        output,
    )
    assert json.loads(output.getvalue()) == {
        "channel": "telegram",
        "external_id": "123456789",
        "removed": True,
    }


def test_operator_creates_delegation_and_issues_bound_agent_token() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A Backend")
    control.create_principal("user-alice", "Alice", "user")
    control.create_principal("agent-synthesis", "Synthesis", "agent")
    control.grant_membership("tenant-a", "user-alice", "tenant_member")
    control.grant_membership("tenant-a", "agent-synthesis", "tenant_member")
    output = io.StringIO()

    run_cli(
        [
            "delegation",
            "create",
            "--id",
            "delegation-alice-synthesis",
            "--tenant-id",
            "tenant-a",
            "--agent-id",
            "agent-synthesis",
            "--subject-user-id",
            "user-alice",
        ],
        control,
        output,
    )
    run_cli(
        [
            "token",
            "issue-delegated",
            "--delegation-id",
            "delegation-alice-synthesis",
        ],
        control,
        output,
    )

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    session = control.authenticate(documents[-1]["access_token"])
    assert session.actor_id == "agent-synthesis"
    assert session.subject_user_id == "user-alice"
