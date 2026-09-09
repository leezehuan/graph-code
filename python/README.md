# graph-code Python

graph-code 的 Python 实现，要求 Python 3.11 或更高版本。

## 安装

```bash
cd python
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1
pip install -e .
```

从仓库根目录的 `.env.example` 复制 `.env` 并配置模型服务。进程环境变量优先；默认读取源码仓库根目录的 `.env`，也可以通过 `MINI_CLAUDE_ENV_FILE` 指定其他配置文件。

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
# 或者
# OPENAI_API_KEY=sk-...
# OPENAI_BASE_URL=https://api.openai.com/v1
```

## 使用

```bash
graph-code "检查当前项目"
graph-code                     # 交互式 REPL
graph-code --resume            # 恢复最近会话
graph-code --yolo "列出文件"
graph-code --plan "设计重构方案"
graph-code --accept-edits "整理代码格式"
graph-code --dont-ask "运行检查"
graph-code --max-cost 0.50 --max-turns 20 "完成任务"
python -m mini_claude "完成任务"
```

输入 `help` 可查看参数和 REPL 命令。常用 REPL 命令有 `/clear`、`/resume`、`/cost`、`/compact`、`/memory`、`/skills`、`/goal` 和 `/loop`。

## 技能与配置

项目技能放在目标项目的 `.claude/skills/<name>/SKILL.md`，用户级技能放在 `~/.claude/skills/`。Agent 会先加载技能索引，需要时再读取完整内容及其附件。技能管理工具支持创建、编辑、局部修补、删除和附件管理。

权限规则位于 `.claude/settings.json`，MCP 服务也可以在该文件中配置。项目和用户级 `.claude/agents/*.md` 可定义自定义子 Agent。

后台技能审视默认每累计 10 个工具调用运行一次；使用 `--skill-review-interval 0` 或设置 `MINI_CLAUDE_SKILL_REVIEW_INTERVAL=0` 可关闭。

## 代码图

代码图工具按需激活，支持 `search`、`query`、`impact` 和 `overview` 四种动作。默认 `mode=fts`，索引保存在 `~/.mini-claude/projects/<项目哈希>/code-graph.sqlite`，不会修改被分析的仓库。

支持 Python、JavaScript/JSX、TypeScript/TSX、Java、Go、Rust、C/C++ 和 C#。语义或混合搜索需要显式开启云端 embedding：

```dotenv
MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS=1
MINI_CLAUDE_EMBEDDING_BASE_URL=https://api.example.com/v1
MINI_CLAUDE_EMBEDDING_MODEL=text-embedding-model
MINI_CLAUDE_EMBEDDING_API_KEY=sk-...
```

只会发送限定名、符号名、类型、父作用域、相对路径和语言等结构元数据，不发送函数体或完整源码。

## 测试

```bash
python -m unittest discover -s tests -p "test_*.py"
```
