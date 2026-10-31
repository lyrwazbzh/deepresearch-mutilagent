# 数据目录说明

`train_v3_1200_merged_clean.jsonl` 是旧版文本标签实验数据，仅为历史材料，使用
`schema_v2`/旧工具集，不得直接用于当前 Qwen2.5 工具路由训练。

当前流水线只接受 `schema_version=3.0` 的标准 `assistant.tool_calls` 轨迹。请通过
`scripts/build_dataset.py` 生成 `routing-v1/train.jsonl`、`dev.jsonl`、`test.jsonl`
及 `manifest.json`，训练与简历指标均以这些新产物为准。
