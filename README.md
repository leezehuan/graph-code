# graph-code

graph-code 是一个基于 Python、LangChain 和 LangGraph 的终端 AI 编程 Agent 基础设施，面向真实的软件工程任务。它支持代码理解、任务拆解、工具调用、权限控制、长期记忆、Skill 热加载、错误恢复以及多 Agent 并行协作。

项目采用 Lead/Worker 架构：Lead 负责接收用户任务、规划执行流程和调度任务；Worker 负责执行具体的 Agent 子任务。PostgreSQL 保存任务、会话和审计状态，RocketMQ 负责 Agent 之间的异步通信，代码图和混合检索帮助 Agent 快速定位代码。

## 快速开始

要求 Python 3.11 或更高版本，并安装 Docker Compose。

### 1. 安装 Python 依赖

```bash
python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. 配置模型和本地服务

在项目根目录创建 `.env` 文件，至少配置模型服务参数：

```dotenv
API_KEY=your-api-key
MODEL_NAME=your-model-name
BASE_URL=https://api.example.com/v1
LIGHT_MODEL_NAME=your-light-model-name
```

启动 PostgreSQL、Redis 和 RocketMQ：

```bash
docker compose up -d
docker compose run --rm rocketmq-init
```

默认连接配置如下，可在 `.env` 中覆盖：

```dotenv
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=langcode
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
REDIS_PORT=6380
ROCKETMQ_PROXY_GRPC_PORT=8081
```

`rocketmq-init` 会创建运行所需的普通消息主题和消费组。该命令可以重复执行，不会删除已有主题、消息或 Docker 数据卷。

### 3. 启动 Lead

```bash
python cli.py
```

CLI 会自动启动默认的 `runtime-001`、`runtime-002` 和 `runtime-003` Worker。Worker 与 Lead 共用当前 Python 环境和工作目录，Worker 的输出会显示在同一个终端中。不要在 CLI 运行时再次手动启动相同的 runtime ID。

也可以单独启动 Worker：

```bash
python -m worker --runtime-id runtime-001
```

默认运行时 ID 可以通过环境变量调整：

```dotenv
LANGCODE_RUNTIME_IDS=runtime-001,runtime-002,runtime-003,runtime-004
```

## 核心能力

### Agent 循环与工具调用

- 基于 LangChain 和 LangGraph 组织 Agent 循环。
- 提供文件读取、文件编辑、Shell 命令、代码图和任务调度等工具。
- 通过权限中间件对文件修改、命令执行和其他有副作用的操作进行检查。
- 支持 Todo、上下文压缩、错误恢复和会话恢复。

### DAG 任务拆解与多 Agent 协作

- Lead 将复杂任务拆解为有依赖关系的 DAG 节点。
- Worker 只认领满足依赖条件的就绪任务，并独立执行一次 Agent 尝试。
- 通过 RocketMQ 传递任务、权限、状态和 Agent 消息。
- 支持后台任务、Agent Team、子 Agent 和自治执行流程。
- PostgreSQL 作为任务和审计状态的事实来源，Outbox 机制保证状态变化最终发布到消息队列。

### 记忆与 Skill

Lead 使用 PostgreSQL Store 和项目内 JSON 文件保存四类长期记忆：

- `semantic`：用户稳定偏好，按用户隔离并可跨项目使用。
- `procedural`：明确的工作规则，可指定用户级或项目级，默认项目级。
- `episodic`：已经验证的历史事件、原因、处理方法及结果，同项目用户共享。
- `project`：当前有效的架构事实、约定与决策，同项目用户共享。

身份来自调用配置 `configurable.user_id` 与规范化工作目录的哈希。缺少用户 ID
时关闭个人记忆读写，项目记忆仍可用；CLI 的“匿名用户”是共享的匿名身份。
这是应用层的数据分区，调用方应提供可信的用户 ID，不等同于身份认证。
Worker 本次不接入长期记忆工具或自动召回。

用户记忆（`semantic` 和用户级 `procedural`）存于 PostgreSQL namespace
`("memories_v2", "user", user_id)`。项目记忆（`project`、`episodic` 和项目级
`procedural`）以每条一份 JSON 文件存于 `<project_root>/.langcode/memories/`，
目录默认由 Git 忽略。文件记录包含稳定 UUID、类型、范围、摘要、内容、来源及
创建/更新时间；写入使用文件锁和原子替换，无需数据库表迁移。现有 PostgreSQL
项目记忆保留但不自动读取或迁移。

每次模型调用前，从各可访问范围最近更新的 100 条记录中由轻量模型选择最多
5 条，注入不超过 2,000 token 的参考信息；当前用户要求优先。
完整、非空的最终回答之后提取新记忆或按候选 ID 更新已有记忆。
提取策略排除临时进度、猜测、敏感凭据及共享记忆中的个人信息。
召回和保存失败仅记录日志，不阻断主任务；共享记录并发更新采用后写入者生效。

Lead 提供 `memory_list(type?, scope?, limit=20, offset=0)`、`memory_get(scope, id)`、
`memory_update(scope, id, description, content)`、`memory_delete(scope, id)`。
更新和删除应依据用户明确要求，目标限定当前用户或项目；不提供批量删除。
更新保留类型和范围。管理操作成功后，本次 Agent 调用跳过自动提取，防止立即
重建已删除记忆或覆盖修改；下一次调用恢复。

`configurable.memory_enabled=false` 同时关闭自动召回和保存；最近用户消息中的
“不要使用记忆”“禁用记忆”“停止记忆”也会在本轮完整工具循环中生效。
管理工具仍可处理明确请求。“忘记所有”不是禁用开关或批量删除命令，需要先
明确范围和记录。这些控制不关闭独立的后台 Skill 审查。
记忆保存事实与事件；后台 Skill 审查从成功操作和用户纠正中提炼通用方法。

项目 Skill 位于 `skills/<name>/SKILL.md`。Skill 支持热加载、权限控制、附件管理和自动审视；用户级 Skill 默认位于 `~/.langcode/skills/`。项目级同名 Skill 优先于用户级 Skill。

### 代码图与混合检索

`code_graph` 使用 Tree-sitter 解析源码，并将索引保存在本地 SQLite 数据库中。支持：

- 符号搜索和全文检索；
- 调用者、被调用者、导入者、测试、继承和引用关系查询；
- 调用链、引用链和变更影响分析；
- FTS、语义检索和 Hybrid 检索；
- Python、C、C++ 等语言的代码分析。

代码图默认使用本地 FTS5，不需要访问云端服务。启用语义或 Hybrid 检索时，可配置 Embedding 服务：

```dotenv
LANGCODE_ACCEPT_CLOUD_EMBEDDINGS=1
LANGCODE_EMBEDDING_BASE_URL=https://api.example.com/v1
LANGCODE_EMBEDDING_MODEL=text-embedding-model
LANGCODE_EMBEDDING_API_KEY=your-api-key
```

索引默认保存在 `~/.langcode/projects/<项目路径哈希>/code-graph.sqlite`，也可以通过 `LANGCODE_CACHE_DIR` 指定缓存目录。代码图服务发送到 Embedding 服务的内容仅包含符号和路径等结构元数据，不包含函数体或完整源文件。

## 运行时配置

Lead 和 Worker 使用 PostgreSQL 保存状态，使用 RocketMQ 作为异步传输层。Worker 通过 RocketMQ Proxy 的 gRPC 地址连接消息服务：

```dotenv
ROCKETMQ_ENDPOINTS=127.0.0.1:8081
TASK_WORK_SHARD_COUNT=64
```

Lead 正常退出、收到 Ctrl+C 或发生异常时，会清理自身启动的 Worker，并等待其退出；外部手动启动的 Worker 不会被 CLI 管理或自动重启。

## 项目结构

```text
cli.py                          Lead CLI 和 Agent 主循环
worker.py                       通用 Worker 运行时
lib/                            数据库、消息、DAG、代码图和 Skill 实现
middlewares/                    权限、记忆、压缩、错误恢复等中间件
skills/                         项目级 Skill
scripts/                        验证和测试脚本
tests/                          单元测试与集成测试
docker/rocketmq/                RocketMQ 配置和初始化脚本
docker-compose.yml              PostgreSQL、Redis、RocketMQ 服务编排
```

## 测试与验证

运行不依赖外部基础设施的测试：

```bash
python -m pytest tests/test_code_graph.py tests/test_skill_store.py tests/test_skill_review.py tests/test_knowledge_integration.py -q
```

运行完整测试集：

```bash
python -m pytest tests -q
```

RocketMQ 和 PostgreSQL 集成测试默认关闭，需要根据测试文件中的环境变量说明显式启用。确定本地服务已启动后，可运行 DAG 验证脚本：

```bash
python scripts/verify_normal_dag.py
```

## 安全边界

Agent 可能执行文件写入、Shell 命令、Skill 管理和其他有副作用的操作。请只在你有权操作的项目和环境中运行，并谨慎使用绕过确认的配置。连接外部模型和 Embedding 服务时，请配置最小权限，并确认发送的数据范围。

## 许可证

MIT License，详见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 及仓库中的许可证说明。
