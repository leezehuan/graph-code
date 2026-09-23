import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import tiktoken
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from lib.memories import MemoryRepository
from lib.memory_tools import create_memory_tools
from middlewares.memory_management_middleware import MemoryManagementMiddleware
from middlewares.memory_saver import MemorySaver
from middlewares.skill_loading_middleware import SkillLoadingMiddleware
from test_skill_review import ScriptedModel, call, make_store, skill_content


def config(user="alice", **extra):
    return {"configurable": {"thread_id": "test", "user_id": user, **extra}}


async def seed(repo, kind="project", content="PostgreSQL is the source of truth", user="alice"):
    return await repo.save(kind, "user" if kind == "semantic" else "project",
                           kind + " description", content, "User confirmed", user)


def test_scopes_sharing_updates_and_legacy(tmp_path):
    async def scenario():
        store = InMemoryStore()
        repo = MemoryRepository(store, tmp_path)
        other = MemoryRepository(store, tmp_path / "other")
        records = [await seed(repo, kind) for kind in ("semantic", "procedural", "episodic", "project")]
        await store.aput(("user_id", "memories"), "old", {"content": "LEGACY"})
        await store.aput(repo.namespace("project"), "legacy-project",
                         {"id": "legacy-project", "scope": "project", "type": "project",
                          "description": "old", "content": "OLD DATABASE DATA"})
        assert len(await repo.list("alice")) == 4
        assert len(await repo.list("bob")) == 3
        assert len(await repo.list()) == 3
        assert len(await other.list("alice")) == 1
        assert await store.aget(repo.namespace("project"), "legacy-project")
        assert all(r["id"] != "legacy-project" for r in await repo.list("alice"))
        assert not await store.aget(repo.namespace("project"), records[-1]["id"])
        for scope, id, user in [("user", records[0]["id"], "bob"), ("user", records[0]["id"], None)]:
            with pytest.raises(ValueError):
                await repo.get(scope, id, user)
        with pytest.raises(ValueError):
            await other.get("project", records[-1]["id"], "alice")
        old = records[-1]
        updated = await repo.save("project", "project", "Updated fact", "New decision", "User correction", "bob", old["id"])
        assert updated["id"] == old["id"] and updated["created_at"] == old["created_at"]
        assert len(await repo.list("alice")) == 4
        with pytest.raises(ValueError):
            await repo.save("semantic", "project", "bad", "bad", "bad")
        with pytest.raises(ValueError):
            await repo.list(kind="unknown")
    asyncio.run(scenario())


def test_extraction_four_types_update_and_invalid_targets(tmp_path):
    async def scenario():
        repo = MemoryRepository(InMemoryStore(), tmp_path)
        data = {kind: [{"description": kind, "content": "Verified " + kind, "source": "User confirmed"}]
                for kind in ("semantic", "procedural", "episodic", "project")}
        model = ScriptedModel(responses=[AIMessage(content=json.dumps(data))])
        saver = MemorySaver(model, repo)
        await saver.extract_and_save([HumanMessage(content="confirmed")], "alice")
        records = await repo.list("alice")
        assert len(records) == 4
        record = next(r for r in records if r["type"] == "project")
        model.responses = [AIMessage(content=json.dumps({"project": [dict(record, content="Corrected")]}))]
        await saver.extract_and_save([HumanMessage(content="correction")], "alice")
        assert (await repo.get("project", record["id"]))["content"] == "Corrected"
        assert len(await repo.list("alice")) == 4
        model.responses = [AIMessage(content=json.dumps({"project": [dict(record, id="outside", content="BAD"),
                                                                      {"description": []}, None]}))]
        await saver.extract_and_save([], "alice")
        assert len(await repo.list("alice")) == 4
        for invalid in ('[]', '{"unknown": []}', 'not json', '{"project": "bad"}'):
            model.responses = [AIMessage(content=invalid)]
            await saver.extract_and_save([], "alice")
        assert len(await repo.list("alice")) == 4
        model.responses = [AIMessage(content=json.dumps(data))]
        await saver.extract_and_save([], None)
        assert len(await repo.list("alice")) == 4
    asyncio.run(scenario())


