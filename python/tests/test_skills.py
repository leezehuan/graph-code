import sys
import tempfile
import unittest
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.skills import (  # noqa: E402
    MAX_SKILL_CONTENT_CHARS,
    SkillStore,
)


def _skill_content(
    name: str,
    description: str = "Reusable workflow",
    body: str = "# Workflow\n\nRun the verified steps.",
    extra: str = "",
) -> str:
    extra_line = f"{extra.rstrip()}\n" if extra else ""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{extra_line}"
        "---\n\n"
        f"{body}\n"
    )


def _write_skill(
    root: Path,
    name: str,
    *,
    category: str | None = None,
    description: str = "Reusable workflow",
    body: str = "# Workflow\n\nRun the verified steps.",
    extra: str = "",
) -> Path:
    directory = root / category / name if category else root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        _skill_content(name, description=description, body=body, extra=extra),
        encoding="utf-8",
    )
    return directory


class SkillStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.project = base / "project"
        self.project.mkdir()
        self.project_skills = self.project / ".claude" / "skills"
        self.user_skills = base / "user-skills"
        self.user_skills.mkdir()
        self.store = SkillStore(
            project_root=self.project,
            user_root=self.user_skills,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_discovery_is_recursive_and_project_overrides_user(self):
        _write_skill(
            self.user_skills,
            "deploy",
            description="User deployment workflow",
            body="USER BODY",
        )
        _write_skill(
            self.project_skills,
            "deploy",
            category="devops",
            description="Project deployment workflow",
            body="PROJECT BODY SECRET",
            extra="user_invocable: false\nwhen-to-use: Use for releases",
        )

        skills = self.store.list()
        self.assertEqual([skill.name for skill in skills], ["deploy"])
        self.assertEqual(skills[0].source, "project")
        self.assertEqual(skills[0].category, "devops")
        self.assertFalse(skills[0].user_invocable)
        self.assertEqual(skills[0].when_to_use, "Use for releases")
        index = self.store.format_index()
        self.assertIn("Project deployment workflow", index)
        self.assertNotIn("PROJECT BODY SECRET", index)

    def test_same_source_duplicate_name_is_ambiguous(self):
        _write_skill(self.project_skills, "duplicate", category="one")
        _write_skill(self.project_skills, "duplicate", category="two")

        self.assertIn("duplicate", self.store.conflicts())
        result = self.store.view("duplicate")
        self.assertFalse(result["success"])
        self.assertIn("Ambiguous", result["error"])

    def test_project_skill_resolves_over_ambiguous_user_skills(self):
        _write_skill(self.user_skills, "shared", category="one")
        _write_skill(self.user_skills, "shared", category="two")
        _write_skill(self.project_skills, "shared", body="PROJECT")

        result = self.store.view("shared")
        self.assertTrue(result["success"])
        self.assertEqual(result["source"], "project")
        self.assertEqual(result["content"], "PROJECT")

    def test_view_lists_and_reads_linked_files_safely(self):
        directory = _write_skill(self.project_skills, "linked")
        reference = directory / "references" / "nested" / "guide.md"
        reference.parent.mkdir(parents=True)
        reference.write_text("reference content", encoding="utf-8")
        binary = directory / "assets" / "sample.bin"
        binary.parent.mkdir()
        binary.write_bytes(b"\xff\xfe\x00")

        main = self.store.view("linked")
        self.assertTrue(main["success"])
        self.assertEqual(
            main["linked_files"]["references"],
            ["references/nested/guide.md"],
        )
        linked = self.store.view("linked", "references/nested/guide.md")
        self.assertEqual(linked["content"], "reference content")
        binary_result = self.store.view("linked", "assets/sample.bin")
        self.assertTrue(binary_result["is_binary"])

        for unsafe in ("../outside.txt", "/outside.txt", "references/../../x"):
            with self.subTest(path=unsafe):
                result = self.store.view("linked", unsafe)
                self.assertFalse(result["success"])

    def test_manage_all_actions_and_immediate_cache_invalidation(self):
        self.assertEqual(self.store.list(), [])
        created = self.store.manage(
            "create",
            "managed",
            content=_skill_content("managed", body="ORIGINAL"),
            category="workflows",
        )
        self.assertTrue(created["success"])
        self.assertEqual([skill.name for skill in self.store.list()], ["managed"])

        edited = self.store.manage(
            "edit",
            "managed",
            content=_skill_content("managed", body="EDITED"),
        )
        self.assertTrue(edited["success"])
        patched = self.store.manage(
            "patch",
            "managed",
            old_string="EDITED",
            new_string="PATCHED",
        )
        self.assertTrue(patched["success"])
        self.assertEqual(self.store.view("managed")["content"], "PATCHED")

        written = self.store.manage(
            "write_file",
            "managed",
            file_path="references/guide.md",
            file_content="first",
        )
        self.assertTrue(written["success"])
        support_patch = self.store.manage(
            "patch",
            "managed",
            file_path="references/guide.md",
            old_string="first",
            new_string="second",
        )
        self.assertTrue(support_patch["success"])
        removed = self.store.manage(
            "remove_file", "managed", file_path="references/guide.md"
        )
        self.assertTrue(removed["success"])
        deleted = self.store.manage("delete", "managed")
        self.assertTrue(deleted["success"])
        self.assertEqual(self.store.list(), [])

    def test_manage_validates_frontmatter_size_names_and_paths(self):
        mismatch = self.store.manage(
            "create", "right", content=_skill_content("wrong")
        )
        self.assertFalse(mismatch["success"])
        missing_description = self.store.manage(
            "create", "missing", content="---\nname: missing\n---\n\nBody"
        )
        self.assertFalse(missing_description["success"])
        invalid_name = self.store.manage(
            "create", "Invalid Name", content=_skill_content("Invalid Name")
        )
        self.assertFalse(invalid_name["success"])
        oversized = self.store.manage(
            "create",
            "large",
            content=_skill_content("large", body="x" * MAX_SKILL_CONTENT_CHARS),
        )
        self.assertFalse(oversized["success"])

        self.assertTrue(
            self.store.manage(
                "create", "paths", content=_skill_content("paths")
            )["success"]
        )
        escaped = self.store.manage(
            "write_file",
            "paths",
            file_path="../outside.txt",
            file_content="bad",
        )
        self.assertFalse(escaped["success"])
        self.assertFalse((self.project / "outside.txt").exists())

    def test_patch_requires_unique_match_unless_replace_all(self):
        _write_skill(self.project_skills, "patches", body="same same")
        result = self.store.manage(
            "patch", "patches", old_string="same", new_string="new"
        )
        self.assertFalse(result["success"])
        result = self.store.manage(
            "patch",
            "patches",
            old_string="same",
            new_string="new",
            replace_all=True,
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.store.view("patches")["content"], "new new")

    def test_background_review_stamps_and_protects_ownership(self):
        background = SkillStore(
            project_root=self.project,
            user_root=self.user_skills,
            origin="background_review",
        )
        created = background.manage(
            "create", "learned", content=_skill_content("learned", body="OLD")
        )
        self.assertTrue(created["success"])
        main = background.view("learned")
        self.assertEqual(main["frontmatter"]["created_by"], "agent")

        fresh_background = SkillStore(
            project_root=self.project,
            user_root=self.user_skills,
            origin="background_review",
        )
        refused = fresh_background.manage(
            "patch", "learned", old_string="OLD", new_string="NEW"
        )
        self.assertFalse(refused["success"])
        self.assertIn("skill_view", refused["error"])
        self.assertTrue(fresh_background.view("learned")["success"])
        patched = fresh_background.manage(
            "patch", "learned", old_string="OLD", new_string="NEW"
        )
        self.assertTrue(patched["success"])

        _write_skill(self.project_skills, "manual", body="MANUAL")
        fresh_background.invalidate()
        self.assertTrue(fresh_background.view("manual")["success"])
        protected = fresh_background.manage(
            "patch", "manual", old_string="MANUAL", new_string="CHANGED"
        )
        self.assertFalse(protected["success"])
        self.assertIn("created_by: agent", protected["error"])

        _write_skill(
            self.user_skills,
            "user-agent",
            body="USER",
            extra="created_by: agent",
        )
        fresh_background.invalidate()
        self.assertTrue(fresh_background.view("user-agent")["success"])
        protected_user = fresh_background.manage(
            "patch", "user-agent", old_string="USER", new_string="CHANGED"
        )
        self.assertFalse(protected_user["success"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
