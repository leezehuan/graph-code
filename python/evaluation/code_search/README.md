# 代码搜索量化评测

本评测使用人工标注的 gold 用例，对比 graph-code 的多种代码搜索策略，
且不修改生产环境的 `code_graph` 接口。

## 检索方法

- `grep_file`（纯文本文件扫描）：直接扫描源码文本，不使用代码图；只参与文件级指标。
- `fts`（FTS5 全文检索）：按符号名、限定名、路径等结构字段做本地词法匹配，
  完全离线，适合已知标识符或文件路径。
- `semantic`（语义向量检索）：把查询和符号结构元数据转成 embedding，按余弦相似度
  排序，适合“负责什么”“核心流程在哪里”一类概念问题。
- `hybrid`（词法 + 向量融合）：同时取得 FTS 和 Semantic 候选，再用 RRF 融合排序；
  兼顾精确名称与自然语言语义，是通用搜索主方案。
- `graph_exact`（精确图查询）：已知目标符号后，直接查询 callers、callees、继承、引用等
  关系；只用于关系任务，不是通用自然语言搜索方法。
- `hybrid_graph`（Hybrid + 图扩展）：先用 Hybrid 找候选，以前三个 anchor 的代码图关系
  对候选重新加权；关系任务则先找 anchor，再做关系遍历。
- `routed`（自动路由）：先由规则路由器判断问题形态，再为节点搜索选择 FTS 或 Hybrid，
  用于衡量“先分类、再检索”的整体效果。

报告分别统计文件级与节点级指标。空结果和 provider 错误不会从分母中删除。
库外问题没有 gold 代码节点，因此不参与代码检索指标。

## 运行方式

离线运行会评测 grep、FTS、精确图遍历、规则路由和报告生成。
Semantic 及模型相关样本会记录为 `offline_mode`，不会被静默忽略。

```powershell
python python/evaluation/code_search/benchmark.py `
  --repo mini=D:\Project\claude-code-from-scratch `
  --repo coc-lite=D:\Project\coc-lite `
  --output python/evaluation/code_search/results
```

在线运行读取源码仓库的 `.env`，需要已有的聊天模型与 embedding 配置：

```powershell
python python/evaluation/code_search/benchmark.py `
  --repo mini=D:\Project\claude-code-from-scratch `
  --repo coc-lite=D:\Project\coc-lite `
  --live `
  --output python/evaluation/code_search/results
```

若 provider 尾延迟较高，可把同一次评测拆成三个可续跑阶段：

```powershell
# 两个仓库的检索评测。
python python/evaluation/code_search/benchmark.py --live --retrieval-only `
  --repo mini=D:\Project\claude-code-from-scratch `
  --repo coc-lite=D:\Project\coc-lite --output python/evaluation/code_search/results

# 分仓库执行生成评测；每条命令都会合并到 latest.json。
python python/evaluation/code_search/benchmark.py --live --generation-only `
  --skip-routing --repo mini=D:\Project\claude-code-from-scratch `
  --output python/evaluation/code_search/results
python python/evaluation/code_search/benchmark.py --live --generation-only `
  --skip-routing --repo coc-lite=D:\Project\coc-lite `
  --output python/evaluation/code_search/results

# 模型路由和库外问答评测。
python python/evaluation/code_search/benchmark.py --live --routing-only `
  --output python/evaluation/code_search/results
```

在线生成每题最多向聊天 provider 发送 12,000 个字符的检索源码上下文。
报告只保存答案哈希、分数、引用、稳定错误码、模型名和语料指纹；不保存源码上下文、
完整答案、endpoint 或 API key。

## 指标说明

检索层报告 `Hit@5`、`Recall@5` 和 `MRR@10`：

- `Hit@5`：前 5 个结果中是否至少命中一个 gold。
- `Recall@5`：前 5 个结果覆盖了多少比例的 gold。
- `MRR@10`：首个 gold 在前 10 个结果中的倒数排名，越接近 1 越好。

主链边际收益使用固定种子 `20260815` 的 10,000 次配对 bootstrap，报告 95% 置信区间。
关系任务还报告 anchor 命中率、邻居 `Recall@5` 和端到端链路成功率。
累加主链在每仓库相同的 20 个节点问题上比较 FTS、Hybrid、Hybrid + 图扩展。

路由层报告总体准确率、各类别召回率、混淆矩阵和固定下游估计。
该估计对规则路由、模型路由和 oracle 使用相同的仓库平均 `Hit@5` 计分表；
路由到不兼容的方法类型时记零分。

生成层报告声明级 Faithfulness（忠实度）和反向问题 embedding Relevancy（答案相关性）。
库外问答报告 LLM 正确性、答案/参考答案余弦相似度及两者均值。
失败的生成或库外样本按零分计入质量均值。

延迟使用线性插值计算 P50/P95。索引构建和首次 embedding 补齐与 warm 搜索分开统计；
生成阶段还分别统计检索、答案生成、评审和端到端延迟。
