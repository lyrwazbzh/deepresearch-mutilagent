from __future__ import annotations

import hashlib
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict

from src.agent.schema.runtime import AssistantMessageEffect, ContextPacket, ExecutionContext, NodeResult, StatePatch
from src.agent.schema.state import AdoptedResult, CriticResult, HypothesisPlan, RouteDecision
from src.agent.schema.report import ReportAst
from src.agent.schema.write import WritePlan
from src.agent.llm.runtime import ChatModel
from src.agent.runtime.agent_loop import AgentLoop
from src.agent.runtime.tool_runtime import ToolPolicy


class PromptIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str
    revision: str


class PromptRenderer:
    def __init__(self, prompt_dir: Path | None = None) -> None:
        directory = prompt_dir or Path(__file__).resolve().parents[1] / "prompts"
        self.environment = Environment(
            loader=FileSystemLoader(directory),
            undefined=StrictUndefined,
            autoescape=False,
        )

    def render(self, name: str, packet: ContextPacket) -> str:
        return self.environment.get_template(name).render(packet=packet.model_dump(mode="json"))

    def identity(self, name: str) -> PromptIdentity:
        source, _, _ = self.environment.loader.get_source(self.environment, name)  # type: ignore[union-attr]
        return PromptIdentity(
            template=name,
            revision=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        )

    def trace(self, name: str, packet: ContextPacket, model: ChatModel) -> dict[str, object]:
        runtime = packet.payload.get("runtime", {})
        return {
            "agent_role": packet.role,
            "prompt": self.identity(name).model_dump(mode="json"),
            "model_binding": getattr(model, "binding_identity", None),
            "task_id": packet.task_id,
            "node_run_id": runtime.get("node_run_id"),
        }


class RouteNode:
    def __init__(self, renderer: PromptRenderer | None = None) -> None:
        self.renderer = renderer or PromptRenderer()

    def run(self, packet: ContextPacket, model: ChatModel) -> NodeResult:
        prompt_name = "route_node.jinja2"
        decision = model.invoke_structured(self.renderer.render(prompt_name, packet), RouteDecision)
        patch: dict[str, object] = {"route": decision.model_dump(mode="json")}
        if decision.mode != "deep":
            patch["plan"] = None
        return NodeResult(
            patch=StatePatch(values=patch),
            output={
                "route": patch["route"],
                "agent_trace": self.renderer.trace(prompt_name, packet, model),
            },
        )


class ChiefArchitect:
    def __init__(self, renderer: PromptRenderer | None = None) -> None:
        self.renderer = renderer or PromptRenderer()

    def direct(self, packet: ContextPacket, model: ChatModel) -> NodeResult:
        prompt_name = "chief_architect_direct.jinja2"
        answer = model.invoke_text(self.renderer.render(prompt_name, packet))
        return NodeResult(
            effects=[AssistantMessageEffect(content=answer)],
            output={
                "answer": answer,
                "agent_trace": self.renderer.trace(prompt_name, packet, model),
            },
        )

    def plan(self, packet: ContextPacket, model: ChatModel) -> NodeResult:
        prompt_name = "chief_architect_plan.jinja2"
        plan = model.invoke_structured(
            self.renderer.render(prompt_name, packet), HypothesisPlan
        )
        return NodeResult(
            patch=StatePatch(values={"plan": plan.model_dump(mode="json")}),
            output={
                "plan": plan.model_dump(mode="json"),
                "agent_trace": self.renderer.trace(prompt_name, packet, model),
            },
        )

    def replan(self, packet: ContextPacket, model: ChatModel) -> NodeResult:
        prompt_name = "chief_architect_replan.jinja2"
        plan = model.invoke_structured(
            self.renderer.render(prompt_name, packet), HypothesisPlan
        )
        return NodeResult(
            patch=StatePatch(values={"plan": plan.model_dump(mode="json")}),
            output={
                "plan": plan.model_dump(mode="json"),
                "agent_trace": self.renderer.trace(prompt_name, packet, model),
            },
        )