def test_recall_real_graph_tool_loop_skills_budget_and_stream(tmp_path):
    async def scenario():
        repo = MemoryRepository(InMemoryStore(), tmp_path)
        await seed(repo, content="PostgreSQL " * 1000)
        light = ScriptedModel(name="memory-internal", responses=[AIMessage(content='["1", "1", "999", {}]'),
                                                                  AIMessage(content='["1"]'), AIMessage(content='{}')])
        primary = ScriptedModel(responses=[call("ping", {}), AIMessage(content="done")])
        @tool
        def ping() -> str:
            """Return pong."""
            return "pong"
        skills = make_store(tmp_path)
        skills.manage("create", "learned", content=skill_content())
        middleware = MemoryManagementMiddleware(light, repo.store, repository=repo, token_budget=200)
        agent = create_agent(primary, tools=[ping], system_prompt="BASE",
                             middleware=[middleware, SkillLoadingMiddleware(tmp_path, store=skills)])
        events = [e async for e in agent.astream_events({"messages": [HumanMessage(content="Inspect database")]},
                                                        config(), version="v2")]
        assert len(primary.seen) == 2
        for request in primary.seen:
            system = request[0]
            assert "BASE" in str(system.content) and "Reusable technique" in str(system.content)
            block = next(b["text"] for b in system.content_blocks if "Available memories" in b.get("text", ""))
            assert len(tiktoken.get_encoding("cl100k_base").encode(block)) <= 200
        assert '"task": "Inspect database"' in light.seen[1][0].text
        assert not any(e.get("name") == "memory-internal" and e.get("event") == "on_chat_model_stream"
                       and "internal_memory_call" not in e.get("tags", []) for e in events)
    asyncio.run(scenario())


@pytest.mark.parametrize("disabled", ["keyword", "config"])
def test_disabled_across_tools_and_next_turn(tmp_path, disabled):
    async def scenario():
        store = InMemoryStore()
        light = ScriptedModel(responses=[AIMessage(content='{}')])
        primary = ScriptedModel(responses=[call("memory_list", {}), AIMessage(content="done"), AIMessage(content="next")])
        middleware = MemoryManagementMiddleware(light, store, tmp_path)
        agent = create_agent(primary, tools=create_memory_tools(middleware.repository),
                             middleware=[middleware], checkpointer=InMemorySaver())
        cfg = config(memory_enabled=False) if disabled == "config" else config()
        await agent.ainvoke({"messages": [HumanMessage(content="禁用记忆" if disabled == "keyword" else "hello")]}, cfg)
        assert not light.seen
        await agent.ainvoke({"messages": [HumanMessage(content="normal task")]}, config())
        assert len(light.seen) == 1
    asyncio.run(scenario())


def test_management_parallel_writes_skip_save_reset_and_pagination(tmp_path):
    async def scenario():
        repo = MemoryRepository(InMemoryStore(), tmp_path)
        first, second = await seed(repo), await seed(repo, "episodic")
        request = call("memory_delete", {"scope": "project", "id": first["id"]}, "delete")
        request.tool_calls.append({"name": "memory_update", "args": {"scope": "project", "id": second["id"],
                                   "description": "Updated", "content": "Corrected"}, "id": "update", "type": "tool_call"})
        model = ScriptedModel(responses=[request, AIMessage(content="done"), AIMessage(content="next")])
        light = ScriptedModel(responses=[AIMessage(content='[]')])
        middleware = MemoryManagementMiddleware(light, repo.store, repository=repo)
        middleware.memory_saver.extract_and_save = AsyncMock()
        tools = create_memory_tools(repo)
        agent = create_agent(model, tools=tools, middleware=[middleware], checkpointer=InMemorySaver())
        state = await agent.ainvoke({"messages": [HumanMessage(content="Delete and correct these records")]}, config())
        assert state["memory_management_written"] is True
        middleware.memory_saver.extract_and_save.assert_not_awaited()
        with pytest.raises(ValueError):
            await repo.get("project", first["id"])
        assert (await repo.get("project", second["id"]))["content"] == "Corrected"
        assert all(json.loads(m.content)["success"] for m in state["messages"] if m.type == "tool")
        await agent.ainvoke({"messages": [HumanMessage(content="new task")]}, config())
        middleware.memory_saver.extract_and_save.assert_awaited_once()
        listed = json.loads(await tools[0].ainvoke({"limit": 1, "offset": 1}, config()))
        assert listed["success"] and listed["total"] == 1 and listed["memories"] == []
        assert not json.loads(await tools[0].ainvoke({"limit": 0}, config()))["success"]
        personal = await seed(repo, "semantic")
        assert not json.loads(await tools[1].ainvoke({"scope": "user", "id": personal["id"]}, config("bob")))["success"]
    asyncio.run(scenario())


