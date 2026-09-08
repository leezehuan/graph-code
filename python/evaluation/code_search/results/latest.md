# 代码搜索量化评测

由 `python/evaluation/code_search/benchmark.py` 生成。
运行时间：`2026-08-15T17:46:16.230642+00:00`。模型：`gpt-5.6-terra` / `qwen3.7-text-embedding`。

## 检索方法

- `fts`（FTS 全文检索）：本地词法匹配，适合明确的符号名和路径。
- `semantic`（语义向量检索）：按 embedding 余弦相似度匹配自然语言概念。
- `hybrid`（混合检索）：用 RRF 融合 FTS 与 Semantic 的候选结果。
- `hybrid_graph`（混合检索 + 图扩展）：再利用前三个候选的代码关系进行重排。
- `routed`（自动路由）：先判断问题形态，再自动选择 FTS 或 Hybrid。

## 仓库概览

| 仓库 | 源码文件数 | 语料 SHA256 | 提交 | 工作树有修改 |
|---|---:|---|---|---|
| mini | 26 | `529d9f8bbb02` | `b353c77f76904108a35fe9a09da05837477822c0` | 是 |
| coc-lite | 79 | `feb64441e0c9` | `0e29f8383f186c2766c21f1287dfc8cb77f59950` | 是 |

## 检索效果

| 仓库 / 方法 | 样本数 | 错误数 | 节点 Hit@5 | 节点 Recall@5 | 节点 MRR@10 | 文件 Hit@5 | 文件 Recall@5 | 文件 MRR@10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| mini / `fts` | 20 | 0 | 0.500 | 0.500 | 0.500 | 0.550 | 0.525 | 0.550 |
| mini / `semantic` | 20 | 3 | 0.750 | 0.725 | 0.642 | 0.850 | 0.850 | 0.750 |
| mini / `hybrid` | 20 | 0 | 0.900 | 0.875 | 0.792 | 1.000 | 1.000 | 0.900 |
| mini / `hybrid_graph` | 20 | 0 | 0.900 | 0.900 | 0.810 | 1.000 | 0.975 | 0.900 |
| mini / `routed` | 20 | 0 | 0.900 | 0.875 | 0.792 | 1.000 | 1.000 | 0.900 |
| coc-lite / `fts` | 20 | 0 | 0.650 | 0.650 | 0.613 | 0.750 | 0.750 | 0.750 |
| coc-lite / `semantic` | 20 | 4 | 0.750 | 0.733 | 0.700 | 0.750 | 0.733 | 0.750 |
| coc-lite / `hybrid` | 20 | 0 | 0.950 | 0.933 | 0.825 | 0.950 | 0.933 | 0.925 |
| coc-lite / `hybrid_graph` | 20 | 0 | 0.950 | 0.933 | 0.771 | 0.950 | 0.933 | 0.925 |
| coc-lite / `routed` | 20 | 0 | 0.950 | 0.933 | 0.812 | 0.950 | 0.933 | 0.925 |

## 关系检索

| 仓库 / 方法 | 样本数 | 错误数 | Anchor 命中率 | 邻居 Recall@5 | 端到端成功率 | MRR@10 |
|---|---:|---:|---:|---:|---:|---:|
| mini / `graph_exact` | 6 | 0 | 1.000 | 0.750 | 0.833 | 0.583 |
| mini / `hybrid_graph` | 6 | 0 | 1.000 | 0.750 | 0.833 | 0.583 |
| coc-lite / `graph_exact` | 6 | 0 | 1.000 | 0.667 | 0.833 | 0.331 |
| coc-lite / `hybrid_graph` | 6 | 0 | 1.000 | 0.667 | 0.833 | 0.331 |

## 主链消融

| 仓库 / 叠加步骤 | 指标 | 增量 | Bootstrap 95% 置信区间 |
|---|---|---:|---:|
| mini / `fts_to_hybrid` | `hit_at_5` | 0.400 | [0.200, 0.600] |
| mini / `fts_to_hybrid` | `recall_at_5` | 0.375 | [0.175, 0.600] |
| mini / `fts_to_hybrid` | `mrr_at_10` | 0.292 | [0.125, 0.475] |
| mini / `hybrid_to_hybrid_graph` | `hit_at_5` | 0.000 | [0.000, 0.000] |
| mini / `hybrid_to_hybrid_graph` | `recall_at_5` | 0.025 | [0.000, 0.075] |
| mini / `hybrid_to_hybrid_graph` | `mrr_at_10` | 0.018 | [-0.020, 0.075] |
| mini / `graph_exact_to_hybrid_graph` | `hit_at_5` | 0.000 | [0.000, 0.000] |
| mini / `graph_exact_to_hybrid_graph` | `recall_at_5` | 0.000 | [0.000, 0.000] |
| mini / `graph_exact_to_hybrid_graph` | `mrr_at_10` | 0.000 | [0.000, 0.000] |
| coc-lite / `fts_to_hybrid` | `hit_at_5` | 0.300 | [0.100, 0.500] |
| coc-lite / `fts_to_hybrid` | `recall_at_5` | 0.283 | [0.100, 0.483] |
| coc-lite / `fts_to_hybrid` | `mrr_at_10` | 0.212 | [0.075, 0.362] |
| coc-lite / `hybrid_to_hybrid_graph` | `hit_at_5` | 0.000 | [0.000, 0.000] |
| coc-lite / `hybrid_to_hybrid_graph` | `recall_at_5` | 0.000 | [0.000, 0.000] |
| coc-lite / `hybrid_to_hybrid_graph` | `mrr_at_10` | -0.054 | [-0.117, -0.008] |
| coc-lite / `graph_exact_to_hybrid_graph` | `hit_at_5` | 0.000 | [0.000, 0.000] |
| coc-lite / `graph_exact_to_hybrid_graph` | `recall_at_5` | 0.000 | [0.000, 0.000] |
| coc-lite / `graph_exact_to_hybrid_graph` | `mrr_at_10` | 0.000 | [0.000, 0.000] |

