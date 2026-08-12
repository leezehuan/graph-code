import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude import tools  # noqa: E402
from mini_claude.autonomy import AUTO_MODE_FAST_PATH_TOOLS  # noqa: E402


class CodeGraphToolContractTests(unittest.TestCase):
    def setUp(self):
        self.previous_rules = tools._cached_rules
        tools._cached_rules = {"allow": [], "deny": []}
        tools.reset_activated_tools()

    def tearDown(self):
        tools._cached_rules = self.previous_rules
        tools.reset_activated_tools()

    def test_code_graph_is_deferred_read_tool_with_expected_schema(self):
        definition = next(
            tool for tool in tools.tool_definitions if tool["name"] == "code_graph"
        )
        self.assertTrue(definition["deferred"])
        self.assertNotIn(
            "code_graph",
            {tool["name"] for tool in tools.get_active_tool_definitions()},
        )

        result = json.loads(
            asyncio.run(tools.execute_tool("tool_search", {"query": "code graph"}))
        )
        self.assertEqual([item["name"] for item in result], ["code_graph"])
        schema = result[0]["input_schema"]
        self.assertEqual(schema["required"], ["action"])
        self.assertEqual(
            schema["properties"]["action"]["enum"],
            ["search", "query", "impact", "overview"],
        )
        self.assertEqual(
            schema["properties"]["relation"]["enum"],
            [
                "callers_of",
                "callees_of",
                "importers_of",
                "tests_for",
                "children_of",
                "inheritors_of",
                "references_to",
            ],
        )

        self.assertIn("code_graph", tools.READ_TOOLS)
        self.assertIn("code_graph", AUTO_MODE_FAST_PATH_TOOLS)
        self.assertNotIn("code_graph", tools.CONCURRENCY_SAFE_TOOLS)
        self.assertEqual(
            tools.check_permission("code_graph", {}, "default")["action"], "allow"
        )
        self.assertEqual(
            tools.check_permission("code_graph", {}, "plan")["action"], "allow"
        )


class CodeGraphBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        self.home = Path(self.temp_dir.name) / "home"
        self.repo.mkdir()
        self.home.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=self.repo, check=True
        )
        self.previous_cwd = Path.cwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def call(self, **inp):
        with patch("pathlib.Path.home", return_value=self.home):
            raw = asyncio.run(tools.execute_tool("code_graph", inp))
        return json.loads(raw)

    def commit_all(self):
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "fixture"], cwd=self.repo, check=True
        )

    def test_search_builds_index_then_reuses_it(self):
        (self.repo / "app.py").write_text(
            "class Greeter:\n"
            "    def greet(self, name):\n"
            "        return format_name(name)\n\n"
            "def format_name(name):\n"
            "    return name.title()\n",
            encoding="utf-8",
        )
        self.commit_all()

        first = self.call(action="search", query="greet")
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["action"], "search")
        self.assertEqual(first["index"]["mode"], "full")
        self.assertEqual(
            first["data"]["results"][0]["qualified_name"],
            "app.py::Greeter.greet",
        )

        second = self.call(action="search", query="Greeter")
        self.assertEqual(second["index"]["mode"], "unchanged")
        self.assertEqual(second["data"]["results"][0]["kind"], "class")

    def test_queries_relationships_and_overview(self):
        (self.repo / "lib.py").write_text(
            "class Base:\n"
            "    pass\n\n"
            "def helper():\n"
            "    return 1\n\n"
            "def callback():\n"
            "    return 2\n",
            encoding="utf-8",
        )
        (self.repo / "app.py").write_text(
            "from lib import Base, callback, helper\n\n"
            "class Worker(Base):\n"
            "    def run(self):\n"
            "        selected = callback\n"
            "        return helper() + selected()\n",
            encoding="utf-8",
        )
        tests_dir = self.repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_app.py").write_text(
            "from app import Worker\n\n"
            "def test_worker():\n"
            "    assert Worker().run() == 3\n",
            encoding="utf-8",
        )
        self.commit_all()

        cases = {
            ("callers_of", "helper"): "app.py::Worker.run",
            ("callees_of", "app.py::Worker.run"): "lib.py::helper",
            ("importers_of", "lib.py"): "app.py",
            ("tests_for", "Worker"): "tests/test_app.py::test_worker",
            ("children_of", "Worker"): "app.py::Worker.run",
            ("inheritors_of", "Base"): "app.py::Worker",
            ("references_to", "callback"): "app.py::Worker.run",
        }
        for (relation, target), expected in cases.items():
            with self.subTest(relation=relation):
                result = self.call(
                    action="query", relation=relation, target=target, max_results=10
                )
                self.assertTrue(result["ok"], result)
                self.assertIn(
                    expected,
                    [item["qualified_name"] for item in result["data"]["results"]],
                )

        overview = self.call(action="overview")
        self.assertTrue(overview["ok"], overview)
        self.assertEqual(overview["data"]["languages"], {"python": 3})
        self.assertIn("tests", overview["data"]["top_level_directories"])
        self.assertGreaterEqual(overview["data"]["test_symbols"], 1)
        self.assertIn(
            "lib.py::helper",
            [item["qualified_name"] for item in overview["data"]["high_indegree"]],
        )

    def test_query_rejects_ambiguous_simple_target(self):
        (self.repo / "one.py").write_text("def duplicate():\n    pass\n", encoding="utf-8")
        (self.repo / "two.py").write_text("def duplicate():\n    pass\n", encoding="utf-8")
        self.commit_all()

        result = self.call(
            action="query", relation="callers_of", target="duplicate"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ambiguous_target")
        self.assertEqual(
            result["error"]["candidates"],
            ["one.py::duplicate", "two.py::duplicate"],
        )

    def test_incremental_refresh_and_two_hop_change_impact(self):
        (self.repo / "core.py").write_text(
            "def calculate():\n    return 1\n", encoding="utf-8"
        )
        (self.repo / "feature.py").write_text(
            "from core import calculate\n\n"
            "def feature():\n    return calculate()\n",
            encoding="utf-8",
        )
        (self.repo / "api.py").write_text(
            "from feature import feature\n\n"
            "def endpoint():\n    return feature()\n",
            encoding="utf-8",
        )
        self.commit_all()
        self.call(action="overview")

        (self.repo / "core.py").write_text(
            "def calculate():\n    return 2\n", encoding="utf-8"
        )
        (self.repo / "new_module.py").write_text(
            "def fresh():\n    return 3\n", encoding="utf-8"
        )

        impact = self.call(action="impact")
        self.assertTrue(impact["ok"], impact)
        self.assertEqual(impact["index"]["mode"], "incremental")
        self.assertEqual(impact["index"]["updated_files"], 2)
        impacted = {
            item["qualified_name"]: (item["depth"], item["via_relation"])
            for item in impact["data"]["impacted"]
        }
        self.assertEqual(impacted["feature.py::feature"], (1, "CALLS"))
        self.assertEqual(impacted["api.py::endpoint"], (2, "CALLS"))
        self.assertEqual(
            impact["data"]["changed_files"], ["core.py", "new_module.py"]
        )

        (self.repo / "core.py").unlink()
        deleted = self.call(action="impact", changed_files=["core.py"])
        self.assertTrue(deleted["ok"], deleted)
        self.assertEqual(deleted["index"]["deleted_files"], 1)
        search = self.call(action="search", query="calculate")
        self.assertEqual(search["data"]["results"], [])

    def test_impact_keeps_old_edges_when_a_symbol_is_removed_from_a_live_file(self):
        (self.repo / "core.py").write_text(
            "def calculate():\n    return 1\n\ndef retained():\n    return 2\n",
            encoding="utf-8",
        )
        (self.repo / "feature.py").write_text(
            "from core import calculate\n\ndef feature():\n    return calculate()\n",
            encoding="utf-8",
        )
        self.commit_all()
        self.call(action="overview")

        (self.repo / "core.py").write_text(
            "def retained():\n    return 2\n",
            encoding="utf-8",
        )
        impact = self.call(action="impact")

        self.assertTrue(impact["ok"], impact)
        self.assertIn(
            "feature.py::feature",
            [item["qualified_name"] for item in impact["data"]["impacted"]],
        )
        removed = self.call(action="search", query="calculate")
        self.assertEqual(removed["data"]["results"], [])

    def test_impact_keeps_deleted_symbol_history_after_an_intervening_search(self):
        (self.repo / "core.py").write_text(
            "def calculate():\n    return 1\n", encoding="utf-8"
        )
        (self.repo / "feature.py").write_text(
            "from core import calculate\n\ndef feature():\n    return calculate()\n",
            encoding="utf-8",
        )
        self.commit_all()
        self.call(action="overview")
        (self.repo / "core.py").unlink()

        self.call(action="search", query="calculate")
        impact = self.call(action="impact")

        self.assertIn(
            "feature.py::feature",
            [item["qualified_name"] for item in impact["data"]["impacted"]],
        )

    def test_impact_tracks_both_sides_of_a_git_rename(self):
        (self.repo / "old_name.py").write_text(
            "def calculate():\n    return 1\n",
            encoding="utf-8",
        )
        self.commit_all()
        self.call(action="overview")

        subprocess.run(
            ["git", "mv", "old_name.py", "new_name.py"],
            cwd=self.repo,
            check=True,
        )
        impact = self.call(action="impact")

        self.assertTrue(impact["ok"], impact)
        self.assertEqual(
            impact["data"]["changed_files"],
            ["new_name.py", "old_name.py"],
        )
        changed_names = {
            item["qualified_name"] for item in impact["data"]["changed_nodes"]
        }
        self.assertIn("new_name.py::calculate", changed_names)
        self.assertIn("old_name.py::calculate", changed_names)

    def test_rejects_changed_paths_outside_project(self):
        (self.repo / "app.py").write_text("def run():\n    pass\n", encoding="utf-8")
        self.commit_all()
        result = self.call(
            action="impact", changed_files=["../outside.py"]
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_path")

    def test_indexes_eight_core_language_families(self):
        fixtures = {
            "sample.py": "class PyType:\n    def run(self):\n        return helper()\n\ndef helper():\n    return 1\n",
            "sample.jsx": "import React from 'react';\nclass JsType {}\nfunction jsRun() { return jsHelp(); }\nfunction jsHelp() {}\n",
            "sample.tsx": "interface TsType { run(): void }\nfunction tsRun() { return tsHelp(); }\nfunction tsHelp() {}\n",
            "Sample.java": "import java.util.List; class JavaType { int run() { return help(); } int help() { return 1; } }\n",
            "sample.go": "package sample\nimport \"fmt\"\ntype GoType struct{}\nfunc goRun() { fmt.Println(\"x\") }\n",
            "sample.rs": "use std::fmt; struct RustType; fn rust_run() { rust_help(); } fn rust_help() {}\n",
            "sample.cpp": "#include <stdio.h>\nclass CppType {}; int cpp_run() { return cpp_help(); } int cpp_help() { return 1; }\n",
            "Sample.cs": "using System; class CsType { int Run() { return Help(); } int Help() { return 1; } }\n",
        }
        for name, content in fixtures.items():
            (self.repo / name).write_text(content, encoding="utf-8")
        self.commit_all()

        overview = self.call(action="overview", max_results=100)
        self.assertTrue(overview["ok"], overview)
        self.assertEqual(
            set(overview["data"]["languages"]),
            {"python", "javascript", "tsx", "java", "go", "rust", "cpp", "csharp"},
        )
        expected_symbols = [
            "PyType", "JsType", "TsType", "JavaType", "GoType", "RustType",
            "CppType", "CsType", "goRun", "rust_run", "cpp_run", "Run",
        ]
        for symbol in expected_symbols:
            with self.subTest(symbol=symbol):
                result = self.call(action="search", query=symbol, max_results=10)
                self.assertTrue(result["data"]["results"], result)

        self.assertGreaterEqual(overview["data"]["edges"].get("CALLS", 0), 6)
        self.assertGreaterEqual(overview["data"]["edges"].get("IMPORTS", 0), 4)

    def test_indexes_overloaded_and_receiver_scoped_methods_without_collisions(self):
        (self.repo / "Overloads.java").write_text(
            "class Overloads {\n"
            "  int convert(int value) { return value; }\n"
            "  String convert(String value) { return value; }\n"
            "}\n",
            encoding="utf-8",
        )
        (self.repo / "receivers.go").write_text(
            "package sample\n"
            "type First struct{}\n"
            "type Second struct{}\n"
            "func (First) Run() {}\n"
            "func (Second) Run() {}\n",
            encoding="utf-8",
        )
        self.commit_all()

        java = self.call(action="search", query="convert", max_results=10)
        self.assertTrue(java["ok"], java)
        self.assertEqual(len(java["data"]["results"]), 2)
        self.assertEqual(
            len({item["qualified_name"] for item in java["data"]["results"]}),
            2,
        )

        go = self.call(action="search", query="Run", max_results=10)
        self.assertTrue(go["ok"], go)
        self.assertEqual(len(go["data"]["results"]), 2)
        ambiguous = self.call(
            action="query", relation="callers_of", target="Run"
        )
        self.assertEqual(ambiguous["error"]["code"], "ambiguous_target")

    def test_indexes_variable_bound_arrow_functions_by_variable_name(self):
        (self.repo / "arrows.ts").write_text(
            "const greet = (name: string) => name.trim();\n"
            "const ping = () => greet('hello');\n",
            encoding="utf-8",
        )
        self.commit_all()

        greet = self.call(action="search", query="greet")
        ping = self.call(action="search", query="ping")

        self.assertEqual(
            [item["qualified_name"] for item in greet["data"]["results"]],
            ["arrows.ts::greet"],
        )
        self.assertEqual(
            [item["qualified_name"] for item in ping["data"]["results"]],
            ["arrows.ts::ping"],
        )

    def test_indexes_common_interface_and_impl_methods_in_type_scope(self):
        (self.repo / "contracts.ts").write_text(
            "interface Runner { run(): void }\n", encoding="utf-8"
        )
        (self.repo / "contracts.go").write_text(
            "package sample\ntype Runner interface { Run() error }\n", encoding="utf-8"
        )
        (self.repo / "worker.rs").write_text(
            "struct Worker; impl Worker { fn run(&self) {} }\n", encoding="utf-8"
        )
        self.commit_all()

        expected = {
            "contracts.ts::Runner.run",
            "contracts.go::Runner.Run",
            "worker.rs::Worker.run",
        }
        actual = set()
        for query in ("run", "Run"):
            result = self.call(action="search", query=query, max_results=20)
            actual.update(item["qualified_name"] for item in result["data"]["results"])

        self.assertTrue(expected <= actual, actual)

    def test_refresh_rehashes_same_size_file_even_when_mtime_is_preserved(self):
        path = self.repo / "stable.py"
        path.write_text("def alpha():\n    return 1\n", encoding="utf-8")
        self.commit_all()
        self.call(action="overview")
        original_stat = path.stat()
        path.write_text("def bravo():\n    return 2\n", encoding="utf-8")
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

        result = self.call(action="search", query="bravo")

        self.assertEqual(result["index"]["mode"], "incremental")
        self.assertTrue(result["data"]["results"], result)

    def test_relationship_limit_counts_unique_sources(self):
        (self.repo / "lib.py").write_text(
            "def helper():\n    return 1\n", encoding="utf-8"
        )
        (self.repo / "app.py").write_text(
            "from lib import helper\n\n"
            "def first():\n    return helper() + helper()\n\n"
            "def second():\n    return helper()\n",
            encoding="utf-8",
        )
        self.commit_all()

        result = self.call(
            action="query", relation="callers_of", target="helper", max_results=2
        )

        self.assertEqual(
            [item["qualified_name"] for item in result["data"]["results"]],
            ["app.py::first", "app.py::second"],
        )

    def test_references_do_not_resolve_shadowed_parameters(self):
        (self.repo / "app.py").write_text(
            "def callback():\n    return 1\n\n"
            "def run(callback):\n    return callback\n",
            encoding="utf-8",
        )
        self.commit_all()

        result = self.call(
            action="query", relation="references_to", target="callback"
        )

        self.assertEqual(result["data"]["results"], [])

    def test_automatic_impact_changed_files_only_lists_source_files(self):
        (self.repo / "app.py").write_text("def run():\n    pass\n", encoding="utf-8")
        (self.repo / "README.md").write_text("initial\n", encoding="utf-8")
        self.commit_all()
        self.call(action="overview")
        (self.repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")

        impact = self.call(action="impact")

        self.assertEqual(impact["data"]["changed_files"], ["app.py"])

    def test_skips_oversized_and_binary_source_files(self):
        (self.repo / "valid.py").write_text("def valid():\n    pass\n", encoding="utf-8")
        (self.repo / "huge.py").write_bytes(b"#" * (2 * 1024 * 1024 + 1))
        (self.repo / "binary.py").write_bytes(b"def nope():\x00\xff\n")
        self.commit_all()

        overview = self.call(action="overview")
        self.assertTrue(overview["ok"], overview)
        self.assertEqual(overview["data"]["languages"], {"python": 1})
        self.assertEqual(overview["index"]["skipped_files"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
