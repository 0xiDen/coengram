from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from agent_memory_service.auth import AuthenticationError, TokenService
from agent_memory_service.cli import run_cli
from agent_memory_service.control import ControlModule, InMemoryControlStore


def _control() -> ControlModule:
    store = InMemoryControlStore()
    return ControlModule(store, TokenService(store))


def test_operator_bootstrap_cli_issues_one_time_operator_access_token() -> None:
    control = _control()
    output = io.StringIO()

    assert (
        run_cli(
            [
                "operator",
                "create",
                "--id",
                "operator-alice",
                "--name",
                "Alice Operator",
                "--role",
                "operator_admin",
            ],
            control,
            output,
        )
        == 0
    )
    assert (
        run_cli(
            [
                "operator",
                "token",
                "issue",
                "--operator-id",
                "operator-alice",
                "--lifetime-days",
                "7",
            ],
            control,
            output,
        )
        == 0
    )

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    created = documents[0]
    issued = documents[1]
    assert created == {
        "active": True,
        "name": "Alice Operator",
        "operator_id": "operator-alice",
        "roles": ["operator_admin"],
    }
    assert issued["access_token"].startswith("op1.")
    assert control.authenticate_operator(issued["access_token"]).operator_id == "operator-alice"

    listed = io.StringIO()
    assert (
        run_cli(
            ["operator", "token", "list", "--operator-id", "operator-alice"],
            control,
            listed,
        )
        == 0
    )
    listed_document = json.loads(listed.getvalue())
    assert listed_document["tokens"][0]["token_id"] == issued["token_id"]
    assert "access_token" not in listed.getvalue()


def test_operator_access_token_rotates_with_bounded_overlap() -> None:
    control = _control()
    control.create_operator(
        "operator-alice",
        "Alice Operator",
        frozenset({"operator_admin", "token_admin"}),
    )
    previous = control.issue_operator_access_token(
        "operator-alice",
        lifetime=timedelta(days=7),
    )

    rotated = control.rotate_operator_access_token(
        previous.token_id,
        overlap=timedelta(minutes=15),
        lifetime=timedelta(days=3),
    )

    assert rotated.credential.access_token.startswith("op1.")
    assert rotated.previous_token_id == previous.token_id
    assert rotated.previous_valid_until < previous.expires_at
    assert control.authenticate_operator(rotated.credential.access_token).roles == frozenset(
        {"operator_admin", "token_admin"}
    )


def test_last_operator_admin_cannot_be_disabled_or_stripped() -> None:
    control = _control()
    control.create_operator("operator-alice", "Alice", frozenset({"operator_admin"}))

    with pytest.raises(ValueError, match="final active operator_admin"):
        control.update_operator("operator-alice", active=False)
    with pytest.raises(ValueError, match="final active operator_admin"):
        control.update_operator("operator-alice", roles=frozenset({"audit_viewer"}))

    control.create_operator("operator-bob", "Bob", frozenset({"operator_admin"}))
    assert not control.update_operator("operator-alice", active=False).active


def test_admin_session_requires_csrf_for_mutation_and_expires_on_idle() -> None:
    control = _control()
    control.create_operator("operator-alice", "Alice", frozenset({"operator_admin"}))
    issued = control.issue_operator_access_token("operator-alice", lifetime=timedelta(days=7))
    now = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)

    session = control.create_admin_session(issued.access_token, now=now)

    assert session.session_token.startswith("adm1.")
    assert (
        control.authenticate_admin_session(
            session.session_token,
            csrf_token=session.csrf_token,
            require_csrf=True,
            now=now + timedelta(minutes=5),
        ).operator_id
        == "operator-alice"
    )
    with pytest.raises(AuthenticationError, match="CSRF"):
        control.authenticate_admin_session(
            session.session_token,
            csrf_token=None,
            require_csrf=True,
            now=now + timedelta(minutes=6),
        )
    with pytest.raises(AuthenticationError, match="expired"):
        control.authenticate_admin_session(
            session.session_token,
            csrf_token=session.csrf_token,
            require_csrf=True,
            now=now + timedelta(minutes=37),
        )
