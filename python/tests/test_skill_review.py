import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude import skill_review  # noqa: E402
from mini_claude.skills import SkillStore  # noqa: E402


class _Parent:
    def __init__(self, store):
        self.skill_store = store
        self.session_id = "session1"

    def _child_runtime_kwargs(self):
        return {
            "model": "test-model",
            "api_base": "https://example.invalid/v1",
            "anthropic_base_url": None,
            "api_key": "test-key",
            "thinking": True,
        }


class _ImmediateThread:
    instances = []

    def __init__(self, *, target, name, daemon):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True
        self.target()


class _FakeAgent:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._skill_action_callback = None
        self.closed = False
        self.run_args = None
        self.__class__.instances.append(self)

    async def run_once(self, prompt, conversation_history=None):
        self.run_args = (prompt, conversation_history)
        self._skill_action_callback(
            {"success": True, "message": "Skill 'learned' created."}
        )
        self._skill_action_callback(
            {"success": True, "message": "Skill 'learned' created."}
        )
        return {"text": "done", "tokens": {"input": 0, "output": 0}}

    async def close(self):
        self.closed = True


class SkillReviewTests(unittest.TestCase):
    def setUp(self):
        _ImmediateThread.instances.clear()
        _FakeAgent.instances.clear()
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        project = base / "project"
        project.mkdir()
        user = base / "user-skills"
        user.mkdir()
        self.parent = _Parent(SkillStore(project_root=project, user_root=user))

    def tearDown(self):
        self.temp.cleanup()

    def test_review_thread_is_daemon_and_agent_is_isolated(self):
        fake_module = types.ModuleType("mini_claude.agent")
        fake_module.Agent = _FakeAgent
        notifications = []
        history = [{"role": "user", "content": "task"}]

        with patch.dict(sys.modules, {"mini_claude.agent": fake_module}), patch.object(
            skill_review.threading, "Thread", _ImmediateThread
        ), patch.object(skill_review, "print_info", notifications.append):
            thread = skill_review.spawn_background_skill_review(self.parent, history)

        self.assertIs(thread, _ImmediateThread.instances[0])
        self.assertTrue(thread.daemon)
        self.assertTrue(thread.started)
        self.assertEqual(thread.name, "skill-review-session1")

        agent = _FakeAgent.instances[0]
        kwargs = agent.kwargs
        self.assertEqual(kwargs["max_turns"], 16)
        self.assertTrue(kwargs["is_sub_agent"])
        self.assertTrue(kwargs["quiet_mode"])
        self.assertTrue(kwargs["skip_background_review"])
        self.assertEqual(kwargs["skill_review_interval"], 0)
        self.assertEqual(kwargs["permission_mode"], "bypassPermissions")
        self.assertEqual(
            {tool["name"] for tool in kwargs["custom_tools"]},
            {"skills_list", "skill_view", "skill_manage"},
        )
        self.assertEqual(kwargs["skill_store"].origin, "background_review")
        self.assertEqual(agent.run_args[1], history)
        self.assertTrue(agent.closed)
        self.assertEqual(
            notifications,
            ["Self-improvement review: Skill 'learned' created."],
        )

    def test_review_errors_are_isolated(self):
        class FailingAgent(_FakeAgent):
            async def run_once(self, prompt, conversation_history=None):
                raise RuntimeError("review failed")

        fake_module = types.ModuleType("mini_claude.agent")
        fake_module.Agent = FailingAgent
        errors = []
        with patch.dict(sys.modules, {"mini_claude.agent": fake_module}), patch.object(
            skill_review.threading, "Thread", _ImmediateThread
        ), patch.object(skill_review, "print_error", errors.append):
            skill_review.spawn_background_skill_review(self.parent, [])

        self.assertEqual(errors, ["Background skill review failed: review failed"])

    def test_prompt_contains_durable_learning_filters(self):
        prompt = skill_review.SKILL_REVIEW_PROMPT
        self.assertIn("class-level umbrella", prompt)
        self.assertIn("unresolved sequence", prompt)
        self.assertIn("created_by: agent", prompt)
        self.assertIn("skill_view before", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
