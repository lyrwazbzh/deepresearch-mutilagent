from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.schema.capability import CapabilitySnapshot, FrozenSkill
from src.agent.runtime.tool_runtime import ToolPolicy, ToolPolicyRegistry, ToolRegistry
from src.agent.backends.observability import Telemetry


class SkillError(RuntimeError):
    pass


class SkillUnavailableError(SkillError):
    pass


class SkillPolicyError(SkillError):
    pass


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    required_tools: list[str] = Field(default_factory=list)


class ActivatedSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata: SkillMetadata
    content_hash: str
    instructions: str


class SkillResource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_name: str
    content_hash: str
    path: str
    content: str


@dataclass(frozen=True)
class SkillDefinition:
    metadata: SkillMetadata
    content_hash: str
    instructions: str
    resources: dict[str, bytes]


DEFAULT_SKILL_WHITELISTS: dict[str, frozenset[str]] = {
    "chief_architect": frozenset({"research-planning"}),
    "deep_scout": frozenset({"rendered-web", "market-research", "policy-analysis"}),
    "data_analyst": frozenset({"data-analysis"}),
    "code_wizard": frozenset({"visualization"}),
    "critic_master": frozenset({"research-critique"}),
    "lead_writer": frozenset({"evidence-writing"}),
}


class SkillLoader:
    """Loads immutable Skill trees; scripts are returned as data and never executed."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def load_all(self) -> list[SkillDefinition]:
        if not self.root.exists():
            return []
        return [self.load(path) for path in sorted(self.root.glob("*/SKILL.md"))]

    def load(self, path: Path) -> SkillDefinition:
        resolved = path.resolve()
        if resolved.parent.parent != self.root:
            raise SkillError("SKILL.md must be one directory below the configured Skill root")
        raw = resolved.read_text(encoding="utf-8")
        metadata, instructions = self._parse(raw)
        resources: dict[str, bytes] = {}
        for family in ("references", "scripts", "assets"):
            directory = resolved.parent / family
            if not directory.exists():
                continue
            for item in sorted(directory.rglob("*")):
                if item.is_file():
                    relative = item.relative_to(resolved.parent).as_posix()
                    resources[relative] = item.read_bytes()
        digest = hashlib.sha256()
        digest.update(raw.encode("utf-8"))
        for name, content in sorted(resources.items()):
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
        return SkillDefinition(
            metadata=metadata,
            content_hash=digest.hexdigest(),
            instructions=instructions.strip(),
            resources=resources,
        )

    @staticmethod
    def _parse(raw: str) -> tuple[SkillMetadata, str]:
        lines = raw.splitlines()
        if len(lines) < 3 or lines[0].strip() != "---":
            raise SkillError("SKILL.md requires frontmatter")
        try:
            boundary = lines[1:].index("---") + 1
        except ValueError as exc:
            raise SkillError("SKILL.md frontmatter is not closed") from exc
        values: dict[str, Any] = {"required_tools": []}
        active_list: str | None = None
        for line in lines[1:boundary]:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("-"):
                if active_list is None:
                    raise SkillError("frontmatter list item has no key")
                values.setdefault(active_list, []).append(stripped[1:].strip())
                continue
            key, separator, value = stripped.partition(":")
            if not separator:
                raise SkillError(f"invalid frontmatter line: {stripped}")
            key = key.strip()
            value = value.strip()
            if key == "required_tools":
                active_list = key
                values[key] = []
            else:
                active_list = None
                values[key] = value
        return SkillMetadata.model_validate(values), "\n".join(lines[boundary + 1 :])


class SkillRegistry:
    def __init__(self, definitions: list[SkillDefinition] | None = None) -> None:
        self._versions: dict[tuple[str, str], SkillDefinition] = {}
        self._latest: dict[str, str] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: SkillDefinition) -> None:
        key = (definition.metadata.name, definition.content_hash)
        self._versions[key] = definition
        self._latest[definition.metadata.name] = definition.content_hash

    def latest(self, name: str) -> SkillDefinition | None:
        content_hash = self._latest.get(name)
        return self._versions.get((name, content_hash)) if content_hash else None

    def resolve(self, frozen: FrozenSkill) -> SkillDefinition:
        try:
            return self._versions[(frozen.name, frozen.content_hash)]
        except KeyError as exc:
            raise SkillUnavailableError(
                f"Frozen Skill is unavailable: {frozen.name}@{frozen.content_hash}"
            ) from exc

    def snapshot(self, names: set[str] | frozenset[str]) -> list[FrozenSkill]:
        output: list[FrozenSkill] = []
        for name in sorted(names):
            definition = self.latest(name)
            if definition is not None:
                output.append(FrozenSkill(name=name, content_hash=definition.content_hash))
        return output


class SkillRuntime:
    def __init__(
        self,
        registry: SkillRegistry,
        whitelists: dict[str, frozenset[str]] | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.registry = registry
        self.whitelists = whitelists or DEFAULT_SKILL_WHITELISTS
        self.telemetry = telemetry

    def metadata(
        self, agent_name: str, snapshot: CapabilitySnapshot | dict[str, Any] | None
    ) -> list[SkillMetadata]:
        if snapshot is None:
            return []
        frozen = CapabilitySnapshot.model_validate(snapshot)
        allowed = self.whitelists.get(agent_name, frozenset())
        return [
            self.registry.resolve(item).metadata
            for item in frozen.skills
            if item.name in allowed
        ]

    def activate(
        self,
        *,
        agent_name: str,
        skill_name: str,
        snapshot: CapabilitySnapshot | dict[str, Any] | None,
        policy: ToolPolicy,
    ) -> ActivatedSkill:
        if self.telemetry is None:
            return self._activate(
                agent_name=agent_name,
                skill_name=skill_name,
                snapshot=snapshot,
                policy=policy,
            )
        with self.telemetry.span(
            "skill.activate",
            attributes={"skill.name": skill_name, "research.agent": agent_name},
        ):
            return self._activate(
                agent_name=agent_name,
                skill_name=skill_name,
                snapshot=snapshot,
                policy=policy,
            )

    def _activate(
        self,
        *,
        agent_name: str,
        skill_name: str,
        snapshot: CapabilitySnapshot | dict[str, Any] | None,
        policy: ToolPolicy,
    ) -> ActivatedSkill:
        if skill_name not in self.whitelists.get(agent_name, frozenset()):
            raise SkillPolicyError(f"{agent_name} cannot activate Skill {skill_name}")
        if snapshot is None:
            raise SkillUnavailableError("Task has no frozen capability snapshot")
        frozen = CapabilitySnapshot.model_validate(snapshot)
        skill = frozen.skill(skill_name)
        if skill is None:
            raise SkillUnavailableError(f"Skill is not frozen for this Task: {skill_name}")
        definition = self.registry.resolve(skill)
        frozen_tools = {item.name for item in frozen.tools}
        required = set(definition.metadata.required_tools)
        if not required.issubset(frozen_tools):
            missing = sorted(required - frozen_tools)
            raise SkillPolicyError(f"Skill required tools are not frozen: {missing}")
        if not required.issubset(policy.allowed_tools):
            missing = sorted(required - set(policy.allowed_tools))
            raise SkillPolicyError(f"Skill required tools are denied by Agent policy: {missing}")
        return ActivatedSkill(
            metadata=definition.metadata,
            content_hash=definition.content_hash,
            instructions=definition.instructions,
        )

    def load_resource(
        self,
        *,
        agent_name: str,
        skill_name: str,
        resource_path: str,
        snapshot: CapabilitySnapshot | dict[str, Any] | None,
        policy: ToolPolicy,
    ) -> SkillResource:
        activated = self.activate(
            agent_name=agent_name,
            skill_name=skill_name,
            snapshot=snapshot,
            policy=policy,
        )
        normalized = PurePosixPath(resource_path)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise SkillPolicyError("Skill resource path escapes its immutable Skill tree")
        if not normalized.parts or normalized.parts[0] not in {
            "references",
            "scripts",
            "assets",
        }:
            raise SkillPolicyError("Only references/scripts/assets may be loaded at L3")
        assert snapshot is not None
        frozen = CapabilitySnapshot.model_validate(snapshot).skill(skill_name)
        assert frozen is not None
        definition = self.registry.resolve(frozen)
        key = normalized.as_posix()
        try:
            content = definition.resources[key].decode("utf-8")
        except KeyError as exc:
            raise SkillUnavailableError(f"Frozen Skill resource is unavailable: {key}") from exc
        except UnicodeDecodeError as exc:
            raise SkillError("Binary Skill assets are not injected into a text Agent context") from exc
        return SkillResource(
            skill_name=skill_name,
            content_hash=activated.content_hash,
            path=key,
            content=content,
        )


class CapabilitySnapshotBuilder:
    def __init__(
        self,
        skill_registry: SkillRegistry,
        tool_registry: ToolRegistry,
        policies: dict[str, ToolPolicy],
        *,
        skill_whitelists: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self.skill_registry = skill_registry
        self.tool_registry = tool_registry
        self.policies = policies
        self.skill_whitelists = skill_whitelists or DEFAULT_SKILL_WHITELISTS

    @property
    def policy_revision(self) -> str:
        return ToolPolicyRegistry.revision(self.policies)

    def build(self, datasource_ids: list[str] | None = None) -> CapabilitySnapshot:
        frozen_tool_names = set(self.tool_registry.names())
        visible_skills: set[str] = set()
        for agent_name, names in self.skill_whitelists.items():
            policy = self.policies.get(agent_name, ToolPolicy(frozenset()))
            for name in names:
                definition = self.skill_registry.latest(name)
                if definition is None:
                    continue
                required = set(definition.metadata.required_tools)
                if required.issubset(frozen_tool_names) and required.issubset(
                    policy.allowed_tools
                ):
                    visible_skills.add(name)
        return CapabilitySnapshot(
            skills=self.skill_registry.snapshot(visible_skills),
            tools=self.tool_registry.snapshot(),
            policy_revision=self.policy_revision,
            datasource_ids=list(dict.fromkeys(datasource_ids or [])),
        )
