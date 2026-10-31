# 贡献指南

## 开发流程

1. 从独立分支提交单一主题改动。
2. 改动工具定义时，以 `contracts/deepresearch_tools.v1.json` 为权威源，同步训练端镜像并校验摘要。
3. 改动在线状态、数据库模型或事件格式时，同时补迁移、契约测试和恢复场景测试。
4. 提交前执行受影响模块的 Ruff、Pytest 或前端检查。

## 代码检查

```bash
cd backend && ruff check app evals scripts && pytest tests -q
cd training && ruff check src scripts tests && pytest tests -q
cd frontend && npm run lint && npm run typecheck && npm test
```

## 安全要求

- 不提交真实 API Key、Token、口令、数据库连接串或包含个人信息的数据。
- 不提交 `training/outputs`、`reports`、生成数据和 Adapter。
- Text2SQL 必须使用服务端配置的只读账号。
- 新增外部调用必须接入超时、重试边界、审计事件和必要的幂等保护。

更多信息见 [README.md](README.md) 与 [九组改造说明](docs/implementation/9-pr-delivery.md)。