@pytest.mark.parametrize("reply", [AIMessage(content=""), AIMessage(content="partial", response_metadata={"finish_reason": "length"}),
                                  AIMessage(content="blocked", response_metadata={"finish_reason": "content_filter"})])
def test_incomplete_replies_do_not_save(tmp_path, reply):
    async def scenario():
        light = ScriptedModel(responses=[AIMessage(content='{}')])
        middleware = MemoryManagementMiddleware(light, InMemoryStore(), tmp_path)
        agent = create_agent(ScriptedModel(responses=[reply]), middleware=[middleware])
        await agent.ainvoke({"messages": [HumanMessage(content="task")]}, config())
        assert not light.seen
    asyncio.run(scenario())


def test_store_failures_do_not_fail_agent(tmp_path):
    async def scenario():
        class FailingStore(InMemoryStore):
            async def asearch(self, *args, **kwargs):
                raise RuntimeError("offline")
        store = FailingStore()
        middleware = MemoryManagementMiddleware(ScriptedModel(responses=[]), store, tmp_path)
        agent = create_agent(ScriptedModel(responses=[AIMessage(content="done")]), middleware=[middleware])
        result = await agent.ainvoke({"messages": [HumanMessage(content="task")]}, config())
        assert result["messages"][-1].content == "done"
    asyncio.run(scenario())


def test_candidates_latest_hundred_and_concurrent_identity(tmp_path):
    async def scenario():
        repo = MemoryRepository(InMemoryStore(), tmp_path)
        first = await seed(repo)
        for i in range(101):
            await repo.save("project", "project", str(i), "fact", "confirmed")
        await repo.save("project", "project", "latest", "newest", "corrected", memory_id=first["id"])
        candidates = await repo.candidates()
        assert len(candidates) == 100
        assert candidates["1"]["id"] == first["id"]
        await seed(repo, "semantic", "ALICE_ONLY", "alice")
        await seed(repo, "semantic", "BOB_ONLY", "bob")
        light = ScriptedModel(responses=[AIMessage(content='["1"]')])
        primary = ScriptedModel(responses=[AIMessage(content="done")])
        middleware = MemoryManagementMiddleware(light, repo.store, repository=repo)
        middleware.memory_saver.extract_and_save = AsyncMock()
        agent = create_agent(primary, middleware=[middleware])
        await asyncio.gather(*[
            agent.ainvoke({"messages": [HumanMessage(content=user)]}, config(user)) for user in ("alice", "bob")])
        for messages in primary.seen:
            user = messages[-1].text
            system = messages[0].text
            assert ("ALICE_ONLY" in system) == (user == "alice")
            assert ("BOB_ONLY" in system) == (user == "bob")
        assert {c.args[1] for c in middleware.memory_saver.extract_and_save.await_args_list} == {"alice", "bob"}
    asyncio.run(scenario())


