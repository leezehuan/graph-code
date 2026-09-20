import asyncio
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from filelock import FileLock
from langchain.agents import create_agent
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from lib.code_graph import CodeGraphService, _cache_path
from lib.knowledge_tools import create_code_graph_tool, create_skill_tools
from lib.skills import SkillStore
from lib.project_cache import project_cache
from lib.sub_agent import create_sub_agent
from middlewares.skill_loading_middleware import SkillLoadingMiddleware
from test_skill_review import ScriptedModel, call, make_store, skill_content


def test_agent_loads_and_refreshes_skills_using_actual_middleware(tmp_path):
    async def scenario():
        store = make_store(tmp_path)
        assert store.manage("create", "learned", content=skill_content())["success"]
        model = ScriptedModel(responses=[call("load_skill", {"skill_name": "learned"}), AIMessage(content="done")])
        agent = create_agent(model, tools=create_skill_tools(store),
                             middleware=[SkillLoadingMiddleware(tmp_path, store=store)])
        result = await agent.ainvoke({"messages": [HumanMessage(content="use skill")]})
        assert "Reusable technique" in str(model.seen[0][0].content)
        assert "Verified steps" in result["messages"][-2].content
        path = tmp_path / "skills/learned/SKILL.md"
        path.write_text(skill_content().replace("Reusable technique", "New description"), encoding="utf-8")
        await agent.ainvoke({"messages": [HumanMessage(content="again")]})
        assert "New description" in str(model.seen[2][0].content)
    asyncio.run(scenario())


def test_tools_enforce_card_allowlist_and_user_write_permission(tmp_path):
    async def scenario():
        store = make_store(tmp_path)
        store.manage("create", "learned", content=skill_content())
        store.manage("create", "hidden", content=skill_content("hidden"))
        limited = make_store(tmp_path, allowed_skill_names={"learned"})
        tools = {t.name: t for t in create_skill_tools(limited)}
        listed = json.loads(await tools["skills_list"].ainvoke({}))
        assert [s["name"] for s in listed["skills"]] == ["learned"]
        for name, args in [("skill_view", {"name": "hidden"}), ("load_skill", {"skill_name": "hidden"}),
                           ("skill_manage", {"name": "hidden", "action": "delete"}),
                           ("skill_manage", {"name": "new", "action": "create", "content": skill_content("new")})]:
            assert "not allowed" in await tools[name].ainvoke(args)

        user_file = store.user_skills_dir / "personal/SKILL.md"
        user_file.parent.mkdir(parents=True)
        user_file.write_text(skill_content("personal"), encoding="utf-8")
        permission = SimpleNamespace(authorize_skill_write=AsyncMock(return_value=False))
        manage = next(t for t in create_skill_tools(store, permission) if t.name == "skill_manage")
        args = {"action": "patch", "name": "personal", "old_string": "Verified", "new_string": "Updated"}
        assert not json.loads(await manage.ainvoke(args))["success"]
        permission.authorize_skill_write.return_value = True
        assert json.loads(await manage.ainvoke(args))["success"]
        assert "Updated" in user_file.read_text()
        assert permission.authorize_skill_write.call_count == 2
    asyncio.run(scenario())


