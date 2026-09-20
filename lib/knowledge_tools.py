"""LangChain tool adapters for workspace knowledge services."""

import asyncio
import json
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from .code_graph import CodeGraphService
from .skills import SkillStore


class CodeGraphInput(BaseModel):
    action: Literal["search", "query", "impact", "overview"]
    query: str = ""
    mode: Literal["fts", "semantic", "hybrid"] = "fts"
    kind: Literal["file", "class", "function"] | None = None
    context_files: list[str] | None = None
    target: str = ""
    relation: Literal["callers_of", "callees_of", "importers_of", "tests_for",
                      "children_of", "inheritors_of", "references_to"] | None = None
    changed_files: list[str] | None = None
    max_results: int = Field(default=20, ge=1, le=100, strict=True)


def create_code_graph_tool(service: CodeGraphService):
    @tool(args_schema=CodeGraphInput)
    async def code_graph(**kwargs) -> str:
        """Build/refresh the local Python/C/C++ graph and search symbols, query relations,
        inspect two-hop change impact, or summarize architecture. Defaults to offline FTS.
        """
        return await service.execute(kwargs)
    return code_graph


def create_skill_tools(store: SkillStore, permission=None, *, include_load: bool = True):
    @tool
    async def skills_list(category: str | None = None) -> str:
        """List available skill names/descriptions, optionally filtered by category."""
        def read():
            return {"skills": [
                {"name": s.name, "description": s.description, "source": s.source,
                 "category": s.category, "created_by": s.created_by}
                for s in store.list(category)
            ], "conflicts": store.conflicts()}
        return json.dumps(await asyncio.to_thread(read), ensure_ascii=False)

    @tool
    async def skill_view(name: str, file_path: str | None = None) -> str:
        """Read skill instructions or an attachment under references/templates/scripts/assets."""
        return json.dumps(await asyncio.to_thread(store.view, name, file_path), ensure_ascii=False, default=str)

    @tool
    async def skill_manage(
        action: Literal["create", "edit", "patch", "delete", "write_file", "remove_file"],
        name: str, content: str | None = None, category: str | None = None,
        file_path: str | None = None, file_content: str | None = None,
        old_string: str | None = None, new_string: str | None = None,
        replace_all: bool = False,
    ) -> str:
        """Manage skills and supporting files. Read with skill_view before changing a skill.
        New skills are project-local. User-level writes require permission.
        """
        skill = await asyncio.to_thread(store.get, name)
        approved_user_path = None
        if action != "create" and skill and skill.source == "user" and store.origin != "background_review":
            if permission is None or not await permission.authorize_skill_write(skill.skill_file):
                return json.dumps({"success": False, "error": "Permission denied for user skill write"})
            approved_user_path = skill.skill_file
        result = await asyncio.to_thread(
            store.manage, action, name, content=content, category=category,
            file_path=file_path, file_content=file_content, old_string=old_string,
            new_string=new_string, replace_all=replace_all,
            approved_user_path=approved_user_path, enforce_user_permission=True,
        )
        return json.dumps(result, ensure_ascii=False)

    @tool
    async def load_skill(skill_name: str) -> str:
        """Load a skill's full SKILL.md content by name (compatibility entry point)."""
        result = await asyncio.to_thread(store.view, skill_name)
        if not result.get("success"):
            return json.dumps(result, ensure_ascii=False)
        from .frontmatter import format_frontmatter
        return format_frontmatter(result["frontmatter"], result["content"])

    return [skills_list, skill_view, skill_manage, *([load_skill] if include_load else [])]
