"""File-backed skills with progressive disclosure and agent-managed writes."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .frontmatter import format_frontmatter, parse_frontmatter


MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000
MAX_SKILL_FILE_BYTES = 1_048_576
SKILL_CACHE_TTL_SECONDS = 30.0
ALLOWED_SUPPORT_DIRS = frozenset({"references", "templates", "scripts", "assets"})
VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

_write_lock = threading.RLock()
_generation_lock = threading.Lock()
_cache_generation = 0


def _current_generation() -> int:
    with _generation_lock:
        return _cache_generation


def _invalidate_all_caches() -> None:
    global _cache_generation
    with _generation_lock:
        _cache_generation += 1


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    when_to_use: str | None = None
    allowed_tools: list[str] | None = None
    user_invocable: bool = True
    context: str = "inline"
    prompt_template: str = ""
    source: str = "project"
    skill_dir: str = ""
    skill_file: str = ""
    category: str | None = None
    created_by: str | None = None


class SkillStore:
    """Discover, read, render, and safely mutate project/user skills."""

    def __init__(
        self,
        *,
        project_root: Path | str | None = None,
        user_root: Path | str | None = None,
        origin: str = "foreground",
    ) -> None:
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.project_skills_dir = self.project_root / ".claude" / "skills"
        self.user_skills_dir = Path(
            user_root or (Path.home() / ".claude" / "skills")
        ).resolve()
        self.origin = origin
        self._cache: tuple[
            int,
            tuple[tuple[str, int], ...],
            float,
            tuple[SkillDefinition, ...],
            frozenset[str],
        ] | None = None
        self._read_paths: set[Path] = set()

    def list(self, category: str | None = None) -> list[SkillDefinition]:
        skills, _ = self._discover()
        if category is None:
            return list(skills)
        return [skill for skill in skills if skill.category == category]

    def conflicts(self) -> list[str]:
        _, conflicts = self._discover()
        return sorted(conflicts)

    def get(self, name: str) -> SkillDefinition | None:
        skill, _ = self._resolve(name)
        return skill

    def view(self, name: str, file_path: str | None = None) -> dict[str, Any]:
        skill, error = self._resolve(name)
        if error:
            return {"success": False, "error": error}
        assert skill is not None

        skill_dir = Path(skill.skill_dir)
        if file_path:
            target, error = self._resolve_support_path(skill_dir, file_path)
            if error:
                return {"success": False, "error": error}
            assert target is not None
            if not target.is_file():
                return {
                    "success": False,
                    "error": f"File '{file_path}' not found in skill '{name}'.",
                    "available_files": self._linked_files(skill_dir),
                }
            self._mark_read(target)
            try:
                content = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return {
                    "success": True,
                    "name": skill.name,
                    "file": file_path,
                    "content": (
                        f"[Binary file: {target.name}, size: {target.stat().st_size} bytes]"
                    ),
                    "is_binary": True,
                }
            except OSError as exc:
                return {"success": False, "error": f"Failed to read '{file_path}': {exc}"}
            return {
                "success": True,
                "name": skill.name,
                "file": file_path,
                "content": content,
                "file_type": target.suffix,
            }

        skill_file = Path(skill.skill_file)
        try:
            raw = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            return {"success": False, "error": f"Failed to read skill '{name}': {exc}"}
        parsed = parse_frontmatter(raw)
        self._mark_read(skill_file)
        return {
            "success": True,
            "name": skill.name,
            "description": skill.description,
            "source": skill.source,
            "category": skill.category,
            "frontmatter": parsed.meta,
            "content": parsed.body,
            "linked_files": self._linked_files(skill_dir),
        }

    def render(self, name: str, args: str = "") -> dict[str, Any] | None:
        skill = self.get(name)
        if not skill:
            return None
        return {
            "prompt": resolve_skill_prompt(skill, args),
            "allowed_tools": skill.allowed_tools,
            "context": skill.context,
        }

    def format_index(self) -> str:
        skills = self.list()
        if not skills:
            return ""

        lines = ["# Available Skills", ""]
        invocable = [skill for skill in skills if skill.user_invocable]
        auto_only = [skill for skill in skills if not skill.user_invocable]
        if invocable:
            lines.append("User-invocable skills (user types /<name> to invoke):")
            for skill in invocable:
                lines.append(f"- **/{skill.name}**: {skill.description}")
                if skill.when_to_use:
                    lines.append(f"  When to use: {skill.when_to_use}")
            lines.append("")
        if auto_only:
            lines.append("Auto-invocable skills:")
            for skill in auto_only:
                lines.append(f"- **{skill.name}**: {skill.description}")
                if skill.when_to_use:
                    lines.append(f"  When to use: {skill.when_to_use}")
            lines.append("")
        lines.append(
            "Use `skill_view` to load a skill's instructions or linked files. "
            "Use `skill` when invoking a registered skill with arguments."
        )
        return "\n".join(lines)

    def manage(
        self,
        action: str,
        name: str,
        *,
        content: str | None = None,
        category: str | None = None,
        file_path: str | None = None,
        file_content: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        error = self._validate_name(name)
        if error:
            return {"success": False, "error": error}

        handlers = {
            "create": self._create,
            "edit": self._edit,
            "patch": self._patch,
            "delete": self._delete,
            "write_file": self._write_file,
            "remove_file": self._remove_file,
        }
        handler = handlers.get(action)
        if handler is None:
            return {
                "success": False,
                "error": (
                    f"Unknown action '{action}'. Use: create, edit, patch, "
                    "delete, write_file, remove_file"
                ),
            }

        with _write_lock:
            result = handler(
                name=name,
                content=content,
                category=category,
                file_path=file_path,
                file_content=file_content,
                old_string=old_string,
                new_string=new_string,
                replace_all=replace_all,
            )
            if result.get("success"):
                _invalidate_all_caches()
                self._cache = None
            return result

    def invalidate(self) -> None:
        self._cache = None

    def _discover(
        self,
    ) -> tuple[tuple[SkillDefinition, ...], frozenset[str]]:
        generation = _current_generation()
        signature = self._directory_signature()
        now = time.monotonic()
        if self._cache:
            cached_generation, cached_signature, expires, skills, conflicts = self._cache
            if (
                cached_generation == generation
                and cached_signature == signature
                and now < expires
            ):
                return skills, conflicts

        user, user_conflicts = self._scan_root(self.user_skills_dir, "user")
        project, project_conflicts = self._scan_root(
            self.project_skills_dir, "project"
        )
        selected = dict(user)
        selected.update(project)
        conflicts = set(project_conflicts)
        conflicts.update(name for name in user_conflicts if name not in project)
        for name in project_conflicts:
            selected.pop(name, None)
        for name in user_conflicts:
            if name not in project:
                selected.pop(name, None)

        skills = tuple(
            sorted(
                selected.values(),
                key=lambda skill: (skill.category or "", skill.name),
            )
        )
        frozen_conflicts = frozenset(conflicts)
        self._cache = (
            generation,
            signature,
            now + SKILL_CACHE_TTL_SECONDS,
            skills,
            frozen_conflicts,
        )
        return skills, frozen_conflicts

    def _scan_root(
        self, root: Path, source: str
    ) -> tuple[dict[str, SkillDefinition], set[str]]:
        candidates: dict[str, list[SkillDefinition]] = {}
        if not root.is_dir():
            return {}, set()
        try:
            skill_files = sorted(root.rglob("SKILL.md"))
        except OSError:
            return {}, set()
        for skill_file in skill_files:
            try:
                relative = skill_file.relative_to(root)
            except ValueError:
                continue
            if any(part in ALLOWED_SUPPORT_DIRS for part in relative.parts[:-1]):
                continue
            skill = self._parse_skill_file(skill_file, root, source)
            if skill:
                candidates.setdefault(skill.name, []).append(skill)

        conflicts = {name for name, items in candidates.items() if len(items) > 1}
        unique = {
            name: items[0]
            for name, items in candidates.items()
            if len(items) == 1
        }
        return unique, conflicts

    def _parse_skill_file(
        self, skill_file: Path, root: Path, source: str
    ) -> SkillDefinition | None:
        try:
            raw = skill_file.read_text(encoding="utf-8")
            parsed = parse_frontmatter(raw)
            meta = parsed.meta
            name = meta.get("name") or skill_file.parent.name
            raw_invocable = self._meta_get(
                meta, "user_invocable", "user-invocable", default="true"
            )
            raw_tools = self._meta_get(meta, "allowed_tools", "allowed-tools")
            allowed_tools: list[str] | None = None
            if raw_tools:
                if raw_tools.startswith("["):
                    try:
                        decoded = json.loads(raw_tools)
                        allowed_tools = [str(item) for item in decoded]
                    except (TypeError, ValueError):
                        allowed_tools = [
                            item.strip() for item in raw_tools.strip("[]").split(",")
                            if item.strip()
                        ]
                else:
                    allowed_tools = [
                        item.strip() for item in raw_tools.split(",") if item.strip()
                    ]
            relative_parent = skill_file.parent.relative_to(root)
            category = (
                str(relative_parent.parent).replace("\\", "/")
                if relative_parent.parent != Path(".")
                else None
            )
            return SkillDefinition(
                name=name,
                description=meta.get("description", "")[:MAX_DESCRIPTION_LENGTH],
                when_to_use=self._meta_get(meta, "when_to_use", "when-to-use"),
                allowed_tools=allowed_tools,
                user_invocable=str(raw_invocable).lower() != "false",
                context="fork" if meta.get("context") == "fork" else "inline",
                prompt_template=parsed.body,
                source=source,
                skill_dir=str(skill_file.parent),
                skill_file=str(skill_file),
                category=category,
                created_by=self._meta_get(meta, "created_by", "created-by"),
            )
        except (OSError, UnicodeError, ValueError):
            return None

    def _resolve(self, name: str) -> tuple[SkillDefinition | None, str | None]:
        skills, conflicts = self._discover()
        if name in conflicts:
            return None, (
                f"Ambiguous skill name '{name}': multiple skills in the same "
                "source use that name. Rename one before loading it."
            )
        for skill in skills:
            if skill.name == name:
                return skill, None
        available = [skill.name for skill in skills[:20]]
        suffix = f" Available skills: {', '.join(available)}." if available else ""
        return None, f"Skill '{name}' not found.{suffix}"

    def _create(self, *, name: str, content: str | None, category: str | None, **_: Any) -> dict[str, Any]:
        if content is None:
            return {"success": False, "error": "content is required for 'create'."}
        error = self._validate_category(category)
        if error:
            return {"success": False, "error": error}
        error = self._validate_skill_content(content, expected_name=name)
        if error:
            return {"success": False, "error": error}
        existing, resolve_error = self._resolve(name)
        if existing or (resolve_error and resolve_error.startswith("Ambiguous")):
            return {"success": False, "error": f"A skill named '{name}' already exists."}

        if self.origin == "background_review":
            content = self._stamp_agent_owner(content)
        target_dir = self.project_skills_dir / category / name if category else self.project_skills_dir / name
        error = self._validate_within(target_dir, self.project_skills_dir)
        if error:
            return {"success": False, "error": error}
        if target_dir.exists():
            return {"success": False, "error": f"Skill directory already exists: {target_dir}"}
        try:
            target_dir.mkdir(parents=True, exist_ok=False)
            self._atomic_write(target_dir / "SKILL.md", content)
        except OSError as exc:
            try:
                if target_dir.is_dir() and not any(target_dir.iterdir()):
                    target_dir.rmdir()
            except OSError:
                pass
            return {"success": False, "error": f"Failed to create skill '{name}': {exc}"}
        return {
            "success": True,
            "action": "create",
            "message": f"Skill '{name}' created.",
            "path": str(target_dir),
        }

    def _edit(self, *, name: str, content: str | None, **_: Any) -> dict[str, Any]:
        if content is None:
            return {"success": False, "error": "content is required for 'edit'."}
        error = self._validate_skill_content(content, expected_name=name)
        if error:
            return {"success": False, "error": error}
        skill, error = self._resolve(name)
        if error:
            return {"success": False, "error": error}
        assert skill is not None
        target = Path(skill.skill_file)
        error = self._background_write_error(skill, target, require_read=True)
        if error:
            return {"success": False, "error": error}
        if self.origin == "background_review":
            content = self._stamp_agent_owner(content)
        try:
            self._atomic_write(target, content)
        except OSError as exc:
            return {"success": False, "error": f"Failed to edit skill '{name}': {exc}"}
        return {"success": True, "action": "edit", "message": f"Skill '{name}' updated."}

    def _patch(
        self,
        *,
        name: str,
        old_string: str | None,
        new_string: str | None,
        file_path: str | None,
        replace_all: bool,
        **_: Any,
    ) -> dict[str, Any]:
        if not old_string:
            return {"success": False, "error": "old_string is required for 'patch'."}
        if new_string is None:
            return {"success": False, "error": "new_string is required for 'patch'."}
        skill, error = self._resolve(name)
        if error:
            return {"success": False, "error": error}
        assert skill is not None
        if file_path:
            target, error = self._resolve_support_path(Path(skill.skill_dir), file_path)
            if error:
                return {"success": False, "error": error}
            assert target is not None
        else:
            target = Path(skill.skill_file)
        if not target.is_file():
            return {"success": False, "error": f"File not found: {file_path or 'SKILL.md'}"}
        error = self._background_write_error(skill, target, require_read=True)
        if error:
            return {"success": False, "error": error}
        try:
            original = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return {"success": False, "error": f"Failed to read patch target: {exc}"}
        count = original.count(old_string)
        if count == 0:
            return {"success": False, "error": "old_string was not found."}
        if count > 1 and not replace_all:
            return {"success": False, "error": f"old_string matched {count} times; make it unique or set replace_all."}
        updated = original.replace(old_string, new_string, -1 if replace_all else 1)
        if target.name == "SKILL.md":
            if self.origin == "background_review":
                updated = self._stamp_agent_owner(updated)
            error = self._validate_skill_content(updated, expected_name=name)
            if error:
                return {"success": False, "error": f"Patch would break SKILL.md: {error}"}
        elif len(updated.encode("utf-8")) > MAX_SKILL_FILE_BYTES:
            return {"success": False, "error": f"Supporting file exceeds {MAX_SKILL_FILE_BYTES} bytes."}
        try:
            self._atomic_write(target, updated)
        except OSError as exc:
            return {"success": False, "error": f"Failed to patch skill '{name}': {exc}"}
        return {
            "success": True,
            "action": "patch",
            "message": f"Patched {file_path or 'SKILL.md'} in skill '{name}' ({count if replace_all else 1} replacement(s)).",
        }

    def _delete(self, *, name: str, **_: Any) -> dict[str, Any]:
        skill, error = self._resolve(name)
        if error:
            return {"success": False, "error": error}
        assert skill is not None
        skill_dir = Path(skill.skill_dir)
        skill_file = Path(skill.skill_file)
        error = self._background_write_error(skill, skill_file, require_read=True)
        if error:
            return {"success": False, "error": error}
        root = self.project_skills_dir if skill.source == "project" else self.user_skills_dir
        error = self._safe_delete_error(skill_dir, root)
        if error:
            return {"success": False, "error": error}
        try:
            shutil.rmtree(skill_dir)
        except OSError as exc:
            return {"success": False, "error": f"Failed to delete skill '{name}': {exc}"}
        return {"success": True, "action": "delete", "message": f"Skill '{name}' deleted."}

    def _write_file(
        self,
        *,
        name: str,
        file_path: str | None,
        file_content: str | None,
        **_: Any,
    ) -> dict[str, Any]:
        if not file_path:
            return {"success": False, "error": "file_path is required for 'write_file'."}
        if file_content is None:
            return {"success": False, "error": "file_content is required for 'write_file'."}
        if len(file_content.encode("utf-8")) > MAX_SKILL_FILE_BYTES:
            return {"success": False, "error": f"Supporting file exceeds {MAX_SKILL_FILE_BYTES} bytes."}
        skill, error = self._resolve(name)
        if error:
            return {"success": False, "error": error}
        assert skill is not None
        target, error = self._resolve_support_path(Path(skill.skill_dir), file_path)
        if error:
            return {"success": False, "error": error}
        assert target is not None
        error = self._background_write_error(skill, target, require_read=target.exists())
        if error:
            return {"success": False, "error": error}
        try:
            self._atomic_write(target, file_content)
        except OSError as exc:
            return {"success": False, "error": f"Failed to write '{file_path}': {exc}"}
        return {
            "success": True,
            "action": "write_file",
            "message": f"Wrote {file_path} in skill '{name}'.",
        }

    def _remove_file(self, *, name: str, file_path: str | None, **_: Any) -> dict[str, Any]:
        if not file_path:
            return {"success": False, "error": "file_path is required for 'remove_file'."}
        skill, error = self._resolve(name)
        if error:
            return {"success": False, "error": error}
        assert skill is not None
        target, error = self._resolve_support_path(Path(skill.skill_dir), file_path)
        if error:
            return {"success": False, "error": error}
        assert target is not None
        if not target.is_file():
            return {"success": False, "error": f"File not found: {file_path}"}
        error = self._background_write_error(skill, target, require_read=True)
        if error:
            return {"success": False, "error": error}
        try:
            target.unlink()
            parent = target.parent
            skill_dir = Path(skill.skill_dir)
            while parent != skill_dir and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        except OSError as exc:
            return {"success": False, "error": f"Failed to remove '{file_path}': {exc}"}
        return {
            "success": True,
            "action": "remove_file",
            "message": f"Removed {file_path} from skill '{name}'.",
        }

    def _background_write_error(
        self, skill: SkillDefinition, target: Path, *, require_read: bool
    ) -> str | None:
        if self.origin != "background_review":
            return None
        if skill.source != "project" or skill.created_by != "agent":
            return (
                f"Refusing background write to '{skill.name}': only project skills "
                "with created_by: agent may be changed automatically."
            )
        if require_read and self._resolved(target) not in self._read_paths:
            return (
                f"Refusing background write to '{skill.name}': read "
                f"{target.name} with skill_view before modifying it."
            )
        return None

    def _resolve_support_path(
        self, skill_dir: Path, file_path: str
    ) -> tuple[Path | None, str | None]:
        if not isinstance(file_path, str) or not file_path.strip():
            return None, "file_path must be a non-empty relative path."
        candidate = Path(file_path)
        windows_candidate = PureWindowsPath(file_path)
        if candidate.is_absolute() or windows_candidate.is_absolute() or windows_candidate.drive:
            return None, "file_path must be relative to the skill directory."
        if ".." in candidate.parts or not candidate.parts:
            return None, "Path traversal ('..') is not allowed."
        if candidate.parts[0] not in ALLOWED_SUPPORT_DIRS or len(candidate.parts) < 2:
            allowed = ", ".join(sorted(ALLOWED_SUPPORT_DIRS))
            return None, f"file_path must be a file under one of: {allowed}."
        target = skill_dir / candidate
        error = self._validate_within(target, skill_dir)
        if error:
            return None, error
        return target, None

    def _linked_files(self, skill_dir: Path) -> dict[str, list[str]]:
        linked: dict[str, list[str]] = {}
        for directory_name in sorted(ALLOWED_SUPPORT_DIRS):
            base = skill_dir / directory_name
            if not base.is_dir():
                continue
            files: list[str] = []
            try:
                candidates = sorted(base.rglob("*"))
            except OSError:
                continue
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                if self._validate_within(candidate, skill_dir) is None:
                    files.append(str(candidate.relative_to(skill_dir)).replace("\\", "/"))
            if files:
                linked[directory_name] = files
        return linked

    def _directory_signature(self) -> tuple[tuple[str, int], ...]:
        signature: list[tuple[str, int]] = []
        for root in (self.user_skills_dir, self.project_skills_dir):
            if not root.is_dir():
                signature.append((str(root), -1))
                continue
            try:
                directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
            except OSError:
                directories = [root]
            for directory in directories:
                try:
                    signature.append((str(directory), directory.stat().st_mtime_ns))
                except OSError:
                    continue
        return tuple(signature)

    def _validate_skill_content(self, content: str, *, expected_name: str) -> str | None:
        if len(content) > MAX_SKILL_CONTENT_CHARS:
            return f"SKILL.md exceeds {MAX_SKILL_CONTENT_CHARS} characters."
        if not content.lstrip("\ufeff").startswith("---"):
            return "SKILL.md must start with frontmatter delimited by '---'."
        parsed = parse_frontmatter(content.lstrip("\ufeff"))
        name = parsed.meta.get("name")
        description = parsed.meta.get("description")
        if not name:
            return "Frontmatter must include 'name'."
        if name != expected_name:
            return f"Frontmatter name '{name}' must match skill name '{expected_name}'."
        if not description:
            return "Frontmatter must include a non-empty 'description'."
        if len(description) > MAX_DESCRIPTION_LENGTH:
            return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."
        if not parsed.body.strip():
            return "SKILL.md must include instructions after the frontmatter."
        return None

    @staticmethod
    def _validate_name(name: str) -> str | None:
        if not isinstance(name, str) or not name:
            return "Skill name is required."
        if len(name) > MAX_NAME_LENGTH:
            return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
        if not VALID_NAME_RE.fullmatch(name):
            return (
                "Skill name must start with a lowercase letter or digit and use "
                "only lowercase letters, digits, dots, underscores, or hyphens."
            )
        return None

    @classmethod
    def _validate_category(cls, category: str | None) -> str | None:
        if category is None or category == "":
            return None
        if not isinstance(category, str) or "/" in category or "\\" in category:
            return "Category must be one safe directory name."
        return cls._validate_name(category)

    @staticmethod
    def _meta_get(meta: dict[str, str], *keys: str, default: str | None = None) -> str | None:
        for key in keys:
            if key in meta:
                return meta[key]
        return default

    @staticmethod
    def _resolved(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

    def _mark_read(self, path: Path) -> None:
        if self.origin == "background_review":
            self._read_paths.add(self._resolved(path))

    def _validate_within(self, path: Path, root: Path) -> str | None:
        try:
            resolved_root = root.resolve()
            resolved_path = path.resolve(strict=False)
            resolved_path.relative_to(resolved_root)
        except (OSError, ValueError):
            return f"Path '{path}' escapes the skills directory."
        return None

    def _safe_delete_error(self, skill_dir: Path, root: Path) -> str | None:
        if skill_dir.is_symlink() or (
            hasattr(skill_dir, "is_junction") and skill_dir.is_junction()
        ):
            return "Refusing to delete a symlink or directory junction."
        try:
            resolved_root = root.resolve()
            resolved_skill = skill_dir.resolve()
            resolved_skill.relative_to(resolved_root)
        except (OSError, ValueError):
            return "Refusing to delete a path outside the skills directory."
        if resolved_skill == resolved_root:
            return "Refusing to delete the skills root directory."
        return None

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                delete=False,
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _stamp_agent_owner(content: str) -> str:
        parsed = parse_frontmatter(content.lstrip("\ufeff"))
        meta = dict(parsed.meta)
        meta.pop("created-by", None)
        meta["created_by"] = "agent"
        return format_frontmatter(meta, parsed.body)


def resolve_skill_prompt(skill: SkillDefinition, args: str) -> str:
    prompt = re.sub(r"\$ARGUMENTS|\$\{ARGUMENTS\}", args, skill.prompt_template)
    return prompt.replace("${CLAUDE_SKILL_DIR}", skill.skill_dir)


_default_store: SkillStore | None = None


def get_default_skill_store() -> SkillStore:
    global _default_store
    project_root = Path.cwd().resolve()
    user_root = (Path.home() / ".claude" / "skills").resolve()
    if (
        _default_store is None
        or _default_store.project_root != project_root
        or _default_store.user_skills_dir != user_root
    ):
        _default_store = SkillStore(project_root=project_root, user_root=user_root)
    return _default_store


def discover_skills() -> list[SkillDefinition]:
    return get_default_skill_store().list()


def get_skill_by_name(name: str) -> SkillDefinition | None:
    return get_default_skill_store().get(name)


def execute_skill(skill_name: str, args: str) -> dict[str, Any] | None:
    return get_default_skill_store().render(skill_name, args)


def build_skill_descriptions() -> str:
    return get_default_skill_store().format_index()


def reset_skill_cache() -> None:
    if _default_store is not None:
        _default_store.invalidate()
    _invalidate_all_caches()
