import sys
import unittest
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude import tools  # noqa: E402
from mini_claude.autonomy import AUTO_MODE_FAST_PATH_TOOLS  # noqa: E402


class SkillToolPermissionTests(unittest.TestCase):
    def setUp(self):
        self.previous_rules = tools._cached_rules
        tools._cached_rules = {"allow": [], "deny": []}

    def tearDown(self):
        tools._cached_rules = self.previous_rules

    def test_skill_tool_definitions_are_registered(self):
        definitions = {tool["name"]: tool for tool in tools.tool_definitions}
        self.assertIn("skills_list", definitions)
        self.assertIn("skill_view", definitions)
        self.assertIn("skill_manage", definitions)
        self.assertEqual(
            definitions["skill_manage"]["input_schema"]["required"],
            ["action", "name"],
        )

    def test_skill_reads_are_safe_and_concurrent(self):
        for name in ("skills_list", "skill_view"):
            with self.subTest(tool=name):
                self.assertIn(name, tools.READ_TOOLS)
                self.assertIn(name, tools.CONCURRENCY_SAFE_TOOLS)
                self.assertIn(name, AUTO_MODE_FAST_PATH_TOOLS)
                self.assertEqual(
                    tools.check_permission(name, {}, "default")["action"],
                    "allow",
                )

    def test_skill_manage_permission_matrix(self):
        create = {"action": "create", "name": "example"}
        patch = {"action": "patch", "name": "example"}
        delete = {"action": "delete", "name": "example"}

        self.assertEqual(
            tools.check_permission("skill_manage", create, "default")["action"],
            "confirm",
        )
        self.assertEqual(
            tools.check_permission("skill_manage", delete, "default")["action"],
            "confirm",
        )
        self.assertEqual(
            tools.check_permission("skill_manage", patch, "default")["action"],
            "allow",
        )
        self.assertEqual(
            tools.check_permission("skill_manage", create, "plan")["action"],
            "deny",
        )
        self.assertEqual(
            tools.check_permission("skill_manage", create, "acceptEdits")["action"],
            "allow",
        )
        self.assertEqual(
            tools.check_permission("skill_manage", create, "bypassPermissions")["action"],
            "allow",
        )
        self.assertEqual(
            tools.check_permission("skill_manage", create, "dontAsk")["action"],
            "deny",
        )

    def test_explicit_deny_still_beats_bypass(self):
        tools._cached_rules = {
            "allow": [],
            "deny": [{"tool": "skill_manage", "pattern": None}],
        }
        result = tools.check_permission(
            "skill_manage",
            {"action": "create", "name": "example"},
            "bypassPermissions",
        )
        self.assertEqual(result["action"], "deny")


if __name__ == "__main__":
    unittest.main(verbosity=2)