def test_worker_registers_only_card_tools_and_never_reviews(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv("LANGCODE_CACHE_DIR", str(tmp_path / ".cache"))
        store = make_store(tmp_path)
        store.manage("create", "learned", content=skill_content())
        store.manage("create", "hidden", content=skill_content("hidden"))
        card = SimpleNamespace(card_tool_allowlist=["code_graph", "skills_list", "load_skill"],
                               card_skill_allowlist=["learned"], agent_card_id="test", agent_card_version="1",
                               card_system_prompt="Worker", thread_id="thread", task_id="task", execution_id="exec",
                               attempt=1, description="inspect", subject="inspect")
        model = ScriptedModel(responses=[call("skills_list", {}), AIMessage(content="done")])
        agent = await create_sub_agent(runtime_id="runtime", task=card, llm=model, checkpointer=InMemorySaver(),
                                       message_bus=SimpleNamespace(), work_dir=tmp_path)
        result = await agent.ainvoke({"messages": [HumanMessage(content="task")]}, {"configurable": {"thread_id": "worker"}})
        assert set(model.tool_names) == set(card.card_tool_allowlist)
        assert "hidden" not in str(model.seen[0][0].content)
        assert "skill_review_rounds" not in result
        listed = json.loads(result["messages"][-2].content)
        assert [s["name"] for s in listed["skills"]] == ["learned"]
        card.card_tool_allowlist = ["unknown"]
        with pytest.raises(ValueError, match="unsupported tools"):
            await create_sub_agent(runtime_id="runtime", task=card, llm=model, checkpointer=InMemorySaver(),
                                   message_bus=SimpleNamespace(), work_dir=tmp_path)
    asyncio.run(scenario())


def test_lead_factory_registers_tools_and_closes_review(tmp_path, monkeypatch):
    import cli

    async def scenario():
        monkeypatch.setattr(cli, "WORK_DIR", tmp_path)
        monkeypatch.setenv("LANGCODE_CACHE_DIR", str(tmp_path / ".cache"))
        monkeypatch.setenv("LANGCODE_SKILL_REVIEW_INTERVAL", "1")
        primary = ScriptedModel(responses=[call("code_graph", {"action": "overview"}), AIMessage(content="done")])
        light = ScriptedModel(name="background-review-model", responses=[AIMessage(content="Nothing to save")])
        models = iter([primary, light])
        monkeypatch.setattr(cli, "ChatOpenAI", lambda **kwargs: next(models))
        monkeypatch.setattr("middlewares.memory_saver.MemorySaver.extract_and_save", AsyncMock())
        agent = await cli.create_coding_agent(InMemorySaver(), InMemoryStore())
        config = {"configurable": {"thread_id": "lead"}}
        events = [event async for event in agent.astream_events(
            {"messages": [HumanMessage(content="inspect")]}, config, version="v2")]
        result = (await agent.aget_state(config)).values
        await agent.skill_review_manager.close()
        assert {"code_graph", "skills_list", "skill_view", "skill_manage", "load_skill"} <= set(primary.tool_names)
        assert result["skill_review_last_submitted"]
        assert len(light.seen) == 1
        assert all(event.get("name") != "background-review-model" for event in events)
    asyncio.run(scenario())


class RecordingEmbeddings(Embeddings):
    def __init__(self):
        self.inputs = []

    def embed_documents(self, texts):
        self.inputs.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return self.embed_documents([text])[0]


def test_explicit_workspace_injected_embeddings_and_tool_contract(tmp_path, monkeypatch):
    async def scenario():
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("def searchable():\n    return 'SECRET_BODY'\n")
        provider = RecordingEmbeddings()
        service = CodeGraphService(repo, tmp_path / ".cache", provider, "fake-v1")
        tool = create_code_graph_tool(service)
        monkeypatch.setenv("LANGCODE_ACCEPT_CLOUD_EMBEDDINGS", "1")
        before = Path.cwd()
        response = json.loads(await tool.ainvoke({"action": "search", "query": "searchable", "mode": "semantic"}))
        assert response["ok"]
        assert Path.cwd() == before
        assert all("SECRET_BODY" not in text for text in provider.inputs)
        assert any("relative path: app.py" in text for text in provider.inputs)
        cached = json.loads(await tool.ainvoke({"action": "search", "query": "searchable", "mode": "semantic"}))
        assert cached["data"]["embedding"]["updated_nodes"] == 0
        assert cached["data"]["embedding"]["cache_hits"] == 2
        schema = tool.args_schema.model_json_schema()
        assert schema["required"] == ["action"]
        assert schema["properties"]["action"]["enum"] == ["search", "query", "impact", "overview"]
    asyncio.run(scenario())


def test_asymmetric_embedding_provider_uses_query_encoder(tmp_path, monkeypatch):
    class AsymmetricEmbeddings(Embeddings):
        def embed_documents(self, texts):
            return [[0.0, 1.0] if "name: target_fn" in text else [1.0, 0.0] for text in texts]

        def embed_query(self, text):
            return [0.0, 1.0]

    (tmp_path / "app.py").write_text("def target_fn():\n    pass\ndef other_fn():\n    pass\n")
    monkeypatch.setenv("LANGCODE_ACCEPT_CLOUD_EMBEDDINGS", "1")
    service = CodeGraphService(tmp_path, tmp_path / ".cache", AsymmetricEmbeddings(), "asymmetric-v1")
    result = json.loads(service.execute_sync({"action": "search", "query": "natural question", "mode": "semantic"}))
    assert result["ok"]
    assert result["data"]["results"][0]["name"] == "target_fn"


def _hold_lock(path, ready, release):
    with FileLock(path):
        ready.set()
        release.wait(10)


def test_graph_lock_coordinates_processes_and_returns_busy(tmp_path):
    service = CodeGraphService(tmp_path, tmp_path / ".cache", lock_timeout=0.05)
    path = str(_cache_path(service.root, service.cache_dir)) + ".lock"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_hold_lock, args=(path, ready, release))
    process.start()
    try:
        assert ready.wait(10)
        result = json.loads(service.execute_sync({"action": "overview"}))
        assert result["error"]["code"] == "graph_busy"
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert json.loads(service.execute_sync({"action": "overview"}))["ok"]