def test_model_and_write_failures_do_not_fail_agent(tmp_path):
    async def scenario():
        class FailingStore(InMemoryStore):
            async def aput(self, *args, **kwargs):
                raise RuntimeError("write failed")
        data = {"semantic": [{"description": "fact", "content": "fact", "source": "user"}]}
        middleware = MemoryManagementMiddleware(ScriptedModel(responses=[AIMessage(content=json.dumps(data))]),
                                                FailingStore(), tmp_path)
        agent = create_agent(ScriptedModel(responses=[AIMessage(content="done")]), middleware=[middleware])
        result = await agent.ainvoke({"messages": [HumanMessage(content="task")]}, config())
        assert result["messages"][-1].text == "done"
        middleware = MemoryManagementMiddleware(ScriptedModel(responses=[]), InMemoryStore(), tmp_path)
        agent = create_agent(ScriptedModel(responses=[AIMessage(content="done")]), middleware=[middleware])
        result = await agent.ainvoke({"messages": [HumanMessage(content="task")]}, config())
        assert result["messages"][-1].text == "done"
        repo = MemoryRepository(InMemoryStore(), tmp_path)
        await seed(repo)
        middleware = MemoryManagementMiddleware(ScriptedModel(responses=[AIMessage(content="invalid")]),
                                                repo.store, repository=repo)
        primary = ScriptedModel(responses=[AIMessage(content="done")])
        agent = create_agent(primary, middleware=[middleware])
        await agent.ainvoke({"messages": [HumanMessage(content="task")]}, config())
        assert primary.seen[0][0].type == "human"
    asyncio.run(scenario())


def test_project_files_routes_types_atomic_update_delete_and_bad_data(tmp_path):
    async def scenario():
        store = InMemoryStore()
        repo = MemoryRepository(store, tmp_path)
        project = await repo.save("project", "project", "architecture", "Postgres", "verified")
        episode = await repo.save("episodic", "project", "incident", "Pool exhaustion", "verified")
        project_rule = await repo.save("procedural", "project", "rule", "Run tests", "user")
        personal = await repo.save("procedural", "user", "personal rule", "Chinese", "user", "alice")
        semantic = await repo.save("semantic", "user", "preference", "Python", "user", "alice")
        assert sorted(p.name for p in (tmp_path / ".langcode/memories").glob("*.json")) == sorted(
            [project["id"] + ".json", episode["id"] + ".json", project_rule["id"] + ".json"])
        assert not await store.aget(repo.namespace("project"), project["id"])
        assert await store.aget(repo.namespace("user", "alice"), personal["id"])
        assert await store.aget(repo.namespace("user", "alice"), semantic["id"])
        updated = await repo.save("project", "project", "architecture updated", "SQLite", "correction",
                                  memory_id=project["id"])
        assert updated["id"] == project["id"] and updated["created_at"] == project["created_at"]
        assert (await repo.get("project", project["id"]))["content"] == "SQLite"
        await repo.delete("project", episode["id"])
        with pytest.raises(ValueError):
            await repo.get("project", episode["id"])
        path = tmp_path / ".langcode/memories/broken.json"
        path.write_text("{broken", encoding="utf-8")
        listed = await repo.list()
        assert {r["id"] for r in listed} == {project["id"], project_rule["id"]}
        for invalid in ("../outside", "not-a-uuid"):
            with pytest.raises(ValueError):
                await repo.get("project", invalid)
        assert not (tmp_path / "outside.json").exists()
        assert not list((tmp_path / ".langcode/memories").glob("*.tmp"))
    asyncio.run(scenario())


def test_project_memory_writes_are_serialized_and_symlink_escape_is_rejected(tmp_path):
    async def scenario():
        root = tmp_path / "workspace"
        root.mkdir()
        repo = MemoryRepository(InMemoryStore(), root)
        records = await asyncio.gather(*[
            repo.save("project", "project", f"fact-{i}", f"value-{i}", "verified")
            for i in range(12)])
        assert len(await repo.list()) == 12
        assert len({r["id"] for r in records}) == 12
        outside = tmp_path / "outside"
        outside.mkdir()
        escaped = MemoryRepository(InMemoryStore(), root)
        escaped.project_memory_dir = outside
        with pytest.raises(ValueError, match="escapes project root"):
            await escaped.save("project", "project", "bad", "bad", "bad")
        assert not list(outside.iterdir())
    asyncio.run(scenario())
