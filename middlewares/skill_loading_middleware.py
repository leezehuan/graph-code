"""Inject the shared skill catalog using LangChain's model wrapper hook."""

import asyncio
from dataclasses import asdict
from pathlib import Path

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from lib.skills import SkillStore


class SkillLoadingMiddleware(AgentMiddleware):
    def __init__(self, work_dir: Path, skill_dir: Path | None = None,
                 allowed_skill_names: set[str] | None = None, store: SkillStore | None = None):
        self.store = store or SkillStore(
            project_root=work_dir, project_skills_dir=skill_dir,
            allowed_skill_names=allowed_skill_names,
        )

    async def awrap_model_call(self, request, handler):
        prompt = await asyncio.to_thread(self.store.format_index)
        if prompt:
            blocks = list(request.system_message.content_blocks) if request.system_message else []
            blocks.append({"type": "text", "text": prompt})
            request = request.override(system_message=SystemMessage(content_blocks=blocks))
        return await handler(request)

    def get_skill_names(self) -> list[str]:
        return [skill.name for skill in self.store.list()]

    def get_skill(self, name: str) -> dict | None:
        skill = self.store.get(name)
        return asdict(skill) if skill else None

    def clear_cache(self):
        self.store.invalidate()
