"""Real encrypted backup to isolated canonical restore acceptance."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from activegraph.core.event import Event  # type: ignore[import-untyped]
from alembic import command
from alembic.config import Config
from neo4j import AsyncGraphDatabase
from neo4j_agent_memory import MemoryClient, MemorySettings, Neo4jConfig
from neo4j_agent_memory.config.settings import (
    EmbeddingConfig,
    EmbeddingProvider,
    ExtractionConfig,
    ExtractorType,
    MemoryConfig,
)
from pydantic import BaseModel, ConfigDict, SecretStr

from agent_memory_service.agents import (
    AgentBudget,
    AgentCapability,
    AgentInvocation,
    AgentModelSettings,
    AgentRunContext,
    AgentRunEvent,
    AgentRunSnapshot,
    AgentRunState,
    AgentRuntimeModule,
    ManagedActiveGraphStoreFactory,
    PostgresAgentRunRepository,
    RecordedProvider,
)
from agent_memory_service.auth import TokenService
from agent_memory_service.backup import (
    BackupBarrierRecord,
    BackupManifest,
    BackupPlan,
    BackupStore,
    BackupValidationError,
    EncryptionCommandFailed,
    RestoreSafetyError,
)
from agent_memory_service.backup_barrier import PostgresBackupConsistencyBarrier
from agent_memory_service.control import ControlModule
from agent_memory_service.durable_memory import PrivateMemoryCommandType
from agent_memory_service.governance import (
    CandidateStatus,
    ProposeKnowledge,
    ReviewDecision,
    ReviewKnowledge,
)
from agent_memory_service.host_backup import (
    HostBackupConfig,
    HostBackupOrchestrator,
    LocalBackupArtifactPublisher,
    PostgresBackupExpectationCollector,
    PostgresBackupSource,
)
from agent_memory_service.host_provisioning import SubprocessHostCommandRunner
from agent_memory_service.host_restore import HostRestoreConfig, HostRestoreDrillExecutor
from agent_memory_service.lifecycle import (
    CorrectMemory,
    ErasureDecision,
    RequestErasure,
    ReviewErasure,
)
from agent_memory_service.models import MemoryItem, PrincipalKind, RetainMemory, TenantSession
from agent_memory_service.pseudonyms import TelemetryPseudonymizer
from agent_memory_service.restore_verify import DockerCanonicalRestoreVerifier
from agent_memory_service.schema import CONTROL_SCHEMA_REVISION, TENANT_SCHEMA_REVISION
from agent_memory_service.stores.neo4j_memory import Neo4jTenantMemoryStore
from agent_memory_service.stores.postgres_control import PostgresControlStore
from agent_memory_service.stores.postgres_governance import PostgresGovernanceStore

TENANT_ID = "tenant-a"
OTHER_TENANT_ID = "tenant-other"
KNOWLEDGE_CLAIM = "restore availability verification requires an isolated recovery drill"
REQUIRED_ENVIRONMENT = (
    "CONTROL_DATABASE_URL",
    "TENANT_DATABASE_URL",
    "BACKUP_NEO4J_TEST_URI",
    "BACKUP_NEO4J_TEST_PASSWORD",
    "BACKUP_NEO4J_PORT",
    "BACKUP_TENANT_COMPOSE_FILE",
    "BACKUP_TENANT_SECRETS_ROOT",
    "BACKUP_CONTROL_PASSWORD_FILE",
    "BACKUP_AGE_IDENTITY_FILE",
    "BACKUP_AGE_RECIPIENTS_FILE",
    "BACKUP_WRONG_AGE_IDENTITY_FILE",
    "BACKUP_EMBEDDING_VECTOR_FILE",
    "PLATFORM_TEST_IMAGE",
)

pytestmark = pytest.mark.skipif(
    any(not os.environ.get(name) for name in REQUIRED_ENVIRONMENT),
    reason="real backup and isolated restore environment is required",
)


class _RestoreEmbedding:
    dimensions = 384

    def __init__(self, vector: Sequence[float]) -> None:
        if len(vector) != self.dimensions:
            raise ValueError("Restore acceptance embedding must have 384 dimensions")
        self._vector = [float(value) for value in vector]

    async def embed(self, _text: str) -> list[float]:
        return list(self._vector)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vector) for _text in texts]


class _AcceptanceInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: str


class _AcceptanceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool


class _AcceptanceCapability(AgentCapability[_AcceptanceInput]):
    name = "restore_acceptance"
    input_model = _AcceptanceInput

    async def execute(
        self,
        _context: AgentRunContext,
        _command: _AcceptanceInput,
    ) -> _AcceptanceResult:
        return _AcceptanceResult(accepted=True)


class _CoordinatedBackupBarrier:
    """Observe real writers waiting before permitting the backup cut."""

    def __init__(
        self,
        delegate: PostgresBackupConsistencyBarrier,
        *,
        tenant_database_url: str,
        entered: threading.Event,
        writer_futures: Sequence[Future[object]],
        application_names: Sequence[str],
        private_idempotency_key: str,
        agent_run_id: str,
        native_event_id: str,
    ) -> None:
        self._delegate = delegate
        self._tenant_database_url = tenant_database_url
        self._entered = entered
        self._writer_futures = tuple(writer_futures)
        self._application_names = tuple(application_names)
        self._private_idempotency_key = private_idempotency_key
        self._agent_run_id = agent_run_id
        self._native_event_id = native_event_id
        self.observed_blocked_cut = False

    def enter(self, plan: BackupPlan) -> None:
        self._delegate.enter(plan)
        self._entered.set()

    def verify_quiescent(self, plan: BackupPlan) -> None:
        _wait_for_advisory_lock_waiters(
            self._tenant_database_url,
            self._application_names,
        )
        assert all(not future.done() for future in self._writer_futures)
        with psycopg.connect(self._tenant_database_url) as connection:
            private_row = connection.execute(
                """
                SELECT count(*)
                FROM memory.private_memory_commands
                WHERE tenant_id = %s AND idempotency_key = %s
                """,
                (plan.tenant_id, self._private_idempotency_key),
            ).fetchone()
            snapshot_row = connection.execute(
                "SELECT count(*) FROM memory.agent_runs WHERE run_id = %s",
                (self._agent_run_id,),
            ).fetchone()
            native_row = connection.execute(
                "SELECT count(*) FROM events WHERE id = %s",
                (self._native_event_id,),
            ).fetchone()
        assert private_row is not None and int(private_row[0]) == 0
        assert snapshot_row is not None and int(snapshot_row[0]) == 0
        assert native_row is not None and int(native_row[0]) == 0
        self._delegate.verify_quiescent(plan)
        self.observed_blocked_cut = True

    def exit(self, plan: BackupPlan) -> None:
        self._delegate.exit(plan)

    def inspect(self, tenant_id: str) -> BackupBarrierRecord | None:
        return self._delegate.inspect(tenant_id)

    def recover(self, tenant_id: str, barrier_id: str) -> None:
        self._delegate.recover(tenant_id, barrier_id)


def _wait_for_advisory_lock_waiters(
    tenant_database_url: str,
    application_names: Sequence[str],
) -> None:
    expected = set(application_names)
    deadline = time.monotonic() + 15
    observed: dict[str, tuple[str | None, str | None]] = {}
    while time.monotonic() < deadline:
        with psycopg.connect(tenant_database_url) as connection:
            rows = connection.execute(
                """
                SELECT application_name, wait_event_type, wait_event
                FROM pg_stat_activity
                WHERE application_name = ANY(%s)
                """,
                (list(expected),),
            ).fetchall()
        observed = {
            str(row[0]): (
                None if row[1] is None else str(row[1]),
                None if row[2] is None else str(row[2]),
            )
            for row in rows
        }
        waiting = {
            name
            for name, (wait_type, wait_event) in observed.items()
            if wait_type == "Lock" and wait_event == "advisory"
        }
        if waiting == expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"Tenant writers did not all wait on the backup advisory lock: {observed}")


def _application_database_url(database_url: str, application_name: str) -> str:
    return psycopg.conninfo.make_conninfo(
        database_url.replace("postgresql+psycopg://", "postgresql://", 1),
        application_name=application_name,
    )


def _upgrade_schemas(repository_root: Path) -> None:
    command.upgrade(Config(repository_root / "alembic-control.ini"), "head")
    command.upgrade(Config(repository_root / "alembic-tenant.ini"), "head")


def _clear_prior_agent_projection_fixtures(tenant_database_url: str) -> None:
    """Give this destructive recovery acceptance an isolated physical Tenant DB."""

    with psycopg.connect(tenant_database_url) as connection:
        activegraph = connection.execute("SELECT to_regclass('events')").fetchone()
        if activegraph is not None and activegraph[0] is not None:
            connection.execute("DELETE FROM events")
            connection.execute("DELETE FROM runs")
        connection.execute("DELETE FROM memory.agent_runs")


async def _seed_representative_state(
    *,
    control: ControlModule,
    tenant_database_url: str,
    neo4j_uri: str,
    neo4j_password: str,
    embedding_vector_file: Path,
) -> tuple[str, int]:
    proposer_id = "agent-backup"
    reviewer_id = "user-curator"
    erasure_admin_id = "user-erasure-admin"
    control.create_tenant(TENANT_ID, "Restore acceptance Tenant")
    control.create_tenant(OTHER_TENANT_ID, "Tenant excluded from isolated restore")
    control.create_principal(proposer_id, "Backup knowledge Agent", PrincipalKind.AGENT.value)
    control.create_principal(reviewer_id, "Backup Knowledge Curator", PrincipalKind.USER.value)
    control.create_principal(
        erasure_admin_id,
        "Backup Erasure Administrator",
        PrincipalKind.USER.value,
    )
    control.grant_membership(TENANT_ID, proposer_id, "tenant_member")
    control.grant_membership(TENANT_ID, reviewer_id, "tenant_member")
    control.grant_membership(TENANT_ID, reviewer_id, "knowledge_curator")
    control.grant_membership(TENANT_ID, erasure_admin_id, "tenant_administrator")
    control.register_tenant_route(
        TENANT_ID,
        neo4j_service_address="neo4j-tenant-a:7687",
        neo4j_secret_name="tenant-a/neo4j_password",
        tenant_database_name="tenant_tenant_a",
        tenant_database_role="tenant_tenant_a_rw",
        healthy=True,
    )
    control.register_tenant_route(
        OTHER_TENANT_ID,
        neo4j_service_address="neo4j-tenant-other:7687",
        neo4j_secret_name="tenant-other/neo4j_password",
        tenant_database_name="tenant_tenant_other",
        tenant_database_role="tenant_tenant_other_rw",
        healthy=True,
    )

    governance = PostgresGovernanceStore(tenant_database_url)
    candidate = await governance.propose(
        TENANT_ID,
        proposer_id,
        ProposeKnowledge(
            claim=KNOWLEDGE_CLAIM,
            source_memory_ids=("private-memory-for-restore",),
            confidence=0.93,
            idempotency_key="backup-restore-candidate",
        ),
    )
    reviewed = await governance.review(
        TENANT_ID,
        reviewer_id,
        ReviewKnowledge(
            candidate_id=candidate.id,
            decision=ReviewDecision.APPROVE,
            rationale="Representative knowledge for recovery acceptance.",
            idempotency_key="backup-restore-review",
        ),
    )
    assert reviewed.status is CandidateStatus.PUBLISHING

    driver = AsyncGraphDatabase.driver(neo4j_uri, auth=("neo4j", neo4j_password))
    async with driver:
        async with driver.session(database="neo4j") as session:
            result = await session.run(
                "CREATE CONSTRAINT memory_schema_version_unique IF NOT EXISTS "
                "FOR (n:MemorySchemaVersion) REQUIRE n.version IS UNIQUE"
            )
            await result.consume()
            result = await session.run("MERGE (:MemorySchemaVersion {version: 1})")
            await result.consume()

    vector = json.loads(embedding_vector_file.read_text(encoding="utf-8"))
    settings = MemorySettings(
        neo4j=Neo4jConfig(
            uri=neo4j_uri,
            username="neo4j",
            password=SecretStr(neo4j_password),
            database="neo4j",
        ),
        embedding=EmbeddingConfig(
            provider=EmbeddingProvider.SENTENCE_TRANSFORMERS,
            model="/opt/coengram/models/bge-small-en-v1.5",
            dimensions=384,
        ),
        llm=None,
        memory=MemoryConfig(multi_tenant=True),
        extraction=ExtractionConfig(
            extractor_type=ExtractorType.NONE,
            enable_llm_fallback=False,
        ),
    )
    async with MemoryClient(settings, embedder=_RestoreEmbedding(vector)) as client:
        graph = Neo4jTenantMemoryStore(client)
        knowledge = await graph.publish_tenant_knowledge(
            candidate.id,
            candidate.claim,
            candidate.confidence,
            candidate.proposer_id,
        )
        assert knowledge.content == KNOWLEDGE_CLAIM
        published = await governance.mark_published(TENANT_ID, candidate.id)
        assert published.status is CandidateStatus.PUBLISHED

        async def project(item_id: str) -> None:
            accepted = await governance.get_memory_command_by_result(TENANT_ID, item_id)
            assert accepted is not None
            assert accepted.result_item is not None
            await graph.apply_private_item(reviewer_id, accepted.result_item)
            await governance.mark_memory_command_applied(TENANT_ID, accepted.id)

        active = await governance.accept_retain(
            TENANT_ID,
            reviewer_id,
            reviewer_id,
            RetainMemory(
                content="Keep recovery drills isolated from production routes.",
                confidence=0.91,
                idempotency_key="backup-active-private",
            ),
        )
        await project(active.id)

        correction_source = await governance.accept_retain(
            TENANT_ID,
            reviewer_id,
            reviewer_id,
            RetainMemory(
                content="Run recovery drills once a year.",
                confidence=0.7,
                idempotency_key="backup-correction-source",
            ),
        )
        await project(correction_source.id)
        corrected = await governance.accept_correction(
            TENANT_ID,
            reviewer_id,
            reviewer_id,
            CorrectMemory(
                memory_id=correction_source.id,
                replacement_content="Run recovery drills every quarter.",
                confidence=0.96,
                reason="Quarterly recovery evidence is required.",
                idempotency_key="backup-correction",
            ),
        )
        await project(corrected.id)

        erased = await governance.accept_retain(
            TENANT_ID,
            reviewer_id,
            reviewer_id,
            RetainMemory(
                content="This payload must not survive completed erasure.",
                idempotency_key="backup-erasure-source",
            ),
        )
        await project(erased.id)
        erasure = await governance.request(
            TENANT_ID,
            reviewer_id,
            reviewer_id,
            RequestErasure(
                memory_id=erased.id,
                reason="Prove that erased content does not reappear after restore.",
                idempotency_key="backup-erasure-request",
            ),
        )
        await governance.review(
            TENANT_ID,
            erasure_admin_id,
            ReviewErasure(
                request_id=erasure.id,
                decision=ErasureDecision.APPROVE,
                rationale="Independent approval for the recovery acceptance fixture.",
                idempotency_key="backup-erasure-review",
            ),
        )
        erase_event = await governance.claim_outbox()
        assert erase_event is not None
        assert erase_event.event_type == "memory.command.accepted"
        erase_command = await governance.get_memory_command(
            TENANT_ID,
            erase_event.aggregate_id,
        )
        assert erase_command is not None
        assert erase_command.command_type is PrivateMemoryCommandType.ERASE
        assert erase_command.target_memory_id == erased.id
        await graph.erase_private(reviewer_id, erased.id)
        await governance.mark_memory_command_applied(TENANT_ID, erase_command.id)

    inspections = await governance.list_private_memory_state(TENANT_ID, reviewer_id)
    assert len(inspections) == 4
    assert {inspection.content for inspection in inspections} == {
        "Keep recovery drills isolated from production routes.",
        "Run recovery drills once a year.",
        "Run recovery drills every quarter.",
        None,
    }
    assert len(await governance.list_completed(TENANT_ID, reviewer_id)) == 1

    activegraph_stores = ManagedActiveGraphStoreFactory(lambda _tenant_id: tenant_database_url)
    try:
        runtime = AgentRuntimeModule(
            capabilities=(_AcceptanceCapability(),),
            provider=RecordedProvider([]),
            repository=PostgresAgentRunRepository(tenant_database_url),
            activegraph_store_factory=activegraph_stores,
        )
        agent_session = TenantSession(
            tenant_id=TENANT_ID,
            actor_id=proposer_id,
            actor_kind=PrincipalKind.AGENT,
            roles=frozenset({"tenant_member"}),
        )
        started = await runtime.start_agent_run(
            agent_session,
            AgentInvocation(
                capability="restore_acceptance",
                input={"goal": "Prove Agent Run recovery"},
                idempotency_key="backup-restore-agent-run",
            ),
        )
        assert started.tenant_id == TENANT_ID
        completed = await runtime.run_until_terminal(agent_session, started.id)
        assert completed.state is AgentRunState.COMPLETED
        assert len(completed.events) >= 3
        return completed.id, len(completed.events)
    finally:
        activegraph_stores.close()


def _cleanup_private_memory_command(
    tenant_database_url: str,
    *,
    tenant_id: str,
    idempotency_key: str,
) -> None:
    with psycopg.connect(tenant_database_url) as connection:
        rows = connection.execute(
            """
            SELECT command_id
            FROM memory.private_memory_commands
            WHERE tenant_id = %s AND idempotency_key = %s
            """,
            (tenant_id, idempotency_key),
        ).fetchall()
        command_ids = [str(row[0]) for row in rows]
        if not command_ids:
            return
        connection.execute(
            "DELETE FROM memory.outbox WHERE aggregate_id = ANY(%s)",
            (command_ids,),
        )
        connection.execute(
            "DELETE FROM memory.governance_audit WHERE target_id = ANY(%s)",
            (command_ids,),
        )
        connection.execute(
            "DELETE FROM memory.private_memory_items WHERE source_command_id = ANY(%s)",
            (command_ids,),
        )
        connection.execute(
            "DELETE FROM memory.private_memory_commands WHERE command_id = ANY(%s)",
            (command_ids,),
        )


def _cleanup_concurrent_agent_write(
    tenant_database_url: str,
    *,
    run_id: str,
    event_id: str,
) -> None:
    with psycopg.connect(tenant_database_url) as connection:
        connection.execute("DELETE FROM events WHERE id = %s", (event_id,))
        connection.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
        connection.execute("DELETE FROM memory.agent_runs WHERE run_id = %s", (run_id,))


def _restore_resources_with_label(target_id: str) -> tuple[str, ...]:
    label = f"memory.restore.target={target_id}"
    resources: list[str] = []
    for kind in ("network", "volume"):
        completed = subprocess.run(
            ("docker", kind, "ls", "--quiet", "--filter", f"label={label}"),
            check=True,
            capture_output=True,
            text=True,
        )
        resources.extend(line for line in completed.stdout.splitlines() if line)
    return tuple(resources)


def _ciphertext_paths(manifest: BackupManifest, artifact_root: Path) -> dict[str, Path]:
    return {
        artifact.artifact_id: artifact_root / artifact.object_key for artifact in manifest.artifacts
    }


def test_real_encrypted_backup_restores_through_canonical_interfaces_and_fails_closed(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    control_database_url = os.environ["CONTROL_DATABASE_URL"]
    tenant_database_url = os.environ["TENANT_DATABASE_URL"]
    neo4j_uri = os.environ["BACKUP_NEO4J_TEST_URI"]
    neo4j_password = os.environ["BACKUP_NEO4J_TEST_PASSWORD"]
    tenant_secrets_root = Path(os.environ["BACKUP_TENANT_SECRETS_ROOT"])
    identity = Path(os.environ["BACKUP_AGE_IDENTITY_FILE"])
    recipients = Path(os.environ["BACKUP_AGE_RECIPIENTS_FILE"])
    wrong_identity = Path(os.environ["BACKUP_WRONG_AGE_IDENTITY_FILE"])
    embedding_vector = Path(os.environ["BACKUP_EMBEDDING_VECTOR_FILE"])
    platform_image = os.environ["PLATFORM_TEST_IMAGE"]
    control_location = urlsplit(control_database_url)
    assert control_location.hostname is not None
    assert control_location.port is not None

    _upgrade_schemas(repository_root)
    _clear_prior_agent_projection_fixtures(tenant_database_url)
    control_store = PostgresControlStore(control_database_url)
    control = ControlModule(control_store, TokenService(control_store))
    representative_run_id, representative_event_count = asyncio.run(
        _seed_representative_state(
            control=control,
            tenant_database_url=tenant_database_url,
            neo4j_uri=neo4j_uri,
            neo4j_password=neo4j_password,
            embedding_vector_file=embedding_vector,
        )
    )

    staging = tmp_path / "staging"
    artifacts = tmp_path / "artifacts"
    workspaces = tmp_path / "restore-workspaces"
    evidence = tmp_path / "evidence"
    for directory in (staging, artifacts, workspaces, evidence):
        directory.mkdir(mode=0o700)

    runner = SubprocessHostCommandRunner(repository_root, timeout_seconds=1200)
    restore = HostRestoreDrillExecutor(
        runner=runner,
        verifier=DockerCanonicalRestoreVerifier(runner=runner, image=platform_image),
        config=HostRestoreConfig(
            workspace_root=workspaces,
            ready_attempts=120,
            ready_interval_seconds=1,
        ),
    )
    publisher = LocalBackupArtifactPublisher(artifacts)
    backup_config = HostBackupConfig(
        staging_directory=staging,
        artifact_directory=artifacts,
        tenant_compose_file=Path(os.environ["BACKUP_TENANT_COMPOSE_FILE"]),
        tenant_secrets_directory=tenant_secrets_root,
        age_recipients_file=recipients,
        age_key_id="backup-restore-acceptance-key",
        control_postgres=PostgresBackupSource(
            host=control_location.hostname,
            port=control_location.port,
            database="memory_control",
            user="memory_control",
            password_file=Path(os.environ["BACKUP_CONTROL_PASSWORD_FILE"]),
        ),
        tenant_postgres_host=control_location.hostname,
        tenant_postgres_port=control_location.port,
        postgres_store_version="17.6-alpine",
        control_schema_version=CONTROL_SCHEMA_REVISION,
        tenant_schema_version=TENANT_SCHEMA_REVISION,
        neo4j_store_version="5.26.28-community",
        neo4j_schema_version="1",
        evidence_directory=evidence,
        age_identity_file=identity,
    )
    expectation_collector = PostgresBackupExpectationCollector(
        tenant_secrets_root,
        postgres_host=control_location.hostname,
        postgres_port=control_location.port,
    )

    # The barrier must reject unfinished graph projection work before it can
    # create a staging directory, ciphertext, or manifest.
    pending_key = "backup-pending-projection-rejection"
    pending_governance = PostgresGovernanceStore(tenant_database_url)
    asyncio.run(
        pending_governance.accept_retain(
            TENANT_ID,
            "user-curator",
            "user-curator",
            RetainMemory(
                content="A pending projection must prevent backup capture.",
                idempotency_key=pending_key,
            ),
        )
    )
    rejected_backup_id = "backup-tenant-a-pending-projection"
    preflight_operator = HostBackupOrchestrator(
        control=control,
        runner=runner,
        publisher=publisher,
        config=backup_config,
        restore_drill=restore,
        expectation_collector=expectation_collector,
        consistency_barrier=PostgresBackupConsistencyBarrier(
            control_database_url=control_database_url,
            tenant_secrets_directory=tenant_secrets_root,
            postgres_host=control_location.hostname,
            postgres_port=control_location.port,
        ),
        telemetry_pseudonymizer=TelemetryPseudonymizer(b"r" * 32),
    )

    # Model a process crash after the durable suspension and advisory lock.
    # Process exit releases the PostgreSQL lock but intentionally cannot run exit().
    crash_plan_path = tmp_path / "abandoned-barrier-plan.json"
    crash_barrier_id_path = tmp_path / "abandoned-barrier-id"
    crash_plan_path.write_text(
        preflight_operator.plan(
            TENANT_ID,
            backup_id="backup-tenant-a-abandoned-barrier",
        ).model_dump_json(),
        encoding="utf-8",
    )
    crash_environment = os.environ.copy()
    crash_environment.update(
        {
            "COENGRAM_CRASH_CONTROL_DATABASE_URL": control_database_url,
            "COENGRAM_CRASH_TENANT_SECRETS": str(tenant_secrets_root),
            "COENGRAM_CRASH_POSTGRES_HOST": control_location.hostname,
            "COENGRAM_CRASH_POSTGRES_PORT": str(control_location.port),
            "COENGRAM_CRASH_PLAN": str(crash_plan_path),
            "COENGRAM_CRASH_BARRIER_ID": str(crash_barrier_id_path),
        }
    )
    subprocess.run(
        (
            sys.executable,
            "-c",
            "import os; from pathlib import Path; "
            "from agent_memory_service.backup import BackupPlan; "
            "from agent_memory_service.backup_barrier import "
            "PostgresBackupConsistencyBarrier; "
            "plan = BackupPlan.model_validate_json("
            "Path(os.environ['COENGRAM_CRASH_PLAN']).read_text()); "
            "barrier = PostgresBackupConsistencyBarrier("
            "control_database_url=os.environ['COENGRAM_CRASH_CONTROL_DATABASE_URL'], "
            "tenant_secrets_directory=Path("
            "os.environ['COENGRAM_CRASH_TENANT_SECRETS']), "
            "postgres_host=os.environ['COENGRAM_CRASH_POSTGRES_HOST'], "
            "postgres_port=int(os.environ['COENGRAM_CRASH_POSTGRES_PORT'])); "
            "barrier.enter(plan); "
            "Path(os.environ['COENGRAM_CRASH_BARRIER_ID']).write_text("
            "str(barrier.barrier_id)); os._exit(0)",
        ),
        check=True,
        cwd=repository_root,
        env=crash_environment,
    )
    abandoned = preflight_operator.barrier_status(TENANT_ID)
    assert abandoned is not None
    assert abandoned.barrier_id == crash_barrier_id_path.read_text(encoding="utf-8")
    try:
        with pytest.raises(BackupValidationError, match="not found"):
            preflight_operator.recover_barrier(TENANT_ID, "wrong-barrier-id")
        assert preflight_operator.barrier_status(TENANT_ID) == abandoned
    finally:
        current_barrier = preflight_operator.barrier_status(TENANT_ID)
        if current_barrier is not None:
            preflight_operator.recover_barrier(TENANT_ID, current_barrier.barrier_id)
    assert preflight_operator.barrier_status(TENANT_ID) is None

    try:
        with pytest.raises(BackupValidationError, match="projections are pending"):
            preflight_operator.create(TENANT_ID, backup_id=rejected_backup_id)
    finally:
        _cleanup_private_memory_command(
            tenant_database_url,
            tenant_id=TENANT_ID,
            idempotency_key=pending_key,
        )
    assert not (staging / rejected_backup_id).exists()
    assert not (artifacts / rejected_backup_id).exists()

    entered = threading.Event()
    application_names = (
        "coengram-backup-private-writer",
        "coengram-backup-agent-snapshot-writer",
        "coengram-backup-activegraph-writer",
    )
    private_writer_key = "backup-concurrent-private-writer"
    concurrent_run_id = "backup-concurrent-agent-run"
    concurrent_native_event_id = "backup_concurrent_native_event"
    private_writer = PostgresGovernanceStore(
        _application_database_url(tenant_database_url, application_names[0])
    )
    snapshot_repository = PostgresAgentRunRepository(
        _application_database_url(tenant_database_url, application_names[1])
    )
    activegraph_stores = ManagedActiveGraphStoreFactory(
        lambda _tenant_id: _application_database_url(
            tenant_database_url,
            application_names[2],
        ),
        max_size=1,
    )
    concurrent_session = TenantSession(
        tenant_id=TENANT_ID,
        actor_id="agent-backup",
        actor_kind=PrincipalKind.AGENT,
        roles=frozenset({"tenant_member"}),
    )
    concurrent_event = AgentRunEvent(
        sequence=1,
        type="agent.run.queued",
        data={"capability": "restore_acceptance"},
    )
    concurrent_snapshot = AgentRunSnapshot(
        run_id=concurrent_run_id,
        tenant_id=TENANT_ID,
        actor_id=concurrent_session.actor_id,
        actor_kind=concurrent_session.actor_kind,
        roles=tuple(sorted(concurrent_session.roles)),
        invocation=AgentInvocation(
            capability="restore_acceptance",
            input={"goal": "Commit only after the backup cut"},
            idempotency_key="backup-concurrent-agent-snapshot",
        ),
        command={"goal": "Commit only after the backup cut"},
        settings=AgentModelSettings(),
        budget=AgentBudget(),
        state=AgentRunState.QUEUED,
        model_calls=0,
        tool_calls=0,
        cost_usd=Decimal("0"),
        events=(concurrent_event,),
    )
    concurrent_native_event = Event(
        id=concurrent_native_event_id,
        type=concurrent_event.type,
        payload={
            "agent_memory_run_event": {
                "version": 1,
                "data": concurrent_event.data,
                "created_at": concurrent_event.created_at.isoformat(),
            }
        },
        actor=concurrent_session.actor_id,
        frame_id=f"frame-{concurrent_run_id}",
        timestamp=concurrent_event.created_at.isoformat(),
    )
    activegraph_store = activegraph_stores(concurrent_session, concurrent_run_id)

    def after_barrier_entered(action: object) -> object:
        if not entered.wait(timeout=20):
            raise AssertionError("Backup barrier was not entered")
        assert callable(action)
        return action()

    manifest: BackupManifest
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            writer_futures: tuple[Future[object], ...] = (
                executor.submit(
                    after_barrier_entered,
                    lambda: asyncio.run(
                        private_writer.accept_retain(
                            TENANT_ID,
                            "user-curator",
                            "user-curator",
                            RetainMemory(
                                content="This write belongs strictly after the backup cut.",
                                idempotency_key=private_writer_key,
                            ),
                        )
                    ),
                ),
                executor.submit(
                    after_barrier_entered,
                    lambda: snapshot_repository.create_or_get(concurrent_snapshot),
                ),
                executor.submit(
                    after_barrier_entered,
                    lambda: activegraph_store.append(concurrent_native_event),
                ),
            )
            coordinated_barrier = _CoordinatedBackupBarrier(
                PostgresBackupConsistencyBarrier(
                    control_database_url=control_database_url,
                    tenant_secrets_directory=tenant_secrets_root,
                    postgres_host=control_location.hostname,
                    postgres_port=control_location.port,
                ),
                tenant_database_url=tenant_database_url,
                entered=entered,
                writer_futures=writer_futures,
                application_names=application_names,
                private_idempotency_key=private_writer_key,
                agent_run_id=concurrent_run_id,
                native_event_id=concurrent_native_event_id,
            )
            operator = HostBackupOrchestrator(
                control=control,
                runner=runner,
                publisher=publisher,
                config=backup_config,
                restore_drill=restore,
                expectation_collector=expectation_collector,
                consistency_barrier=coordinated_barrier,
                telemetry_pseudonymizer=TelemetryPseudonymizer(b"r" * 32),
            )
            try:
                manifest = operator.create(TENANT_ID, backup_id="backup-tenant-a-live-001")
            finally:
                entered.set()
            private_item = writer_futures[0].result(timeout=20)
            saved_snapshot = writer_futures[1].result(timeout=20)
            writer_futures[2].result(timeout=20)

        assert coordinated_barrier.observed_blocked_cut
        assert isinstance(private_item, MemoryItem)
        assert private_item.content == "This write belongs strictly after the backup cut."
        assert saved_snapshot == concurrent_snapshot
        activegraph_store.upsert_run(
            created_at=datetime.now(UTC).isoformat(),
            goal="restore_acceptance",
            frame_id=f"frame-{concurrent_run_id}",
        )
        with psycopg.connect(tenant_database_url) as connection:
            committed = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM memory.private_memory_commands
                     WHERE tenant_id = %s AND idempotency_key = %s),
                    (SELECT count(*) FROM memory.agent_runs WHERE run_id = %s),
                    (SELECT count(*) FROM events WHERE id = %s)
                """,
                (
                    TENANT_ID,
                    private_writer_key,
                    concurrent_run_id,
                    concurrent_native_event_id,
                ),
            ).fetchone()
        assert committed == (1, 1, 1)
    finally:
        entered.set()
        activegraph_stores.close()
        _cleanup_private_memory_command(
            tenant_database_url,
            tenant_id=TENANT_ID,
            idempotency_key=private_writer_key,
        )
        _cleanup_concurrent_agent_write(
            tenant_database_url,
            run_id=concurrent_run_id,
            event_id=concurrent_native_event_id,
        )

    manifest_path = artifacts / manifest.backup_id / "manifest.json"
    verified = operator.verify(manifest_path)
    assert verified == manifest
    assert manifest.expectations.governance_candidate_count == 1
    assert manifest.expectations.private_memory.active.count == 2
    assert manifest.expectations.private_memory.correction_chain.count == 1
    assert manifest.expectations.private_memory.completed_erasure.count == 1
    assert manifest.expectations.agent_run_count == 1
    assert manifest.expectations.representative_agent_run_id == representative_run_id
    assert (
        manifest.expectations.representative_activegraph_event_count == representative_event_count
    )
    assert "This payload must not survive" not in manifest_path.read_text(encoding="utf-8")

    target_id = "restore-drill-tenant-a-live"
    record = operator.restore_drill(
        manifest_path,
        target_id=target_id,
        operator_id="integration-operator",
    )
    assert record.outcome == "passed"
    assert record.within_rto
    assert record.passed_checks == (
        "public-recall",
        "private-memory",
        "governance",
        "agent-runs",
        "tenant-isolation",
    )
    assert not (workspaces / target_id).exists()
    assert _restore_resources_with_label(target_id) == ()

    ciphertext = _ciphertext_paths(manifest, artifacts)
    corrupt_artifact = next(iter(ciphertext.values()))
    original_ciphertext = corrupt_artifact.read_bytes()
    try:
        corrupt_artifact.write_bytes(original_ciphertext + b"corrupt")
        with pytest.raises(BackupValidationError, match="integrity|size"):
            operator.verify(manifest_path)
    finally:
        corrupt_artifact.write_bytes(original_ciphertext)

    original_manifest = manifest_path.read_text(encoding="utf-8")
    try:
        manifest_path.write_text(
            manifest.model_copy(update={"complete": False}).model_dump_json(),
            encoding="utf-8",
        )
        with pytest.raises(BackupValidationError, match="incomplete"):
            operator.verify(manifest_path)
    finally:
        manifest_path.write_text(original_manifest, encoding="utf-8")

    tenant_artifact = next(
        artifact for artifact in manifest.artifacts if artifact.store is BackupStore.TENANT_POSTGRES
    )
    incompatible = manifest.model_copy(
        update={
            "artifacts": tuple(
                artifact.model_copy(update={"schema_version": "incompatible"})
                if artifact is tenant_artifact
                else artifact
                for artifact in manifest.artifacts
            )
        }
    )
    incompatible_target = "restore-drill-tenant-a-incompatible"
    with pytest.raises(RestoreSafetyError, match="schema version is incompatible"):
        restore.execute(
            manifest=incompatible,
            ciphertext_paths=ciphertext,
            target_id=incompatible_target,
            operator_id="integration-operator",
            age_identity_file=identity,
        )
    assert not (workspaces / incompatible_target).exists()
    assert _restore_resources_with_label(incompatible_target) == ()

    wrong_key_target = "restore-drill-tenant-a-wrong-key"
    with pytest.raises(EncryptionCommandFailed, match="decryption failed"):
        restore.execute(
            manifest=manifest,
            ciphertext_paths=ciphertext,
            target_id=wrong_key_target,
            operator_id="integration-operator",
            age_identity_file=wrong_identity,
        )
    assert (workspaces / wrong_key_target).is_dir()
    assert _restore_resources_with_label(wrong_key_target) == ()
    restore.cleanup(wrong_key_target)
    assert not (workspaces / wrong_key_target).exists()