## 冷启动延迟（毫秒）

| 仓库 / 阶段 | 延迟 | 错误 | 更新节点数 |
|---|---:|---|---:|
| mini / `index_build` | 829.4944000081159 | 无 | n/a |
| mini / `embedding_backfill` | 5326.887999995961 | embedding_error | n/a |
| coc-lite / `index_build` | 1084.5742000092287 | 无 | n/a |
| coc-lite / `embedding_backfill` | 13642.491799997515 | embedding_error | n/a |

## 热搜索延迟（毫秒）

| 仓库 / 方法 | 成功样本数 | P50 | P95 | 错误数 |
|---|---:|---:|---:|---:|
| mini / `grep_file` | 20 | 6.760750002285931 | 7.870059995912015 | 0 |
| mini / `fts` | 20 | 101.96295000059763 | 110.09404999349499 | 0 |
| mini / `semantic` | 17 | 491.7659999919124 | 1491.3155600050231 | 3 |
| mini / `hybrid` | 20 | 507.9853999995976 | 5328.3329349920705 | 0 |
| mini / `hybrid_graph` | 20 | 2628.1218499934766 | 7637.791614995528 | 0 |
| mini / `routed` | 20 | 270.31814999645576 | 1589.9225550092538 | 0 |
| mini / `graph_exact_relation` | 6 | 101.35469999659108 | 116.44800000431133 | 0 |
| mini / `hybrid_graph_relation` | 6 | 595.0981999994838 | 4084.569249993365 | 0 |
| coc-lite / `grep_file` | 20 | 17.094900002120994 | 19.776560004538624 | 0 |
| coc-lite / `fts` | 20 | 123.83565000345698 | 127.33349498957978 | 0 |
| coc-lite / `semantic` | 16 | 559.3085499931476 | 1584.5721250043425 | 4 |
| coc-lite / `hybrid` | 20 | 549.9802999911481 | 11646.919884999083 | 0 |
| coc-lite / `hybrid_graph` | 20 | 3237.9152499925112 | 6362.004190043809 | 0 |
| coc-lite / `routed` | 20 | 307.922600004531 | 1566.593660001672 | 0 |
| coc-lite / `graph_exact_relation` | 6 | 119.7604499902809 | 127.39967500237981 | 0 |
| coc-lite / `hybrid_graph_relation` | 6 | 708.7077499963925 | 5265.6595000044035 | 0 |

## 路由评测

| 路由器 | 样本数 | 准确率 | 错误数 | P50（毫秒） | P95（毫秒） |
|---|---:|---:|---:|---:|---:|
| `heuristic` | 40 | 0.825 | 0 | 0.0037999925552867353 | 0.008559992420487102 |
| `model` | 40 | 0.450 | 0 | 2904.661299995496 | 11228.57228000066 |
| `oracle` | 40 | 1.000 | 0 | n/a | n/a |

### 各类别召回率

| 路由器 / 路由类别 | 召回率 |
|---|---:|
| `heuristic` / `fts` | 0.875 |
| `heuristic` / `graph_exact` | 1.000 |
| `heuristic` / `hybrid` | 1.000 |
| `heuristic` / `hybrid_graph` | 0.750 |
| `heuristic` / `no_code_search` | 0.500 |
| `model` / `fts` | 0.000 |
| `model` / `graph_exact` | 0.875 |
| `model` / `hybrid` | 0.250 |
| `model` / `hybrid_graph` | 0.125 |
| `model` / `no_code_search` | 1.000 |
| `oracle` / `fts` | 1.000 |
| `oracle` / `graph_exact` | 1.000 |
| `oracle` / `hybrid` | 1.000 |
| `oracle` / `hybrid_graph` | 1.000 |
| `oracle` / `no_code_search` | 1.000 |

### 混淆矩阵

`heuristic`（行 = 预标注，列 = 预测）

