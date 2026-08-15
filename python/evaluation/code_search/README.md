# Code Search Benchmark

This benchmark compares Mini Claude's code-search strategies on curated gold
cases without changing the production `code_graph` interface.

## Strategies

- `grep_file`: query-time source scan, no code graph; file metrics only.
- `fts`: local FTS5 node search.
- `semantic`: embedding cosine search.
- `hybrid`: FTS5 and semantic candidates merged by the production RRF ranker.
- `graph_exact`: direct relation traversal from a known symbol.
- `hybrid_graph`: hybrid candidates reranked with relations from the top three
  anchors; relation cases use hybrid anchor search followed by traversal.
- `routed`: the rule router selects FTS or hybrid for each node-search case.

The report keeps file and node metrics separate. Empty results and provider
errors remain in metric denominators. Out-of-repository questions have no gold
code node and are excluded from retrieval metrics.

## Run

Offline runs exercise grep, FTS, direct graph traversal, routing rules, and
report generation. Semantic and model-backed rows are recorded as offline
errors.

```powershell
python python/evaluation/code_search/benchmark.py `
  --repo mini=D:\Project\claude-code-from-scratch `
  --repo coc-lite=D:\Project\coc-lite `
  --output python/evaluation/code_search/results
```

The live run uses the source repository's `.env` and requires the existing
chat and embedding settings:

```powershell
python python/evaluation/code_search/benchmark.py `
  --repo mini=D:\Project\claude-code-from-scratch `
  --repo coc-lite=D:\Project\coc-lite `
  --live `
  --output python/evaluation/code_search/results
```

For providers with high tail latency, checkpoint the same run in three stages:

```powershell
# Retrieval for both repositories.
python python/evaluation/code_search/benchmark.py --live --retrieval-only `
  --repo mini=D:\Project\claude-code-from-scratch `
  --repo coc-lite=D:\Project\coc-lite --output python/evaluation/code_search/results

# Generation per repository; each command merges into latest.json.
python python/evaluation/code_search/benchmark.py --live --generation-only `
  --skip-routing --repo mini=D:\Project\claude-code-from-scratch `
  --output python/evaluation/code_search/results
python python/evaluation/code_search/benchmark.py --live --generation-only `
  --skip-routing --repo coc-lite=D:\Project\coc-lite `
  --output python/evaluation/code_search/results

# Model routing and out-of-repository QA.
python python/evaluation/code_search/benchmark.py --live --routing-only `
  --output python/evaluation/code_search/results
```

Live generation sends at most 12,000 characters of retrieved source context
per question to the configured chat provider. Reports store answer hashes,
scores, citations, provider errors, model names, and corpus fingerprints; they
do not store source contexts, answers, endpoints, or API keys.

## Metrics

Retrieval reports Hit@5, Recall@5, and MRR@10. Main-chain deltas use 10,000
paired bootstrap samples with seed `20260815`. Relation tasks additionally
report anchor hit rate, neighbor Recall@5, and end-to-end chain success.
The cumulative main chain compares FTS, hybrid, and hybrid plus graph expansion
on the same 20 node cases per repository.

Routing reports overall accuracy, per-class recall, confusion matrices, and a
controlled downstream estimate. The estimate applies one repository-average
Hit@5 scorecard to heuristic, model, and oracle predictions; selecting an
incompatible route family receives zero credit.

Generation reports claim-level faithfulness and reverse-question embedding
relevancy. Out-of-repository QA reports LLM correctness, answer/reference
cosine similarity, and their mean. Latency uses inclusive linear interpolation
for P50 and P95. Index build and first embedding backfill are separated from
warm search; generation also splits retrieval, answer generation, evaluation,
and end-to-end latency. Provider failures are saved as stable error codes, not
raw response text, and failed generation rows contribute zero to aggregate
quality scores.
