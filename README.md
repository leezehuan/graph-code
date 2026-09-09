# graph-code

graph-code 是一个基于 Python 的终端 AI 编程 Agent，面向真实的软件工程任务。它能够理解和修改代码、执行命令、调用外部工具、规划任务、管理长会话，并通过代码图、长期记忆、经验 Skill 和多 Agent 协作持续扩大工作范围。

graph-code 兼容 Anthropic 与 OpenAI Chat Completions 协议，也可以通过 MCP stdio 服务接入额外工具。它的核心目标是：在权限边界内，把“理解问题、定位代码、实施修改、验证结果、沉淀经验”放进同一个可恢复的终端工作流。

## 快速开始

要求 Python 3.11 或更高版本。

```bash
cd python
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1
pip install -e .
```

在项目根目录创建 `.env`（可复制 `.env.example`），配置一个模型服务：

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
# 或使用 OpenAI 兼容接口
# OPENAI_API_KEY=sk-...
# OPENAI_BASE_URL=https://api.openai.com/v1
```

安装后可以从任意项目目录运行：

```bash
graph-code "检查这个项目的测试并修复失败项"
graph-code                 # 交互式 REPL
graph-code --resume        # 恢复最近一次会话
graph-code --yolo "列出项目结构"
graph-code --plan "设计一次重构"
graph-code --accept-edits "整理代码格式"
graph-code --max-cost 0.50 --max-turns 20 "完成任务"
```

也可以不安装命令，直接运行：

```bash
python -m mini_claude "检查当前项目"
```

## 能力概览

### 代码理解与混合检索

- 使用 [Tree-sitter](https://tree-sitter.github.io/tree-sitter/) 解析多语言源码，提取符号、作用域以及调用、引用、继承等关系。
- 使用 SQLite FTS5 对符号名、限定名、文件路径等结构字段做本地全文检索。
- 可选接入 Embedding 服务进行语义检索，使用 Reciprocal Rank Fusion（RRF）融合词法和向量候选，兼顾精确找符号和按意图找代码。
- 支持符号搜索、关系查询、调用链/引用链、变更影响分析和索引概览；代码图按需构建，索引存放在用户目录，不修改被分析的仓库。
- 支持 Python、JavaScript/JSX、TypeScript/TSX、Java、Go、Rust、C/C++ 和 C#。

在 5 个仓库的检索评测中，混合检索的 `Hit@5` 达到 90%～95%。评测方法、样本和可复现命令见 [`python/evaluation/code_search/README.md`](python/evaluation/code_search/README.md)；仓库内最新报告位于 [`python/evaluation/code_search/results/latest.md`](python/evaluation/code_search/results/latest.md)。项目源码地址：[graph-code](https://github.com/leezehuan/graph-code)。

### 长上下文与会话恢复

Agent 根据上下文窗口利用率逐级执行四类压缩：

1. 工具结果预算裁剪，优先控制单次结果占用。
2. 清理陈旧工具结果，保留对当前任务更有价值的内容。
3. 空闲微压缩，合并或缩短低价值历史片段。
4. 会话摘要压缩，在接近窗口上限时重建精简上下文。

超过阈值的完整工具输出会先持久化到磁盘，再在上下文中保留摘要和回读路径，Agent 可以按需读取原始结果。会话可通过 `--resume` 或 `/resume` 恢复。

### Fork-Return 多 Agent 协作

主 Agent 负责拆分任务和汇总结果；子 Agent 在隔离上下文中独立执行，完成后将结果返回主 Agent：

```text
主 Agent ──分派──> explore / plan / general 子 Agent
    ^                  │
    └────结果返回──────┘