| 预标注 / 预测 | `fts` | `graph_exact` | `hybrid` | `hybrid_graph` | `no_code_search` | `error` |
|---|---:|---:|---:|---:|---:|---:|
| `fts` | 7 | 0 | 1 | 0 | 0 | 0 |
| `graph_exact` | 0 | 8 | 0 | 0 | 0 | 0 |
| `hybrid` | 0 | 0 | 8 | 0 | 0 | 0 |
| `hybrid_graph` | 0 | 0 | 2 | 6 | 0 | 0 |
| `no_code_search` | 0 | 0 | 4 | 0 | 4 | 0 |

`model`（行 = 预标注，列 = 预测）

| 预标注 / 预测 | `fts` | `graph_exact` | `hybrid` | `hybrid_graph` | `no_code_search` | `error` |
|---|---:|---:|---:|---:|---:|---:|
| `fts` | 0 | 6 | 0 | 0 | 2 | 0 |
| `graph_exact` | 1 | 7 | 0 | 0 | 0 | 0 |
| `hybrid` | 1 | 1 | 2 | 2 | 2 | 0 |
| `hybrid_graph` | 0 | 7 | 0 | 1 | 0 | 0 |
| `no_code_search` | 0 | 0 | 0 | 0 | 8 | 0 |

`oracle`（行 = 预标注，列 = 预测）

| 预标注 / 预测 | `fts` | `graph_exact` | `hybrid` | `hybrid_graph` | `no_code_search` | `error` |
|---|---:|---:|---:|---:|---:|---:|
| `fts` | 8 | 0 | 0 | 0 | 0 | 0 |
| `graph_exact` | 0 | 8 | 0 | 0 | 0 | 0 |
| `hybrid` | 0 | 0 | 8 | 0 | 0 | 0 |
| `hybrid_graph` | 0 | 0 | 0 | 8 | 0 | 0 |
| `no_code_search` | 0 | 0 | 0 | 0 | 8 | 0 |

### 固定下游效果

| 路由器 | 预期 Hit@5 | 相对 oracle 的差值 | 样本数 |
|---|---:|---:|---:|
| `heuristic` | 0.700 | -0.133 | 40 |
| `model` | 0.573 | -0.260 | 40 |
| `oracle` | 0.833 | 0.000 | 40 |

该受控估计对所有路由器使用同一份仓库平均 Hit@5 计分表；预测到不兼容的路由类型时记零分。

## 生成质量

| 仓库 / 方法 | 样本数 | 成功数 | 忠实度 | 答案相关性 | 错误数 | P50（毫秒） | P95（毫秒） |
|---|---:|---:|---:|---:|---:|---:|---:|
| mini / `grep_file` | 6 | 5 | 0.778 | 0.689 | 1 | 16743.21259999124 | 21527.86504001124 |
| mini / `fts` | 6 | 4 | 0.667 | 0.602 | 2 | 12642.110900007538 | 16105.29308001278 |
| mini / `hybrid` | 6 | 5 | 0.806 | 0.691 | 1 | 18967.314099980285 | 43302.41674000572 |
| coc-lite / `grep_file` | 6 | 6 | 0.963 | 0.751 | 0 | 16236.603350000223 | 27555.66629999521 |
| coc-lite / `fts` | 6 | 4 | 0.667 | 0.603 | 2 | 11144.029900009627 | 11809.232480008359 |
| coc-lite / `hybrid` | 6 | 5 | 0.643 | 0.661 | 1 | 21515.79470001161 | 49773.22943999606 |

### 生成各阶段延迟（毫秒）

| 仓库 / 阶段 | 成功样本数 | P50 | P95 |
|---|---:|---:|---:|
| mini / `retrieval` | 14 | 113.62279999593738 | 14287.081009995009 |
| mini / `generation` | 14 | 7130.890100001125 | 12099.328195009002 |
| mini / `evaluation` | 14 | 6183.765950001543 | 13180.961995001417 |
| mini / `end_to_end` | 14 | 16079.70700001897 | 31513.044130010523 |
| coc-lite / `retrieval` | 15 | 144.50910000596195 | 16754.434419996654 |
| coc-lite / `generation` | 15 | 5034.667300002184 | 12623.013670000362 |
| coc-lite / `evaluation` | 15 | 7267.781300004572 | 13050.244250000098 |
| coc-lite / `end_to_end` | 15 | 15965.450499992585 | 38723.374459997256 |

## 库外问答

样本数：4，成功数：3，错误数：1，LLM 正确性：0.750，embedding 余弦相似度：0.644，综合分：0.697。

## 说明

- `grep_file` 是文件级基线，因此不出现在节点检索方法表中。
- `semantic`、`hybrid` 和 `hybrid_graph` 需要 `--live`；离线运行会记录 `offline_mode` 错误，不会静默删除样本。
- 生成和库外问答中的失败样本按零分计入质量均值。
- 本报告不包含源码正文、endpoint 或 API key。
