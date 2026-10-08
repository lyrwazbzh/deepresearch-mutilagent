from __future__ import annotations

import json
import uuid
from dataclasses import replace

from sqlalchemy import func, select

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.evaluation.research_evaluation import (
    BaseSuite,
    EvalCaseResult,
    EvalRunner,
    EvalSuiteName,
    ImmutableDataset,
)
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import PHASE1_BINDINGS
from src.agent.backends.persistence.database import create_session_factory
from src.agent.backends.persistence.models import (
    EvalCaseResultRow,
    EvalRunRow,
    ModelBindingRow,
    ModelConfigHeadRow,
    ModelConfigRow,
    ObservabilitySpanRow,
)
from src.agent.task_service import CreateTaskRequest


class SmokeRagSuite(BaseSuite):
    name: EvalSuiteName = "rag"

    def execute_case(self, case, eval_scope_id, manifest):
        del eval_scope_id, manifest
        return EvalCaseResult(
            case_id=str(case["case_id"]),
            outcome="passed",
            metrics={"recall_at_5": 1.0},
            diagnostics={"persistence_smoke": True},
        )

    def summarize(self, results):
        return {"recall_at_5": sum(item.metrics["recall_at_5"] for item in results) / len(results)}


def main() -> None:
    base = Settings.from_env()
    suffix = uuid.uuid4().hex[:10]
    tenant_id = f"tenant_phase12_{suffix}"
    user_id = f"user_phase12_{suffix}"
    config_id = f"model_phase12_{suffix}"
    sessions = create_session_factory(base.database_url)
    with sessions() as session, session.begin():
        session.add(
            ModelConfigRow(
                model_config_id=config_id,
                version=1,
                tenant_id=tenant_id,
                user_id=user_id,
                provider="fake",
                model="phase12-nonbillable",
                purpose="chat",
            )
        )
        session.flush()
        session.add(
            ModelConfigHeadRow(
                tenant_id=tenant_id,
                user_id=user_id,
                model_config_id=config_id,
                current_version=1,
                enabled_for_new_tasks=True,
            )
        )
        session.flush()
        for key in PHASE1_BINDINGS:
            session.add(
                ModelBindingRow(
                    tenant_id=tenant_id,
                    binding_key=key,
                    purpose="chat",
                    priority=0,
                    model_config_id=config_id,
                )
            )
    settings = replace(
        base,
        default_tenant_id=tenant_id,
        default_user_id=user_id,
        model_mode="fake",
        auth_enabled=False,
        phase2_enabled=False,
        phase3_enabled=False,
        phase5_enabled=False,
        phase9_enabled=False,
        minio_endpoint=None,
        elasticsearch_url=None,
        neo4j_url=None,
        telemetry_enabled=True,
        telemetry_export_delay_millis=50,
    )
    container = build_container(
        settings,
        session_factory=sessions,
        event_broker=InMemoryEventBroker(),
    )
    created = container.task_service.create(
        tenant_id,
        user_id,
        f"phase12-real-{suffix}",
        CreateTaskRequest(query="Verify the observable runtime."),
    )
    result = container.worker.run(created.projection.task_id)
    assert result.outcome == "success"
    assert container.telemetry is not None
    assert container.telemetry.force_flush()

    dataset = ImmutableDataset(
        suite="rag",
        version="phase12-real-smoke-v1",
        cases=({"case_id": "persistence_1"},),
    )
    eval_run = EvalRunner(sessions).run(
        SmokeRagSuite(),
        dataset,
        config_snapshot={"retrieval_config": "persistence-smoke"},
        code_revision="working-tree",
    )
    with sessions() as session:
        spans = list(
            session.scalars(
                select(ObservabilitySpanRow)
                .where(
                    ObservabilitySpanRow.tenant_id == tenant_id,
                    ObservabilitySpanRow.task_id == created.projection.task_id,
                )
                .order_by(ObservabilitySpanRow.started_at)
            )
        )
        case_count = session.scalar(
            select(func.count())
            .select_from(EvalCaseResultRow)
            .where(EvalCaseResultRow.eval_run_id == eval_run.eval_run_id)
        )
        persisted_run = session.get(EvalRunRow, eval_run.eval_run_id)
    names = [row.name for row in spans]
    assert "research.run" in names
    assert names.count("graph.node") == 2
    assert names.count("gen_ai.chat") == 2
    assert any(row.kind == "CLIENT" for row in spans)
    assert all("query" not in row.attributes for row in spans)
    assert case_count == 1 and persisted_run is not None
    print(
        json.dumps(
            {
                "task_outcome": result.outcome,
                "task_id": created.projection.task_id,
                "span_count": len(spans),
                "span_names": names,
                "eval_run_id": eval_run.eval_run_id,
                "eval_status": persisted_run.status,
                "eval_case_count": case_count,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
