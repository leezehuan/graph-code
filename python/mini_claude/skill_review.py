"""Best-effort background review that turns completed work into skills."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from .skills import SkillStore
from .tools import tool_definitions
from .ui import print_error, print_info

if TYPE_CHECKING:
    from .agent import Agent


SKILL_REVIEW_TOOL_NAMES = frozenset({"skills_list", "skill_view", "skill_manage"})
SKILL_REVIEW_MAX_TURNS = 16

SKILL_REVIEW_PROMPT = """Review the conversation above and update the project skill library when durable procedural knowledge emerged.

Prefer class-level umbrella skills rather than one narrow skill per session. Work in this order:
1. If an agent-created skill loaded in the conversation covers the learning, read it with skill_view and patch it.
2. Otherwise use skills_list and skill_view to find an existing agent-created umbrella skill.
3. Add concise references, templates, or scripts when details do not belong in SKILL.md, and add a pointer from SKILL.md.
4. Create a new class-level skill only when no existing skill covers the task class.

Act on durable signals: user corrections to workflow or output, a non-trivial successful technique, a workaround verified during the task, a changed course that produced a working result, or a missing pitfall in a skill that was used. Include trigger conditions, actionable steps, pitfalls, and verification.

Do not save one-off task narratives, missing local dependencies, unconfigured credentials, transient errors that disappeared after retry, permanent negative claims about a tool, or an unresolved sequence that never produced a working method. Do not present failed guesses as reliable guidance.

Only project skills marked created_by: agent may be changed automatically. User-authored and user-level skills are protected. Read the exact SKILL.md or supporting file with skill_view before editing, patching, overwriting, removing, or deleting it. The store adds created_by: agent to newly created skills automatically.

You can only call skills_list, skill_view, and skill_manage. If nothing is worth saving, reply exactly: Nothing to save."""


def spawn_background_skill_review(
    parent: "Agent", messages_snapshot: list[dict[str, Any]]
) -> threading.Thread:
    """Start one isolated review thread and return it for tests/observability."""

    def _target() -> None:
        actions: list[str] = []

        async def _run() -> None:
            from .agent import Agent

            review_store = SkillStore(
                project_root=parent.skill_store.project_root,
                user_root=parent.skill_store.user_skills_dir,
                origin="background_review",
            )
            review_tools = [
                tool for tool in tool_definitions
                if tool.get("name") in SKILL_REVIEW_TOOL_NAMES
            ]
            review_agent = Agent(
                **parent._child_runtime_kwargs(),
                permission_mode="bypassPermissions",
                max_turns=SKILL_REVIEW_MAX_TURNS,
                custom_system_prompt=SKILL_REVIEW_PROMPT,
                custom_tools=review_tools,
                is_sub_agent=True,
                skill_store=review_store,
                skill_review_interval=0,
                skip_background_review=True,
                quiet_mode=True,
            )

            def _record_action(result: dict) -> None:
                message = result.get("message")
                if isinstance(message, str) and message:
                    actions.append(message)

            review_agent._skill_action_callback = _record_action
            try:
                await review_agent.run_once(
                    "Review the completed conversation now and update the skill library when appropriate.",
                    conversation_history=messages_snapshot,
                )
            finally:
                await review_agent.close()

        try:
            asyncio.run(_run())
        except Exception as exc:
            print_error(f"Background skill review failed: {exc}")
            return

        unique_actions = list(dict.fromkeys(actions))
        if unique_actions:
            print_info("Self-improvement review: " + " | ".join(unique_actions))

    thread = threading.Thread(
        target=_target,
        name=f"skill-review-{parent.session_id}",
        daemon=True,
    )
    thread.start()
    return thread


__all__ = [
    "SKILL_REVIEW_MAX_TURNS",
    "SKILL_REVIEW_PROMPT",
    "SKILL_REVIEW_TOOL_NAMES",
    "spawn_background_skill_review",
]
