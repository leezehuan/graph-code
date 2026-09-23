"""Lead memory management. Identity is injected, never chosen by the model."""
import json
from typing import Literal
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import Command
from .memories import user_identity


def create_memory_tools(repository):
    def encoded(value):
        return json.dumps(value, ensure_ascii=False)

    @tool
    async def memory_list(config: RunnableConfig, type: Literal["semantic", "procedural", "episodic", "project"] | None = None,
                          scope: Literal["user", "project"] | None = None, limit: int = 20, offset: int = 0) -> str:
        """List accessible memories. Project memories are shared; user memories are personal."""
        try:
            if not 1 <= limit <= 100 or offset < 0:
                raise ValueError("limit must be 1..100 and offset must be nonnegative")
            records = await repository.list(user_identity(config), scope, type)
            return encoded({"success": True, "total": len(records), "memories": records[offset:offset + limit]})
        except Exception as exc:
            return encoded({"success": False, "error": str(exc)})

    @tool
    async def memory_get(scope: Literal["user", "project"], id: str, config: RunnableConfig) -> str:
        """Read one memory belonging to the current user or project."""
        try:
            return encoded({"success": True, "memory": await repository.get(scope, id, user_identity(config))})
        except Exception as exc:
            return encoded({"success": False, "error": str(exc)})

    @tool
    async def memory_update(scope: Literal["user", "project"], id: str, description: str, content: str,
                            runtime: ToolRuntime) -> Command | str:
        """Update one memory only on explicit user request. Preserve its type and scope."""
        try:
            user_id = user_identity(runtime.config)
            previous = await repository.get(scope, id, user_id)
            record = await repository.save(previous["type"], scope, description, content,
                                           "Explicit user-requested update", user_id, id)
            return Command(update={"memory_management_written": True, "messages": [ToolMessage(
                content=encoded({"success": True, "memory": record}), tool_call_id=runtime.tool_call_id,
                name="memory_update")]})
        except Exception as exc:
            return encoded({"success": False, "error": str(exc)})

    @tool
    async def memory_delete(scope: Literal["user", "project"], id: str, runtime: ToolRuntime) -> Command | str:
        """Delete one memory only on explicit user request. Clarify ambiguous requests; no bulk deletion."""
        try:
            await repository.delete(scope, id, user_identity(runtime.config))
            return Command(update={"memory_management_written": True, "messages": [ToolMessage(
                content=encoded({"success": True, "id": id}), tool_call_id=runtime.tool_call_id,
                name="memory_delete")]})
        except Exception as exc:
            return encoded({"success": False, "error": str(exc)})

    return [memory_list, memory_get, memory_update, memory_delete]
