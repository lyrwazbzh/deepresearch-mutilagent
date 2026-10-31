# DeepResearch Tool Router 训练与推理

本目录是整合仓库的工具路由训练与推理子系统，唯一数据契约是标准
`assistant.tool_calls`，工具 Schema 来自 `config/deepresearch_tools.v1.json`。
训练目标是 Qwen2.5-7B-Instruct 的 4-bit QLoRA Adapter；模型只决定工具选择与参数，
研究终止、Critic 返工和状态迁移仍由主项目的 AsyncIO 状态机控制。

## 流水线

1. `scripts/expand_seed_questions.py`：将人工种子扩展为多领域问题。
2. `scripts/synthesize_routing_trajectories.py`：教师模型并发合成标准工具调用轨迹。
3. `scripts/build_dataset.py`：脱敏、Schema 校验、质量过滤、近重复去重、难度分桶及 train/dev/test 切分。
4. `scripts/02_train.py`：NF4 4-bit QLoRA，独立验证集、checkpoint 与断点续训。
5. `scripts/evaluate_tool_router.py`：工具选择、参数 JSON/Schema、轨迹成功率评测与回归门禁。
6. `scripts/package_release.py`：仅将通过门禁的 Adapter、Schema、Manifest 和评测报告打包。
7. `src.serve.app`：提供 `/v1/tool-routing`、`/v1/model`、`/healthz` 和 `/metrics`。

完整命令见 `docs/操作手册.md`。简历中的“5,000+ 条”和“76%→92%”必须以
`manifest.json` 与实际评测报告为准，脚本不会伪造这些结果。