```

- `explore`：只读代码探索；`plan`：只读分析并输出结构化计划；`general`：使用完整工具集执行任务。
- 支持在目标项目的 `.claude/agents/*.md` 中定义自定义 Agent。
- 子 Agent 继承调用方的权限边界；隔离上下文避免并行探索污染主会话。

### 长期记忆与经验沉淀

记忆采用文件化存储，并按边界明确的四种类型组织：

- `user`：用户角色、偏好和知识背景。
- `feedback`：用户纠正、约束以及后续应用方式。
- `project`：项目目标、决策、进展和长期上下文。
- `reference`：外部文档、工具或仪表盘的引用。

每轮任务可异步召回相关记忆，并限制注入规模和文件大小；记忆带有时间信息，过期内容会提示 Agent 回到当前源码核验。记忆默认落盘到 `~/.mini-claude/projects/<项目哈希>/memory/`。成功的操作和用户纠正还可以由后台审视流程提炼为可复用的项目 Skill。Skill 支持渐进式加载、附件管理和显式权限控制，存放于 `.claude/skills/` 或 `~/.claude/skills/`。

### 工具、权限与自治循环

- 文件读取、编辑、目录浏览、正则搜索、Shell、网页抓取和延迟加载工具。
- MCP stdio 工具服务，以及项目级 `.claude/settings.json` 权限规则。
- `default`、`acceptEdits`、`dontAsk`、`bypassPermissions`、`plan` 等权限模式，另有 `--yolo` 快捷方式。
- `/goal` 完成条件、`/loop` 重复任务和 Auto Mode 自治循环，支持最大回合数、成本和超时约束。

## 技术栈

| 领域 | 技术 |
| --- | --- |
| 语言与并发 | Python 3.11+、AsyncIO |
| 模型协议 | Anthropic API、OpenAI 兼容 Chat Completions |
| 代码解析 | Tree-sitter、tree-sitter-language-pack |
| 本地索引 | SQLite、FTS5 |
| 语义检索 | Embedding、余弦相似度、RRF |
| 终端界面 | Rich |
| 扩展协议 | MCP stdio |

## 常用 REPL 命令

输入 `help` 查看完整帮助；常用命令包括：

```text
/clear      清空当前会话
/resume     恢复最近会话
/cost       查看 token 与成本
/compact    压缩上下文
/memory     查看记忆
/skills     查看技能
/goal ...   设置完成条件
/loop ...   按间隔或动态节奏重复任务
exit        退出
```

## 代码图配置

代码图默认使用本地 FTS5，完全离线。需要语义或混合检索时，显式配置 Embedding 服务：

```dotenv
MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS=1
MINI_CLAUDE_EMBEDDING_BASE_URL=https://api.example.com/v1
MINI_CLAUDE_EMBEDDING_MODEL=text-embedding-model
MINI_CLAUDE_EMBEDDING_API_KEY=sk-...
```

索引默认保存在 `~/.mini-claude/projects/<项目哈希>/code-graph.sqlite`。索引阶段发送到 Embedding 服务的源码信息仅包含限定名、符号名、类型、父作用域、相对路径和语言等结构元数据，不包含函数体或完整源码；不设置 `MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS=1` 时不会发起云端向量请求。

## 项目结构

```text
python/mini_claude/   Agent、CLI、工具、技能、记忆、MCP 和代码图实现
python/tests/         Python 单元测试
python/evaluation/    代码搜索评测与结果
assets/               运行时资源（包括 Auto Mode 规则）
```

## 开发与验证

```bash
cd python
python -m unittest discover -s tests -p "test_*.py"
```

代码搜索评测支持离线 FTS/图查询和在线 Semantic/Hybrid 流程，详见 [`python/evaluation/code_search/README.md`](python/evaluation/code_search/README.md)。

## 安全边界

Agent 会对文件修改、Shell 命令、Skill 管理和其他有副作用的操作执行权限检查。请只在你有权操作的项目和环境中使用 `--yolo`、`dontAsk`、`bypassPermissions` 或 Auto Mode；连接外部模型和 Embedding 服务时也应配置最小权限，并确认数据发送范围。

## 许可证

MIT License，见 [`LICENSE`](LICENSE)。
