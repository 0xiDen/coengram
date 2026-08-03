"""`coengramctl` operator CLI Adapter."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TextIO
from urllib.parse import quote

from agent_memory_service.backup import BackupOperator
from agent_memory_service.control import ControlModule, is_token_unused_for_30_days
from agent_memory_service.decommission import (
    DecommissionCancellationDocument,
    DecommissionConfirmationDocument,
    DecommissionFinalizationDocument,
    DecommissionRecord,
    DecommissionRequestDocument,
    DecommissionService,
    DecommissionTombstone,
)
from agent_memory_service.manifest import TenantManifest
from agent_memory_service.provisioning import ProvisioningPlan, ProvisioningState, TenantProvisioner
from agent_memory_service.schema import CONTROL_SCHEMA_REVISION, TENANT_SCHEMA_REVISION
from agent_memory_service.tenant_migration import (
    ActiveMigrationPlan,
    ActiveMigrationState,
    ActiveTenantMigrationService,
)


class DeadLetterRedriveService(Protocol):
    def redrive(
        self,
        *,
        tenant_id: str,
        event_id: str,
        operator_id: str,
    ) -> object: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coengramctl")
    resources = parser.add_subparsers(dest="resource", required=True)

    tenant = resources.add_parser("tenant").add_subparsers(dest="action", required=True)
    tenant_plan = tenant.add_parser("plan")
    tenant_plan.add_argument("--manifest", required=True)
    tenant_apply = tenant.add_parser("apply")
    tenant_apply.add_argument("--manifest", required=True)
    tenant_apply.add_argument(
        "--confirm",
        required=True,
        help="repeat the immutable Tenant ID to authorize infrastructure mutation",
    )

    principal = resources.add_parser("principal").add_subparsers(dest="action", required=True)
    principal_create = principal.add_parser("create")
    principal_create.add_argument("--id", required=True)
    principal_create.add_argument("--kind", choices=("user", "agent"), required=True)
    principal_create.add_argument("--name", required=True)
    principal.add_parser("list")
    principal_inspect = principal.add_parser("inspect")
    principal_inspect.add_argument("--id", required=True)
    principal_update = principal.add_parser("update")
    principal_update.add_argument("--id", required=True)
    principal_update.add_argument("--name")
    principal_update.add_argument("--active", choices=("true", "false"))
    principal_disable = principal.add_parser("disable")
    principal_disable.add_argument("--id", required=True)

    membership = resources.add_parser("membership").add_subparsers(dest="action", required=True)
    membership_grant = membership.add_parser("grant")
    membership_grant.add_argument("--tenant-id", required=True)
    membership_grant.add_argument("--principal-id", required=True)
    membership_grant.add_argument("--role", required=True)
    membership_list = membership.add_parser("list")
    membership_list.add_argument("--tenant-id", required=True)
    membership_inspect = membership.add_parser("inspect")
    membership_inspect.add_argument("--tenant-id", required=True)
    membership_inspect.add_argument("--principal-id", required=True)
    membership_update = membership.add_parser("update")
    membership_update.add_argument("--tenant-id", required=True)
    membership_update.add_argument("--principal-id", required=True)
    membership_update.add_argument("--role", action="append")
    membership_update.add_argument("--active", choices=("true", "false"))
    membership_disable = membership.add_parser("disable")
    membership_disable.add_argument("--tenant-id", required=True)
    membership_disable.add_argument("--principal-id", required=True)
    membership_revoke_role = membership.add_parser("revoke-role")
    membership_revoke_role.add_argument("--tenant-id", required=True)
    membership_revoke_role.add_argument("--principal-id", required=True)
    membership_revoke_role.add_argument("--role", required=True)

    token = resources.add_parser("token").add_subparsers(dest="action", required=True)
    token_issue = token.add_parser("issue")
    token_issue.add_argument("--tenant-id", required=True)
    token_issue.add_argument("--principal-id", required=True)
    token_issue.add_argument("--lifetime-days", type=int)
    token_delegated = token.add_parser("issue-delegated")
    token_delegated.add_argument("--delegation-id", required=True)
    token_delegated.add_argument("--lifetime-days", type=int)
    token_revoke = token.add_parser("revoke")
    token_revoke.add_argument("--token-id", required=True)
    token_list = token.add_parser("list")
    token_list.add_argument("--tenant-id", required=True)
    token_list.add_argument("--principal-id", required=True)
    token_rotate = token.add_parser("rotate")
    token_rotate.add_argument("--token-id", required=True)
    token_rotate.add_argument("--overlap-minutes", type=int, required=True)
    token_rotate.add_argument("--lifetime-days", type=int)

    delegation = resources.add_parser("delegation").add_subparsers(dest="action", required=True)
    delegation_create = delegation.add_parser("create")
    delegation_create.add_argument("--id", required=True)
    delegation_create.add_argument("--tenant-id", required=True)
    delegation_create.add_argument("--agent-id", required=True)
    delegation_create.add_argument("--subject-user-id", required=True)
    delegation_list = delegation.add_parser("list")
    delegation_list.add_argument("--tenant-id", required=True)
    delegation_inspect = delegation.add_parser("inspect")
    delegation_inspect.add_argument("--id", required=True)
    delegation_update = delegation.add_parser("update")
    delegation_update.add_argument("--id", required=True)
    delegation_update.add_argument("--active", choices=("true", "false"), required=True)
    delegation_revoke = delegation.add_parser("revoke")
    delegation_revoke.add_argument("--id", required=True)

    channel = resources.add_parser("channel").add_subparsers(dest="action", required=True)
    channel_bind = channel.add_parser("bind")
    channel_bind.add_argument("--id", required=True)
    channel_bind.add_argument("--channel", choices=("telegram",), required=True)
    channel_bind.add_argument("--external-id", required=True)
    channel_bind.add_argument("--delegation-id", required=True)
    for action in ("inspect", "disable", "remove"):
        command = channel.add_parser(action)
        command.add_argument("--channel", choices=("telegram",), required=True)
        command.add_argument("--external-id", required=True)

    decommission = resources.add_parser("decommission").add_subparsers(dest="action", required=True)
    decommission_request = decommission.add_parser("request")
    decommission_request.add_argument("--input", required=True)
    for action in ("confirm", "cancel", "finalize"):
        command = decommission.add_parser(action)
        command.add_argument("--input", required=True)
        command.add_argument(
            "--confirm-tenant-id",
            required=True,
            help="repeat the immutable Tenant ID from the JSON document",
        )

    backup = resources.add_parser("backup").add_subparsers(dest="action", required=True)
    for action in ("plan", "create"):
        command = backup.add_parser(action)
        command.add_argument("--tenant-id", required=True)
        command.add_argument("--backup-id")
    backup_verify = backup.add_parser("verify")
    backup_verify.add_argument("--manifest", required=True)
    backup_restore = backup.add_parser("restore-drill")
    backup_restore.add_argument("--manifest", required=True)
    backup_restore.add_argument("--target-id", required=True)
    backup_restore.add_argument("--operator-id", required=True)
    backup_restore.add_argument(
        "--confirm-target",
        required=True,
        help="repeat the isolated restore target identifier",
    )
    backup_restore_latest = backup.add_parser("restore-latest-drill")
    backup_restore_latest.add_argument("--tenant-id", required=True)
    backup_restore_latest.add_argument("--target-id", required=True)
    backup_restore_latest.add_argument("--operator-id", required=True)
    backup_restore_latest.add_argument(
        "--confirm-target",
        required=True,
        help="repeat the isolated restore target identifier",
    )
    backup_cleanup = backup.add_parser("cleanup-restore-drill")
    backup_cleanup.add_argument("--target-id", required=True)
    backup_cleanup.add_argument("--operator-id", required=True)
    backup_cleanup.add_argument(
        "--confirm-target",
        required=True,
        help="repeat the preserved failed-drill target identifier",
    )
    for action in ("status", "retention-plan"):
        command = backup.add_parser(action)
        command.add_argument("--tenant-id", required=True)
    backup_barrier_status = backup.add_parser("barrier-status")
    backup_barrier_status.add_argument("--tenant-id", required=True)
    backup_barrier_recovery = backup.add_parser("recover-barrier")
    backup_barrier_recovery.add_argument("--tenant-id", required=True)
    backup_barrier_recovery.add_argument("--barrier-id", required=True)
    backup_barrier_recovery.add_argument(
        "--confirm",
        required=True,
        help="repeat TENANT_ID:BARRIER_ID to reactivate the exact abandoned Tenant",
    )
    backup_retention_apply = backup.add_parser("retention-apply")
    backup_retention_apply.add_argument("--tenant-id", required=True)
    backup_retention_apply.add_argument(
        "--confirm",
        required=True,
        help="repeat the immutable Tenant ID to authorize exact backup-set deletion",
    )

    migration = resources.add_parser("migration").add_subparsers(dest="action", required=True)
    for action in ("plan", "apply", "resume"):
        command = migration.add_parser(action)
        command.add_argument("--tenant-id", required=True)
        if action != "plan":
            command.add_argument(
                "--confirm",
                required=True,
                help="repeat the immutable active Tenant ID",
            )
    outbox = resources.add_parser("outbox").add_subparsers(dest="action", required=True)
    outbox_redrive = outbox.add_parser("redrive")
    outbox_redrive.add_argument("--tenant-id", required=True)
    outbox_redrive.add_argument("--event-id", required=True)
    outbox_redrive.add_argument("--operator-id", required=True)
    outbox_redrive.add_argument(
        "--confirm",
        required=True,
        help="repeat the immutable Tenant ID to schedule exact redelivery",
    )
    return parser


def run_cli(
    argv: Sequence[str],
    control: ControlModule,
    output: TextIO = sys.stdout,
    *,
    provisioner: TenantProvisioner | None = None,
    decommissioner: DecommissionService | None = None,
    backup: BackupOperator | None = None,
    tenant_migrator: ActiveTenantMigrationService | None = None,
    dead_letters: DeadLetterRedriveService | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if args.resource == "outbox":
        if dead_letters is None:
            raise RuntimeError("Dead-letter recovery is not configured")
        if args.confirm != args.tenant_id:
            raise ValueError("Outbox confirmation must match the immutable Tenant ID")
        result = dead_letters.redrive(
            tenant_id=args.tenant_id,
            event_id=args.event_id,
            operator_id=args.operator_id,
        )
        from agent_memory_service.outbox import DeadLetterRedriveResult

        if not isinstance(result, DeadLetterRedriveResult):
            raise TypeError("Expected a dead-letter redrive result")
        _write(
            output,
            {
                "operation": "redrive",
                "tenant_id": result.tenant_id,
                "event_id": result.event_id,
                "event_type": result.event_type,
                "state": result.state,
            },
        )
    elif args.resource == "migration":
        if tenant_migrator is None:
            raise RuntimeError("Active Tenant migration is not configured")
        if args.action == "plan":
            migration_plan = tenant_migrator.plan(args.tenant_id)
            _write(output, _active_migration_plan_document(migration_plan))
        else:
            if args.confirm != args.tenant_id:
                raise ValueError("Migration confirmation must match the immutable active Tenant ID")
            if args.action == "apply":
                migration_state = tenant_migrator.apply(args.tenant_id)
            else:
                migration_state = tenant_migrator.resume(args.tenant_id)
            _write(
                output,
                {
                    "operation": args.action,
                    "result": _active_migration_state_document(migration_state),
                },
            )
    elif args.resource == "backup":
        if backup is None:
            raise RuntimeError("Host backup is not configured")
        if args.action == "plan":
            backup_plan = backup.plan(args.tenant_id, backup_id=args.backup_id)
            _write(output, {"operation": "plan", **backup_plan.model_dump(mode="json")})
        elif args.action == "create":
            backup_manifest = backup.create(args.tenant_id, backup_id=args.backup_id)
            _write(
                output,
                {
                    "operation": "create",
                    "manifest": backup_manifest.model_dump(mode="json"),
                },
            )
        elif args.action == "verify":
            verified_manifest = backup.verify(Path(args.manifest))
            _write(
                output,
                {
                    "operation": "verify",
                    "backup_id": verified_manifest.backup_id,
                    "valid": True,
                },
            )
        elif args.action in {"restore-drill", "restore-latest-drill"}:
            if args.confirm_target != args.target_id:
                raise ValueError(
                    "Restore drill confirmation must match the isolated target identifier"
                )
            if args.action == "restore-drill":
                record = backup.restore_drill(
                    Path(args.manifest),
                    target_id=args.target_id,
                    operator_id=args.operator_id,
                )
            else:
                record = backup.restore_latest_drill(
                    args.tenant_id,
                    target_id=args.target_id,
                    operator_id=args.operator_id,
                )
            _write(
                output,
                {
                    "operation": args.action,
                    "record": record.model_dump(mode="json"),
                },
            )
        elif args.action == "cleanup-restore-drill":
            if args.confirm_target != args.target_id:
                raise ValueError(
                    "Restore cleanup confirmation must match the isolated target identifier"
                )
            backup.cleanup_restore_drill(args.target_id)
            _write(
                output,
                {
                    "operation": "cleanup-restore-drill",
                    "target_id": args.target_id,
                    "operator_id": args.operator_id,
                    "removed": True,
                },
            )
        elif args.action == "status":
            _write(
                output,
                {
                    "operation": "status",
                    "health": backup.health(args.tenant_id).model_dump(mode="json"),
                },
            )
        elif args.action == "barrier-status":
            barrier = backup.barrier_status(args.tenant_id)
            _write(
                output,
                {
                    "operation": "barrier-status",
                    "tenant_id": args.tenant_id,
                    "barrier": (
                        None
                        if barrier is None
                        else {
                            "tenant_id": barrier.tenant_id,
                            "barrier_id": barrier.barrier_id,
                            "started_at": barrier.started_at.isoformat(),
                        }
                    ),
                },
            )
        elif args.action == "recover-barrier":
            expected_confirmation = f"{args.tenant_id}:{args.barrier_id}"
            if args.confirm != expected_confirmation:
                raise ValueError("Backup barrier confirmation must match TENANT_ID:BARRIER_ID")
            backup.recover_barrier(args.tenant_id, args.barrier_id)
            _write(
                output,
                {
                    "operation": "recover-barrier",
                    "tenant_id": args.tenant_id,
                    "barrier_id": args.barrier_id,
                    "recovered": True,
                },
            )
        elif args.action == "retention-plan":
            _write(
                output,
                {
                    "operation": "retention-plan",
                    "tenant_id": args.tenant_id,
                    "plan": backup.retention_plan(args.tenant_id).model_dump(mode="json"),
                },
            )
        else:
            if args.confirm != args.tenant_id:
                raise ValueError("Retention confirmation must match the immutable Tenant ID")
            _write(
                output,
                {
                    "operation": "retention-apply",
                    "tenant_id": args.tenant_id,
                    "result": backup.apply_retention(args.tenant_id).model_dump(mode="json"),
                },
            )
    elif args.resource == "decommission":
        if decommissioner is None:
            raise RuntimeError("Host Tenant decommissioning is not configured")
        document_path = Path(args.input)
        if args.action == "request":
            document = _load_decommission_request(document_path)
            decommission_result: DecommissionRecord | DecommissionTombstone = (
                decommissioner.request(
                    request_id=document.request_id,
                    resources=document.resources,
                    actor_id=document.actor_id,
                    reason=document.reason,
                    requested_at=document.requested_at,
                    protection_policy=document.protection_policy,
                )
            )
        elif args.action == "confirm":
            confirmation = _load_decommission_confirmation(document_path)
            _require_exact_cli_confirmation(args.confirm_tenant_id, confirmation.tenant_id)
            decommission_result = decommissioner.confirm(
                request_id=confirmation.request_id,
                tenant_id=confirmation.tenant_id,
                actor_id=confirmation.actor_id,
                evidence=confirmation.evidence,
                confirmed_at=confirmation.confirmed_at,
            )
        elif args.action == "cancel":
            cancellation = _load_decommission_cancellation(document_path)
            _require_exact_cli_confirmation(args.confirm_tenant_id, cancellation.tenant_id)
            decommission_result = decommissioner.cancel(
                request_id=cancellation.request_id,
                tenant_id=cancellation.tenant_id,
                actor_id=cancellation.actor_id,
                cancelled_at=cancellation.cancelled_at,
            )
        else:
            finalization = _load_decommission_finalization(document_path)
            _require_exact_cli_confirmation(args.confirm_tenant_id, finalization.tenant_id)
            decommission_result = decommissioner.finalize(
                request_id=finalization.request_id,
                tenant_id=finalization.tenant_id,
                finalized_at=finalization.finalized_at,
            )
        _write(output, _decommission_document(decommission_result, operation=args.action))
    elif args.resource == "tenant" and args.action in {"plan", "apply"}:
        if provisioner is None:
            raise RuntimeError("Host Tenant provisioning is not configured")
        tenant_manifest = _load_manifest(Path(args.manifest))
        provisioning_plan = provisioner.plan(tenant_manifest)
        if args.action == "plan":
            _write(output, _plan_document(provisioning_plan))
        else:
            if args.confirm != tenant_manifest.tenant_id:
                raise ValueError("Tenant apply confirmation must match the immutable Tenant ID")
            provisioning_result = provisioner.apply(tenant_manifest)
            _write(
                output,
                {
                    "operation": "apply",
                    "plan": _plan_document(provisioning_plan),
                    "result": _state_document(provisioning_result),
                },
            )
    elif args.resource == "principal" and args.action == "create":
        principal_record = control.create_principal(args.id, args.name, args.kind)
        _write(output, _principal_document(principal_record))
    elif args.resource == "principal" and args.action == "list":
        _write(
            output,
            {"principals": [_principal_document(item) for item in control.list_principals()]},
        )
    elif args.resource == "principal" and args.action == "inspect":
        _write(output, _principal_document(control.inspect_principal(args.id)))
    elif args.resource == "principal" and args.action == "update":
        _write(
            output,
            _principal_document(
                control.update_principal(
                    args.id,
                    name=args.name,
                    active=None if args.active is None else args.active == "true",
                )
            ),
        )
    elif args.resource == "principal" and args.action == "disable":
        principal_record = control.disable_principal(args.id)
        _write(
            output,
            {
                "principal_id": principal_record.principal_id,
                "active": principal_record.active,
            },
        )
    elif args.resource == "membership" and args.action == "grant":
        membership_record = control.grant_membership(args.tenant_id, args.principal_id, args.role)
        _write(
            output,
            {
                "tenant_id": membership_record.tenant_id,
                "principal_id": membership_record.principal_id,
                "roles": sorted(membership_record.roles),
            },
        )
    elif args.resource == "membership" and args.action == "list":
        _write(
            output,
            {
                "tenant_id": args.tenant_id,
                "memberships": [
                    _membership_document(item) for item in control.list_memberships(args.tenant_id)
                ],
            },
        )
    elif args.resource == "membership" and args.action == "inspect":
        _write(
            output,
            _membership_document(control.inspect_membership(args.tenant_id, args.principal_id)),
        )
    elif args.resource == "membership" and args.action == "update":
        _write(
            output,
            _membership_document(
                control.update_membership(
                    args.tenant_id,
                    args.principal_id,
                    roles=None if args.role is None else frozenset(args.role),
                    active=None if args.active is None else args.active == "true",
                )
            ),
        )
    elif args.resource == "membership" and args.action == "disable":
        membership_record = control.disable_membership(args.tenant_id, args.principal_id)
        _write(output, _membership_document(membership_record))
    elif args.resource == "membership" and args.action == "revoke-role":
        membership_record = control.revoke_membership_role(
            args.tenant_id, args.principal_id, args.role
        )
        _write(output, _membership_document(membership_record))
    elif args.resource == "token" and args.action == "issue":
        lifetime = None if args.lifetime_days is None else timedelta(days=args.lifetime_days)
        credential = control.issue_access_token(
            args.tenant_id,
            args.principal_id,
            lifetime=lifetime,
        )
        _write(output, _credential_document(credential))
    elif args.resource == "token" and args.action == "issue-delegated":
        if args.lifetime_days is None:
            credential = control.issue_delegated_access_token(args.delegation_id)
        else:
            credential = control.issue_delegated_access_token(
                args.delegation_id,
                lifetime=timedelta(days=args.lifetime_days),
            )
        _write(output, _credential_document(credential))
    elif args.resource == "token" and args.action == "revoke":
        _write(
            output,
            {
                "token_id": args.token_id,
                "revoked": control.revoke_access_token(args.token_id),
            },
        )
    elif args.resource == "token" and args.action == "rotate":
        replacement_lifetime = (
            None if args.lifetime_days is None else timedelta(days=args.lifetime_days)
        )
        rotated = control.rotate_access_token(
            args.token_id,
            overlap=timedelta(minutes=args.overlap_minutes),
            lifetime=replacement_lifetime,
        )
        _write(
            output,
            {
                **_credential_document(rotated.credential),
                "previous_token_id": rotated.previous_token_id,
                "previous_valid_until": rotated.previous_valid_until.isoformat(),
            },
        )
    elif args.resource == "token" and args.action == "list":
        checked_at = datetime.now(UTC)
        _write(
            output,
            {
                "tenant_id": args.tenant_id,
                "principal_id": args.principal_id,
                "tokens": [
                    {
                        "token_id": record.token_id,
                        "expires_at": record.expires_at.isoformat(),
                        "revoked_at": (
                            None if record.revoked_at is None else record.revoked_at.isoformat()
                        ),
                        "last_used_at": (
                            None if record.last_used_at is None else record.last_used_at.isoformat()
                        ),
                        "unused_for_30_days": (is_token_unused_for_30_days(record, now=checked_at)),
                    }
                    for record in control.list_tokens(args.tenant_id, args.principal_id)
                ],
            },
        )
    elif args.resource == "channel" and args.action == "bind":
        binding = control.create_channel_binding(
            args.id,
            channel=args.channel,
            external_id=args.external_id,
            delegation_id=args.delegation_id,
        )
        _write(output, _binding_document(binding))
    elif args.resource == "delegation" and args.action == "create":
        delegation_record = control.create_delegation(
            args.id,
            tenant_id=args.tenant_id,
            agent_id=args.agent_id,
            subject_user_id=args.subject_user_id,
        )
        _write(output, _delegation_document(delegation_record))
    elif args.resource == "delegation" and args.action == "list":
        _write(
            output,
            {
                "tenant_id": args.tenant_id,
                "delegations": [
                    _delegation_document(item) for item in control.list_delegations(args.tenant_id)
                ],
            },
        )
    elif args.resource == "delegation" and args.action == "inspect":
        _write(output, _delegation_document(control.inspect_delegation(args.id)))
    elif args.resource == "delegation" and args.action == "update":
        _write(
            output,
            _delegation_document(control.update_delegation(args.id, active=args.active == "true")),
        )
    elif args.resource == "delegation" and args.action == "revoke":
        delegation_record = control.revoke_delegation(args.id)
        _write(
            output,
            {
                "delegation_id": delegation_record.delegation_id,
                "tenant_id": delegation_record.tenant_id,
                "active": delegation_record.active,
            },
        )
    elif args.resource == "channel" and args.action == "inspect":
        _write(
            output,
            _binding_document(control.inspect_channel_binding(args.channel, args.external_id)),
        )
    elif args.resource == "channel" and args.action == "disable":
        _write(
            output,
            _binding_document(control.disable_channel_binding(args.channel, args.external_id)),
        )
    elif args.resource == "channel" and args.action == "remove":
        _write(
            output,
            {
                "channel": args.channel,
                "external_id": args.external_id,
                "removed": control.remove_channel_binding(args.channel, args.external_id),
            },
        )
    else:  # pragma: no cover - argparse prevents this branch
        raise AssertionError("Unhandled operator command")
    return 0


def _write(output: TextIO, document: object) -> None:
    output.write(json.dumps(document, sort_keys=True) + "\n")


def _binding_document(binding: object) -> dict[str, object]:
    from agent_memory_service.control import ChannelBindingRecord

    if not isinstance(binding, ChannelBindingRecord):
        raise TypeError("Expected a Channel Binding")
    return {
        "binding_id": binding.binding_id,
        "channel": binding.channel,
        "external_id": binding.external_id,
        "tenant_id": binding.tenant_id,
        "user_id": binding.user_id,
        "agent_id": binding.agent_id,
        "delegation_id": binding.delegation_id,
        "active": binding.active,
    }


def _credential_document(credential: object) -> dict[str, object]:
    from agent_memory_service.auth import IssuedCredential

    if not isinstance(credential, IssuedCredential):
        raise TypeError("Expected an issued credential")
    return {
        "token_id": credential.token_id,
        "access_token": credential.access_token,
        "expires_at": credential.expires_at.isoformat(),
        "warning": "This Access Token is shown once; store it securely.",
    }


def _membership_document(membership: object) -> dict[str, object]:
    from agent_memory_service.control import MembershipRecord

    if not isinstance(membership, MembershipRecord):
        raise TypeError("Expected a Tenant Membership")
    return {
        "tenant_id": membership.tenant_id,
        "principal_id": membership.principal_id,
        "roles": sorted(membership.roles),
        "active": membership.active,
    }


def _principal_document(principal: object) -> dict[str, object]:
    from agent_memory_service.control import PrincipalRecord

    if not isinstance(principal, PrincipalRecord):
        raise TypeError("Expected a Principal")
    return {
        "principal_id": principal.principal_id,
        "name": principal.name,
        "kind": principal.kind.value,
        "active": principal.active,
    }


def _delegation_document(delegation: object) -> dict[str, object]:
    from agent_memory_service.control import DelegationRecord

    if not isinstance(delegation, DelegationRecord):
        raise TypeError("Expected a Delegation")
    return {
        "delegation_id": delegation.delegation_id,
        "tenant_id": delegation.tenant_id,
        "agent_id": delegation.agent_id,
        "subject_user_id": delegation.subject_user_id,
        "active": delegation.active,
    }


def _load_manifest(path: Path) -> TenantManifest:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Tenant Manifest path must identify a regular file")
    if path.stat().st_size > 1_000_000:
        raise ValueError("Tenant Manifest exceeds the operator input limit")
    return TenantManifest.import_json(path.read_text(encoding="utf-8"))


def _read_operator_document(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Operator input path must identify a regular file")
    if path.stat().st_size > 1_000_000:
        raise ValueError("Operator input exceeds the 1 MB limit")
    return path.read_text(encoding="utf-8")


def _load_decommission_request(path: Path) -> DecommissionRequestDocument:
    return DecommissionRequestDocument.model_validate_json(_read_operator_document(path))


def _load_decommission_confirmation(path: Path) -> DecommissionConfirmationDocument:
    return DecommissionConfirmationDocument.model_validate_json(_read_operator_document(path))


def _load_decommission_cancellation(path: Path) -> DecommissionCancellationDocument:
    return DecommissionCancellationDocument.model_validate_json(_read_operator_document(path))


def _load_decommission_finalization(path: Path) -> DecommissionFinalizationDocument:
    return DecommissionFinalizationDocument.model_validate_json(_read_operator_document(path))


def _require_exact_cli_confirmation(actual: str, expected: str) -> None:
    if actual != expected:
        raise ValueError("Decommission confirmation must match the immutable Tenant ID")


def _decommission_document(
    result: DecommissionRecord | DecommissionTombstone, *, operation: str
) -> dict[str, object]:
    if isinstance(result, DecommissionTombstone):
        return {
            "operation": operation,
            "request_id": result.request_id,
            "tenant_id": result.tenant_id,
            "status": result.status,
            "destroyed_at": result.destroyed_at.isoformat(),
        }
    return {
        "operation": operation,
        "request_id": result.request_id,
        "tenant_id": result.resources.tenant_id,
        "state": result.state.value,
        "revision": result.revision,
        "suspension_steps": [step.value for step in result.suspension_steps],
        "confirmed_by": result.confirmed_by,
        "grace_ends_at": (
            None if result.grace_ends_at is None else result.grace_ends_at.isoformat()
        ),
        "destruction_steps": [step.value for step in result.destruction_steps],
        "last_failed_action": result.last_failed_action,
    }


def _plan_document(plan: ProvisioningPlan) -> dict[str, object]:
    return {
        "operation": "plan",
        "tenant_id": plan.tenant_id,
        "manifest_fingerprint": plan.manifest_fingerprint,
        "current_status": plan.current_status.value,
        "noop": plan.is_noop,
        "steps": [
            {
                "step": item.step.value,
                "action": item.action,
                "completed": item.completed,
            }
            for item in plan.steps
        ],
    }


def _state_document(state: ProvisioningState) -> dict[str, object]:
    return {
        "tenant_id": state.tenant_id,
        "status": state.status.value,
        "completed_steps": [step.value for step in state.completed_steps],
        "failed_step": None if state.failed_step is None else state.failed_step.value,
        "failure_code": state.failure_code,
        "attempt": state.attempt,
    }


def _active_migration_plan_document(plan: ActiveMigrationPlan) -> dict[str, object]:
    return {
        "operation": "plan",
        "tenant_id": plan.tenant_id,
        "migration_id": plan.migration_id,
        "postgres_target_version": plan.postgres_target_version,
        "neo4j_target_version": plan.neo4j_target_version,
        "current_status": plan.current_status.value,
        "noop": plan.is_noop,
        "steps": [
            {
                "step": item.step.value,
                "action": item.action,
                "completed": item.completed,
            }
            for item in plan.steps
        ],
    }


def _active_migration_state_document(state: ActiveMigrationState) -> dict[str, object]:
    return {
        "tenant_id": state.tenant_id,
        "migration_id": state.migration_id,
        "status": state.status.value,
        "completed_steps": [step.value for step in state.completed_steps],
        "failed_step": None if state.failed_step is None else state.failed_step.value,
        "failure_code": state.failure_code,
        "attempt": state.attempt,
        "revision": state.revision,
    }


def main() -> None:
    arguments = sys.argv[1:]
    # Help is a documentation path, not an operator action.  Let argparse render
    # help before loading protected production configuration so a freshly
    # installed CLI remains discoverable on any host.
    if "-h" in arguments or "--help" in arguments:
        _parser().parse_args(arguments)
        return

    from agent_memory_service.auth import TokenService
    from agent_memory_service.platform import PlatformConfig
    from agent_memory_service.stores.postgres_control import PostgresControlStore

    config = PlatformConfig.from_env()
    store = PostgresControlStore(config.control_database_url)
    control = ControlModule(store, TokenService(store))
    provisioner = None
    decommissioner = None
    backup_operator = None
    tenant_migrator = None
    dead_letters = None
    if len(arguments) >= 2 and arguments[0] == "tenant" and arguments[1] in {"plan", "apply"}:
        from agent_memory_service.host_provisioning import create_host_tenant_provisioner

        repository_root = Path(os.getenv("MEMORY_REPOSITORY_ROOT", Path.cwd()))
        admin_user = os.getenv("MEMORY_POSTGRES_ADMIN_USER", "postgres")
        admin_password = _secret_from_environment("MEMORY_POSTGRES_ADMIN_PASSWORD_FILE")
        admin_database_url = (
            f"postgresql://{quote(admin_user, safe='')}:{quote(admin_password, safe='')}@"
            f"{config.tenant_postgres_host}:{config.tenant_postgres_port}/postgres"
        )
        provisioner = create_host_tenant_provisioner(
            control_database_url=config.control_database_url,
            postgres_admin_url=admin_database_url,
            postgres_host=config.tenant_postgres_host,
            postgres_port=config.tenant_postgres_port,
            secrets_root=config.tenant_credentials_dir,
            tenant_secrets_group_id=_required_positive_integer("MEMORY_TENANT_SECRETS_GID"),
            repository_root=repository_root,
            telemetry_pseudonymizer=config.telemetry_pseudonymizer,
        )
    if len(arguments) >= 2 and arguments[0] == "decommission":
        from agent_memory_service.host_decommission import create_host_decommission_service

        repository_root = Path(os.getenv("MEMORY_REPOSITORY_ROOT", Path.cwd()))
        evidence_root = Path(_required_environment("MEMORY_RECOVERY_EVIDENCE_DIR"))
        admin_user = os.getenv("MEMORY_POSTGRES_ADMIN_USER", "postgres")
        admin_password = _secret_from_environment("MEMORY_POSTGRES_ADMIN_PASSWORD_FILE")
        admin_database_url = (
            f"postgresql://{quote(admin_user, safe='')}:{quote(admin_password, safe='')}@"
            f"{config.tenant_postgres_host}:{config.tenant_postgres_port}/postgres"
        )
        decommissioner = create_host_decommission_service(
            control_database_url=config.control_database_url,
            postgres_admin_url=admin_database_url,
            secrets_root=config.tenant_credentials_dir,
            evidence_root=evidence_root,
            repository_root=repository_root,
            postgres_host=config.tenant_postgres_host,
            postgres_port=config.tenant_postgres_port,
            telemetry_pseudonymizer=config.telemetry_pseudonymizer,
        )
    if len(arguments) >= 2 and arguments[0] == "backup":
        from agent_memory_service.backup_barrier import PostgresBackupConsistencyBarrier
        from agent_memory_service.host_backup import (
            HostBackupConfig,
            HostBackupOrchestrator,
            LocalBackupArtifactPublisher,
            PostgresBackupExpectationCollector,
            PostgresBackupSource,
        )
        from agent_memory_service.host_provisioning import SubprocessHostCommandRunner
        from agent_memory_service.host_restore import (
            HostRestoreConfig,
            HostRestoreDrillExecutor,
        )
        from agent_memory_service.restore_verify import DockerCanonicalRestoreVerifier
        from agent_memory_service.stores.postgres_decommission import (
            PostgresDecommissionRetentionAdapter,
        )

        repository_root = Path(os.getenv("MEMORY_REPOSITORY_ROOT", Path.cwd()))
        control_password_file = Path(_required_environment("MEMORY_CONTROL_DATABASE_PASSWORD_FILE"))
        age_identity = os.getenv("MEMORY_BACKUP_AGE_IDENTITY_FILE", "").strip()
        postgres_version = os.getenv("POSTGRES_VERSION", "17.6-alpine")
        neo4j_version = os.getenv("NEO4J_VERSION", "5.26.28-community")
        postgres_digest = os.getenv(
            "POSTGRES_DIGEST",
            "sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94",
        )
        neo4j_digest = os.getenv(
            "NEO4J_DIGEST",
            "sha256:362542416de6c09a971484d1893878016cc3b5cdec166e54b1c824a220ecd6b9",
        )
        command_runner = SubprocessHostCommandRunner(
            repository_root,
            timeout_seconds=3600,
        )
        restore_executor = None
        restore_workspace = os.getenv("MEMORY_RESTORE_WORKSPACE_DIR", "").strip()
        verifier_image = os.getenv("MEMORY_PLATFORM_IMAGE", "").strip()
        if age_identity and restore_workspace and verifier_image:
            restore_executor = HostRestoreDrillExecutor(
                runner=command_runner,
                verifier=DockerCanonicalRestoreVerifier(
                    runner=command_runner,
                    image=verifier_image,
                ),
                config=HostRestoreConfig(
                    workspace_root=Path(restore_workspace),
                    postgres_image=f"postgres:{postgres_version}@{postgres_digest}",
                    postgres_store_version=postgres_version,
                    neo4j_image=f"neo4j:{neo4j_version}@{neo4j_digest}",
                    neo4j_store_version=neo4j_version,
                    verifier_embedding_model=config.embedding_model,
                ),
            )
        backup_config = HostBackupConfig(
            staging_directory=Path(_required_environment("MEMORY_BACKUP_STAGING_DIR")),
            artifact_directory=Path(_required_environment("MEMORY_BACKUP_ARTIFACT_DIR")),
            tenant_compose_file=repository_root / "deploy" / "tenant.compose.yaml",
            tenant_secrets_directory=config.tenant_credentials_dir,
            age_recipients_file=Path(_required_environment("MEMORY_BACKUP_AGE_RECIPIENTS_FILE")),
            age_identity_file=Path(age_identity) if age_identity else None,
            age_key_id=_required_environment("MEMORY_BACKUP_AGE_KEY_ID"),
            control_postgres=PostgresBackupSource(
                host=os.getenv("MEMORY_CONTROL_DATABASE_HOST", "postgres"),
                port=config.tenant_postgres_port,
                database=os.getenv("MEMORY_CONTROL_DATABASE_NAME", "memory_control"),
                user=os.getenv("MEMORY_CONTROL_DATABASE_USER", "memory_control"),
                password_file=control_password_file,
            ),
            tenant_postgres_host=config.tenant_postgres_host,
            tenant_postgres_port=config.tenant_postgres_port,
            postgres_store_version=postgres_version,
            control_schema_version=os.getenv(
                "MEMORY_CONTROL_SCHEMA_VERSION",
                CONTROL_SCHEMA_REVISION,
            ),
            tenant_schema_version=os.getenv(
                "MEMORY_TENANT_SCHEMA_VERSION",
                TENANT_SCHEMA_REVISION,
            ),
            neo4j_store_version=neo4j_version,
            neo4j_schema_version=os.getenv("MEMORY_NEO4J_SCHEMA_VERSION", "1"),
            evidence_directory=Path(_required_environment("MEMORY_RECOVERY_EVIDENCE_DIR")),
            neo4j_admin_image=f"neo4j:{neo4j_version}@{neo4j_digest}",
        )
        backup_operator = HostBackupOrchestrator(
            control=control,
            runner=command_runner,
            publisher=LocalBackupArtifactPublisher(backup_config.artifact_directory),
            config=backup_config,
            restore_drill=restore_executor,
            retention_protection=PostgresDecommissionRetentionAdapter(config.control_database_url),
            expectation_collector=PostgresBackupExpectationCollector(
                config.tenant_credentials_dir,
                postgres_host=config.tenant_postgres_host,
                postgres_port=config.tenant_postgres_port,
            ),
            consistency_barrier=PostgresBackupConsistencyBarrier(
                control_database_url=config.control_database_url,
                tenant_secrets_directory=config.tenant_credentials_dir,
                postgres_host=config.tenant_postgres_host,
                postgres_port=config.tenant_postgres_port,
            ),
            telemetry_pseudonymizer=config.telemetry_pseudonymizer,
        )
    if len(arguments) >= 2 and arguments[0] == "migration":
        from agent_memory_service.host_tenant_migration import (
            create_host_active_tenant_migrator,
        )

        repository_root = Path(os.getenv("MEMORY_REPOSITORY_ROOT", Path.cwd()))
        tenant_migrator = create_host_active_tenant_migrator(
            control=control,
            control_database_url=config.control_database_url,
            postgres_host=config.tenant_postgres_host,
            postgres_port=config.tenant_postgres_port,
            secrets_root=config.tenant_credentials_dir,
            evidence_root=Path(_required_environment("MEMORY_RECOVERY_EVIDENCE_DIR")),
            repository_root=repository_root,
            telemetry_pseudonymizer=config.telemetry_pseudonymizer,
        )
    if len(arguments) >= 2 and arguments[0] == "outbox":
        from agent_memory_service.host_outbox import HostDeadLetterRedriveService
        from agent_memory_service.routing import FileSecretReader, RoutedGovernanceStore

        amqp_password = _secret_from_environment("MEMORY_AMQP_PASSWORD_FILE")
        amqp_host = os.getenv("MEMORY_AMQP_HOST", "rabbitmq")
        amqp_port = _positive_integer_from_environment("MEMORY_AMQP_PORT", default=5672)
        amqp_user = os.getenv("MEMORY_AMQP_USER", "memory")
        amqp_vhost = os.getenv("MEMORY_AMQP_VHOST", "memory")
        if not amqp_vhost or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in amqp_vhost
        ):
            raise RuntimeError("MEMORY_AMQP_VHOST must use a safe private-vhost name")
        governance = RoutedGovernanceStore(
            control,
            FileSecretReader(config.tenant_credentials_dir),
            postgres_host=config.tenant_postgres_host,
            postgres_port=config.tenant_postgres_port,
        )
        dead_letters = HostDeadLetterRedriveService(
            governance,
            amqp_url=(
                f"amqp://{quote(amqp_user, safe='')}:{quote(amqp_password, safe='')}@"
                f"{amqp_host}:{amqp_port}/{quote(amqp_vhost, safe='')}"
            ),
            exchange_name=os.getenv("MEMORY_AMQP_EXCHANGE", "memory.events.v1"),
            queue_name=os.getenv("MEMORY_AMQP_GRAPH_QUEUE", "memory.graph.apply.v1"),
            dead_letter_exchange_name=os.getenv(
                "MEMORY_AMQP_DEAD_LETTER_EXCHANGE", "memory.dead-letter.v1"
            ),
            dead_letter_queue_name=os.getenv(
                "MEMORY_AMQP_DEAD_LETTER_QUEUE", "memory.graph.dead-letter.v1"
            ),
        )
    raise SystemExit(
        run_cli(
            arguments,
            control,
            provisioner=provisioner,
            decommissioner=decommissioner,
            backup=backup_operator,
            tenant_migrator=tenant_migrator,
            dead_letters=dead_letters,
        )
    )


def _secret_from_environment(name: str) -> str:
    configured = os.getenv(name, "").strip()
    if not configured:
        raise RuntimeError(f"Required secret file variable {name} is not set")
    path = Path(configured)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Secret file configured by {name} is not a regular file")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Secret file configured by {name} is empty")
    return value


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def _required_positive_integer(name: str) -> int:
    return _positive_integer_from_environment(name)


def _positive_integer_from_environment(name: str, *, default: int | None = None) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        if default is None:
            raise RuntimeError(f"Required environment variable {name} is not set")
        value = str(default)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"Required environment variable {name} must be an integer") from exc
    if parsed < 1:
        raise RuntimeError(f"Required environment variable {name} must be positive")
    return parsed


if __name__ == "__main__":
    main()
