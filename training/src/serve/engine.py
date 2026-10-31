"""Qwen2.5 + PEFT adapter inference with schema-validated tool output."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator

from src.contracts.tool_routing import ToolCall, ToolRoutingRequest, ToolRoutingResponse
from src.data.schema_v3 import load_tool_schema
from src.eval.parser import parse_tool_calls
from src.train.chat_format import load_openai_tools


class ModelOutputError(RuntimeError):
    pass


class ToolRouterEngine:
    def __init__(self, *, model, tokenizer, tools, schema, model_version: str,
                 adapter_version: str | None, confidence: float = 0.8,
                 max_new_tokens: int = 384):
        self.model = model
        self.tokenizer = tokenizer
        self.tools = tools
        self.schema = schema
        self.model_version = model_version
        self.adapter_version = adapter_version
        self.confidence = confidence
        self.max_new_tokens = max_new_tokens
        self._lock = asyncio.Lock()

    @classmethod
    def from_environment(cls):
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

        model_name = os.getenv("TOOL_ROUTER_MODEL", "Qwen/Qwen2.5-7B-Instruct")
        adapter = os.getenv("TOOL_ROUTER_ADAPTER", "").strip() or None
        schema_path = os.getenv("TOOL_ROUTER_SCHEMA", "config/deepresearch_tools.v1.json")
        manifest_path = os.getenv("TOOL_ROUTER_MANIFEST", "").strip()
        manifest = {}
        if manifest_path:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            actual_digest = hashlib.sha256(Path(schema_path).read_bytes()).hexdigest()
            expected_digest = manifest.get("tool_schema_sha256")
            if expected_digest and actual_digest != expected_digest:
                raise RuntimeError("tool schema digest does not match release manifest")
            model_name = manifest.get("base_model", model_name)
            if not adapter and manifest.get("adapter_path"):
                adapter = str(Path(manifest_path).resolve().parent / manifest["adapter_path"])

        if not torch.cuda.is_available():
            raise RuntimeError("tool-router serving requires CUDA")
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        base = AutoModelForCausalLM.from_pretrained(
            model_name,
            trust_remote_code=True,
            device_map="auto",
            quantization_config=BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=dtype,
            ),
        )
        model = PeftModel.from_pretrained(base, adapter) if adapter else base
        model.eval()
        return cls(
            model=model,
            tokenizer=tokenizer,
            tools=load_openai_tools(schema_path),
            schema=load_tool_schema(schema_path),
            model_version=model_name,
            adapter_version=manifest.get("created_at") or adapter,
            confidence=float(os.getenv("TOOL_ROUTER_CONFIDENCE", "0.8")),
            max_new_tokens=int(os.getenv("TOOL_ROUTER_MAX_NEW_TOKENS", "384")),
        )

    async def route(self, request: ToolRoutingRequest) -> ToolRoutingResponse:
        async with self._lock:
            return await asyncio.to_thread(self._route_sync, request)

    def _route_sync(self, request: ToolRoutingRequest) -> ToolRoutingResponse:
        import torch

        enabled_tools = [
            tool for tool in self.tools
            if tool["function"]["name"] in {item.value for item in request.allowed_tools}
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 DeepResearch 工具路由器。只能从给定工具中选择 1-3 个调用；"
                    "参数必须满足工具 JSON Schema，不负责结束研究任务。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(request.model_dump(mode="json"), ensure_ascii=False),
            },
        ]
        prompt = self.tokenizer.apply_chat_template(
            messages, tools=enabled_tools, tokenize=False, add_generation_prompt=True
        )
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        with torch.inference_mode():
            output = self.model.generate(
                **inputs,
                do_sample=False,
                max_new_tokens=self.max_new_tokens,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        text = self.tokenizer.decode(
            output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=False
        )
        try:
            predictions = parse_tool_calls(text)[:3]
        except Exception as exc:
            raise ModelOutputError(f"invalid model tool output: {exc}") from exc

        calls = []
        for prediction in predictions:
            if prediction.name not in {item.value for item in request.allowed_tools}:
                raise ModelOutputError(f"model selected disallowed tool: {prediction.name}")
            spec = self.schema.get(prediction.name)
            if spec is None:
                raise ModelOutputError(f"unknown tool: {prediction.name}")
            errors = list(Draft202012Validator(spec["parameters"]).iter_errors(
                prediction.arguments
            ))
            if errors:
                raise ModelOutputError(
                    f"invalid arguments for {prediction.name}: {errors[0].message}"
                )
            calls.append(ToolCall(
                call_id=f"call_{uuid.uuid4().hex[:16]}",
                name=prediction.name,
                arguments=prediction.arguments,
            ))
        response = ToolRoutingResponse(
            decision_id=f"decision_{uuid.uuid4().hex}",
            calls=calls,
            confidence=self.confidence,
            model_version=self.model_version,
            adapter_version=self.adapter_version,
        )
        response.ensure_allowed(request)
        return response

    def model_info(self) -> dict:
        return {
            "model_version": self.model_version,
            "adapter_version": self.adapter_version,
            "schema_version": "1.0",
        }
