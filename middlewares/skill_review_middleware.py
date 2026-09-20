"""Checkpointed trigger state; review work stays outside the foreground graph."""

import hashlib
import logging
import os

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage
from langgraph.config import get_config
from typing_extensions import NotRequired

from lib.skill_review import SkillReviewManager

logger = logging.getLogger(__name__)


class SkillReviewAgentState(AgentState):
    skill_review_rounds: NotRequired[int]
    skill_review_last_counted: NotRequired[str]
    skill_review_last_submitted: NotRequired[str]


class SkillReviewMiddleware(AgentMiddleware):
    state_schema = SkillReviewAgentState

    def __init__(self, manager: SkillReviewManager, interval: int | None = None):
        self.manager = manager
        self.interval = max(0, int(os.getenv("LANGCODE_SKILL_REVIEW_INTERVAL", "10")) if interval is None else interval)

    @staticmethod
    def _position(message):
        return message.id or hashlib.sha256(message.model_dump_json().encode()).hexdigest()

    async def aafter_model(self, state, runtime):
        messages = state.get("messages", [])
        if not self.interval or not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return None
        position = self._position(last)
        if position == state.get("skill_review_last_counted"):
            return None
        return {"skill_review_rounds": state.get("skill_review_rounds", 0) + 1,
                "skill_review_last_counted": position}

    async def aafter_agent(self, state, runtime):
        messages = state.get("messages", [])
        if not self.interval or state.get("skill_review_rounds", 0) < self.interval or not messages:
            return None
        last = messages[-1]
        if (not isinstance(last, AIMessage) or last.tool_calls or not last.content
                or last.response_metadata.get("finish_reason") in {"length", "content_filter"}):
            return None
        position = self._position(last)
        if position == state.get("skill_review_last_submitted"):
            return None
        thread_id = get_config().get("configurable", {}).get("thread_id")
        if not thread_id:
            return None
        try:
            if not self.manager.submit(str(thread_id), position, messages):
                return None
        except Exception:
            logger.exception("Unable to submit skill review", extra={"thread_id": thread_id})
            return None
        return {"skill_review_rounds": 0, "skill_review_last_submitted": position}
