# Agent Task Platform

[English](README.md)

Agent Task Platform 是一个面向生产环境的、以 Task/Run 为核心的持久化 AI Agent 平台。

调用方提交任务后会立即获得 Run 标识；平台异步执行任务，并持久化执行状态、重试、Stage、工具调用、模型调用、日志、输出与 Callback 投递记录。因此进程重启后仍可恢复任务执行。

## 为什么以 Task 和 Run 为核心？

许多开源 Agent 框架以 Chat 或 Session 作为主要工作单元。Agent Task Platform 则以可持久化的 Task 和 Run 为起点：调用方提交工作，平台创建执行记录，Worker 可以在不依赖单个 HTTP 连接的情况下完成排队、重试、恢复、取消、观测与结果投递。

Chat 或 Session 上下文仍然可以服务于某个 Agent，但它是可选的上下文组织方式，而不是调度和可靠性的根实体。这个定位更适合异步工作流、长时间执行的操作，以及需要可审计结果的系统集成。

它适用于需要可靠执行与可观测性的异步 Agent 工作流，而不是以聊天会话为中心的框架。

## 核心能力

- 基于 `request_id` 的幂等提交与确定性 `route_tag` 路由
- PostgreSQL 队列租约、重试、取消和恢复
- Durable Stage、checkpoint 与 Child Run
- 受治理的 Python、HTTP、MCP Tool：Schema、权限、限流、超时、重试和副作用保护
- 带不可变 Run Snapshot 的版本化 Skill
- 支持 OpenAI-compatible 服务的模型网关、Fallback、用量与审计记录
- Callback 签名、出站 URL 策略、结构化日志和 scoped API key

## 快速开始

依赖：Python 3.11+、PostgreSQL 和 [uv](https://docs.astral.sh/uv/)。Docker 仅用于运行集成测试。

```bash
uv sync --extra dev
createdb agent_task_platform_dev
cp config/.env.dev.example config/.env.dev
```

编辑 `config/.env.dev`，填入本地 PostgreSQL 连接信息，并替换 `REPLACE_WITH_LOCAL_KEY` 与 `REPLACE_WITH_LOCAL_CALLBACK_SECRET`。内置示例不需要模型服务或外部 API key。

```bash
ENV_MODE=dev alembic upgrade head
ENV_MODE=dev psql agent_task_platform_dev -f sql/seed.dml.sql
ENV_MODE=dev agent-task-platform serve
```

在另一个终端运行离线示例：

```bash
KEY=<你的本地 API key> bash scripts/request_example_run.sh
```

该示例调用 `calculator-agent`，并通过 Tool Gateway 调用本地、确定性的天气与计算器 Tool，不依赖外部模型或服务。

## API 示例

所有 `/v1/*` 接口均需要拥有对应 scope 的 `x-api-key`。

```bash
curl -X POST http://127.0.0.1:8765/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: <你的本地 API key>' \
  -d '{
    "route_tag": "example.calculator",
    "request_id": "example-001",
    "external_id": "demo-case-001",
    "input": {"city": "Example City", "expression": "12 * (3 + 4)"}
  }'
```

接口返回 `202 Accepted`，其中包含 `run_id`、`trace_id` 和 `conversation_id`。通过 `GET /v1/runs/{run_id}` 或 `GET /v1/runs/{run_id}/result` 查询执行状态和结果。

## 文档

- [Getting Started](docs/getting-started.md)
- [Architecture](docs/architecture.md)
- [Task 和 Run 概念](docs/concepts/task-and-run.md)
- [Durable Stages](docs/concepts/durable-stages.md)
- [Tools 和 Skills](docs/concepts/tools-and-skills.md)
- [配置参考](docs/reference/configuration.md)
- [HTTP API](docs/reference/http-api.md)
- [扩展 Agent 或 Tool](docs/guides/build-an-extension.md)
- [部署](docs/guides/deployment.md)
- [安全模型](docs/security/threat-model.md)

## 项目结构

```text
domain/          领域模型与生命周期枚举
infra/           PostgreSQL 持久化、协调、限流、出站策略
framework/       Agent Runtime、Tool Gateway、Skill Runtime、Model Gateway
orchestration/   提交、执行、调度、恢复、Worker、Callback
interfaces/      FastAPI、CLI、配置和运维看板
agent_hub/       离线示例 Agent
plugins/         离线示例 Tool 与 Skill
config/          公开示例注册与环境配置
```

## 开发

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

集成测试会使用 Testcontainers 启动 PostgreSQL，因此需要 Docker。

`sql/ddl.sql` 与 `sql/seed.dml.sql` 是生成文件。修改 ORM 模型或公开 registry YAML 后，运行：

```bash
uv run python scripts/gen_sql.py
```

## 当前状态

项目正在准备首个公开版本。在发布稳定版本前，不应将当前 API、插件契约或数据库 Schema 视为长期稳定接口。

## 许可证

Copyright 2026 ByteCaprice。本项目使用 [Apache License 2.0](LICENSE)。
