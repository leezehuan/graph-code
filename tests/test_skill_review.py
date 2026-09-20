import asyncio
import copy
import json
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from lib.skill_review import SkillReviewManager, create_skill_review_graph
from lib.skills import SkillStore
from middlewares.skill_review_middleware import SkillReviewMiddleware


class ScriptedModel(FakeMessagesListChatModel):
    seen: list = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.tool_names = [t.name for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(copy.deepcopy(messages))
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        result = copy.deepcopy(result)
        result.generations[0].message.id = str(uuid4())
        return result


def call(name, args, identifier="call-1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier}])


def skill_content(name="learned", body="Verified steps"):
    return f"---\nname: {name}\ndescription: Reusable technique\n---\n\n{body}"


def make_store(tmp_path, **kwargs):
    return SkillStore(project_root=tmp_path, user_root=tmp_path / "user-skills",
                      cache_dir=tmp_path / ".cache", **kwargs)


def test_review_graph_writes_protected_skill_and_does_not_replay_history(tmp_path):
    async def scenario():
        store = make_store(tmp_path, origin="background_review")
        model = ScriptedModel(responses=[
            call("skill_manage", {"action": "create", "name": "learned", "content": skill_content()}),
            AIMessage(content="Saved"),
        ])
        history = [SystemMessage(content="OLD SYSTEM"), HumanMessage(content="A useful correction"),
                   call("bash", {"command": "do-not-execute"}), AIMessage(content="Verified")]
        original = copy.deepcopy(history)
        result = await create_skill_review_graph(model, store).ainvoke({"snapshot": history})
        assert result["turns"] == 2
        assert "created" in result["summary"]
        assert store.view("learned")["frontmatter"]["created_by"] == "agent"
        assert history == original
        assert set(model.tool_names) == {"skills_list", "skill_view", "skill_manage"}
        assert "OLD SYSTEM" not in str(model.seen[0])
        assert not any(getattr(m, "tool_calls", []) for m in model.seen[0])
    asyncio.run(scenario())


def test_review_read_before_write_and_noop(tmp_path):
    async def scenario():
        store = make_store(tmp_path, origin="background_review")
        assert store.manage("create", "learned", content=skill_content())["success"]
        model = ScriptedModel(responses=[
            call("skill_manage", {"action": "patch", "name": "learned", "old_string": "Verified", "new_string": "New"}),
            call("skill_view", {"name": "learned"}, "read"),
            call("skill_manage", {"action": "patch", "name": "learned", "old_string": "Verified", "new_string": "New"}, "patch"),
            AIMessage(content="done"),
        ])
        result = await create_skill_review_graph(model, store).ainvoke({"snapshot": []})
        assert len(result["actions"]) == 1
        assert "skill_view" in model.seen[1][-1].content
        assert store.view("learned")["content"] == "New steps"
        noop = ScriptedModel(responses=[AIMessage(content="Nothing to save")])
        result = await create_skill_review_graph(noop, store).ainvoke({"snapshot": []})
        assert result["summary"] == "Nothing to save"
    asyncio.run(scenario())


def test_review_rejects_unknown_tools_and_stops_at_sixteen_models(tmp_path):
    async def scenario():
        model = ScriptedModel(responses=[call("bash", {"command": "bad"})])
        result = await create_skill_review_graph(model, make_store(tmp_path, origin="background_review")).ainvoke(
            {"snapshot": []}, config={"recursion_limit": 40})
        assert result["turns"] == 16
        assert not result["actions"]
        assert "not available" in result["messages"][-1].content
    asyncio.run(scenario())


def test_trigger_counts_rounds_persists_and_isolates_threads(tmp_path):
    async def scenario():
        review_model = ScriptedModel(responses=[AIMessage(content="Nothing to save")])
        manager = SkillReviewManager(review_model, make_store(tmp_path))
        saver = InMemorySaver()

        @tool
        def ping() -> str:
            """Return pong."""
            return "pong"

        def build():
            # Two tool calls in one response still count as only one round.
            response = call("ping", {}, "one")
            response.tool_calls.append({"name": "ping", "args": {}, "id": "two", "type": "tool_call"})
            return create_agent(ScriptedModel(responses=[response, AIMessage(content="done")]), tools=[ping],
                                middleware=[SkillReviewMiddleware(manager, interval=2)], checkpointer=saver)

        result = await build().ainvoke({"messages": [HumanMessage(content="first")]}, {"configurable": {"thread_id": "a"}})
        assert result["skill_review_rounds"] == 1
        assert not manager._submitted
        other = await build().ainvoke({"messages": [HumanMessage(content="other")]}, {"configurable": {"thread_id": "b"}})
        assert other["skill_review_rounds"] == 1
        result = await build().ainvoke({"messages": [HumanMessage(content="second")]}, {"configurable": {"thread_id": "a"}})
        assert result["skill_review_rounds"] == 0
        assert result["skill_review_last_submitted"]
        assert set(manager._submitted) == {"a"}
        await manager.close()
        assert len(review_model.seen) == 1
        assert all("Nothing to save" not in str(m.content) for m in result["messages"])
    asyncio.run(scenario())


@pytest.mark.parametrize("interval,final", [(0, AIMessage(content="done")), (1, AIMessage(content="partial", response_metadata={"finish_reason": "length"}))])
def test_disabled_or_truncated_does_not_submit(tmp_path, interval, final):
    async def scenario():
        manager = SkillReviewManager(None, make_store(tmp_path))
        agent = create_agent(ScriptedModel(responses=[final]),
                             middleware=[SkillReviewMiddleware(manager, interval=interval)], checkpointer=InMemorySaver())
        await agent.ainvoke({"messages": [HumanMessage(content="task")], "skill_review_rounds": 10},
                            {"configurable": {"thread_id": "a"}})
        assert not manager._submitted
    asyncio.run(scenario())


def test_background_queue_deduplicates_copies_and_isolates_failure(tmp_path, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        snapshots = []

        class Graph:
            async def ainvoke(self, state, config):
                snapshots.append(state["snapshot"])
                started.set()
                await release.wait()
                if len(snapshots) == 1:
                    raise RuntimeError("isolated failure")
                return {"summary": "Nothing to save"}

        monkeypatch.setattr("lib.skill_review.create_skill_review_graph", lambda *args: Graph())
        manager = SkillReviewManager(None, make_store(tmp_path))
        snapshot = [HumanMessage(content="original")]
        assert manager.submit("a", "1", snapshot)
        assert not manager.submit("a", "1", snapshot)
        snapshot[0].content = "mutated"
        await asyncio.wait_for(started.wait(), 2)
        assert snapshots[0][0].content == "original"
        assert manager.submit("a", "2", snapshot)
        assert not manager.submit("a", "1", snapshot)
        release.set()
        await manager.close()
        assert len(snapshots) == 2
        assert not manager.submit("a", "3", snapshot)
    asyncio.run(scenario())


def test_background_shutdown_cancels_pending_work(tmp_path, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class Graph:
            async def ainvoke(self, state, config):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        monkeypatch.setattr("lib.skill_review.create_skill_review_graph", lambda *args: Graph())
        manager = SkillReviewManager(None, make_store(tmp_path))
        manager.submit("a", "1", [])
        await started.wait()
        await manager.close(timeout=0.01)
        assert cancelled.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_or_cancelled_foreground_never_submits(tmp_path, cancel):
    async def scenario():
        started = asyncio.Event()

        class FailingModel(ScriptedModel):
            async def _agenerate(self, *args, **kwargs):
                if cancel:
                    started.set()
                    await asyncio.Event().wait()
                raise RuntimeError("model failed")

        manager = SkillReviewManager(None, make_store(tmp_path))
        agent = create_agent(FailingModel(responses=[]),
                             middleware=[SkillReviewMiddleware(manager, interval=1)], checkpointer=InMemorySaver())
        invocation = asyncio.create_task(agent.ainvoke(
            {"messages": [HumanMessage(content="task")], "skill_review_rounds": 10},
            {"configurable": {"thread_id": "a"}}))
        if cancel:
            await asyncio.wait_for(started.wait(), 2)
            invocation.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await invocation
        assert not manager._submitted
    asyncio.run(scenario())
