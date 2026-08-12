import copy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

for module_name, class_name in (
    ("anthropic", "AsyncAnthropic"),
    ("openai", "AsyncOpenAI"),
):
    if module_name not in sys.modules:
        module = types.ModuleType(module_name)
        setattr(module, class_name, object)
        sys.modules[module_name] = module

from mini_claude.agent import Agent  # noqa: E402


class SkillAgentTests(unittest.IsolatedAsyncioTestCase):
    def _bare_agent(self):
        agent = object.__new__(Agent)
        agent._aborted = False
        agent.is_sub_agent = False
        agent.skip_background_review = False
        agent.skill_review_interval = 2
        agent._iters_since_skill = 2
        agent.use_openai = False
        agent._anthropic_messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]
        agent._openai_messages = []
        agent.quiet_mode = False
        return agent

    def test_review_trigger_uses_deep_snapshot_and_resets_counter(self):
        agent = self._bare_agent()
        original = copy.deepcopy(agent._anthropic_messages)
        captured = []

        def _spawn(parent, snapshot):
            captured.append((parent, snapshot))

        with patch(
            "mini_claude.skill_review.spawn_background_skill_review", _spawn
        ):
            agent._maybe_spawn_skill_review(True)

        self.assertEqual(agent._iters_since_skill, 0)
        self.assertIs(captured[0][0], agent)
        self.assertEqual(captured[0][1], original)
        self.assertIsNot(captured[0][1], agent._anthropic_messages)
        captured[0][1][0]["content"] = "changed"
        self.assertEqual(agent._anthropic_messages[0]["content"], "task")

    def test_review_trigger_guards(self):
        cases = (
            ("incomplete", {"completed": False}),
            ("aborted", {"_aborted": True}),
            ("subagent", {"is_sub_agent": True}),
            ("skipped", {"skip_background_review": True}),
            ("disabled", {"skill_review_interval": 0}),
            ("below-threshold", {"_iters_since_skill": 1}),
        )
        for label, overrides in cases:
            with self.subTest(case=label):
                agent = self._bare_agent()
                completed = overrides.pop("completed", True)
                for key, value in overrides.items():
                    setattr(agent, key, value)
                with patch(
                    "mini_claude.skill_review.spawn_background_skill_review"
                ) as spawn:
                    agent._maybe_spawn_skill_review(completed)
                spawn.assert_not_called()

    async def test_run_once_replaces_openai_system_and_copies_history(self):
        agent = object.__new__(Agent)
        agent.use_openai = True
        agent._openai_messages = [{"role": "system", "content": "review system"}]
        agent._anthropic_messages = []
        agent.total_input_tokens = 0
        agent.total_output_tokens = 0
        history = [
            {"role": "system", "content": "parent system"},
            {"role": "user", "content": "task"},
        ]

        async def _chat(prompt):
            agent._output_buffer.append("reviewed")

        agent.chat = _chat
        result = await agent.run_once("review", conversation_history=history)

        self.assertEqual(result["text"], "reviewed")
        self.assertEqual(agent._openai_messages[0]["content"], "review system")
        self.assertEqual(agent._openai_messages[1:], [history[1]])
        self.assertIsNot(agent._openai_messages[1], history[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
