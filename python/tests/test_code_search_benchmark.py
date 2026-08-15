import tempfile
import unittest
from pathlib import Path
import sys
import subprocess
import json

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from evaluation.code_search import (  # noqa: E402
    aggregate_ranked_cases,
    build_context,
    classify_route,
    create_source_snapshot,
    percentile,
    provider_error_code,
    routing_downstream_metrics,
    summarize_relation_paths,
    summarize_routing_predictions,
)


class CodeSearchBenchmarkTests(unittest.TestCase):
    def test_curated_dataset_has_the_committed_sample_sizes(self):
        cases = json.loads(
            (_PYTHON_DIR / "evaluation" / "code_search" / "cases.json").read_text(
                encoding="utf-8"
            )
        )

        for repository in ("mini", "coc-lite"):
            self.assertEqual(len(cases["repositories"][repository]["search"]), 20)
            self.assertEqual(len(cases["repositories"][repository]["relations"]), 6)
            self.assertEqual(len(cases["repositories"][repository]["qa"]), 6)
        self.assertEqual(len(cases["routing"]), 40)
        self.assertEqual(len(cases["out_of_repo"]), 4)

    def test_ranked_metrics_include_empty_and_failed_cases(self):
        cases = [
            ({"a", "b"}, ["a", "x", "b"]),
            ({"c"}, []),
            ({"d"}, None),
        ]

        result = aggregate_ranked_cases(cases)

        self.assertEqual(result["samples"], 3)
        self.assertEqual(result["errors"], 1)
        self.assertAlmostEqual(result["hit_at_5"], 1 / 3)
        self.assertAlmostEqual(result["recall_at_5"], 1 / 3)
        self.assertAlmostEqual(result["mrr_at_10"], 1 / 3)

    def test_percentile_uses_inclusive_linear_interpolation(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]

        self.assertEqual(percentile(values, 0.5), 30.0)
        self.assertEqual(percentile(values, 0.95), 48.0)
        self.assertIsNone(percentile([], 0.5))

    def test_rule_router_distinguishes_search_shapes(self):
        examples = {
            "python/mini_claude/tools.py::check_permission": "fts",
            "项目如何进行增量索引刷新": "hybrid",
            "check_permission 的 callers_of": "graph_exact",
            "哪些组件依赖环境变量加载流程": "hybrid_graph",
            "解释一下快速排序的平均复杂度": "no_code_search",
        }

        for query, expected in examples.items():
            with self.subTest(query=query):
                self.assertEqual(classify_route(query), expected)

    def test_routing_summary_reports_per_class_recall_and_confusion(self):
        cases = [
            {"label": "fts"},
            {"label": "fts"},
            {"label": "hybrid"},
        ]

        result = summarize_routing_predictions(
            cases, ["fts", "hybrid", "error"], [1.0, 3.0], errors=1
        )

        self.assertAlmostEqual(result["accuracy"], 1 / 3)
        self.assertEqual(result["per_class_recall"], {"fts": 0.5, "hybrid": 0.0})
        self.assertEqual(result["confusion"]["fts"]["hybrid"], 1)
        self.assertEqual(result["confusion"]["hybrid"]["error"], 1)
        self.assertEqual(result["latency_p50_ms"], 2.0)

    def test_routing_downstream_uses_one_fixed_strategy_scorecard(self):
        routing = {
            "heuristic": {
                "confusion": {
                    "fts": {"fts": 1, "hybrid": 1, "graph_exact": 0,
                            "hybrid_graph": 0, "no_code_search": 0, "error": 0},
                    "hybrid": {"fts": 0, "hybrid": 2, "graph_exact": 0,
                               "hybrid_graph": 0, "no_code_search": 0, "error": 0},
                    "graph_exact": {"fts": 0, "hybrid": 0, "graph_exact": 1,
                                    "hybrid_graph": 0, "no_code_search": 0, "error": 0},
                    "hybrid_graph": {"fts": 0, "hybrid": 0, "graph_exact": 0,
                                     "hybrid_graph": 1, "no_code_search": 0, "error": 0},
                    "no_code_search": {"fts": 0, "hybrid": 0, "graph_exact": 0,
                                       "hybrid_graph": 0, "no_code_search": 1, "error": 0},
                }
            },
            "oracle": {
                "confusion": {
                    label: {candidate: int(label == candidate)
                            for candidate in ("fts", "hybrid", "graph_exact",
                                              "hybrid_graph", "no_code_search", "error")}
                    for label in ("fts", "hybrid", "graph_exact", "hybrid_graph",
                                  "no_code_search")
                }
            },
        }
        repositories = [{
            "node_metrics": {
                "fts": {"hit_at_5": 0.4},
                "hybrid": {"hit_at_5": 0.8},
            },
            "relation_metrics": {
                "graph_exact": {"hit_at_5": 0.6},
                "hybrid_graph": {"hit_at_5": 0.7},
            },
        }]

        result = routing_downstream_metrics(routing, repositories)

        self.assertEqual(result["scorecard"]["hybrid"], 0.8)
        self.assertAlmostEqual(result["routers"]["heuristic"]["score"], 5.1 / 7)
        self.assertAlmostEqual(result["routers"]["oracle"]["score"], 3.5 / 5)
        self.assertAlmostEqual(
            result["routers"]["heuristic"]["delta_vs_oracle"], 5.1 / 7 - 3.5 / 5
        )

    def test_relation_path_summary_counts_anchor_and_end_to_end_failures(self):
        paths = [
            {"anchor_hit": True, "neighbors": ({"a", "b"}, ["a", "x"])},
            {"anchor_hit": False, "neighbors": ({"c"}, None)},
        ]

        result = summarize_relation_paths(paths)

        self.assertEqual(result["samples"], 2)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["anchor_hit_rate"], 0.5)
        self.assertEqual(result["neighbor_recall_at_5"], 0.25)
        self.assertEqual(result["end_to_end_success_rate"], 0.5)

    def test_provider_errors_are_reduced_to_stable_codes(self):
        class HttpFailure(Exception):
            status_code = 502

        self.assertEqual(provider_error_code(HttpFailure("secret endpoint")), "provider_http_502")
        self.assertEqual(
            provider_error_code(RuntimeError("Connection error.")),
            "provider_connection_error",
        )
        self.assertEqual(provider_error_code(RuntimeError("embedding_error")), "embedding_error")

    def test_context_deduplicates_nodes_and_honors_character_budget(self):
        source = "def alpha():\n    return '" + ("x" * 100) + "'\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text(source, encoding="utf-8")
            hits = [
                {
                    "qualified_name": "app.py::alpha",
                    "file_path": "app.py",
                    "line_start": 1,
                    "line_end": 2,
                },
                {
                    "qualified_name": "app.py::alpha",
                    "file_path": "app.py",
                    "line_start": 1,
                    "line_end": 2,
                },
            ]

            context, citations = build_context(root, hits, max_chars=80)

        self.assertLessEqual(len(context), 80)
        self.assertEqual(citations, ["app.py::alpha"])
        self.assertEqual(context.count("app.py::alpha"), 1)

    def test_snapshot_excludes_benchmark_code_and_preserves_project_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            snapshot = base / "snapshot"
            (source / "python" / "mini_claude").mkdir(parents=True)
            (source / "python" / "tests").mkdir(parents=True)
            (source / "python" / "evaluation" / "code_search").mkdir(
                parents=True
            )
            (source / "python" / "mini_claude" / "app.py").write_text("x = 1")
            (source / "python" / "tests" / "test_app.py").write_text("x = 2")
            (source / "python" / "tests" / "test_code_search_benchmark.py").write_text(
                "x = 4"
            )
            (source / "python" / "evaluation" / "code_search" / "benchmark.py").write_text(
                "x = 3"
            )
            (source / "README.md").write_text("docs")

            metadata = create_source_snapshot(source, snapshot)

            self.assertTrue((snapshot / "python" / "mini_claude" / "app.py").is_file())
            self.assertTrue((snapshot / "python" / "tests" / "test_app.py").is_file())
            self.assertFalse(
                (snapshot / "python" / "tests" / "test_code_search_benchmark.py").exists()
            )
            self.assertFalse((snapshot / "python" / "evaluation").exists())
            self.assertFalse((snapshot / "README.md").exists())
            self.assertEqual(metadata["source_files"], 2)
            self.assertEqual(len(metadata["corpus_sha256"]), 64)

    def test_snapshot_obeys_git_ignore_for_nested_source_trees(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            snapshot = base / "snapshot"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            (source / ".gitignore").write_text("vendor/\n", encoding="utf-8")
            (source / "app.py").write_text("x = 1", encoding="utf-8")
            (source / "vendor").mkdir()
            (source / "vendor" / "copied.py").write_text("x = 2", encoding="utf-8")

            metadata = create_source_snapshot(source, snapshot)

            self.assertTrue((snapshot / "app.py").is_file())
            self.assertFalse((snapshot / "vendor").exists())
            self.assertEqual(metadata["source_files"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
