"""Durable learning policy adapted from the source project."""

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
