from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock, RLock
from typing import Any

from sqlalchemy import select

from src.agent.identity import analysis_request_id, canonical_json
from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import (
    AssistantMessageEffect,
    Failpoint,
    NodeResult,
    PendingWriteEffect,
    StatePatch,
)
from src.agent.schema.state import AnalysisRequest, HypothesisPlan, ResearchState
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    ConversationMessageRow,
    NodeEffectRow,
    NodeRunRow,
    OutboxMessageRow,
    PendingWriteRow,
    ResearchCheckpointRow,
    ResearchEventRow,
    ResearchTaskRow,
    TaskKnowledgeGraphRow,
    utcnow,
)
from src.agent.backends.persistence.repositories import CheckpointRepository
from src.agent.errors import LeaseLostError, PatchOwnershipError
from src.agent.task_service import new_id


NODE_OWNERSHIP: dict[str, frozenset[str]] = {
    "route": frozenset({"route", "plan"}),
    "focused_dispatch": frozenset(),
    "chief_direct": frozenset(),
    "chief_plan": frozenset({"plan"}),
    "memory_recall_plan": frozenset({"memory_context"}),
    "deep_scout": frozenset(),
    "data_analyst": frozenset(),
    "code_wizard": frozenset(),
    "retrieval_dispatch": frozenset({"research_rounds"}),
    "deep_scout_web": frozenset({"research_rounds"}),
    "deep_scout_rag": frozenset({"research_rounds"}),
    "data_analyst_sql": frozenset({"research_rounds"}),
    "retrieval_join": frozenset(),
    "merge_evidence": frozenset({"evidence_pool"}),
    "analysis_decision": frozenset({"analysis_requests"}),
    "code_wizard_compute": frozenset({"research_rounds", "analysis_requests"}),
    "coverage": frozenset({"coverage"}),
    "chief_deepen": frozenset({"plan", "execution"}),
    "memory_recall_deepen": frozenset({"memory_context"}),
    "critic": frozenset({"critic"}),
    "phase3_boundary": frozenset(),
    "database_write_gate": frozenset(),
    "prepare_write": frozenset({"pending_write"}),
    "lead_writer": frozenset(),
    "validate_report": frozenset({"report"}),
    "revise": frozenset({"execution"}),
    "finalize": frozenset({"report"}),
    "graph_success": frozenset(),
    "graph_failure": frozenset({"reliability"}),
}

BRANCH_OWNERSHIP = {
    "deep_scout_web": "web",
    "deep_scout_rag": "rag",
    "data_analyst_sql": "sql",
    "code_wizard_compute": "compute",
}


