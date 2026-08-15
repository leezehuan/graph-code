# Mini Claude Code — Python 版

与 TypeScript 版功能 99% 一致的 Python 实现。**需要 Python >= 3.11**。

> 📖 完整教程文档见 [claude-code-from-scratch](https://github.com/Windy3f3f3f3f/claude-code-from-scratch)（文档中所有代码块均支持 TypeScript / Python 切换）

## 快速开始

```bash
# 安装（需要 Python 3.11+）
cd python
pip install -e .

# 设置 API Key
export ANTHROPIC_API_KEY=sk-ant-...

# 运行
mini-claude-py "hello"               # 一次性模式
mini-claude-py                       # 交互式 REPL
mini-claude-py --yolo "list files"   # 跳过确认
mini-claude-py --plan "refactor this" # 计划模式
mini-claude-py --skill-review-interval 0 "hello" # 关闭后台技能审视
python -m mini_claude "hello"        # 也可以用 python -m 方式运行

# 使用 OpenAI 兼容后端
OPENAI_API_KEY=sk-xxx mini-claude-py --api-base https://api.openai.com/v1 --model gpt-4o "hello"
```

## 技能沉淀

技能目录只把名称和描述注入系统提示词。Agent 需要使用时，通过
`skill_view` 按需读取 `SKILL.md`，以及 `references/`、`templates/`、
`scripts/`、`assets/` 中的附件。`skills_list` 用于查看目录，
`skill_manage` 支持创建、编辑、局部修补、删除和管理附件。

新技能写入当前项目的 `.claude/skills/`。默认每累计 10 个工具调用迭代，
主 Agent 会在成功回复后启动一次后台审视，从已验证的试错和用户纠正中提取
可复用做法。后台只会自动修改带有 `created_by: agent` 标记的项目技能；
人工技能和用户级技能保持只读。使用 `--skill-review-interval N` 或
`MINI_CLAUDE_SKILL_REVIEW_INTERVAL=N` 调整频率，设置为 `0` 可关闭。

## 按需代码图

Agent 内置了延迟加载的 `code_graph` 工具。需要理解代码结构、追踪调用关系或
审查变更影响时，Agent 会先通过 `tool_search` 激活它；普通对话不会加载解析器，
也不会增加启动开销。

工具提供四种动作：

- `search`：按符号名、限定名或相对文件路径搜索。默认使用本地 FTS5，
  也可显式选择云端语义搜索或 FTS5 + 语义混合搜索。
- `query`：查询调用、导入、测试、包含、继承和引用关系。
- `impact`：分析当前 Git 变更的两跳反向依赖影响。
- `overview`：返回语言、目录、节点、关系、测试符号和高入度符号摘要。

首次调用会建立索引，后续调用仅刷新发生变化的源码。索引保存在
`~/.mini-claude/projects/<项目哈希>/code-graph.sqlite`，不会修改被分析仓库。
支持 Python、JavaScript/JSX、TypeScript/TSX、Java、Go、Rust、C/C++ 和 C#。

`search` 支持以下可选参数：

- `mode=fts`：默认值，完全离线；FTS5 不可用或无结果时回退到关键词搜索。
- `mode=semantic`：使用 OpenAI-compatible embedding 做余弦相似度搜索。
- `mode=hybrid`：用 RRF 合并 FTS5 和语义候选；provider 失败时回退到本地搜索。
- `kind`：限定 `file`、`class` 或 `function`。
- `context_files`：用项目内相对路径提升相关结果。

语义和混合模式只发送限定名、符号名、类型、父作用域、相对路径和语言等结构
元数据，不发送函数体或完整源码。即使已配置 provider，默认 FTS5 和其他三个动作
也不会联网。启用云端 embedding 必须显式设置。推荐在项目根目录的 `.env` 中配置（该文件已被 Git 忽略，
可从仓库提供的 `.env.example` 复制）：

```bash
MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS=1
MINI_CLAUDE_EMBEDDING_BASE_URL=https://api.example.com/v1
MINI_CLAUDE_EMBEDDING_MODEL=text-embedding-model
MINI_CLAUDE_EMBEDDING_API_KEY=sk-... # 可选
```

启动 `mini-claude-py` 时会固定读取 Mini Claude 源码仓库根目录的 `.env`，与启动时
所在的工作目录或被分析仓库无关。已有的进程环境变量优先于文件中的值；特殊部署
可用 `MINI_CLAUDE_ENV_FILE` 指定另一份配置文件。默认 `mode=fts` 和其他三个动作
仍不会联网。

向量按 endpoint 哈希和 model 隔离缓存到同一个 `code-graph.sqlite`。缓存不保存
API key 或原始 endpoint；源码增删改时只补齐受影响节点。

## 文件结构

| Python 文件 | 对应 TypeScript | 说明 |
|-------------|----------------|------|
| `agent.py` | `agent.ts` | Agent 核心循环、双后端、4 层压缩 |
| `tools.py` | `tools.ts` | 内置工具定义、执行与 5 种权限模式 |
| `code_graph.py` | — | 按需代码结构索引、混合搜索、关系查询与变更影响 |
| `__main__.py` | `cli.ts` | CLI 入口与 REPL |
| `ui.py` | `ui.ts` | 终端 UI（rich） |
| `prompt.py` | `prompt.ts` | 系统提示词构造 |
| `session.py` | `session.ts` | 会话管理 |
| `memory.py` | `memory.ts` | 记忆系统 |
| `skills.py` | `skills.ts` | 技能系统 |
| `subagent.py` | `subagent.ts` | 子 Agent |
| `frontmatter.py` | `frontmatter.ts` | YAML frontmatter 解析 |

## 依赖

- `anthropic` — Anthropic SDK（流式）
- `openai` — OpenAI SDK（兼容后端）
- `rich` — 终端彩色输出
- `tree-sitter` / `tree-sitter-language-pack` — 多语言代码结构解析
