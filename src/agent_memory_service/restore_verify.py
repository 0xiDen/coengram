"""Content-safe verification for an isolated restore namespace."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx
import psycopg
from activegraph.store.postgres import PostgresEventStore  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, TypeAdapter

from agent_memory_service.agents import (
    AgentBudgetUsage,
    AgentCapability,
    AgentRunContext,
    AgentRuntimeModule,
    AgentRunView,
    RecordedProvider,
)
from agent_memory_service.agents.persistence import PostgresAgentRunRepository
from agent_memory_service.archive import ArchiveScope, decode_archive
from agent_memory_service.auth import TokenService
from agent_memory_service.backup import (
    RestoreExpectations,
    RestoreProofTarget,
    activegraph_event_sequence_digest,
    content_safe_digest,
    private_memory_correction_digest,
    private_memory_erasure_digest,
    private_memory_item_digest,
    projected_agent_run_events,
    tenant_knowledge_digest,
)
from agent_memory_service.control import ControlModule
from agent_memory_service.http import create_http_app
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import (
    MemoryItem,
    MemoryScope,
    MemoryState,
    MutationState,
    PrincipalKind,
    PrivateMemoryInspection,
    RecallQuery,
    RecallResult,
    TenantSession,
)
from agent_memory_service.routing import (
    FileSecretReader,
    RoutedGovernanceStore,
    RoutedTenantMemoryRouter,
)
from agent_memory_service.schema import (
    CONTROL_SCHEMA_REVISION,
    TENANT_SCHEMA_REVISION,
    SchemaRequirement,
    require_schema,
)
from agent_memory_service.stores.postgres_control import PostgresControlStore

_CHECKS = (
    "public-recall",
    "private-memory",
    "governance",
    "agent-runs",
    "tenant-isolation",
)
_LOGGER = logging.getLogger("memory.restore")
_PRIVATE_INSPECTIONS = TypeAdapter(tuple[PrivateMemoryInspection, ...])


def _tenant_knowledge_digest_sets_match(
    expected: set[str],
    actual: set[str],
) -> bool:
    """Compare complete proof sets and log only content-free mismatch counts."""

    missing = expected - actual
    unexpected = actual - expected
    if not missing and not unexpected:
        return True
    _LOGGER.warning(
        "Tenant Knowledge restore digest set mismatch",
        extra={
            "expected_digest_count": len(expected),
            "actual_digest_count": len(actual),
            "missing_digest_count": len(missing),
            "unexpected_digest_count": len(unexpected),
        },
    )
    return False


class RestoreVerificationReport(BaseModel):
    """Only named pass/fail facts; never restored content or exception text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    public_recall: bool
    private_memory: bool
    governance: bool
    agent_runs: bool
    tenant_isolation: bool

    @property
    def passed_checks(self) -> tuple[str, ...]:
        results = (
            self.public_recall,
            self.private_memory,
            self.governance,
            self.agent_runs,
            self.tenant_isolation,
        )
        return tuple(name for name, passed in zip(_CHECKS, results, strict=True) if passed)

    @property
    def passed(self) -> bool:
        return len(self.passed_checks) == len(_CHECKS)


class RestoreProbe(Protocol):
    async def verify_public_recall(self) -> bool: ...

    async def verify_private_memory(self) -> bool: ...

    async def verify_governance(self) -> bool: ...

    async def verify_agent_runs(self) -> bool: ...

    async def verify_no_other_tenant_routing(self) -> bool: ...


async def run_restore_checks(probe: RestoreProbe) -> RestoreVerificationReport:
    """Run every check independently and reduce failures to content-free booleans."""

    async def safe(check: Callable[[], Awaitable[bool]]) -> bool:
        try:
            result = await check()
        except Exception:  # noqa: BLE001 - the report deliberately hides adapter detail
            return False
        return result is True

    return RestoreVerificationReport(
        public_recall=await safe(probe.verify_public_recall),
        private_memory=await safe(probe.verify_private_memory),
        governance=await safe(probe.verify_governance),
        agent_runs=await safe(probe.verify_agent_runs),
        tenant_isolation=await safe(probe.verify_no_other_tenant_routing),
    )