@dataclass(frozen=True)
class CommitOutcome:
    state: ResearchState
    checkpoint_id: str
    event_id: int


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class StateManager:
    def __init__(
        self,
        session_factory: SessionFactory,
        failpoint: Failpoint | None = None,
        *,
        phase5_enabled: bool = False,
        phase9_enabled: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.failpoint = failpoint or (lambda _: None)
        self.phase5_enabled = phase5_enabled
        self.phase9_enabled = phase9_enabled
        # PostgreSQL serializes concurrent branch commits through the row lock below.
        # SQLite ignores SELECT .. FOR UPDATE, so keep an equivalent per-task lock in
        # the worker process as well.  This also narrows the race window before the
        # database lock is acquired on production backends.
        self._task_locks_guard = Lock()
        self._task_locks: dict[str, RLock] = {}

    def _hit(self, step: str) -> None:
        self.failpoint(step)

    def _task_lock(self, task_id: str) -> RLock:
        with self._task_locks_guard:
            return self._task_locks.setdefault(task_id, RLock())

    def commit_node(
        self,
        node_run_id: str,
        patch: StatePatch,
        expected_lease_token: str,
        *,
        effects: list[AssistantMessageEffect | PendingWriteEffect] | None = None,
        output: dict[str, Any] | None = None,
    ) -> CommitOutcome:
        effects = effects or []
        now = utcnow()
        with self.session_factory() as lookup_session:
            task_id = lookup_session.scalar(
                select(NodeRunRow.task_id).where(NodeRunRow.node_run_id == node_run_id)
            )
        if task_id is None:
            raise LookupError(f"NodeRun {node_run_id} not found")
        with self._task_lock(task_id), self.session_factory() as session, session.begin():
            node_run = session.scalar(
                select(NodeRunRow).where(NodeRunRow.node_run_id == node_run_id).with_for_update()
            )
            if node_run is None:
                raise LookupError(f"NodeRun {node_run_id} not found")
            task = session.scalar(
                select(ResearchTaskRow)
                .where(ResearchTaskRow.task_id == node_run.task_id)
                .with_for_update()
            )
            if task is None:
                raise LookupError(f"Task {node_run.task_id} not found")
            leased_until = _aware(task.leased_until)
            if (
                task.status not in {"running", "pausing", "cancelling"}
                or task.lease_token != expected_lease_token
                or leased_until is None
                or leased_until <= now
            ):
                raise LeaseLostError("Task lease is stale or no longer active.")
            if node_run.status == "completed":
                checkpoint = session.get(ResearchCheckpointRow, node_run.output_checkpoint_id)
                if checkpoint is None:
                    raise RuntimeError("Completed NodeRun has no checkpoint")
                event = session.scalar(
                    select(ResearchEventRow)
                    .where(
                        ResearchEventRow.task_id == task.task_id,
                        ResearchEventRow.payload_json["node_run_id"].as_string() == node_run_id,
                    )
                    .order_by(ResearchEventRow.event_id.desc())
                    .limit(1)
                )
                return CommitOutcome(
                    state=ResearchState.model_validate(checkpoint.state_snapshot),
                    checkpoint_id=checkpoint.checkpoint_id,
                    event_id=event.event_id if event else 0,
                )

            allowed = NODE_OWNERSHIP.get(node_run.node_name)
            if allowed is None:
                raise PatchOwnershipError(f"Unknown node ownership: {node_run.node_name}")
            patch_fields = set(patch.values) | set(patch.list_appends)
            if patch.branch_updates:
                patch_fields.add("research_rounds")
            if patch.analysis_request_appends or patch.analysis_status_updates:
                patch_fields.add("analysis_requests")
            forbidden = patch_fields - set(allowed)
            if forbidden:
                raise PatchOwnershipError(
                    f"{node_run.node_name} cannot write fields: {sorted(forbidden)}"
                )
            if (
                "analysis_requests" in patch.values
                or "analysis_requests" in patch.list_appends
            ):
                raise PatchOwnershipError(
                    "analysis_requests must use its single-producer/status patch operations"
                )

            if node_run.node_name in BRANCH_OWNERSHIP and "research_rounds" in patch.values:
                self._validate_branch_patch(
                    node_run.node_name,
                    task.latest_state.get("research_rounds", {}),
                    patch.values["research_rounds"],
                )
            if patch.branch_updates:
                self._validate_branch_updates(node_run.node_name, patch.branch_updates)
            if patch.analysis_request_appends:
                self._validate_analysis_appends(
                    node_run.node_name,
                    task.task_id,
                    task.latest_state,
                    patch.analysis_request_appends,
                )
            if patch.analysis_status_updates:
                self._validate_analysis_status_updates(
                    node_run.node_name,
                    task.latest_state,
                    patch.analysis_status_updates,
                )
            if node_run.node_name == "chief_deepen":
                self._validate_deepen_patch(task.latest_state, patch.values)
            if node_run.node_name == "revise":
                self._validate_revision_patch(task.latest_state, patch.values)
            pending_effects = [item for item in effects if isinstance(item, PendingWriteEffect)]
            if pending_effects:
                if node_run.node_name != "prepare_write" or len(pending_effects) != 1:
                    raise PatchOwnershipError("Only PrepareWrite may create one PendingWrite")
                if patch.values.get("pending_write") != pending_effects[0].pending_write.get("write_id"):
                    raise PatchOwnershipError("PendingWrite effect must match ResearchState.pending_write")

            current_payload = dict(task.latest_state)
            current_payload.update(patch.values)
            if patch.branch_updates:
                rounds = dict(current_payload.get("research_rounds") or {})
                for depth, updates in patch.branch_updates.items():
                    round_payload = dict(rounds.get(depth) or {})
                    round_payload.update(updates)
                    rounds[depth] = round_payload
                current_payload["research_rounds"] = rounds
            for field, additions in patch.list_appends.items():
                current = current_payload.get(field)
                if not isinstance(current, list):
                    raise PatchOwnershipError(f"{field} is not an appendable list field")
                if field == "evidence_pool":
                    by_id = {item["evidence_id"]: item for item in current}
                    appended = list(current)
                    for item in additions:
                        identity = item.get("evidence_id") if isinstance(item, dict) else None
                        if not identity:
                            raise ValueError("Evidence append requires evidence_id")
                        prior = by_id.get(identity)
                        if prior is not None:
                            if prior != item:
                                raise ValueError(
                                    f"Evidence {identity} conflicts with committed state"
                                )
                            continue
                        by_id[identity] = item
                        appended.append(item)
                    current_payload[field] = appended
                else:
                    current_payload[field] = [*current, *additions]
            if patch.analysis_request_appends:
                requests = list(current_payload.get("analysis_requests") or [])
                by_id = {item["analysis_id"]: item for item in requests}
                for item in patch.analysis_request_appends:
                    prior = by_id.get(item["analysis_id"])
                    if prior is not None:
                        immutable_prior = {key: value for key, value in prior.items() if key != "status"}
                        immutable_item = {key: value for key, value in item.items() if key != "status"}
                        if immutable_prior != immutable_item:
                            raise ValueError(
                                f"AnalysisRequest {item['analysis_id']} conflicts with committed state"
                            )
                        continue
                    requests.append(item)
                    by_id[item["analysis_id"]] = item
                current_payload["analysis_requests"] = requests
            if patch.analysis_status_updates:
                requests = list(current_payload.get("analysis_requests") or [])
                positions = {item["analysis_id"]: index for index, item in enumerate(requests)}
                for analysis_id, status in patch.analysis_status_updates.items():
                    index = positions[analysis_id]
                    prior = dict(requests[index])
                    old_status = prior.get("status", "pending")
                    if old_status not in {"pending", status}:
                        raise ValueError(
                            f"AnalysisRequest {analysis_id} already finished as {old_status}"
                        )
                    prior["status"] = status
                    requests[index] = prior
                current_payload["analysis_requests"] = requests
            timestamps = dict(current_payload.get("timestamps") or {})
            timestamps["updated_at"] = now.isoformat()
            current_payload["timestamps"] = timestamps
            state = ResearchState.model_validate(current_payload)
            state_json = state.model_dump(mode="json")
            task.latest_state = state_json
            task.updated_at = now
            self._hit("state_updated")

            checkpoint_id = new_id("ckpt")
            checkpoint = ResearchCheckpointRow(
                checkpoint_id=checkpoint_id,
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                checkpoint_seq=CheckpointRepository(
                    session, task.tenant_id, task.task_id
                ).next_seq(),
                state_snapshot=state_json,
                graph_position=node_run.node_name,
                scheduling_state={"node_key": node_run.node_key},
            )
            session.add(checkpoint)
            self._hit("checkpoint_added")

            changed = sorted(patch_fields)
            new_evidence = patch.list_appends.get("evidence_pool", [])
            if (
                self.phase9_enabled
                and node_run.node_name == "merge_evidence"
                and new_evidence
            ):
                query_hash = hashlib.sha256(
                    " ".join(state.query.casefold().split()).encode()
                ).hexdigest()
                hypotheses = (
                    [
                        {
                            "hypothesis_id": item.hypothesis_id,
                            "claim": item.claim,
                        }
                        for item in state.plan.hypotheses
                    ]
                    if state.plan
                    else []
                )
                plan_hash = hashlib.sha256(
                    canonical_json(hypotheses)
                ).hexdigest()
                metadata = session.get(
                    TaskKnowledgeGraphRow, (task.tenant_id, task.task_id)
                )
                if metadata is None:
                    metadata = TaskKnowledgeGraphRow(
                        tenant_id=task.tenant_id,
                        task_id=task.task_id,
                        status="building",
                        query_hash=query_hash,
                        plan_hash=plan_hash,
                        processed_evidence_count=0,
                        updated_at=now,
                    )
                    session.add(metadata)
                else:
                    if (
                        metadata.query_hash != query_hash
                        or metadata.plan_hash != plan_hash
                    ):
                        metadata.processed_evidence_count = 0
                    metadata.status = "building"
                    metadata.query_hash = query_hash
                    metadata.plan_hash = plan_hash
                    metadata.updated_at = now
                session.add(
                    OutboxMessageRow(
                        message_id=new_id("outbox"),
                        tenant_id=task.tenant_id,
                        kind="knowledge_graph.build",
                        aggregate_id=task.task_id,
                        payload_json={"task_id": task.task_id},
                        idempotency_key=f"kg:{task.task_id}:{node_run.node_run_id}",
                        available_at=now,
                    )
                )
                changed.append("knowledge_graph")
            for index, effect in enumerate(effects):
                self._commit_effect(session, task, node_run, effect, index)
                if effect.kind == "assistant_message" and "messages" not in changed:
                    changed.append("messages")
            if pending_effects:
                changed.extend(item for item in ("task_status",) if item not in changed)
            if node_run.node_name in {"route", "chief_plan", "focused_dispatch"} and "phase" not in changed:
                changed.append("phase")

            event = ResearchEventRow(
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                event_type="task_updated",
                payload_json={
                    "event_id": 0,
                    "task_id": task.task_id,
                    "changed": changed,
                    "at": now.isoformat(),
                    "node_run_id": node_run.node_run_id,
                },
                created_at=now,
            )
            session.add(event)
            session.flush()
            event.payload_json = {**event.payload_json, "event_id": event.event_id}
            self._hit("event_added")
            session.add(
                OutboxMessageRow(
                    message_id=new_id("outbox"),
                    tenant_id=task.tenant_id,
                    kind="research.event.publish",
                    aggregate_id=str(event.event_id),
                    payload_json={"task_id": task.task_id, "event_id": event.event_id},
                    idempotency_key=f"event:{event.event_id}",
                    available_at=now,
                )
            )
            self._hit("outbox_added")

            result = NodeResult(
                patch=patch,
                effects=effects,
                output=output or {},
            )
            node_run.result_json = result.model_dump(mode="json")
            node_run.output_checkpoint_id = checkpoint_id
            node_run.status = "completed"
            node_run.updated_at = now
            if pending_effects:
                budget = TaskBudget.model_validate(task.task_budget)
                accounted = _aware(task.budget_accounted_at)
                if accounted is not None:
                    budget.usage.wall_time_ms += max(
                        0, int((now - accounted).total_seconds() * 1000)
                    )
                task.task_budget = budget.model_dump(mode="json")
                task.status = "paused"
                task.lease_token = None
                task.leased_until = None
                task.budget_accounted_at = None
            self._hit("node_completed")
            session.flush()
            return CommitOutcome(state=state, checkpoint_id=checkpoint_id, event_id=event.event_id)

    @staticmethod
    def _validate_branch_patch(
        node_name: str,
        current_rounds: dict[str, Any],
        proposed_rounds: Any,
    ) -> None:
        if not isinstance(proposed_rounds, dict):
            raise PatchOwnershipError("research_rounds patch must be an object")
        owned_branch = BRANCH_OWNERSHIP[node_name]
        all_depths = set(current_rounds) | set(proposed_rounds)
        for depth in all_depths:
            before = current_rounds.get(depth, {})
            after = proposed_rounds.get(depth, {})
            if not isinstance(before, dict) or not isinstance(after, dict):
                raise PatchOwnershipError("research_rounds depth must be an object")
            for branch in set(before) | set(after):
                if branch != owned_branch and before.get(branch) != after.get(branch):
                    raise PatchOwnershipError(
                        f"{node_name} cannot write research_rounds[{depth}].{branch}"
                    )

    @staticmethod
    def _validate_branch_updates(
        node_name: str, branch_updates: dict[str, dict[str, Any]]
    ) -> None:
        owned_branch = BRANCH_OWNERSHIP.get(node_name)
        if owned_branch is None:
            raise PatchOwnershipError(f"{node_name} cannot submit branch updates")
        for depth, updates in branch_updates.items():
            if not depth.isdigit() or not isinstance(updates, dict) or not updates:
                raise PatchOwnershipError("branch update must target a numeric depth")
            forbidden = set(updates) - {owned_branch}
            if forbidden:
                raise PatchOwnershipError(
                    f"{node_name} cannot write branches: {sorted(forbidden)}"
                )

    @staticmethod
    def _validate_analysis_appends(
        node_name: str,
        task_id: str,
        current_state: dict[str, Any],
        additions: list[dict[str, Any]],
    ) -> None:
        if node_name != "analysis_decision":
            raise PatchOwnershipError(f"{node_name} cannot create AnalysisRequest")
        state = ResearchState.model_validate(current_state)
        hypothesis_ids = {
            hypothesis.hypothesis_id for hypothesis in state.plan.hypotheses
        } if state.plan else set()
        evidence_ids = {evidence.evidence_id for evidence in state.evidence_pool}
        for payload in additions:
            request = AnalysisRequest.model_validate(payload)
            if request.status != "pending":
                raise PatchOwnershipError("new AnalysisRequest must be pending")
            if request.hypothesis_id not in hypothesis_ids:
                raise ValueError(f"Unknown hypothesis_id: {request.hypothesis_id}")
            if not set(request.input_evidence_ids) <= evidence_ids:
                raise ValueError("AnalysisRequest references unknown Evidence")
            expected = analysis_request_id(
                task_id,
                request.hypothesis_id,
                request.objective,
                request.input_evidence_ids,
                request.expected_output,
            )
            if request.analysis_id != expected:
                raise ValueError("AnalysisRequest analysis_id is not its stable semantic identity")

    @staticmethod
    def _validate_analysis_status_updates(
        node_name: str,
        current_state: dict[str, Any],
        updates: Mapping[str, str],
    ) -> None:
        if node_name != "code_wizard_compute":
            raise PatchOwnershipError(f"{node_name} cannot update AnalysisRequest status")
        existing = {
            item["analysis_id"]: item for item in current_state.get("analysis_requests", [])
        }
        missing = set(updates) - set(existing)
        if missing:
            raise ValueError(f"Unknown AnalysisRequest values: {sorted(missing)}")

    @staticmethod
    def _validate_deepen_patch(
        current_state: dict[str, Any], proposed: dict[str, Any]
    ) -> None:
        # A valid deepen decision may conclude that every proposed query is a
        # duplicate.  In that terminal case Deepen records only node output so
        # the graph can stop with ``no_new_queries``; there is no state patch to
        # validate.  Any non-empty patch must still atomically advance both the
        # plan and execution depth.
        if not proposed:
            return
        if set(proposed) != {"plan", "execution"}:
            raise PatchOwnershipError("Deepen must atomically update only plan and execution")
        current = ResearchState.model_validate(current_state)
        if current.plan is None:
            raise ValueError("Deepen requires an existing plan")
        plan = HypothesisPlan.model_validate(proposed["plan"])
        before_by_id = {item.hypothesis_id: item for item in current.plan.hypotheses}
        after_by_id = {item.hypothesis_id: item for item in plan.hypotheses}
        if not set(before_by_id) <= set(after_by_id):
            raise PatchOwnershipError("Deepen cannot remove hypotheses")
        for hypothesis_id, before in before_by_id.items():
            after = after_by_id[hypothesis_id]
            if (
                before.claim != after.claim
                or before.priority != after.priority
                or before.refuted_if != after.refuted_if
                or not set(before.evidence_needed) <= set(after.evidence_needed)
            ):
                raise PatchOwnershipError("Deepen cannot rewrite an existing hypothesis")
            old_queries = {item.query_id: item for item in before.queries}
            new_queries = {item.query_id: item for item in after.queries}
            if not set(old_queries) <= set(new_queries) or any(
                new_queries[key] != value for key, value in old_queries.items()
            ):
                raise PatchOwnershipError("Deepen cannot remove or rewrite an existing query")
        if plan.deepen_history[: len(current.plan.deepen_history)] != current.plan.deepen_history:
            raise PatchOwnershipError("Deepen history must be append-only")
        execution = dict(proposed["execution"])
        current_execution = current.execution.model_dump(mode="json")
        expected = {**current_execution, "depth": current.execution.depth + 1}
        if execution != expected or execution["depth"] > execution["max_depth"]:
            raise PatchOwnershipError("Deepen must increment research depth exactly once")

    @staticmethod
    def _validate_revision_patch(
        current_state: dict[str, Any], proposed: dict[str, Any]
    ) -> None:
        if set(proposed) != {"execution"}:
            raise PatchOwnershipError("Revise must update only execution")
        current = ResearchState.model_validate(current_state)
        execution = dict(proposed["execution"])
        expected = {
            **current.execution.model_dump(mode="json"),
            "revision_round": current.execution.revision_round + 1,
        }
        if execution != expected or execution["revision_round"] > execution["max_revision_rounds"]:
            raise PatchOwnershipError("Revise must increment revision_round exactly once")

    def _commit_effect(
        self,
        session: Any,
        task: ResearchTaskRow,
        node_run: NodeRunRow,
        effect: AssistantMessageEffect | PendingWriteEffect,
        index: int,
    ) -> None:
        if isinstance(effect, PendingWriteEffect):
            payload = effect.pending_write
            existing = session.scalar(
                select(PendingWriteRow).where(
                    PendingWriteRow.tenant_id == task.tenant_id,
                    PendingWriteRow.write_id == payload["write_id"],
                )
            )
            if existing is not None:
                if existing.plan_hash != payload["plan_hash"]:
                    raise ValueError("PendingWrite replay changed plan_hash")
                return
            session.add(
                PendingWriteRow(
                    write_id=payload["write_id"], task_id=task.task_id,
                    tenant_id=task.tenant_id, user_id=task.user_id,
                    plan_json=payload["plan"], plan_hash=payload["plan_hash"],
                    preview_json=payload["preview"],
                    policy_revision=payload["policy_revision"], status="pending",
                    operation_key=payload["operation_key"],
                    expires_at=payload.get("expires_at"),
                )
            )
            return
        effect_name = f"{effect.kind}:{index}"
        operation_key = hashlib.sha256(
            f"{task.tenant_id}|{task.task_id}|{node_run.node_run_id}|{effect_name}".encode()
        ).hexdigest()
        existing = session.scalar(
            select(NodeEffectRow).where(NodeEffectRow.operation_key == operation_key)
        )
        if existing is not None:
            return
        message_id = f"msg_{operation_key[:32]}"
        session.add(
            ConversationMessageRow(
                message_id=message_id,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                thread_id=task.thread_id,
                task_id=task.task_id,
                role="assistant",
                content=effect.content,
            )
        )
        if self.phase5_enabled:
            session.add(
                OutboxMessageRow(
                    message_id=new_id("outbox"),
                    tenant_id=task.tenant_id,
                    kind="memory.extraction.requested",
                    aggregate_id=task.thread_id,
                    payload_json={
                        "tenant_id": task.tenant_id,
                        "user_id": task.user_id,
                        "thread_id": task.thread_id,
                        "task_id": task.task_id,
                        "assistant_message_id": message_id,
                        "extractor_version": "research-ltm-extractor-v1",
                        "prompt_version": "research-ltm-prompt-v1",
                        "input_hash": hashlib.sha256(
                            effect.content.encode("utf-8")
                        ).hexdigest(),
                    },
                    idempotency_key=f"memory-extraction:{message_id}",
                    available_at=utcnow() + timedelta(seconds=30),
                )
            )
        session.add(
            NodeEffectRow(
                effect_id=new_id("effect"),
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                node_run_id=node_run.node_run_id,
                effect_name=effect_name,
                operation_key=operation_key,
                result_json={"message_id": message_id},
            )
        )
