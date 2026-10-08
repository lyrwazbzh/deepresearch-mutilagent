from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


BindingKey = Literal[
    "routing", "reasoning", "tool", "data", "code", "writing", "embedding", "rerank"
]
Purpose = Literal["chat", "embedding", "rerank"]


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_config_id: str
    version: int = Field(ge=1)
    tenant_id: str
    user_id: str
    provider: str
    model: str
    base_url: str | None = None
    purpose: Purpose
    secret_ref: str | None = None


class ResolvedModelRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_config_id: str
    version: int = Field(ge=1)


class FrozenModelBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_key: BindingKey
    purpose: Purpose
    primary: ResolvedModelRevision
    fallback: list[ResolvedModelRevision] = Field(default_factory=list)


class ModelBindingSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bindings: dict[BindingKey, FrozenModelBinding]

    def get(self, key: BindingKey) -> FrozenModelBinding:
        try:
            return self.bindings[key]
        except KeyError as exc:
            raise KeyError(f"Task has no frozen model binding for {key}") from exc