@dataclass(frozen=True, slots=True)
class RestoreVerificationContext:
    target_id: str
    source_tenant_id: str
    network_name: str
    workspace: Path
    secrets_directory: Path
    control_database_name: str
    control_database_user: str
    postgres_host: str
    postgres_port: int
    embedding_model: str
    expectations: RestoreExpectations


class RestoreVerificationRunner(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DockerCanonicalRestoreVerifier:
    """Run canonical module checks in an unexposed container on the drill network."""

    runner: RestoreVerificationRunner
    image: str

    def __post_init__(self) -> None:
        if (
            not self.image
            or self.image.startswith("-")
            or any(character.isspace() for character in self.image)
        ):
            raise ValueError("Restore verifier image is invalid")

    def verify(self, context: RestoreVerificationContext) -> RestoreVerificationReport:
        report = context.workspace / "verification.json"
        report.unlink(missing_ok=True)
        expectations = context.workspace / "restore-expectations.json"
        if expectations.is_symlink() or expectations.exists():
            raise RuntimeError("Restore expectations path already exists")
        document = context.expectations.model_dump_json().encode("utf-8")
        descriptor = os.open(expectations, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, document)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.runner.run(
            (
                "docker",
                "run",
                "--name",
                f"memory-{context.target_id}-verifier",
                "--network",
                context.network_name,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--tmpfs",
                "/tmp:size=64m,mode=1777",
                "--volume",
                f"{context.workspace}:/run/restore:rw",
                "--volume",
                f"{context.secrets_directory}:/run/restore-secrets:ro",
                "--env",
                f"RESTORE_SOURCE_TENANT_ID={context.source_tenant_id}",
                "--env",
                f"RESTORE_TARGET_ID={context.target_id}",
                "--env",
                f"RESTORE_POSTGRES_HOST={context.postgres_host}",
                "--env",
                f"RESTORE_POSTGRES_PORT={context.postgres_port}",
                "--env",
                f"RESTORE_CONTROL_DATABASE_NAME={context.control_database_name}",
                "--env",
                f"RESTORE_CONTROL_DATABASE_USER={context.control_database_user}",
                "--env",
                "RESTORE_CONTROL_PASSWORD_FILE=/run/restore-secrets/postgres_superuser_password",
                "--env",
                "RESTORE_TENANT_SECRETS_DIR=/run/restore-secrets/tenants",
                "--env",
                f"RESTORE_EMBEDDING_MODEL={context.embedding_model}",
                "--env",
                "RESTORE_REPORT_PATH=/run/restore/verification.json",
                "--env",
                "RESTORE_EXPECTATIONS_PATH=/run/restore/restore-expectations.json",
                self.image,
                "python",
                "-m",
                "agent_memory_service.restore_verify",
            )
        )
        if report.is_symlink() or not report.is_file() or report.stat().st_size > 64_000:
            raise RuntimeError("Restore verifier did not produce a valid report")
        try:
            return RestoreVerificationReport.model_validate_json(report.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RuntimeError("Restore verifier report is invalid") from exc


class _ReplayCommand(BaseModel):
    model_config = ConfigDict(extra="allow")


class _RestoreReplayCapability(AgentCapability[_ReplayCommand]):
    input_model = _ReplayCommand

    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(
        self,
        _context: AgentRunContext,
        _command: _ReplayCommand,
    ) -> None:
        raise RuntimeError("Restore replay capability cannot execute")


@dataclass(frozen=True, slots=True)
class CanonicalRestoreProbe:
    """Exercise restored state only through normal application module interfaces."""

    target_id: str
    source_tenant_id: str
    control_database_url: str
    tenant_secrets_directory: Path
    postgres_host: str
    postgres_port: int
    embedding_model: str
    expectations: RestoreExpectations

    def _modules(
        self,
    ) -> tuple[
        ControlModule,
        TokenService,
        RoutedTenantMemoryRouter,
        RoutedGovernanceStore,
        MemoryModule,
    ]:
        control_store = PostgresControlStore(self.control_database_url)
        tokens = TokenService(control_store)
        control = ControlModule(control_store, tokens)
        secrets = FileSecretReader(self.tenant_secrets_directory)
        router = RoutedTenantMemoryRouter(
            control,
            secrets,
            embedding_model=self.embedding_model,
        )
        governance = RoutedGovernanceStore(
            control,
            secrets,
            postgres_host=self.postgres_host,
            postgres_port=self.postgres_port,
        )
        memory = MemoryModule(
            router,
            governance,
            erasures=governance,
            self_approval=control,
            commands=governance,
        )
        return control, tokens, router, governance, memory

    def _session(self) -> TenantSession:
        return TenantSession(
            tenant_id=self.source_tenant_id,
            actor_id="restore-verifier",
            actor_kind=PrincipalKind.USER,
            roles=frozenset({"tenant_member", "knowledge_curator", "tenant_administrator"}),
        )

    def _require_control_schema(self) -> None:
        require_schema(
            SchemaRequirement(
                database_url=self.control_database_url,
                expected_revision=CONTROL_SCHEMA_REVISION,
                store_name="Isolated Control Store",
            )
        )

    def _require_tenant_schema(self, governance: RoutedGovernanceStore) -> str:
        database_url = governance.database_url_for_tenant(self.source_tenant_id)
        require_schema(
            SchemaRequirement(
                database_url=database_url,
                expected_revision=TENANT_SCHEMA_REVISION,
                store_name="Isolated Tenant Store",
            )
        )
        return database_url

    async def verify_public_recall(self) -> bool:
        self._require_control_schema()
        control, tokens, router, governance, memory = self._modules()
        principal_id = f"restore-verifier-{self.target_id}"
        principal_created = False
        try:
            self._require_tenant_schema(governance)
            knowledge = await router.for_tenant(self.source_tenant_id).list_tenant_knowledge()
            by_digest = {
                tenant_knowledge_digest(
                    content=item.content,
                    confidence=item.confidence,
                    actor_id=item.provenance.actor_id,
                    source=item.provenance.source,
                ): item
                for item in knowledge
            }
            if len(by_digest) != len(knowledge):
                return False
            if not _tenant_knowledge_digest_sets_match(
                set(self.expectations.tenant_knowledge_digests),
                set(by_digest),
            ):
                return False
            representative_digest = self.expectations.tenant_knowledge_digests[0]
            representative = by_digest.get(representative_digest)
            if representative is None:
                return False
            control.create_principal(
                principal_id,
                "Isolated restore verifier",
                PrincipalKind.USER.value,
            )
            principal_created = True
            control.grant_membership(self.source_tenant_id, principal_id, "tenant_member")
            credential = control.issue_access_token(self.source_tenant_id, principal_id)
            app = create_http_app(memory, tokens)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://restore-verifier.internal",
            ) as client:
                response = await client.post(
                    "/api/v1/memories/recall",
                    headers={"Authorization": f"Bearer {credential.access_token}"},
                    json={
                        "query": representative.content,
                        "limit": 100,
                    },
                )
            if response.status_code != 200:
                return False
            recalled = RecallResult.model_validate(response.json())
            recalled_digests: set[str] = set()
            for item in recalled.items:
                if item.scope is not MemoryScope.TENANT_KNOWLEDGE:
                    continue
                recalled_digests.add(
                    tenant_knowledge_digest(
                        content=item.content,
                        confidence=item.confidence,
                        actor_id=item.provenance.actor_id,
                        source=item.provenance.source,
                    )
                )
            return representative_digest in recalled_digests
        finally:
            if principal_created:
                control.disable_principal(principal_id)
            await router.close()

    async def verify_private_memory(self) -> bool:
        """Prove personal state through authenticated inspection, archive, and recall."""

        self._require_control_schema()
        control, tokens, router, governance, memory = self._modules()
        try:
            database_url = self._require_tenant_schema(governance)
            owner_ids = self._private_memory_owner_ids(database_url)
            app = create_http_app(memory, tokens)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            active_digests: list[str] = []
            correction_digests: list[str] = []
            erasure_digests: list[str] = []
            active_representatives: dict[
                str,
                tuple[str, PrivateMemoryInspection, TenantSession, bool],
            ] = {}
            correction_representatives: dict[
                str,
                tuple[
                    str,
                    PrivateMemoryInspection,
                    PrivateMemoryInspection,
                    TenantSession,
                    bool,
                ],
            ] = {}
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://restore-verifier.internal",
            ) as client:
                for owner_id in owner_ids:
                    principal = control.inspect_principal(owner_id)
                    membership = control.inspect_membership(
                        self.source_tenant_id,
                        owner_id,
                    )
                    session = TenantSession(
                        tenant_id=self.source_tenant_id,
                        actor_id=owner_id,
                        actor_kind=principal.kind,
                        roles=membership.roles,
                    )
                    publicly_authenticable = principal.active and membership.active
                    if publicly_authenticable:
                        credential = control.issue_access_token(
                            self.source_tenant_id,
                            owner_id,
                        )
                        headers = {"Authorization": f"Bearer {credential.access_token}"}
                        inspection_response = await client.get(
                            "/api/v1/memories",
                            headers=headers,
                        )
                        archive_response = await client.get(
                            "/api/v1/memory-archives/private",
                            headers=headers,
                        )
                        if (
                            inspection_response.status_code != 200
                            or archive_response.status_code != 200
                        ):
                            return False
                        inspections = _PRIVATE_INSPECTIONS.validate_python(
                            inspection_response.json()
                        )
                        archive_data = archive_response.content
                    else:
                        inspections = await memory.inspect_private(session)
                        archive_data = await memory.export_private(session)
                    if any(item.owner_principal_id != owner_id for item in inspections):
                        return False
                    decoded = decode_archive(archive_data, ArchiveScope.PRIVATE)
                    if any(
                        item.owner_principal_id != owner_id for item in decoded.memory_items
                    ) or any(
                        tombstone.owner_principal_id != owner_id
                        for tombstone in decoded.erasure_tombstones
                    ):
                        return False
                    archived_by_id = {item.id: item for item in decoded.memory_items}
                    if len(archived_by_id) != len(decoded.memory_items):
                        return False
                    graph_items = await router.for_tenant(self.source_tenant_id).list_private(
                        owner_id
                    )
                    expected_graph = {
                        item.id: item
                        for item in inspections
                        if item.mutation_state is MutationState.APPLIED
                        and item.state is not MemoryState.ERASED
                    }
                    actual_graph = {item.id: item for item in graph_items}
                    if (
                        len(actual_graph) != len(graph_items)
                        or set(actual_graph) != set(expected_graph)
                        or any(
                            not _graph_memory_matches_authoritative_state(
                                actual_graph[memory_id],
                                inspection,
                                archived_by_id.get(memory_id),
                            )
                            for memory_id, inspection in expected_graph.items()
                        )
                    ):
                        return False
                    by_id = {item.id: item for item in inspections}
                    if len(by_id) != len(inspections):
                        return False
                    for item in inspections:
                        try:
                            digest = private_memory_item_digest(item)
                        except ValueError:
                            continue
                        active_digests.append(digest)
                        active_representatives[digest] = (
                            owner_id,
                            item,
                            session,
                            publicly_authenticable,
                        )
                    for replacement in inspections:
                        ancestor = (
                            None
                            if replacement.supersedes_id is None
                            else by_id.get(replacement.supersedes_id)
                        )
                        if ancestor is None:
                            continue
                        try:
                            digest = private_memory_correction_digest(ancestor, replacement)
                        except ValueError:
                            continue
                        correction_digests.append(digest)
                        correction_representatives[digest] = (
                            owner_id,
                            ancestor,
                            replacement,
                            session,
                            publicly_authenticable,
                        )
                    for tombstone in decoded.erasure_tombstones:
                        inspection = by_id.get(tombstone.memory_id)
                        if inspection is None:
                            return False
                        try:
                            erasure_digests.append(
                                private_memory_erasure_digest(inspection, tombstone)
                            )
                        except ValueError:
                            return False

                expected = self.expectations.private_memory
                if not (
                    _restore_proof_target_matches(expected.active, active_digests)
                    and _restore_proof_target_matches(
                        expected.correction_chain,
                        correction_digests,
                    )
                    and _restore_proof_target_matches(
                        expected.completed_erasure,
                        erasure_digests,
                    )
                ):
                    return False
                active_representative = expected.active.representative_digest
                if active_representative is not None:
                    representative = active_representatives.get(active_representative)
                    if representative is None or not await _private_item_is_recalled(
                        client,
                        control,
                        memory,
                        self.source_tenant_id,
                        *representative,
                    ):
                        return False
                correction_representative = expected.correction_chain.representative_digest
                if correction_representative is not None:
                    representative_chain = correction_representatives.get(correction_representative)
                    if representative_chain is None or not await _correction_is_recalled(
                        client,
                        control,
                        memory,
                        self.source_tenant_id,
                        *representative_chain,
                    ):
                        return False
            return True
        finally:
            await router.close()

    async def verify_governance(self) -> bool:
        self._require_control_schema()
        _control, _tokens, router, governance, memory = self._modules()
        try:
            self._require_tenant_schema(governance)
            candidates = await memory.list_knowledge_candidates(self._session())
            return (
                len(candidates) == self.expectations.governance_candidate_count
                and content_safe_digest(
                    [candidate.model_dump(mode="json") for candidate in candidates]
                )
                == self.expectations.governance_digest
            )
        finally:
            await router.close()

    async def verify_agent_runs(self) -> bool:
        self._require_control_schema()
        _control, _tokens, router, governance, _memory = self._modules()
        try:
            database_url = self._require_tenant_schema(governance)
            repository = PostgresAgentRunRepository(database_url)
            if (
                self._tenant_row_count(database_url, "agent_runs")
                != self.expectations.agent_run_count
            ):
                return False
            representative = self.expectations.representative_agent_run_id
            if representative is None:
                return True
            snapshot = repository.get(self.source_tenant_id, representative)
            if (
                snapshot is None
                or snapshot.tenant_id != self.source_tenant_id
                or content_safe_digest(snapshot.model_dump(mode="json"))
                != self.expectations.representative_agent_run_digest
            ):
                return False
            native_store = PostgresEventStore(database_url, representative)
            try:
                native_events = tuple(native_store.iter_events())
                native_run = native_store.get_run()
            finally:
                native_store.close()
            if (
                native_run is None
                or len(native_events) != self.expectations.representative_activegraph_event_count
                or activegraph_event_sequence_digest(native_events)
                != self.expectations.representative_activegraph_event_digest
                or projected_agent_run_events(native_events) != snapshot.events
            ):
                return False
            capability = _RestoreReplayCapability(snapshot.invocation.capability)
            runtime = AgentRuntimeModule(
                capabilities=(capability,),
                provider=RecordedProvider(()),
                settings=snapshot.settings,
                budget=snapshot.budget,
                repository=repository,
                activegraph_store_factory=lambda _session, run_id: PostgresEventStore(
                    database_url,
                    run_id,
                ),
            )
            expected_view = AgentRunView(
                id=snapshot.run_id,
                tenant_id=snapshot.tenant_id,
                actor_id=snapshot.actor_id,
                capability=snapshot.invocation.capability,
                state=snapshot.state,
                settings=snapshot.settings,
                budget=snapshot.budget,
                usage=AgentBudgetUsage(
                    model_calls=snapshot.model_calls,
                    tool_calls=snapshot.tool_calls,
                    events=len(snapshot.events),
                    cost_usd=snapshot.cost_usd,
                ),
                events=snapshot.events,
                output=snapshot.output,
                failure=snapshot.failure,
                resumable=snapshot.resumable,
            )
            return (
                runtime.status(snapshot.session(), representative) == expected_view
                and runtime.replay(snapshot.session(), representative) == expected_view
            )
        finally:
            await router.close()

    async def verify_no_other_tenant_routing(self) -> bool:
        self._require_control_schema()
        control, _tokens, router, _governance, _memory = self._modules()
        try:
            if self._backup_barrier_count() != 0:
                return False
            routes = control.list_tenant_routes()
            if len(routes) != 1:
                return False
            route = routes[0]
            normalized = self.source_tenant_id.replace("-", "_")
            return (
                route.tenant_id == self.source_tenant_id
                and route.neo4j_service_address == f"neo4j-{self.source_tenant_id}:7687"
                and route.neo4j_secret_name == f"{self.source_tenant_id}/neo4j_password"
                and route.tenant_database_name == f"tenant_{normalized}"
                and route.tenant_database_role == f"tenant_{normalized}_rw"
            )
        finally:
            await router.close()

    def _tenant_row_count(self, database_url: str, table: str) -> int:
        if table not in {"knowledge_candidates", "agent_runs"}:
            raise ValueError("Unsupported restore verification table")
        with psycopg.connect(database_url) as connection:
            row = connection.execute(
                f"SELECT count(*) FROM memory.{table} WHERE tenant_id = %s",
                (self.source_tenant_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Restore verification count was unavailable")
        return int(row[0])

    def _private_memory_owner_ids(self, database_url: str) -> tuple[str, ...]:
        with psycopg.connect(database_url) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT owner_principal_id
                FROM memory.private_memory_items
                WHERE tenant_id = %s
                ORDER BY owner_principal_id
                """,
                (self.source_tenant_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _backup_barrier_count(self) -> int:
        with psycopg.connect(self.control_database_url) as connection:
            row = connection.execute("SELECT count(*) FROM control.backup_barriers").fetchone()
        if row is None:
            raise RuntimeError("Restored backup barrier count was unavailable")
        return int(row[0])


def _restore_proof_target_matches(
    expected: RestoreProofTarget,
    digests: Sequence[str],
) -> bool:
    ordered = tuple(sorted(set(digests)))
    return (
        len(ordered) == expected.count
        and content_safe_digest(ordered) == expected.digest
        and (expected.representative_digest is None or expected.representative_digest in ordered)
    )


def _graph_memory_matches_authoritative_state(
    graph_item: MemoryItem,
    inspection: PrivateMemoryInspection,
    archived_item: MemoryItem | None,
) -> bool:
    return (
        archived_item is not None
        and graph_item.id == inspection.id
        and graph_item == archived_item
        and graph_item.owner_principal_id == inspection.owner_principal_id
        and graph_item.scope is MemoryScope.PRIVATE
        and graph_item.content == inspection.content
        and graph_item.kind is inspection.kind
        and graph_item.confidence == inspection.confidence
        and graph_item.supersedes_id == inspection.supersedes_id
        and (
            inspection.state is MemoryState.ERASURE_PENDING or graph_item.state is inspection.state
        )
    )


async def _private_item_is_recalled(
    client: httpx.AsyncClient,
    control: ControlModule,
    memory: MemoryModule,
    tenant_id: str,
    owner_id: str,
    item: PrivateMemoryInspection,
    session: TenantSession,
    publicly_authenticable: bool,
) -> bool:
    if item.content is None:
        return False
    if publicly_authenticable:
        credential = control.issue_access_token(tenant_id, owner_id)
        response = await client.post(
            "/api/v1/memories/recall",
            headers={"Authorization": f"Bearer {credential.access_token}"},
            json={"query": item.content, "limit": 100},
        )
        if response.status_code != 200:
            return False
        recalled = RecallResult.model_validate(response.json())
    else:
        recalled = await memory.recall(
            session,
            RecallQuery(query=item.content, limit=100),
        )
    return any(
        recalled_item.id == item.id
        and recalled_item.scope is MemoryScope.PRIVATE
        and recalled_item.owner_principal_id == owner_id
        for recalled_item in recalled.items
    )


async def _correction_is_recalled(
    client: httpx.AsyncClient,
    control: ControlModule,
    memory: MemoryModule,
    tenant_id: str,
    owner_id: str,
    ancestor: PrivateMemoryInspection,
    replacement: PrivateMemoryInspection,
    session: TenantSession,
    publicly_authenticable: bool,
) -> bool:
    if replacement.content is None:
        return False
    if publicly_authenticable:
        credential = control.issue_access_token(tenant_id, owner_id)
        response = await client.post(
            "/api/v1/memories/recall",
            headers={"Authorization": f"Bearer {credential.access_token}"},
            json={"query": replacement.content, "limit": 100},
        )
        if response.status_code != 200:
            return False
        recalled = RecallResult.model_validate(response.json())
    else:
        recalled = await memory.recall(
            session,
            RecallQuery(query=replacement.content, limit=100),
        )
    private_ids = {
        item.id
        for item in recalled.items
        if item.scope is MemoryScope.PRIVATE and item.owner_principal_id == owner_id
    }
    return replacement.id in private_ids and ancestor.id not in private_ids


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def _protected_secret(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError("Restore verifier secret file is not protected")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("Restore verifier secret file is empty")
    return value


def _probe_from_environment() -> tuple[CanonicalRestoreProbe, Path]:
    source_tenant_id = _required("RESTORE_SOURCE_TENANT_ID")
    host = _required("RESTORE_POSTGRES_HOST")
    try:
        port = int(_required("RESTORE_POSTGRES_PORT"))
    except ValueError as exc:
        raise RuntimeError("Restore verifier PostgreSQL port is invalid") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("Restore verifier PostgreSQL port is invalid")
    database = _required("RESTORE_CONTROL_DATABASE_NAME")
    user = _required("RESTORE_CONTROL_DATABASE_USER")
    password = _protected_secret(Path(_required("RESTORE_CONTROL_PASSWORD_FILE")))
    control_database_url = (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(database, safe='')}"
    )
    expectations_path = Path(_required("RESTORE_EXPECTATIONS_PATH"))
    expected_root = Path("/run/restore").resolve()
    resolved_expectations = expectations_path.resolve()
    try:
        resolved_expectations.relative_to(expected_root)
    except ValueError as exc:
        raise RuntimeError("Restore expectations path is outside its workspace") from exc
    if (
        expectations_path.is_symlink()
        or not expectations_path.is_file()
        or expectations_path.stat().st_size > 1_000_000
    ):
        raise RuntimeError("Restore expectations file is invalid")
    try:
        expectations = RestoreExpectations.model_validate_json(
            expectations_path.read_text(encoding="utf-8")
        )
    except ValueError as exc:
        raise RuntimeError("Restore expectations document is invalid") from exc
    return (
        CanonicalRestoreProbe(
            target_id=_required("RESTORE_TARGET_ID"),
            source_tenant_id=source_tenant_id,
            control_database_url=control_database_url,
            tenant_secrets_directory=Path(_required("RESTORE_TENANT_SECRETS_DIR")),
            postgres_host=host,
            postgres_port=port,
            embedding_model=_required("RESTORE_EMBEDDING_MODEL"),
            expectations=expectations,
        ),
        Path(_required("RESTORE_REPORT_PATH")),
    )


def _write_report(path: Path, report: RestoreVerificationReport) -> None:
    root = Path("/run/restore").resolve()
    destination = path.resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Restore verifier report path is outside its workspace") from exc
    if destination.is_symlink() or destination.exists():
        raise RuntimeError("Restore verifier report path already exists")
    document = json.dumps(
        report.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, document)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    probe, report_path = _probe_from_environment()
    _write_report(report_path, asyncio.run(run_restore_checks(probe)))


if __name__ == "__main__":
    main()
