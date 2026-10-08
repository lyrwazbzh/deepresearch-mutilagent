from __future__ import annotations

from celery import Celery  # type: ignore[import-untyped]

from src.agent.config import Settings

settings = Settings.from_env()
celery_app = Celery("deepresearch", broker=settings.celery_broker_url)
celery_app.conf.update(
    task_ignore_result=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "research.run": {"queue": "research"},
        "document.ingest": {"queue": "ingestion"},
        "database_write.execute": {"queue": "research"},
        "memory.extract": {"queue": "research"},
        "memory.embed": {"queue": "research"},
        "memory.gc": {"queue": "research"},
        "memory.recover": {"queue": "research"},
        "knowledge_graph.build": {"queue": "research"},
        "report.export": {"queue": "export"},
    },
)


@celery_app.task(name="research.run")
def run_research_task(task_id: str) -> None:
    from src.agent.container import build_container
    from src.agent.errors import InvalidStateError

    try:
        build_container(settings).worker.run(task_id)
    except InvalidStateError:
        # At-least-once redelivery after another worker claimed or completed the task is a no-op.
        return


@celery_app.task(
    name="document.ingest",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def ingest_document(doc_id: str) -> None:
    from src.agent.container import build_container

    build_container(settings).document_worker.run(doc_id)


@celery_app.task(
    name="database_write.execute",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
)
def execute_database_write(write_id: str, plan_hash: str) -> None:
    from src.agent.container import build_container

    build_container(settings).write_executor.execute(write_id, plan_hash)


@celery_app.task(
    name="memory.extract",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def extract_memory(payload: dict[str, object]) -> None:
    from src.agent.container import build_container

    build_container(settings).memory_extraction_worker.run(payload)


@celery_app.task(
    name="memory.embed",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
)
def embed_memory(payload: dict[str, object]) -> None:
    from src.agent.container import build_container

    build_container(settings).memory_embedding_worker.run(payload)


@celery_app.task(name="memory.gc")
def gc_memory() -> None:
    from src.agent.container import build_container

    build_container(settings).memory_service.gc()


@celery_app.task(name="memory.recover")
def recover_memory_embeddings() -> None:
    from src.agent.container import build_container

    build_container(settings).memory_service.recover_pending_embeddings()


@celery_app.task(
    name="knowledge_graph.build",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
)
def build_task_knowledge_graph(task_id: str) -> None:
    from src.agent.container import build_container

    build_container(settings).knowledge_graph_service.build(task_id)


@celery_app.task(name="report.export")
def export_report(export_id: str) -> None:
    from src.agent.container import build_container

    build_container(settings).export_worker.run(export_id)
