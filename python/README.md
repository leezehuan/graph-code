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

## 文件结构

| Python 文件 | 对应 TypeScript | 说明 |
|-------------|----------------|------|
| `agent.py` | `agent.ts` | Agent 核心循环、双后端、4 层压缩 |
| `tools.py` | `tools.ts` | 10 个工具 + 5 种权限模式 |
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