class FocusedAgent:
    prompt_name: str
    agent_name: str

    def __init__(
        self,
        loop: AgentLoop,
        policy: ToolPolicy,
        renderer: PromptRenderer | None = None,
    ) -> None:
        self.loop = loop
        self.policy = policy
        self.renderer = renderer or PromptRenderer()

    def run(
        self,
        packet: ContextPacket,
        execution: ExecutionContext,
        model: ChatModel,
    ) -> NodeResult:
        prompt = self.renderer.render(self.prompt_name, packet)
        answer = self.loop.run(
            agent_name=self.agent_name,
            model=model,
            packet=packet,
            execution=execution,
            policy=self.policy,
            prompt=prompt,
        )
        return NodeResult(
            effects=[AssistantMessageEffect(content=answer)],
            output={
                "answer": answer,
                "agent": self.agent_name,
                "agent_trace": self.renderer.trace(self.prompt_name, packet, model),
            },
        )

    def retrieve(
        self,
        packet: ContextPacket,
        execution: ExecutionContext,
        model: ChatModel,
        branch: str,
    ) -> list[AdoptedResult]:
        items: list[AdoptedResult] = []
        runtime = packet.payload.get("runtime")
        for assignment in packet.payload.get("assignments", []):
            hypothesis = assignment["hypothesis"]
            scoped_packet = packet.model_copy(
                update={
                    "payload": {
                        "branch": branch,
                        "hypothesis": hypothesis,
                        "queries": assignment["queries"],
                        "relevant_evidence_summaries": assignment.get(
                            "relevant_evidence_summaries", []
                        ),
                        "resource_scope": packet.payload.get("resource_scope", {}),
                        "runtime": runtime,
                    }
                }
            )
            items.extend(
                self.loop.run_retrieval(
                    agent_name=self.agent_name,
                    branch=branch,  # type: ignore[arg-type]
                    hypothesis_id=hypothesis["hypothesis_id"],
                    model=model,
                    packet=scoped_packet,
                    execution=execution,
                    policy=self.policy,
                    prompt=self.renderer.render(self.prompt_name, scoped_packet),
                )
            )
        return items


class DeepScout(FocusedAgent):
    prompt_name = "deep_scout.jinja2"
    agent_name = "deep_scout"


class DataAnalyst(FocusedAgent):
    prompt_name = "data_analyst.jinja2"
    agent_name = "data_analyst"

    def prepare_write(self, packet: ContextPacket, model: ChatModel) -> WritePlan:
        return model.invoke_structured(
            self.renderer.render("data_analyst_prepare_write.jinja2", packet), WritePlan
        )


class CodeWizard(FocusedAgent):
    prompt_name = "code_wizard.jinja2"
    agent_name = "code_wizard"

    def compute(
        self,
        packet: ContextPacket,
        execution: ExecutionContext,
        model: ChatModel,
    ) -> list[AdoptedResult]:
        request = packet.payload.get("analysis_request")
        if not isinstance(request, dict):
            raise ValueError("CodeWizard requires one AnalysisRequest")
        return self.loop.run_retrieval(
            agent_name=self.agent_name,
            branch="compute",
            hypothesis_id=str(request["hypothesis_id"]),
            model=model,
            packet=packet,
            execution=execution,
            policy=self.policy,
            prompt=self.renderer.render(self.prompt_name, packet),
        )


class CriticMaster:
    def __init__(self, renderer: PromptRenderer | None = None) -> None:
        self.renderer = renderer or PromptRenderer()

    def review(self, packet: ContextPacket, model: ChatModel) -> NodeResult:
        prompt_name = "critic_master.jinja2"
        result = model.invoke_structured(
            self.renderer.render(prompt_name, packet), CriticResult
        )
        return NodeResult(
            patch=StatePatch(values={"critic": result.model_dump(mode="json")}),
            output={
                "critic": result.model_dump(mode="json"),
                "agent_trace": self.renderer.trace(prompt_name, packet, model),
            },
        )


class LeadWriter:
    def __init__(self, renderer: PromptRenderer | None = None) -> None:
        self.renderer = renderer or PromptRenderer()

    def write(self, packet: ContextPacket, model: ChatModel) -> ReportAst:
        candidate = model.invoke_structured(
            self.renderer.render("lead_writer.jinja2", packet), ReportAst
        )
        self._validate_candidate(packet, candidate)
        return candidate

    def revise(self, packet: ContextPacket, model: ChatModel) -> ReportAst:
        candidate = model.invoke_structured(
            self.renderer.render("lead_writer_revise.jinja2", packet), ReportAst
        )
        self._validate_candidate(packet, candidate)
        return candidate

    @staticmethod
    def _validate_candidate(packet: ContextPacket, candidate: ReportAst) -> None:
        allowed = {
            str(item["evidence_id"])
            for item in packet.payload.get("approved_evidence", [])
            if isinstance(item, dict) and item.get("evidence_id")
        }
        unknown = sorted(
            {citation.evidence_id for citation in candidate.citations} - allowed
        )
        if unknown:
            raise ValueError(f"ReportAst cites Evidence outside approved context: {unknown}")
