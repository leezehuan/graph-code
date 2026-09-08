# Mini Claude Code

一个面向软件工程任务的 Python 命令行 Agent。它支持 Anthropic 和 OpenAI 兼容接口，能够读写代码、执行命令、搜索项目、管理会话，并通过技能、子 Agent、MCP、记忆、代码图和自治循环扩展工作流。

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
mini-claude-py "检查这个项目的测试并修复失败项"
mini-claude-py                 # 交互式 REPL
mini-claude-py --resume        # 恢复最近一次会话
mini-claude-py --yolo "列出项目结构"
mini-claude-py --plan "设计一次重构"
```

也可以不安装命令，直接运行：

```bash
python -m mini_claude "检查当前项目"
```

## 主要能力

- Anthropic 与 OpenAI 兼容后端，支持流式输出、重试和成本/回合数限制。
- 文件读写、编辑、目录列表、正则搜索、Shell、网页抓取和延迟加载工具。
- 默认确认、自动接受编辑、YOLO、计划和 CI 等权限模式，并支持 `.claude/settings.json` 规则。
- 会话恢复、记忆召回、项目技能、子 Agent 与 MCP stdio 工具服务。
- 代码图索引：符号搜索、调用关系、变更影响，以及可选的混合语义搜索。
- `/goal`、`/loop` 和 Auto Mode 自治工作流。

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

代码图默认使用本地 FTS5，不会联网。只有显式设置 `MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS=1` 并配置 embedding 服务时，语义搜索才会发送必要的结构元数据。

## 安全说明

Agent 会对文件修改、Shell 命令和其他有副作用的操作执行权限检查。请只在你有权操作的项目和环境中使用 `--yolo` 或 Auto Mode，并为外部模型服务配置必要的最小权限。

MIT License。
