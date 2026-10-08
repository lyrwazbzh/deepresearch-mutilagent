from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.agent.tools.database_read import (
    DataSourceService,
    EnvironmentCredentialResolver,
    HostPortDatabaseEgressPolicy,
    SecretsCredentialResolver,
    VersionedPostgresFactory,
    tool_definitions as data_tool_definitions,
)
from src.agent.tools.compute import (
    ComputeResourceResolver,
    DockerSandboxRunner,
    SandboxLimits,
    SandboxRunner,
    tool_definitions as compute_tool_definitions,
)
from src.agent.tools.datasets import DatasetService, tool_definitions as dataset_tool_definitions
from src.agent.tools.documents import (
    DocumentIndex,
    DocumentService,
    ElasticsearchDocumentIndex,
    InMemoryDocumentIndex,
    RetrievalGateway,
    TaskBindingResolver,
    tool_definitions as document_tool_definitions,
)
from src.agent.tools.web import tool_definitions as web_tool_definitions
from src.agent.tools.skill_management import (
    CapabilitySnapshotBuilder,
    SkillLoader,
    SkillRegistry,
    SkillRuntime,
)
from src.agent.config import Settings
from src.agent.backends.audit import AuditService
from src.agent.backends.observability import Telemetry
from src.agent.tools.database_write import (
    PendingWriteService,
    PostgresWriteTargetFactory,
    PrepareWriteService,
    WriteExecutor,
    WritePolicyService,
    WriteTargetFactory,
)
from src.agent.graph.evidence import EvidencePipeline
from src.agent.backends.events import EventBroker, EventService, RedisEventBroker
from src.agent.main_agent import ResearchGraph
from src.agent.llm.runtime import PHASE1_BINDINGS, PHASE2_BINDINGS, ModelResolver, ModelRuntime
from src.agent.tools.knowledge_graph import (
    EvidenceContentResolver,
    InMemoryKnowledgeGraphStore,
    KnowledgeGraphStore,
    ModelKnowledgeExtractor,
    Neo4jKnowledgeGraphStore,
    TaskKnowledgeGraphService,
    tool_definitions as knowledge_graph_tool_definitions,
)
from src.agent.memory.service import MemoryService
from src.agent.backends.persistence.database import SessionFactory, create_session_factory
from src.agent.runtime.context import ContextBuilder
from src.agent.runtime.node_runner import NodeRunner
from src.agent.runtime.state_manager import StateManager
from src.agent.runtime.tool_runtime import ToolPolicy, ToolPolicyRegistry, ToolRegistry, ToolRuntime
from src.agent.runtime.tool_decisions import DecisionTraceService, ToolDecisionReviewService
from src.agent.evaluation.model_rollout import ToolCanaryAssigner
from src.agent.reporting.reporting import CitationValidator, ReportPublisher
from src.agent.reporting.share_export import (
    ExportManager,
    ExportWorker,
    FinalReportLoader,
    ReportRenderer,
    ShareManager,
)
from src.agent.task_service import QueryCompletionService, TaskService
from src.agent.backends.secrets import SecretsService
from src.agent.workers.research import ResearchWorker
from src.agent.workers.memory import MemoryEmbeddingWorker, MemoryExtractionWorker
from src.agent.workers.documents import DocumentIngestionWorker
from src.agent.backends.workspace import (
    InMemoryObjectStore,
    MinioObjectStore,
    ObjectStore,
    WorkspaceLimits,
    WorkspaceService,
)


@dataclass
class Container:
    settings: Settings
    session_factory: SessionFactory
    secrets_service: SecretsService | None
    model_resolver: ModelResolver
    model_runtime: object
    task_service: TaskService
    state_manager: StateManager
    node_runner: NodeRunner
    graph: ResearchGraph
    worker: ResearchWorker
    document_worker: DocumentIngestionWorker
    event_service: EventService
    workspace_service: WorkspaceService
    document_service: DocumentService
    retrieval_gateway: RetrievalGateway
    datasource_service: DataSourceService
    pending_write_service: PendingWriteService
    write_executor: WriteExecutor
    dataset_service: DatasetService
    tool_registry: ToolRegistry
    tool_runtime: ToolRuntime
    skill_registry: SkillRegistry
    skill_runtime: SkillRuntime
    tool_policy_registry: ToolPolicyRegistry
    compute_resource_resolver: ComputeResourceResolver
    sandbox_runner: SandboxRunner
    memory_service: MemoryService
    memory_extraction_worker: MemoryExtractionWorker
    memory_embedding_worker: MemoryEmbeddingWorker
    knowledge_graph_service: TaskKnowledgeGraphService
    telemetry: Telemetry | None
    audit_service: AuditService
    decision_trace_service: DecisionTraceService
    tool_decision_review_service: ToolDecisionReviewService
    share_manager: ShareManager
    export_manager: ExportManager
    export_worker: ExportWorker


