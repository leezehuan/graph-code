import asyncio
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude import tools  # noqa: E402
from mini_claude.autonomy import AUTO_MODE_FAST_PATH_TOOLS  # noqa: E402

_EMBEDDING_ENVIRONMENT_VARIABLES = (
    "MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS",
    "MINI_CLAUDE_EMBEDDING_BASE_URL",
    "MINI_CLAUDE_EMBEDDING_MODEL",
    "MINI_CLAUDE_EMBEDDING_API_KEY",
)


def embedding_environment(**values):
    environment = dict(os.environ)
    for name in _EMBEDDING_ENVIRONMENT_VARIABLES:
        environment.pop(name, None)
    environment.update(values)
    return environment


class FakeEmbeddingServer:
    def __init__(self, vector_for, responder=None):
        self.vector_for = vector_for
        self.responder = responder
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                owner.requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "payload": payload,
                })
                if owner.responder is None:
                    response_status = 200
                    response_payload = {
                        "data": [
                            {"index": index, "embedding": owner.vector_for(text)}
                            for index, text in enumerate(payload["input"])
                        ]
                    }
                else:
                    response_status, response_payload = owner.responder(
                        payload, len(owner.requests)
                    )
                body = (
                    response_payload
                    if isinstance(response_payload, bytes)
                    else json.dumps(response_payload).encode("utf-8")
                )
                self.send_response(response_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class NoFtsConnection(sqlite3.Connection):
    def executescript(self, sql_script):
        if "CREATE VIRTUAL TABLE" in sql_script:
            raise sqlite3.OperationalError("no such module: fts5")
        return super().executescript(sql_script)


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
        self.assertEqual(
            schema["properties"]["mode"],
            {
                "type": "string",
                "enum": ["fts", "semantic", "hybrid"],
                "default": "fts",
            },
        )
        self.assertEqual(
            schema["properties"]["kind"]["enum"],
            ["file", "class", "function"],
        )
        self.assertEqual(
            schema["properties"]["context_files"],
            {"type": "array", "items": {"type": "string"}},
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

    def test_search_defaults_to_fts_multi_word_and_returns_scores(self):
        users = self.repo / "users"
        users.mkdir()
        (users / "account.py").write_text(
            "def load_profile():\n    return 1\n\n"
            "def load_settings():\n    return 2\n",
            encoding="utf-8",
        )
        self.commit_all()

        result = self.call(action="search", query="users load_profile")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["requested_mode"], "fts")
        self.assertEqual(result["data"]["search_mode"], "fts")
        self.assertEqual(
            [item["qualified_name"] for item in result["data"]["results"]],
            ["users/account.py::load_profile"],
        )
        self.assertIsInstance(result["data"]["results"][0]["score"], float)

        fallback = self.call(action="search", query="ofile")
        self.assertEqual(fallback["data"]["requested_mode"], "fts")
        self.assertEqual(fallback["data"]["search_mode"], "keyword")
        self.assertEqual(
            fallback["data"]["results"][0]["qualified_name"],
            "users/account.py::load_profile",
        )
        operator_text = self.call(
            action="search", query="load_profile OR load_settings"
        )
        self.assertEqual(operator_text["data"]["results"], [])
        wildcard_text = self.call(action="search", query="%")
        self.assertEqual(wildcard_text["data"]["results"], [])

    def test_search_falls_back_when_sqlite_fts5_is_unavailable(self):
        (self.repo / "users.py").write_text(
            "def load_profile():\n    return 1\n", encoding="utf-8"
        )
        self.commit_all()
        real_connect = sqlite3.connect

        def connect_without_fts(*args, **kwargs):
            return real_connect(*args, **kwargs, factory=NoFtsConnection)

        with patch("sqlite3.connect", side_effect=connect_without_fts):
            search = self.call(action="search", query="profile")
            overview = self.call(action="overview")

        self.assertTrue(search["ok"], search)
        self.assertEqual(search["data"]["search_mode"], "keyword")
        self.assertEqual(
            search["data"]["results"][0]["qualified_name"],
            "users.py::load_profile",
        )
        self.assertEqual(
            overview["data"]["search_index"],
            {"fts_available": False, "fts_nodes": 0, "embedding_sets": []},
        )

    def test_search_filters_kind_boosts_context_and_validates_options(self):
        for directory in ("first", "second"):
            path = self.repo / directory
            path.mkdir()
            (path / "shared.py").write_text(
                "def shared():\n    return 1\n", encoding="utf-8"
            )
        self.commit_all()

        boosted = self.call(
            action="search",
            query="shared",
            kind="function",
            context_files=["second/shared.py"],
            max_results=2,
        )
        self.assertEqual(
            [item["qualified_name"] for item in boosted["data"]["results"]],
            ["second/shared.py::shared", "first/shared.py::shared"],
        )
        files = self.call(action="search", query="shared", kind="file")
        self.assertTrue(files["data"]["results"], files)
        self.assertEqual(
            {item["kind"] for item in files["data"]["results"]}, {"file"}
        )

        invalid_mode = self.call(action="search", query="shared", mode="vector")
        self.assertEqual(invalid_mode["error"]["code"], "invalid_search_mode")
        invalid_kind = self.call(action="search", query="shared", kind="method")
        self.assertEqual(invalid_kind["error"]["code"], "invalid_kind")
        invalid_path = self.call(
            action="search", query="shared", context_files=["../outside.py"]
        )
        self.assertEqual(invalid_path["error"]["code"], "invalid_context_path")
        absolute_path = self.call(
            action="search",
            query="shared",
            context_files=[str((self.repo / "first" / "shared.py").resolve())],
        )
        self.assertEqual(absolute_path["error"]["code"], "invalid_context_path")
        empty_path = self.call(
            action="search", query="shared", context_files=[""]
        )
        self.assertEqual(empty_path["error"]["code"], "invalid_context_path")

    def test_semantic_search_requires_explicit_cloud_egress_consent(self):
        (self.repo / "app.py").write_text(
            "def process_order():\n    return 1\n", encoding="utf-8"
        )
        self.commit_all()
        with FakeEmbeddingServer(lambda _text: [1.0]) as server:
            environment = embedding_environment(
                MINI_CLAUDE_EMBEDDING_BASE_URL=server.base_url,
                MINI_CLAUDE_EMBEDDING_MODEL="test-model",
            )
            with patch.dict(os.environ, environment, clear=True):
                semantic = self.call(
                    action="search", query="orders", mode="semantic"
                )
                hybrid = self.call(
                    action="search", query="orders", mode="hybrid"
                )

        for result in (semantic, hybrid):
            self.assertFalse(result["ok"], result)
            self.assertEqual(
                result["error"]["code"], "cloud_egress_not_accepted"
            )
        self.assertEqual(server.requests, [])

    def test_semantic_search_embeds_structure_and_ranks_by_cosine_similarity(self):
        (self.repo / "billing.py").write_text(
            "def process_invoice():\n    return 'private source'\n",
            encoding="utf-8",
        )
        (self.repo / "auth.py").write_text(
            "def validate_token():\n    return True\n", encoding="utf-8"
        )
        self.commit_all()

        def vector_for(text):
            if text == "charge customer" or "process_invoice" in text:
                return [1.0, 0.0]
            return [0.0, 1.0]

        with FakeEmbeddingServer(vector_for) as server:
            environment = embedding_environment(
                MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
                MINI_CLAUDE_EMBEDDING_BASE_URL=server.base_url,
                MINI_CLAUDE_EMBEDDING_MODEL="test-model",
                MINI_CLAUDE_EMBEDDING_API_KEY="secret-key",
            )
            with patch.dict(os.environ, environment, clear=True):
                result = self.call(
                    action="search",
                    query="charge customer",
                    mode="semantic",
                    kind="function",
                )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["requested_mode"], "semantic")
        self.assertEqual(result["data"]["search_mode"], "semantic")
        self.assertEqual(
            result["data"]["results"][0]["qualified_name"],
            "billing.py::process_invoice",
        )
        self.assertEqual(result["data"]["embedding"]["provider"], "openai-compatible")
        self.assertEqual(result["data"]["embedding"]["model"], "test-model")
        self.assertGreater(result["data"]["embedding"]["updated_nodes"], 0)
        self.assertTrue(all(request["path"] == "/v1/embeddings" for request in server.requests))
        self.assertTrue(
            all(request["authorization"] == "Bearer secret-key" for request in server.requests)
        )
        document_text = " ".join(
            text
            for request in server.requests[:-1]
            for text in request["payload"]["input"]
        )
        self.assertIn("process_invoice", document_text)
        self.assertNotIn("private source", document_text)

    def test_semantic_search_reuses_cache_and_refreshes_only_changed_file_nodes(self):
        (self.repo / "stable.py").write_text(
            "def stable_symbol():\n    return 1\n", encoding="utf-8"
        )
        (self.repo / "changed.py").write_text(
            "def changed_symbol():\n    return 1\n", encoding="utf-8"
        )
        self.commit_all()

        with FakeEmbeddingServer(lambda _text: [1.0, 0.0]) as server:
            environment = embedding_environment(
                MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
                MINI_CLAUDE_EMBEDDING_BASE_URL=server.base_url,
                MINI_CLAUDE_EMBEDDING_MODEL="cache-model",
            )
            with patch.dict(os.environ, environment, clear=True):
                first = self.call(
                    action="search", query="symbol", mode="semantic"
                )
                first_request_count = len(server.requests)
                second = self.call(
                    action="search", query="symbol", mode="semantic"
                )
                second_requests = server.requests[first_request_count:]

                (self.repo / "changed.py").write_text(
                    "def changed_symbol():\n    return 2\n", encoding="utf-8"
                )
                before_refresh = len(server.requests)
                refreshed = self.call(
                    action="search", query="symbol", mode="semantic"
                )
                refresh_requests = server.requests[before_refresh:]

        self.assertEqual(first["data"]["embedding"]["updated_nodes"], 4)
        self.assertEqual(second["data"]["embedding"]["updated_nodes"], 0)
        self.assertEqual(second["data"]["embedding"]["cache_hits"], 4)
        self.assertEqual(len(second_requests), 1)
        self.assertEqual(second_requests[0]["payload"]["input"], ["symbol"])
        self.assertEqual(refreshed["data"]["embedding"]["updated_nodes"], 2)
        self.assertEqual(refreshed["data"]["embedding"]["cache_hits"], 2)
        self.assertEqual(len(refresh_requests), 2)
        self.assertEqual(len(refresh_requests[0]["payload"]["input"]), 2)
        self.assertEqual(refresh_requests[1]["payload"]["input"], ["symbol"])

    def test_hybrid_search_merges_fts_and_semantic_candidates_with_context_boost(self):
        legacy = self.repo / "legacy"
        legacy.mkdir()
        (legacy / "payment.py").write_text(
            "def lookup_intent():\n    return 1\n", encoding="utf-8"
        )
        (self.repo / "billing.py").write_text(
            "def process_invoice():\n    return 1\n", encoding="utf-8"
        )
        self.commit_all()

        def vector_for(text):
            if text == "payment intent" or "process_invoice" in text:
                return [1.0, 0.0]
            return [0.0, 1.0]

        with FakeEmbeddingServer(vector_for) as server:
            environment = embedding_environment(
                MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
                MINI_CLAUDE_EMBEDDING_BASE_URL=server.base_url,
                MINI_CLAUDE_EMBEDDING_MODEL="hybrid-model",
            )
            with patch.dict(os.environ, environment, clear=True):
                merged = self.call(
                    action="search", query="payment intent", mode="hybrid",
                    kind="function", max_results=2,
                )
                boosted = self.call(
                    action="search", query="payment intent", mode="hybrid",
                    kind="function", context_files=["billing.py"], max_results=2,
                )

        self.assertTrue(merged["ok"], merged)
        self.assertEqual(merged["data"]["requested_mode"], "hybrid")
        self.assertEqual(merged["data"]["search_mode"], "hybrid")
        self.assertEqual(
            merged["data"]["results"][0]["qualified_name"],
            "legacy/payment.py::lookup_intent",
        )
        self.assertEqual(
            boosted["data"]["results"][0]["qualified_name"],
            "billing.py::process_invoice",
        )
        self.assertTrue(
            all(isinstance(item["score"], float) for item in boosted["data"]["results"])
        )

    def test_semantic_fails_strictly_and_hybrid_falls_back_on_provider_error(self):
        (self.repo / "orders.py").write_text(
            "def process_order():\n    return 1\n", encoding="utf-8"
        )
        self.commit_all()

        with FakeEmbeddingServer(
            lambda _text: [1.0],
            responder=lambda _payload, _number: (400, {"error": "bad request"}),
        ) as server:
            environment = embedding_environment(
                MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
                MINI_CLAUDE_EMBEDDING_BASE_URL=server.base_url,
                MINI_CLAUDE_EMBEDDING_MODEL="failing-model",
            )
            with patch.dict(os.environ, environment, clear=True):
                semantic = self.call(
                    action="search", query="process_order", mode="semantic"
                )
                hybrid = self.call(
                    action="search", query="process_order", mode="hybrid"
                )

        self.assertFalse(semantic["ok"], semantic)
        self.assertEqual(semantic["error"]["code"], "embedding_error")
        self.assertTrue(hybrid["ok"], hybrid)
        self.assertEqual(hybrid["data"]["requested_mode"], "hybrid")
        self.assertIn(hybrid["data"]["search_mode"], {"fts", "keyword"})
        self.assertEqual(
            hybrid["data"]["warnings"],
            [{
                "code": "embedding_error",
                "message": "Embedding provider returned HTTP 400",
                "fallback": "fts",
            }],
        )
        self.assertEqual(
            hybrid["data"]["results"][0]["qualified_name"],
            "orders.py::process_order",
        )
        self.assertTrue(
            all(request["authorization"] is None for request in server.requests)
        )

    def test_missing_provider_config_is_strict_for_semantic_and_warns_for_hybrid(self):
        (self.repo / "orders.py").write_text(
            "def process_order():\n    return 1\n", encoding="utf-8"
        )
        self.commit_all()
        with patch.dict(
            os.environ,
            embedding_environment(MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1"),
            clear=True,
        ):
            semantic = self.call(
                action="search", query="process_order", mode="semantic"
            )
            hybrid = self.call(
                action="search", query="process_order", mode="hybrid"
            )

        self.assertEqual(semantic["error"]["code"], "provider_unavailable")
        self.assertTrue(hybrid["ok"], hybrid)
        self.assertEqual(hybrid["data"]["requested_mode"], "hybrid")
        self.assertEqual(
            hybrid["data"]["warnings"][0]["code"], "provider_unavailable"
        )

    def test_invalid_embedding_endpoint_returns_embedding_error(self):
        (self.repo / "app.py").write_text(
            "def searchable():\n    return 1\n", encoding="utf-8"
        )
        self.commit_all()
        environment = embedding_environment(
            MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
            MINI_CLAUDE_EMBEDDING_BASE_URL="not-a-url",
            MINI_CLAUDE_EMBEDDING_MODEL="test-model",
        )
        with patch.dict(os.environ, environment, clear=True):
            result = self.call(
                action="search", query="searchable", mode="semantic"
            )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "embedding_error")

    def test_embedding_transport_failures_are_mapped_and_hybrid_falls_back(self):
        (self.repo / "app.py").write_text(
            "def searchable():\n    return 1\n", encoding="utf-8"
        )
        self.commit_all()
        environment = embedding_environment(
            MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
            MINI_CLAUDE_EMBEDDING_BASE_URL="http://127.0.0.1:9/v1",
            MINI_CLAUDE_EMBEDDING_MODEL="transport-model",
        )
        failures = (
            TimeoutError("timed out"),
            http.client.IncompleteRead(b"{}", 10),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch.dict(os.environ, environment, clear=True):
                    with patch("urllib.request.urlopen", side_effect=failure):
                        semantic = self.call(
                            action="search", query="searchable", mode="semantic"
                        )
                        hybrid = self.call(
                            action="search", query="searchable", mode="hybrid"
                        )
                self.assertEqual(semantic["error"]["code"], "embedding_error")
                self.assertTrue(hybrid["ok"], hybrid)
                self.assertEqual(
                    hybrid["data"]["warnings"][0]["code"], "embedding_error"
                )

    def test_embedding_retries_transient_errors_and_rejects_invalid_responses(self):
        (self.repo / "search.py").write_text(
            "def searchable():\n    return 1\n", encoding="utf-8"
        )
        self.commit_all()

        def retry_responder(payload, number):
            if number == 1:
                return 429, {"error": "rate limited"}
            if number == 2:
                return 500, {"error": "temporary"}
            return 200, {
                "data": [
                    {"index": index, "embedding": [1.0, 0.0]}
                    for index, _text in enumerate(payload["input"])
                ]
            }

        with FakeEmbeddingServer(lambda _text: [1.0], retry_responder) as server:
            environment = embedding_environment(
                MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
                MINI_CLAUDE_EMBEDDING_BASE_URL=server.base_url,
                MINI_CLAUDE_EMBEDDING_MODEL="retry-model",
            )
            with patch.dict(os.environ, environment, clear=True):
                retried = self.call(
                    action="search", query="searchable", mode="semantic"
                )
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(len(server.requests), 4)

        invalid_responders = {
            "json": lambda _payload, _number: (200, b"not-json"),
            "index_order": lambda payload, _number: (
                200,
                {"data": [
                    {
                        "index": len(payload["input"]) - index - 1,
                        "embedding": [1.0, 0.0],
                    }
                    for index, _text in enumerate(payload["input"])
                ]},
            ),
            "dimension": lambda payload, _number: (
                200,
                {"data": [
                    {
                        "index": index,
                        "embedding": [1.0] if index == 0 else [1.0, 0.0],
                    }
                    for index, _text in enumerate(payload["input"])
                ]},
            ),
            "non_numeric": lambda payload, _number: (
                200,
                {"data": [
                    {"index": index, "embedding": ["invalid"]}
                    for index, _text in enumerate(payload["input"])
                ]},
            ),
            "float32_overflow": lambda payload, _number: (
                200,
                {"data": [
                    {"index": index, "embedding": [1e39]}
                    for index, _text in enumerate(payload["input"])
                ]},
            ),
        }
        for name, responder in invalid_responders.items():
            with self.subTest(response=name):
                with FakeEmbeddingServer(lambda _text: [1.0], responder) as server:
                    environment = embedding_environment(
                        MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
                        MINI_CLAUDE_EMBEDDING_BASE_URL=server.base_url,
                        MINI_CLAUDE_EMBEDDING_MODEL=f"invalid-{name}",
                    )
                    with patch.dict(os.environ, environment, clear=True):
                        result = self.call(
                            action="search", query="searchable", mode="semantic"
                        )
                self.assertFalse(result["ok"], result)
                self.assertEqual(
                    result["error"]["code"], "embedding_response_invalid"
                )

    def test_embedding_caches_are_provider_scoped_and_drop_deleted_symbols(self):
        (self.repo / "keep.py").write_text(
            "def keep_symbol():\n    return 1\n", encoding="utf-8"
        )
        (self.repo / "delete.py").write_text(
            "def delete_symbol():\n    return 1\n", encoding="utf-8"
        )
        self.commit_all()

        with FakeEmbeddingServer(lambda _text: [1.0, 0.0]) as first_server:
            base_environment = embedding_environment(
                MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
                MINI_CLAUDE_EMBEDDING_BASE_URL=first_server.base_url,
            )
            with patch.dict(
                os.environ,
                {**base_environment, "MINI_CLAUDE_EMBEDDING_MODEL": "model-a"},
                clear=True,
            ):
                first = self.call(
                    action="search", query="symbol", mode="semantic"
                )
            with patch.dict(
                os.environ,
                {**base_environment, "MINI_CLAUDE_EMBEDDING_MODEL": "model-b"},
                clear=True,
            ):
                changed_model = self.call(
                    action="search", query="symbol", mode="semantic"
                )
            with patch.dict(
                os.environ,
                {**base_environment, "MINI_CLAUDE_EMBEDDING_MODEL": "model-a"},
                clear=True,
            ):
                reused = self.call(
                    action="search", query="symbol", mode="semantic"
                )

            with FakeEmbeddingServer(lambda _text: [1.0, 0.0]) as second_server:
                second_environment = embedding_environment(
                    MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
                    MINI_CLAUDE_EMBEDDING_BASE_URL=second_server.base_url,
                    MINI_CLAUDE_EMBEDDING_MODEL="model-a",
                )
                with patch.dict(os.environ, second_environment, clear=True):
                    changed_endpoint = self.call(
                        action="search", query="symbol", mode="semantic"
                    )

            (self.repo / "delete.py").unlink()
            with patch.dict(
                os.environ,
                {**base_environment, "MINI_CLAUDE_EMBEDDING_MODEL": "model-a"},
                clear=True,
            ):
                after_delete = self.call(
                    action="search", query="symbol", mode="semantic"
                )
            overview = self.call(action="overview")

        self.assertEqual(first["data"]["embedding"]["updated_nodes"], 4)
        self.assertEqual(changed_model["data"]["embedding"]["updated_nodes"], 4)
        self.assertEqual(reused["data"]["embedding"]["updated_nodes"], 0)
        self.assertEqual(changed_endpoint["data"]["embedding"]["updated_nodes"], 4)
        self.assertNotIn(
            "delete.py::delete_symbol",
            [item["qualified_name"] for item in after_delete["data"]["results"]],
        )
        embedding_sets = overview["data"]["search_index"]["embedding_sets"]
        self.assertEqual(len(embedding_sets), 3)
        self.assertEqual([item["nodes"] for item in embedding_sets], [2, 2, 2])
        self.assertTrue(
            all("base_url" not in item and "api_key" not in item for item in embedding_sets)
        )

    def test_embedding_batches_commit_progress_and_resume_after_failure(self):
        source = "\n".join(
            f"def symbol_{index}():\n    return {index}\n" for index in range(70)
        )
        (self.repo / "many.py").write_text(source, encoding="utf-8")
        self.commit_all()

        def responder(payload, number):
            if number == 2:
                return 400, {"error": "second batch failed"}
            return 200, {
                "data": [
                    {"index": index, "embedding": [1.0, 0.0]}
                    for index, _text in enumerate(payload["input"])
                ]
            }

        with FakeEmbeddingServer(lambda _text: [1.0, 0.0], responder) as server:
            environment = embedding_environment(
                MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
                MINI_CLAUDE_EMBEDDING_BASE_URL=server.base_url,
                MINI_CLAUDE_EMBEDDING_MODEL="batch-model",
            )
            with patch.dict(os.environ, environment, clear=True):
                failed = self.call(
                    action="search", query="symbol", mode="semantic"
                )
                resumed = self.call(
                    action="search", query="symbol", mode="semantic"
                )

        self.assertEqual(failed["error"]["code"], "embedding_error")
        self.assertEqual(
            [len(request["payload"]["input"]) for request in server.requests],
            [64, 7, 7, 1],
        )
        self.assertEqual(resumed["data"]["embedding"]["cache_hits"], 64)
        self.assertEqual(resumed["data"]["embedding"]["updated_nodes"], 7)

    def test_non_semantic_actions_never_call_the_embedding_provider(self):
        (self.repo / "app.py").write_text(
            "def helper():\n    return 1\n\ndef run():\n    return helper()\n",
            encoding="utf-8",
        )
        self.commit_all()

        with FakeEmbeddingServer(lambda _text: [1.0]) as server:
            environment = embedding_environment(
                MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS="1",
                MINI_CLAUDE_EMBEDDING_BASE_URL=server.base_url,
                MINI_CLAUDE_EMBEDDING_MODEL="unused-model",
            )
            with patch.dict(os.environ, environment, clear=True):
                search = self.call(action="search", query="helper")
                query = self.call(
                    action="query", relation="callers_of", target="helper"
                )
                impact = self.call(action="impact", changed_files=["app.py"])
                overview = self.call(action="overview")

        self.assertTrue(all(item["ok"] for item in (search, query, impact, overview)))
        self.assertEqual(server.requests, [])
        self.assertTrue(overview["data"]["search_index"]["fts_available"])
        self.assertGreater(overview["data"]["search_index"]["fts_nodes"], 0)

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
