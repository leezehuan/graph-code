"""Route personal memories to PostgreSQL and project memories to JSON files."""
import asyncio
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

TYPES = {"semantic", "procedural", "episodic", "project"}
SCOPES = {"user", "project"}
MEMORY_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class MemoryRepository:
    def __init__(self, store, project_root):
        self.store = store
        self.project_root = Path(project_root).resolve()
        self.project_memory_dir = self.project_root / ".langcode" / "memories"
        identity = os.path.normcase(str(Path(project_root).resolve()))
        self.project_id = hashlib.sha256(identity.encode()).hexdigest()[:16]

    def _project_path(self, memory_id):
        if not isinstance(memory_id, str) or not MEMORY_ID.fullmatch(memory_id):
            raise ValueError("Invalid memory ID")
        root = self.project_memory_dir.resolve()
        if not root.is_relative_to(self.project_root):
            raise ValueError("Project memory directory escapes project root")
        path = (root / f"{memory_id}.json").resolve()
        if path.parent != root:
            raise ValueError("Memory path escapes project memory directory")
        return path

    def _project_lock(self):
        lexical = self.project_memory_dir.absolute()
        if not lexical.is_relative_to(self.project_root):
            raise ValueError("Project memory directory escapes project root")
        langcode_dir = (self.project_root / ".langcode").resolve()
        if not langcode_dir.is_relative_to(self.project_root):
            raise ValueError("Project memory directory escapes project root")
        if self.project_memory_dir.exists() and not self.project_memory_dir.resolve().is_relative_to(self.project_root):
            raise ValueError("Project memory directory escapes project root")
        self.project_memory_dir.mkdir(parents=True, exist_ok=True)
        root = self.project_memory_dir.resolve()
        if not root.is_relative_to(self.project_root):
            raise ValueError("Project memory directory escapes project root")
        lock_path = self.project_memory_dir / ".lock"
        if lock_path.exists() and lock_path.resolve().parent != root:
            raise ValueError("Project memory lock escapes project memory directory")
        return FileLock(str(lock_path), timeout=10)

    def _read_project_records(self):
        self.project_memory_dir.mkdir(parents=True, exist_ok=True)
        records = []
        for path in self.project_memory_dir.glob("*.json"):
            try:
                resolved = path.resolve()
                if resolved.parent != self.project_memory_dir.resolve():
                    continue
                record = json.loads(resolved.read_text(encoding="utf-8"))
                memory_id = resolved.stem
                if (not isinstance(record, dict) or record.get("id") != memory_id or
                        record.get("scope") != "project" or record.get("type") not in {"procedural", "episodic", "project"}):
                    continue
                records.append(record)
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
        return records

    def _read_project_record(self, memory_id):
        path = self._project_path(memory_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("Memory not found in current scope") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Memory file is invalid") from exc
        if not isinstance(record, dict) or record.get("id") != memory_id or record.get("scope") != "project":
            raise ValueError("Memory file is invalid")
        return record

    def _write_project_record(self, record):
        self.project_memory_dir.mkdir(parents=True, exist_ok=True)
        path = self._project_path(record["id"])
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.project_memory_dir,
                                             prefix=f".{record['id']}.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(record, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def _project_list(self, kind=None):
        return await asyncio.to_thread(self._list_project_records, kind)

    def _list_project_records(self, kind=None):
        with self._project_lock():
            return self._read_project_records_filtered(kind)

    def _read_project_records_filtered(self, kind=None):
        records = self._read_project_records()
        if kind is not None:
            records = [record for record in records if record.get("type") == kind]
        return records

    async def _project_get(self, memory_id):
        return await asyncio.to_thread(self._get_project_record, memory_id)

    def _get_project_record(self, memory_id):
        with self._project_lock():
            return self._read_project_record(memory_id)

    async def _project_save(self, record):
        await asyncio.to_thread(self._save_project_record, record)

    def _save_project_record(self, record):
        with self._project_lock():
            self._write_project_record(record)

    async def _project_delete(self, memory_id):
        await asyncio.to_thread(self._delete_project_record, memory_id)

    def _delete_project_record(self, memory_id):
        with self._project_lock():
            path = self._project_path(memory_id)
            self._read_project_record(memory_id)
            path.unlink()

    def namespace(self, scope, user_id=None):
        if scope not in SCOPES:
            raise ValueError("Unknown memory scope")
        if scope == "user" and not user_id:
            raise ValueError("Personal memory requires user_id")
        return ("memories_v2", scope, str(user_id) if scope == "user" else self.project_id)

    async def list(self, user_id=None, scope=None, kind=None):
        if kind is not None and kind not in TYPES:
            raise ValueError("Unknown memory type")
        scopes = [scope] if scope is not None else (["user", "project"] if user_id else ["project"])
        records = []
        for current in scopes:
            if current == "project":
                records.extend(await self._project_list(kind))
                continue
            namespace = self.namespace(current, user_id)
            offset = 0
            while True:
                page = await self.store.asearch(namespace, limit=100, offset=offset)
                for item in page:
                    if item.namespace == namespace and (kind is None or item.value.get("type") == kind):
                        records.append(dict(item.value, id=item.key, scope=current))
                if len(page) < 100:
                    break
                offset += len(page)
        return sorted(records, key=lambda r: (r.get("updated_at", ""), r["id"]), reverse=True)

    async def candidates(self, user_id=None):
        records = []
        for scope in (["user", "project"] if user_id else ["project"]):
            records.extend((await self.list(user_id, scope))[:100])
        return {str(i): r for i, r in enumerate(records, 1)}

    async def get(self, scope, memory_id, user_id=None):
        if scope == "project":
            item = await self._project_get(memory_id)
            return dict(item, id=memory_id, scope=scope)
        item = await self.store.aget(self.namespace(scope, user_id), memory_id)
        if item is None:
            raise ValueError("Memory not found in current scope")
        return dict(item.value, id=item.key, scope=scope)

    async def save(self, kind, scope, description, content, source, user_id=None, memory_id=None):
        if kind not in TYPES or scope not in SCOPES:
            raise ValueError("Unknown memory type or scope")
        if (kind == "semantic" and scope != "user") or (kind in {"project", "episodic"} and scope != "project"):
            raise ValueError("Memory type does not allow this scope")
        for value, maximum in ((description, 500), (content, 16000), (source, 2000)):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError("Memory text is empty, invalid, or too long")
        namespace = self.namespace(scope, user_id)
        previous = await self.get(scope, memory_id, user_id) if memory_id else None
        if previous and previous["type"] != kind:
            raise ValueError("Cannot change memory type")
        now = datetime.now(timezone.utc).isoformat()
        record = {"id": memory_id or str(uuid4()), "type": kind, "scope": scope,
                  "description": description.strip(), "content": content.strip(), "source": source,
                  "created_at": previous["created_at"] if previous else now, "updated_at": now}
        if scope == "project":
            await self._project_save(record)
        else:
            await self.store.aput(namespace, record["id"], record)
        return record

    async def delete(self, scope, memory_id, user_id=None):
        await self.get(scope, memory_id, user_id)
        if scope == "project":
            await self._project_delete(memory_id)
        else:
            await self.store.adelete(self.namespace(scope, user_id), memory_id)


def user_identity(config):
    value = config.get("configurable", {}).get("user_id")
    return str(value).strip() if value is not None and str(value).strip() else None


def latest_user_text(messages):
    return next((m.text for m in reversed(messages) if m.type == "human"), "")


def memory_enabled(config, messages):
    return config.get("configurable", {}).get("memory_enabled", True) is not False and not any(
        word in latest_user_text(messages) for word in ("不要使用记忆", "禁用记忆", "停止记忆")
    )