def build_container(
    settings: Settings | None = None,
    *,
    session_factory: SessionFactory | None = None,
    model_runtime: object | None = None,
    event_broker: EventBroker | None = None,
    workspace_store: ObjectStore | None = None,
    document_store: ObjectStore | None = None,
    document_index: DocumentIndex | None = None,
    tool_registry: ToolRegistry | None = None,
    sandbox_runner: SandboxRunner | None = None,
    write_target_factory: WriteTargetFactory | None = None,
    knowledge_graph_store: KnowledgeGraphStore | None = None,
    telemetry: Telemetry | None = None,
    tool_canary_assigner: ToolCanaryAssigner | None = None,
) -> Container:
    settings = settings or Settings.from_env()
    sessions = session_factory or create_session_factory(settings.database_url)
    secrets_service = SecretsService.from_environment(sessions)
    resolved_telemetry = telemetry or (
        Telemetry(
            sessions,
            max_queue_size=settings.telemetry_max_queue_size,
            max_export_batch_size=settings.telemetry_max_export_batch_size,
            schedule_delay_millis=settings.telemetry_export_delay_millis,
        )
        if settings.telemetry_enabled
        else None
    )
    audit_service = AuditService(sessions)
    decision_trace_service = DecisionTraceService(sessions)
    tool_decision_review_service = ToolDecisionReviewService(
        sessions, audit_service=audit_service
    )
    resolver = ModelResolver()
    runtime = model_runtime or ModelRuntime(
        sessions,
        embedding_dimensions=settings.openai_embedding_dimensions,
        telemetry=resolved_telemetry,
    )
    state_manager = StateManager(
        sessions,
        phase5_enabled=settings.phase5_enabled,
        phase9_enabled=settings.phase9_enabled,
    )

    if workspace_store is None:
        workspace_store = (
            MinioObjectStore(
                settings.minio_endpoint,
                settings.minio_access_key,
                settings.minio_secret_key,
                bucket="workspaces",
                secure=settings.minio_secure,
            )
            if settings.minio_endpoint
            else InMemoryObjectStore()
        )
    if document_store is None:
        document_store = (
            MinioObjectStore(
                settings.minio_endpoint,
                settings.minio_access_key,
                settings.minio_secret_key,
                bucket="documents",
                secure=settings.minio_secure,
            )
            if settings.minio_endpoint
            else InMemoryObjectStore()
        )
    workspace = WorkspaceService(
        sessions,
        workspace_store,
        limits=WorkspaceLimits(
            tenant_bytes=settings.tenant_workspace_bytes_max,
            task_bytes=settings.task_workspace_bytes_max,
            file_bytes=settings.workspace_file_max_bytes,
        ),
    )
    citation_validator = CitationValidator(sessions, workspace, resolved_telemetry)
    report_loader = FinalReportLoader(sessions, workspace, citation_validator)
    share_manager = ShareManager(
        sessions,
        workspace,
        report_loader,
        audit_service,
        default_days=settings.share_expires_default_days,
        max_days=settings.share_expires_max_days,
        public_base_url=settings.share_public_base_url,
    )
    export_manager = ExportManager(
        sessions, workspace, report_loader, audit_service
    )
    export_worker = ExportWorker(
        sessions,
        workspace,
        report_loader,
        ReportRenderer(),
        max_attempts=settings.export_max_attempts,
        expires_days=settings.export_expires_days,
        execution_timeout_seconds=settings.export_execution_timeout_seconds,
        telemetry=resolved_telemetry,
    )
    index = document_index or (
        ElasticsearchDocumentIndex(
            settings.elasticsearch_url,
            index_name=settings.elasticsearch_document_index,
            text_analyzer=settings.elasticsearch_text_analyzer,
            search_analyzer=settings.elasticsearch_search_analyzer,
        )
        if settings.elasticsearch_url
        else InMemoryDocumentIndex()
    )
    documents = DocumentService(sessions, document_store, index)
    graph_store = knowledge_graph_store or (
        Neo4jKnowledgeGraphStore(
            settings.neo4j_url,
            settings.neo4j_username,
            settings.neo4j_password,
            settings.neo4j_database,
        )
        if settings.neo4j_url
        else InMemoryKnowledgeGraphStore()
    )
    knowledge_graph = TaskKnowledgeGraphService(
        sessions,
        graph_store,
        ModelKnowledgeExtractor(runtime),
        EvidenceContentResolver(workspace, documents),
    )
    document_worker = DocumentIngestionWorker(sessions, documents, runtime, resolver)  # type: ignore[arg-type]
    retrieval = RetrievalGateway(
        documents, index, runtime, resolved_telemetry  # type: ignore[arg-type]
    )
    credential_resolver = (
        SecretsCredentialResolver(secrets_service)
        if secrets_service is not None
        else EnvironmentCredentialResolver()
    )
    database_egress = HostPortDatabaseEgressPolicy(
        settings.datasource_egress_allowlist
    )
    data_sources = DataSourceService(
        sessions,
        credential_resolver,
        VersionedPostgresFactory(settings.phase6_limits),
        database_egress,
    )
    write_policies = WritePolicyService(sessions)
    prepare_writes = PrepareWriteService(write_policies)
    pending_writes = PendingWriteService(sessions)
    resolved_write_targets = write_target_factory or PostgresWriteTargetFactory(
        ledger_schema=settings.external_write_ledger_schema,
        ledger_table=settings.external_write_ledger_table,
    )
    write_executor = WriteExecutor(
        sessions,
        write_policies,
        credential_resolver,
        resolved_write_targets,
        database_egress if write_target_factory is None else None,
    )
    datasets = DatasetService(sessions, workspace)
    memories = MemoryService(
        sessions, settings, resolver, telemetry=resolved_telemetry
    )
    memory_extraction_worker = MemoryExtractionWorker(
        sessions, memories, runtime  # type: ignore[arg-type]
    )
    memory_embedding_worker = MemoryEmbeddingWorker(
        sessions, memories, runtime  # type: ignore[arg-type]
    )
    compute_resources = ComputeResourceResolver(sessions, workspace, documents, datasets)
    sandbox_limits = SandboxLimits.from_phase6(settings.phase6_limits)
    sandbox = sandbox_runner or DockerSandboxRunner(
        image=settings.sandbox_image,
        runtime=settings.sandbox_runtime,
        limits=sandbox_limits,
    )

    registry = tool_registry or ToolRegistry()
    if tool_registry is None:
        definitions = [
            *web_tool_definitions(
                workspace=workspace,
                tavily_api_key=settings.resolve_secret(settings.tavily_secret_ref),
                jina_api_key=settings.resolve_secret(settings.jina_secret_ref),
            ),
            *document_tool_definitions(
                documents,
                retrieval,
                TaskBindingResolver(sessions),
            ),
            *data_tool_definitions(
                data_sources, workspace, settings.phase6_limits
            ),
            *dataset_tool_definitions(
                datasets, workspace, settings.phase6_limits
            ),
            *compute_tool_definitions(
                resolver=compute_resources,
                workspace=workspace,
                runner=sandbox,
                limits=settings.phase6_limits,
            ),
            *(
                knowledge_graph_tool_definitions(knowledge_graph)
                if settings.phase9_enabled
                else []
            ),
        ]
        for definition in definitions:
            registry.register(definition)
    tool_runtime = ToolRuntime(
        registry,
        session_factory=sessions,
        inline_budget=settings.tool_inline_budget,
        telemetry=resolved_telemetry,
        audit_service=audit_service,
    )
    policies = {
        "deep_scout": ToolPolicy(
            frozenset(
                {
                    "search_web",
                    "read_webpage",
                    "render_page",
                    "search_documents",
                    "search_chunks",
                    "read_document",
                    "query_knowledge_graph",
                }
            )
        ),
        "data_analyst": ToolPolicy(
            frozenset(
                {
                    "inspect_schema",
                    "sample_table",
                    "execute_readonly_sql",
                    "inspect_dataset",
                    "profile_dataset",
                    "query_dataset",
                }
            )
        ),
        "code_wizard": ToolPolicy(
            frozenset({"sandbox_execute", "generate_chart", "render_table"})
        ),
    }
    policy_registry = ToolPolicyRegistry(policies)
    skill_registry = SkillRegistry(
        SkillLoader(Path(__file__).resolve().parents[1] / "skills").load_all()
    )
    skill_runtime = SkillRuntime(skill_registry, telemetry=resolved_telemetry)
    capability_builder = CapabilitySnapshotBuilder(
        skill_registry,
        registry,
        policies,
    )
    required_bindings = PHASE2_BINDINGS if settings.phase2_enabled else PHASE1_BINDINGS
    task_service = TaskService(
        sessions,
        resolver,
        query_completion=QueryCompletionService(
            message_limit=settings.query_completion_message_limit,
            token_budget=settings.query_completion_token_budget,
        ),
        required_bindings=required_bindings,
        capability_snapshot_builder=lambda datasource_ids: capability_builder.build(
            datasource_ids
        ),
        phase9_enabled=settings.phase9_enabled,
        tool_canary_assigner=tool_canary_assigner,
    )
    node_runner = NodeRunner(
        sessions,
        ContextBuilder(
            dataset_scope_resolver=datasets.list_refs,
            datasource_scope_resolver=write_policies.metadata_scope,
            memory_context_resolver=memories.rehydrate,
            skill_runtime=skill_runtime,
        ),
        state_manager,
        resolved_telemetry,
    )
    graph = ResearchGraph(
        node_runner,
        runtime,  # type: ignore[arg-type]
        tool_registry=registry,
        tool_runtime=tool_runtime,
        tool_policies=policies,
        skill_runtime=skill_runtime,
        tool_policy_registry=policy_registry,
        decision_trace_service=decision_trace_service,
        evidence_pipeline=EvidencePipeline(workspace, documents),
        prepare_write_service=prepare_writes,
        citation_validator=citation_validator,
        report_publisher=ReportPublisher(workspace),
        memory_service=memories if settings.phase5_enabled else None,
        phase2_enabled=settings.phase2_enabled,
        phase3_enabled=settings.phase3_enabled,
        phase4_enabled=settings.phase4_enabled,
        agent_max_steps=settings.agent_max_steps,
        retrieval_branch_concurrency=settings.retrieval_branch_concurrency,
    )
    worker = ResearchWorker(
        sessions,
        graph,
        lease_seconds=settings.lease_seconds,
        tenant_max_running=settings.tenant_max_running_tasks,
        user_max_running=settings.user_max_running_tasks,
        telemetry=resolved_telemetry,
    )
    broker = event_broker or RedisEventBroker(
        settings.redis_url, heartbeat_seconds=settings.sse_heartbeat_seconds
    )
    return Container(
        settings=settings,
        session_factory=sessions,
        secrets_service=secrets_service,
        model_resolver=resolver,
        model_runtime=runtime,
        task_service=task_service,
        state_manager=state_manager,
        node_runner=node_runner,
        graph=graph,
        worker=worker,
        document_worker=document_worker,
        event_service=EventService(
            sessions,
            broker,
            connections_per_task_max=settings.sse_connections_per_task_max,
            connections_per_user_max=settings.sse_connections_per_user_max,
            client_buffer_events_max=settings.sse_client_buffer_events_max,
            client_buffer_bytes_max=settings.sse_client_buffer_bytes_max,
            retention_days=settings.task_event_retention_days,
        ),  # type: ignore[arg-type]
        workspace_service=workspace,
        document_service=documents,
        retrieval_gateway=retrieval,
        datasource_service=data_sources,
        pending_write_service=pending_writes,
        write_executor=write_executor,
        dataset_service=datasets,
        tool_registry=registry,
        tool_runtime=tool_runtime,
        skill_registry=skill_registry,
        skill_runtime=skill_runtime,
        tool_policy_registry=policy_registry,
        compute_resource_resolver=compute_resources,
        sandbox_runner=sandbox,
        memory_service=memories,
        memory_extraction_worker=memory_extraction_worker,
        memory_embedding_worker=memory_embedding_worker,
        knowledge_graph_service=knowledge_graph,
        telemetry=resolved_telemetry,
        audit_service=audit_service,
        decision_trace_service=decision_trace_service,
        tool_decision_review_service=tool_decision_review_service,
        share_manager=share_manager,
        export_manager=export_manager,
        export_worker=export_worker,
    )