def test_store_conflict_detection_yaml_and_cross_instance_refresh(tmp_path):
    foreground = make_store(tmp_path)
    background = make_store(tmp_path, origin="background_review")
    assert background.manage("create", "learned", content=skill_content())["success"]
    assert background.view("learned")["success"]
    assert foreground.manage("patch", "learned", old_string="Verified", new_string="Manual")["success"]
    conflict = background.manage("edit", "learned", content=skill_content())
    assert not conflict["success"] and "changed since" in conflict["error"]
    assert background.view("learned")["content"] == "Manual steps"
    assert background.manage("patch", "learned", old_string="Manual", new_string="Revalidated")["success"]
    content = "---\nname: multiline\ndescription: >\n  first line\n  second line\nallowed-tools:\n  - read_file\n---\nInstructions"
    assert foreground.manage("create", "multiline", content=content)["success"]
    assert background.get("multiline").description.strip() == "first line second line"
    assert background.get("multiline").allowed_tools == ["read_file"]


def _create_skill_in_process(root, ready, start, results):
    store = SkillStore(project_root=root, user_root=Path(root) / "user-skills", cache_dir=Path(root) / ".cache")
    ready.set()
    start.wait(10)
    results.put(store.manage("create", "concurrent", content=skill_content("concurrent")))


def test_skill_creation_is_serialized_across_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready = [ctx.Event(), ctx.Event()]
    start, results = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_create_skill_in_process, args=(str(tmp_path), ready[i], start, results))
                 for i in range(2)]
    try:
        for process in processes:
            process.start()
        assert all(event.wait(10) for event in ready)
        start.set()
        outcomes = [results.get(timeout=10), results.get(timeout=10)]
        assert sorted(outcome["success"] for outcome in outcomes) == [False, True]
        assert make_store(tmp_path).view("concurrent")["content"] == "Verified steps"
    finally:
        start.set()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
        results.close()


def test_cancelled_review_cannot_write_after_waiting_for_lock(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    cancelled = threading.Event()
    store = make_store(tmp_path, origin="background_review", cancel_event=cancelled)
    lock_path = project_cache(store.project_skills_dir, store.cache_dir) / "skills.lock"
    with ThreadPoolExecutor() as executor:
        with FileLock(lock_path):
            future = executor.submit(store.manage, "create", "learned", content=skill_content())
            cancelled.set()
            outcome = future.result(timeout=2)
    assert not outcome["success"]
    assert not (tmp_path / "skills/learned").exists()


@pytest.mark.parametrize("metadata", ["true", "1", "{bad: value}", "[read_file, 1]"])
def test_invalid_skill_metadata_does_not_break_agent(tmp_path, metadata):
    async def scenario():
        store = make_store(tmp_path)
        store.manage("create", "learned", content=skill_content())
        invalid = tmp_path / "skills/broken/SKILL.md"
        invalid.parent.mkdir(parents=True)
        invalid.write_text(f"---\nname: broken\ndescription: invalid\nallowed_tools: {metadata}\n---\nbody")
        model = ScriptedModel(responses=[AIMessage(content="done")])
        agent = create_agent(model, middleware=[SkillLoadingMiddleware(tmp_path, store=store)])
        result = await agent.ainvoke({"messages": [HumanMessage(content="task")]})
        assert result["messages"][-1].content == "done"
        assert "learned" in str(model.seen[0][0].content)
        assert "broken" not in str(model.seen[0][0].content)
    asyncio.run(scenario())


def test_missing_allowed_skill_does_not_disclose_hidden_names(tmp_path):
    store = make_store(tmp_path)
    store.manage("create", "secret-skill", content=skill_content("secret-skill"))
    restricted = make_store(tmp_path, allowed_skill_names={"missing"})
    result = restricted.view("missing")
    assert not result["success"]
    assert "secret-skill" not in result["error"]


def test_background_delete_requires_current_reads_of_every_attachment(tmp_path):
    store = make_store(tmp_path, origin="background_review")
    foreground = make_store(tmp_path)
    store.manage("create", "learned", content=skill_content())
    store.manage("write_file", "learned", file_path="references/check.md", file_content="old")
    store.view("learned")
    result = store.manage("delete", "learned")
    assert not result["success"] and "skill_view" in result["error"]
    store.view("learned", "references/check.md")
    foreground.manage("write_file", "learned", file_path="references/check.md", file_content="new")
    result = store.manage("delete", "learned")
    assert not result["success"] and "changed since" in result["error"]
    store.view("learned", "references/check.md")
    assert store.manage("delete", "learned")["success"]
