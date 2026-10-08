from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictCapabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrozenSkill(StrictCapabilityModel):
    name: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)


class FrozenTool(StrictCapabilityModel):
    name: str = Field(min_length=1)
    schema_hash: str = Field(min_length=64, max_length=64)
    provider_ref: str = Field(min_length=1)


class CapabilitySnapshot(StrictCapabilityModel):
    skills: list[FrozenSkill] = Field(default_factory=list)
    tools: list[FrozenTool] = Field(default_factory=list)
    policy_revision: str = Field(min_length=1)
    datasource_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def identities_are_unique(self) -> "CapabilitySnapshot":
        skill_names = [item.name for item in self.skills]
        tool_names = [item.name for item in self.tools]
        if len(skill_names) != len(set(skill_names)):
            raise ValueError("capability snapshot skill names must be unique")
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("capability snapshot tool names must be unique")
        if len(self.datasource_ids) != len(set(self.datasource_ids)):
            raise ValueError("capability snapshot datasource_ids must be unique")
        return self

    def skill(self, name: str) -> FrozenSkill | None:
        return next((item for item in self.skills if item.name == name), None)

    def tool(self, name: str) -> FrozenTool | None:
        return next((item for item in self.tools if item.name == name), None)
