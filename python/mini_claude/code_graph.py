"""Local, on-demand code graph used by the internal ``code_graph`` tool.

The design is a lightweight reimplementation of the core ideas from
code-review-graph (MIT, Copyright (c) 2026 Tirth Kanani). It intentionally
does not include that project's MCP server, daemon, embeddings, visualisation,
community detection, or framework-specific analysis.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import sqlite3
import subprocess
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_RESULTS = 20
MAX_RESULTS = 100

EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".mts": "typescript",
    ".cts": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
}

CLASS_TYPES = {
    "python": {"class_definition"},
    "javascript": {"class_declaration", "class"},
    "typescript": {
        "class_declaration", "class", "interface_declaration",
        "type_alias_declaration", "enum_declaration",
    },
    "tsx": {
        "class_declaration", "class", "interface_declaration",
        "type_alias_declaration", "enum_declaration",
    },
    "java": {"class_declaration", "interface_declaration", "enum_declaration"},
    "go": {"type_spec"},
    "rust": {"struct_item", "enum_item", "trait_item"},
    "c": {"struct_specifier", "type_definition"},
    "cpp": {"class_specifier", "struct_specifier"},
    "csharp": {
        "class_declaration", "interface_declaration", "enum_declaration",
        "struct_declaration", "record_declaration", "record_struct_declaration",
    },
}

TYPE_SCOPE_TYPES = {
    "rust": {"impl_item"},
}

FUNCTION_TYPES = {
    "python": {"function_definition"},
    "javascript": {"function_declaration", "method_definition", "arrow_function"},
    "typescript": {
        "function_declaration", "method_definition", "method_signature",
        "arrow_function",
    },
    "tsx": {
        "function_declaration", "method_definition", "method_signature",
        "arrow_function",
    },
    "java": {"method_declaration", "constructor_declaration"},
    "go": {"function_declaration", "method_declaration", "method_elem"},
    "rust": {"function_item", "function_signature_item"},
    "c": {"function_definition"},
    "cpp": {"function_definition"},
    "csharp": {"method_declaration", "constructor_declaration"},
}

IMPORT_TYPES = {
    "python": {"import_statement", "import_from_statement"},
    "javascript": {"import_statement"},
    "typescript": {"import_statement"},
    "tsx": {"import_statement"},
    "java": {"import_declaration"},
    "go": {"import_declaration", "import_spec"},
    "rust": {"use_declaration"},
    "c": {"preproc_include"},
    "cpp": {"preproc_include"},
    "csharp": {"using_directive"},
}

CALL_TYPES = {
    "python": {"call"},
    "javascript": {"call_expression", "new_expression"},
    "typescript": {"call_expression", "new_expression"},
    "tsx": {"call_expression", "new_expression"},
    "java": {"method_invocation", "object_creation_expression", "method_reference"},
    "go": {"call_expression"},
    "rust": {"call_expression", "macro_invocation"},
    "c": {"call_expression"},
    "cpp": {"call_expression"},
    "csharp": {"invocation_expression", "object_creation_expression"},
}

INHERITANCE_TYPES = {
    "superclasses", "superclass", "base_class_clause", "class_heritage",
    "extends_clause", "extends_type_clause", "implements_clause",
    "base_list", "trait_bounds", "type_parameters",
}

IDENTIFIER_TYPES = {
    "identifier", "type_identifier", "field_identifier", "property_identifier",
    "namespace_identifier",
}

IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "node_modules", "vendor",
    "dist", "build", "target", "coverage", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".mini-claude",
}

TEST_FILE_MARKERS = ("test_", "_test.", ".test.", ".spec.", "/tests/", "/__tests__/")


@dataclass(frozen=True)
class ParsedNode:
    kind: str
    name: str
    qualified_name: str
    parent_qualified: str
    line_start: int
    line_end: int
    is_test: bool


@dataclass(frozen=True)
class ParsedEdge:
    kind: str
    source_qualified: str
    target_name: str
    line: int


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=10, check=False,
    )


def _project_root() -> Path:
    cwd = Path.cwd().resolve()
    result = _run_git(cwd, "rev-parse", "--show-toplevel")
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return cwd


def _git_head(root: Path) -> str:
    result = _run_git(root, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else ""


def _cache_path(root: Path) -> Path:
    digest = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:16]
    directory = Path.home() / ".mini-claude" / "projects" / digest
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "code-graph.sqlite"


def _connect(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_cache_path(root), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _create_schema(conn)
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            language TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY,
            file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL UNIQUE,
            parent_qualified TEXT NOT NULL,
            line_start INTEGER NOT NULL,
            line_end INTEGER NOT NULL,
            is_test INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY,
            file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            source_qualified TEXT NOT NULL,
            target_name TEXT NOT NULL,
            target_qualified TEXT,
            line INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
        CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
        CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_qualified, kind);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_qualified, kind);
        """
    )
    version = conn.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    if version is not None and int(version[0]) != SCHEMA_VERSION:
        conn.executescript(
            "DELETE FROM edges; DELETE FROM nodes; DELETE FROM files; "
            "DELETE FROM metadata WHERE key='index_initialized';"
        )
    conn.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _source_files(root: Path) -> tuple[list[Path], int]:
    git_check = _run_git(root, "rev-parse", "--is-inside-work-tree")
    skipped = 0
    paths: list[Path] = []
    if git_check.returncode == 0:
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, timeout=20, check=False,
        )
        if result.returncode == 0:
            raw_paths = result.stdout.decode("utf-8", errors="replace").split("\0")
            candidates = (root / item for item in raw_paths if item)
        else:
            candidates = (path for path in root.rglob("*") if path.is_file())
    else:
        candidates = (path for path in root.rglob("*") if path.is_file())

    for path in candidates:
        try:
            rel = path.resolve().relative_to(root)
        except (OSError, ValueError):
            skipped += 1
            continue
        if any(part in IGNORED_DIRS for part in rel.parts[:-1]):
            skipped += 1
            continue
        if EXTENSIONS.get(path.suffix.lower()) is None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            skipped += 1
            continue
        if size > MAX_FILE_BYTES or not path.is_file() or path.is_symlink():
            skipped += 1
            continue
        try:
            with path.open("rb") as handle:
                if b"\x00" in handle.read(8192):
                    skipped += 1
                    continue
        except OSError:
            skipped += 1
            continue
        paths.append(path)
    return sorted(set(paths), key=lambda item: item.as_posix()), skipped


def _is_test_path(relative_path: str) -> bool:
    normalized = f"/{relative_path.lower()}"
    name = PurePosixPath(relative_path).name.lower()
    return (
        name.startswith("test_")
        or any(marker in normalized for marker in TEST_FILE_MARKERS)
        or name.endswith("test.java")
        or name.endswith("test.cs")
    )


def _node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _named_child(node: Any, *fields: str) -> Any | None:
    for field in fields:
        child = node.child_by_field_name(field)
        if child is not None:
            return child
    return None


def _find_identifier(node: Any) -> Any | None:
    direct = _named_child(node, "name", "declarator", "type")
    if direct is not None:
        if direct.type in IDENTIFIER_TYPES:
            return direct
        found = _find_identifier(direct)
        if found is not None:
            return found
    for child in node.named_children:
        if child.type in IDENTIFIER_TYPES:
            return child
    return None


def _arrow_binding_name(node: Any, source: bytes) -> str:
    parent = node.parent
    while parent is not None and parent.type not in {
        "variable_declarator", "lexical_declaration", "variable_declaration",
    }:
        parent = parent.parent
    if parent is None:
        return ""
    identifier = parent.child_by_field_name("name")
    return _node_text(identifier, source) if identifier is not None else ""


def _go_receiver_name(node: Any, source: bytes) -> str:
    if node.type != "method_declaration":
        return ""
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return ""
    stack = [receiver]
    names: list[str] = []
    while stack:
        current = stack.pop()
        if current.type in {"type_identifier", "qualified_type"}:
            names.append(_node_text(current, source).lstrip("*"))
        stack.extend(reversed(current.named_children))
    return names[-1] if names else ""


def _function_local_names(node: Any, source: bytes) -> frozenset[str]:
    names: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current is not node and current.type in set().union(*FUNCTION_TYPES.values()):
            continue
        if "parameter" in current.type:
            for child in current.named_children:
                identifier = (
                    child if child.type in IDENTIFIER_TYPES else _find_identifier(child)
                )
                if identifier is not None:
                    names.add(_node_text(identifier, source))
        elif current.type in {
            "variable_declarator", "assignment", "assignment_expression",
        }:
            identifier = _named_child(current, "name", "left")
            if identifier is not None:
                found = identifier if identifier.type in IDENTIFIER_TYPES else _find_identifier(identifier)
                if found is not None:
                    names.add(_node_text(found, source))
        stack.extend(reversed(current.named_children))
    return frozenset(names)


def _call_name(node: Any, source: bytes) -> str:
    candidate = _named_child(node, "function", "name", "constructor", "type")
    if candidate is None and node.named_children:
        candidate = node.named_children[0]
    if candidate is None:
        return ""
    text = _node_text(candidate, source).strip()
    for separator in ("::", ".", "->"):
        if separator in text:
            text = text.rsplit(separator, 1)[-1]
    return text.strip("!<>()[]{}&* ")


def _import_targets(node: Any, language: str, source: bytes) -> list[str]:
    text = _node_text(node, source).strip()
    strings: list[str] = []

    def walk(current: Any) -> None:
        if current.type in {
            "string", "string_literal", "interpreted_string_literal",
            "system_lib_string", "raw_string_literal",
        }:
            value = _node_text(current, source).strip("'\"<>` ")
            if value:
                strings.append(value)
        for child in current.named_children:
            walk(child)

    walk(node)
    if strings:
        return list(dict.fromkeys(strings))
    if language == "python":
        prefix = text.partition("import")[0].replace("from", "").strip()
        if prefix:
            return [prefix]
        return [part.strip().split(" as ")[0] for part in text[6:].split(",")]
    if language in {"java", "csharp"}:
        value = text.replace("import", "", 1).replace("using", "", 1).strip(" ;")
        return [value] if value else []
    if language == "rust":
        value = text.removeprefix("use").strip(" ;")
        return [value] if value else []
    return []


def _inheritance_names(node: Any, source: bytes) -> list[str]:
    names: list[str] = []
    superclasses = node.child_by_field_name("superclasses")
    superclass_span = (
        (superclasses.start_byte, superclasses.end_byte)
        if superclasses is not None else None
    )
    for child in node.named_children:
        if (
            (child.start_byte, child.end_byte) == superclass_span
            or child.type in INHERITANCE_TYPES
            or "heritage" in child.type
        ):
            stack = [child]
            while stack:
                current = stack.pop()
                if current.type in {"type_identifier", "identifier"}:
                    value = _node_text(current, source)
                    if value and value not in names:
                        names.append(value)
                stack.extend(reversed(current.named_children))
    return names


def _parse_file(root: Path, path: Path, source: bytes) -> tuple[list[ParsedNode], list[ParsedEdge]]:
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError as exc:
        raise RuntimeError(
            "code_graph requires tree-sitter-language-pack; reinstall Mini Claude dependencies"
        ) from exc

    relative_path = _relative(root, path)
    language = EXTENSIONS[path.suffix.lower()]
    parser = get_parser(language)
    tree = parser.parse(source)
    test_file = _is_test_path(relative_path)
    nodes = [
        ParsedNode("file", relative_path, relative_path, "", 1,
                   source.count(b"\n") + 1, test_file)
    ]
    edges: list[ParsedEdge] = []
    used_qualified_names = {relative_path}

    def unique_qualified_name(base: str, current: Any) -> str:
        if base not in used_qualified_names:
            used_qualified_names.add(base)
            return base
        line, column = current.start_point
        candidate = f"{base}@{line + 1}:{column + 1}"
        used_qualified_names.add(candidate)
        return candidate

    def walk(
        current: Any, parent_qn: str, class_qn: str, function_qn: str,
        local_names: frozenset[str],
    ) -> None:
        node_type = current.type
        next_parent, next_class, next_function = parent_qn, class_qn, function_qn
        if node_type in CLASS_TYPES.get(language, set()):
            identifier = _find_identifier(current)
            if identifier is not None:
                name = _node_text(identifier, source)
                scope = class_qn.rsplit("::", 1)[-1] if class_qn else ""
                local_name = f"{scope}.{name}" if scope else name
                qn = unique_qualified_name(f"{relative_path}::{local_name}", current)
                nodes.append(ParsedNode(
                    "class", name, qn, parent_qn, current.start_point[0] + 1,
                    current.end_point[0] + 1, test_file,
                ))
                edges.append(ParsedEdge("CONTAINS", parent_qn, qn, current.start_point[0] + 1))
                for base in _inheritance_names(current, source):
                    edges.append(ParsedEdge("INHERITS", qn, base, current.start_point[0] + 1))
                next_parent = next_class = qn
        elif node_type in TYPE_SCOPE_TYPES.get(language, set()):
            identifier = _named_child(current, "type")
            if identifier is not None:
                name = _node_text(identifier, source).lstrip("*&")
                matches = [node for node in nodes if node.name == name]
                qn = matches[0].qualified_name if len(matches) == 1 else f"{relative_path}::{name}"
                next_parent = next_class = qn
        elif node_type in FUNCTION_TYPES.get(language, set()):
            identifier = _find_identifier(current)
            name = _node_text(identifier, source) if identifier is not None else ""
            if node_type == "arrow_function":
                name = _arrow_binding_name(current, source) or name
            if name:
                receiver = _go_receiver_name(current, source) if language == "go" else ""
                if receiver:
                    local_name = f"{receiver}.{name}"
                elif class_qn:
                    class_scope = class_qn.rsplit("::", 1)[-1]
                    local_name = f"{class_scope}.{name}"
                elif function_qn:
                    function_scope = function_qn.rsplit("::", 1)[-1]
                    local_name = f"{function_scope}.{name}"
                else:
                    local_name = name
                qn = unique_qualified_name(f"{relative_path}::{local_name}", current)
                is_test = test_file or name.lower().startswith("test")
                nodes.append(ParsedNode(
                    "function", name, qn, parent_qn, current.start_point[0] + 1,
                    current.end_point[0] + 1, is_test,
                ))
                edges.append(ParsedEdge("CONTAINS", parent_qn, qn, current.start_point[0] + 1))
                next_parent = next_function = qn
                local_names = _function_local_names(current, source)
        elif node_type in IMPORT_TYPES.get(language, set()):
            for target in _import_targets(current, language, source):
                edges.append(ParsedEdge("IMPORTS", relative_path, target, current.start_point[0] + 1))
            return
        elif node_type in CALL_TYPES.get(language, set()):
            target = _call_name(current, source)
            if target:
                caller = function_qn or class_qn or relative_path
                edges.append(ParsedEdge("CALLS", caller, target, current.start_point[0] + 1))
        elif (
            node_type in IDENTIFIER_TYPES
            and function_qn
            and _node_text(current, source) not in local_names
        ):
            parent_type = current.parent.type if current.parent is not None else ""
            excluded = (
                CLASS_TYPES.get(language, set())
                | FUNCTION_TYPES.get(language, set())
                | IMPORT_TYPES.get(language, set())
                | CALL_TYPES.get(language, set())
            )
            if parent_type not in excluded:
                edges.append(ParsedEdge(
                    "REFERENCES", function_qn, _node_text(current, source),
                    current.start_point[0] + 1,
                ))

        for child in current.named_children:
            walk(child, next_parent, next_class, next_function, local_names)

    walk(tree.root_node, relative_path, "", "", frozenset())
    return nodes, edges


def _hash(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()


def _resolve_import(root: Path, source_file: str, target: str, files: set[str]) -> str | None:
    source_dir = PurePosixPath(source_file).parent
    normalized = target.replace("\\", "/").strip("./")
    candidates: list[str] = []
    if target.startswith("."):
        base = posixpath.normpath(posixpath.join(source_dir.as_posix(), target))
        candidates.extend([base, f"{base}.py", f"{base}.js", f"{base}.ts", f"{base}.tsx"])
        candidates.extend([f"{base}/index.js", f"{base}/index.ts", f"{base}/__init__.py"])
    dotted = normalized.replace(".", "/")
    candidates.extend([normalized, dotted])
    for stem in (normalized, dotted):
        candidates.extend(f"{stem}{suffix}" for suffix in EXTENSIONS)
        candidates.extend(f"{stem}/{name}" for name in ("__init__.py", "index.js", "index.ts"))
    target_leaf = normalized.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    for candidate in candidates:
        canonical = PurePosixPath(candidate).as_posix()
        if canonical in files:
            return canonical
    leaf_matches = [path for path in files if PurePosixPath(path).stem == target_leaf]
    return leaf_matches[0] if len(leaf_matches) == 1 else None


def _resolve_edges(conn: sqlite3.Connection, root: Path) -> None:
    rows = conn.execute("SELECT qualified_name, name, file_path, parent_qualified FROM nodes").fetchall()
    by_name: dict[str, list[sqlite3.Row]] = {}
    by_qn = {row["qualified_name"]: row for row in rows}
    files = {row["qualified_name"] for row in rows if "::" not in row["qualified_name"]}
    for row in rows:
        by_name.setdefault(row["name"], []).append(row)

    edges = conn.execute(
        "SELECT id, kind, source_qualified, target_name, file_path FROM edges"
    ).fetchall()
    for edge in edges:
        target: str | None = None
        raw = edge["target_name"]
        if raw in by_qn:
            target = raw
        elif edge["kind"] == "IMPORTS":
            target = _resolve_import(root, edge["file_path"], raw, files)
        else:
            candidates = by_name.get(raw, [])
            source_file = edge["file_path"]
            local = [item for item in candidates if item["file_path"] == source_file]
            source = by_qn.get(edge["source_qualified"])
            if source is not None and source["parent_qualified"]:
                scoped = [
                    item for item in local
                    if item["parent_qualified"] == source["parent_qualified"]
                ]
                if len(scoped) == 1:
                    target = scoped[0]["qualified_name"]
            if target is None and len(local) == 1:
                target = local[0]["qualified_name"]
            elif target is None and len(candidates) == 1:
                target = candidates[0]["qualified_name"]
        conn.execute("UPDATE edges SET target_qualified=? WHERE id=?", (target, edge["id"]))


def _refresh(conn: sqlite3.Connection, root: Path) -> dict[str, Any]:
    paths, skipped = _source_files(root)
    current = {_relative(root, path): path for path in paths}
    stored_rows = conn.execute(
        "SELECT path, size, mtime_ns, content_hash FROM files"
    ).fetchall()
    stored = {row["path"]: row for row in stored_rows}
    deleted = sorted(set(stored) - set(current))
    changed: list[tuple[str, Path, bytes, os.stat_result]] = []

    for relative_path, path in current.items():
        try:
            stat = path.stat()
        except OSError:
            skipped += 1
            continue
        old = stored.get(relative_path)
        source = path.read_bytes()
        digest = _hash(source)
        if old is not None and old["content_hash"] == digest:
            conn.execute(
                "UPDATE files SET size=?, mtime_ns=? WHERE path=?",
                (stat.st_size, stat.st_mtime_ns, relative_path),
            )
            continue
        changed.append((relative_path, path, source, stat))

    affected = deleted + [relative_path for relative_path, *_ in changed]
    previous_snapshot: dict[str, Any] = {"nodes": [], "incoming": []}
    head = _git_head(root)
    saved = conn.execute(
        "SELECT value FROM metadata WHERE key='impact_snapshot'"
    ).fetchone()
    if saved is not None:
        try:
            payload = json.loads(saved[0])
            if payload.get("head") == head:
                previous_snapshot = payload.get("snapshot", previous_snapshot)
        except (TypeError, ValueError):
            pass
    if affected:
        placeholders = ",".join("?" for _ in affected)
        old_nodes = conn.execute(
            f"SELECT * FROM nodes WHERE file_path IN ({placeholders})", affected
        ).fetchall()
        old_names = [row["qualified_name"] for row in old_nodes]
        previous_snapshot["nodes"].extend(dict(row) for row in old_nodes)
        if old_names:
            node_placeholders = ",".join("?" for _ in old_names)
            incoming = conn.execute(
                f"SELECT * FROM edges WHERE target_qualified IN ({node_placeholders})",
                old_names,
            ).fetchall()
            previous_snapshot["incoming"].extend(dict(row) for row in incoming)

    previous_snapshot["nodes"] = list({
        row["qualified_name"]: row for row in previous_snapshot["nodes"]
    }.values())
    previous_snapshot["incoming"] = list({
        (row["source_qualified"], row["target_qualified"], row["kind"], row["line"]): row
        for row in previous_snapshot["incoming"]
    }.values())

    initialized = conn.execute(
        "SELECT value FROM metadata WHERE key='index_initialized'"
    ).fetchone() is not None
    initial = not initialized
    with conn:
        for relative_path in deleted:
            conn.execute("DELETE FROM files WHERE path=?", (relative_path,))
        for relative_path, path, source, stat in changed:
            nodes, edges = _parse_file(root, path, source)
            conn.execute("DELETE FROM files WHERE path=?", (relative_path,))
            conn.execute(
                "INSERT INTO files(path, language, size, mtime_ns, content_hash) VALUES(?, ?, ?, ?, ?)",
                (relative_path, EXTENSIONS[path.suffix.lower()], stat.st_size,
                 stat.st_mtime_ns, _hash(source)),
            )
            conn.executemany(
                """INSERT INTO nodes(
                    file_path, kind, name, qualified_name, parent_qualified,
                    line_start, line_end, is_test
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (relative_path, node.kind, node.name, node.qualified_name,
                     node.parent_qualified, node.line_start, node.line_end,
                     int(node.is_test))
                    for node in nodes
                ],
            )
            conn.executemany(
                """INSERT INTO edges(
                    file_path, kind, source_qualified, target_name, target_qualified, line
                ) VALUES(?, ?, ?, ?, NULL, ?)""",
                [
                    (relative_path, edge.kind, edge.source_qualified,
                     edge.target_name, edge.line)
                    for edge in edges
                ],
            )
        if changed or deleted:
            _resolve_edges(conn, root)
        if initial:
            conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('index_initialized', '1')"
            )
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('impact_snapshot', ?)",
            (json.dumps({"head": head, "snapshot": previous_snapshot}),),
        )

    mode = "full" if initial else ("incremental" if changed or deleted else "unchanged")
    return {
        "mode": mode,
        "updated_files": len(changed),
        "deleted_files": len(deleted),
        "skipped_files": skipped,
        "_previous_snapshot": previous_snapshot,
    }


def _node_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "kind": row["kind"],
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "file_path": row["file_path"],
        "line_start": row["line_start"],
        "line_end": row["line_end"],
        "is_test": bool(row["is_test"]),
    }


def _error(action: Any, code: str, message: str, **details: Any) -> dict[str, Any]:
    error = {"code": code, "message": message}
    error.update(details)
    return {"ok": False, "action": action, "error": error}


def _validate(inp: dict[str, Any]) -> tuple[str, int] | dict[str, Any]:
    action = inp.get("action")
    if action not in {"search", "query", "impact", "overview"}:
        return _error(action, "invalid_action", "action must be search, query, impact, or overview")
    value = inp.get("max_results", DEFAULT_MAX_RESULTS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RESULTS:
        return _error(action, "invalid_max_results", "max_results must be an integer from 1 to 100")
    if action == "search" and not str(inp.get("query", "")).strip():
        return _error(action, "missing_query", "query is required for search")
    if action == "query" and (
        not str(inp.get("target", "")).strip()
        or inp.get("relation") not in {
            "callers_of", "callees_of", "importers_of", "tests_for",
            "children_of", "inheritors_of", "references_to",
        }
    ):
        return _error(action, "invalid_query", "target and a supported relation are required")
    changed_files = inp.get("changed_files")
    if changed_files is not None and (
        not isinstance(changed_files, list)
        or not all(isinstance(item, str) for item in changed_files)
    ):
        return _error(action, "invalid_changed_files", "changed_files must be a list of paths")
    return action, value


def _search(conn: sqlite3.Connection, text: str, limit: int) -> dict[str, Any]:
    needle = text.strip().lower()
    rows = conn.execute(
        """SELECT * FROM nodes
        WHERE lower(name) LIKE ? OR lower(qualified_name) LIKE ? OR lower(file_path) LIKE ?
        ORDER BY
          CASE WHEN lower(name)=? THEN 0 WHEN lower(name) LIKE ? THEN 1 ELSE 2 END,
          qualified_name
        LIMIT ?""",
        (f"%{needle}%", f"%{needle}%", f"%{needle}%", needle, f"{needle}%", limit),
    ).fetchall()
    return {"results": [_node_dict(row) for row in rows], "result_count": len(rows)}


def _resolve_target(
    conn: sqlite3.Connection, target: str,
) -> sqlite3.Row | dict[str, Any]:
    exact = conn.execute(
        "SELECT * FROM nodes WHERE qualified_name=?", (target,)
    ).fetchone()
    if exact is not None:
        return exact
    candidates = conn.execute(
        "SELECT * FROM nodes WHERE name=? ORDER BY qualified_name", (target,)
    ).fetchall()
    if not candidates:
        return _error("query", "target_not_found", f"No code node matches '{target}'")
    if len(candidates) > 1:
        return _error(
            "query", "ambiguous_target",
            f"'{target}' matches multiple code nodes; use a qualified name",
            candidates=[row["qualified_name"] for row in candidates],
        )
    return candidates[0]


def _rows_for_qualified_names(
    conn: sqlite3.Connection, names: Iterable[str], limit: int,
) -> list[sqlite3.Row]:
    unique = list(dict.fromkeys(name for name in names if name))[:limit]
    if not unique:
        return []
    placeholders = ",".join("?" for _ in unique)
    rows = conn.execute(
        f"SELECT * FROM nodes WHERE qualified_name IN ({placeholders})", unique
    ).fetchall()
    by_name = {row["qualified_name"]: row for row in rows}
    return [by_name[name] for name in unique if name in by_name]


def _query(
    conn: sqlite3.Connection, relation: str, target: str, limit: int,
) -> dict[str, Any]:
    resolved = _resolve_target(conn, target)
    if isinstance(resolved, dict):
        return resolved
    qualified = resolved["qualified_name"]
    edge_kinds: tuple[str, ...]
    names: list[str]

    if relation == "children_of":
        rows = conn.execute(
            "SELECT * FROM nodes WHERE parent_qualified=? ORDER BY qualified_name LIMIT ?",
            (qualified, limit),
        ).fetchall()
        return {"results": [_node_dict(row) for row in rows], "result_count": len(rows)}

    if relation == "callers_of":
        edge_kinds = ("CALLS",)
        edges = conn.execute(
            "SELECT DISTINCT source_qualified FROM edges WHERE target_qualified=? AND kind='CALLS' "
            "ORDER BY source_qualified LIMIT ?", (qualified, limit),
        ).fetchall()
        names = [row[0] for row in edges]
    elif relation == "callees_of":
        edge_kinds = ("CALLS",)
        edges = conn.execute(
            "SELECT DISTINCT target_qualified FROM edges WHERE source_qualified=? AND kind='CALLS' "
            "AND target_qualified IS NOT NULL ORDER BY target_qualified LIMIT ?",
            (qualified, limit),
        ).fetchall()
        names = [row[0] for row in edges]
    elif relation == "importers_of":
        edge_kinds = ("IMPORTS",)
        edges = conn.execute(
            "SELECT DISTINCT source_qualified FROM edges WHERE target_qualified=? AND kind='IMPORTS' "
            "ORDER BY source_qualified LIMIT ?", (qualified, limit),
        ).fetchall()
        names = [row[0] for row in edges]
    elif relation == "inheritors_of":
        edge_kinds = ("INHERITS",)
        edges = conn.execute(
            "SELECT DISTINCT source_qualified FROM edges WHERE target_qualified=? AND kind='INHERITS' "
            "ORDER BY source_qualified LIMIT ?", (qualified, limit),
        ).fetchall()
        names = [row[0] for row in edges]
    elif relation == "references_to":
        edge_kinds = ("REFERENCES",)
        edges = conn.execute(
            "SELECT DISTINCT source_qualified FROM edges WHERE target_qualified=? AND kind='REFERENCES' "
            "ORDER BY source_qualified LIMIT ?", (qualified, limit),
        ).fetchall()
        names = [row[0] for row in edges]
    else:
        edge_kinds = ("CALLS", "REFERENCES")
        edges = conn.execute(
            "SELECT DISTINCT e.source_qualified FROM edges e "
            "JOIN nodes source ON source.qualified_name=e.source_qualified "
            "WHERE e.target_qualified=? AND e.kind IN ('CALLS', 'REFERENCES') "
            "AND source.is_test=1 ORDER BY e.source_qualified LIMIT ?",
            (qualified, limit),
        ).fetchall()
        names = [row[0] for row in edges]

    rows = _rows_for_qualified_names(conn, names, limit)
    return {
        "results": [_node_dict(row) for row in rows],
        "result_count": len(rows),
        "edge_kinds": list(edge_kinds),
    }


def _overview(conn: sqlite3.Connection, limit: int) -> dict[str, Any]:
    language_rows = conn.execute(
        "SELECT language, COUNT(*) AS count FROM files GROUP BY language ORDER BY language"
    ).fetchall()
    languages = {row["language"]: row["count"] for row in language_rows}
    files = [row[0] for row in conn.execute("SELECT path FROM files ORDER BY path")]
    top_level = Counter(
        PurePosixPath(path).parts[0]
        for path in files
        if len(PurePosixPath(path).parts) > 1
    )
    node_counts = {
        row["kind"]: row["count"]
        for row in conn.execute(
            "SELECT kind, COUNT(*) AS count FROM nodes GROUP BY kind ORDER BY kind"
        )
    }
    edge_counts = {
        row["kind"]: row["count"]
        for row in conn.execute(
            "SELECT kind, COUNT(*) AS count FROM edges GROUP BY kind ORDER BY kind"
        )
    }
    hubs = conn.execute(
        """SELECT n.*, COUNT(e.id) AS indegree
        FROM nodes n JOIN edges e ON e.target_qualified=n.qualified_name
        WHERE e.kind != 'CONTAINS'
        GROUP BY n.id ORDER BY indegree DESC, n.qualified_name LIMIT ?""",
        (limit,),
    ).fetchall()
    return {
        "languages": languages,
        "top_level_directories": dict(sorted(top_level.items())),
        "nodes": node_counts,
        "edges": edge_counts,
        "test_symbols": conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE is_test=1 AND kind!='file'"
        ).fetchone()[0],
        "high_indegree": [
            {**_node_dict(row), "indegree": row["indegree"]} for row in hubs
        ],
    }


def _normalize_changed_files(root: Path, values: Iterable[str]) -> list[str] | dict[str, Any]:
    normalized: list[str] = []
    for value in values:
        raw = Path(value)
        candidate = raw if raw.is_absolute() else root / raw
        try:
            resolved = candidate.resolve(strict=False)
            relative = resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            return _error("impact", "invalid_path", f"Path is outside the project: {value}")
        if relative not in normalized:
            normalized.append(relative)
    return sorted(normalized)


def _git_changed_files(root: Path) -> list[str]:
    result = _run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if result.returncode != 0:
        return []
    entries = result.stdout.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        record_paths = [path]
        if "R" in status or "C" in status:
            if index < len(entries) and entries[index]:
                record_paths.append(entries[index])
                index += 1
        for record_path in record_paths:
            normalized = PurePosixPath(record_path.replace("\\", "/")).as_posix()
            if (
                normalized
                and PurePosixPath(normalized).suffix.lower() in EXTENSIONS
                and normalized not in paths
            ):
                paths.append(normalized)
    return sorted(paths)


def _impact(
    conn: sqlite3.Connection,
    root: Path,
    changed_values: list[str] | None,
    limit: int,
    previous_snapshot: dict[str, Any],
) -> dict[str, Any]:
    if changed_values is None:
        changed_files = _git_changed_files(root)
    else:
        validated = _normalize_changed_files(root, changed_values)
        if isinstance(validated, dict):
            return validated
        changed_files = validated

    changed_nodes = conn.execute(
        f"SELECT * FROM nodes WHERE file_path IN ({','.join('?' for _ in changed_files)})"
        if changed_files else "SELECT * FROM nodes WHERE 0",
        changed_files,
    ).fetchall()
    previous_nodes = [
        row for row in previous_snapshot.get("nodes", [])
        if row["file_path"] in changed_files
    ]
    seeds = {row["qualified_name"] for row in changed_nodes}
    seeds.update(row["qualified_name"] for row in previous_nodes)

    incoming_by_target: dict[str, list[tuple[str, str]]] = {}
    for edge in conn.execute(
        "SELECT source_qualified, target_qualified, kind FROM edges "
        "WHERE target_qualified IS NOT NULL AND kind IN "
        "('CALLS', 'IMPORTS', 'INHERITS', 'REFERENCES')"
    ):
        incoming_by_target.setdefault(edge["target_qualified"], []).append(
            (edge["source_qualified"], edge["kind"])
        )
    for edge in previous_snapshot.get("incoming", []):
        incoming_by_target.setdefault(edge["target_qualified"], []).append(
            (edge["source_qualified"], edge["kind"])
        )

    queue = deque((seed, 0) for seed in sorted(seeds))
    visited = set(seeds)
    impacted: list[dict[str, Any]] = []
    while queue and len(impacted) < limit:
        current, depth = queue.popleft()
        if depth >= 2:
            continue
        for source, relation in sorted(incoming_by_target.get(current, [])):
            if source in visited:
                continue
            visited.add(source)
            row = conn.execute(
                "SELECT * FROM nodes WHERE qualified_name=?", (source,)
            ).fetchone()
            if row is None:
                continue
            impacted.append({
                **_node_dict(row),
                "depth": depth + 1,
                "via_relation": relation,
                "via_target": current,
            })
            queue.append((source, depth + 1))
            if len(impacted) >= limit:
                break

    current_names = {row["qualified_name"] for row in changed_nodes}
    removed_nodes = [
        row for row in previous_nodes if row["qualified_name"] not in current_names
    ]
    return {
        "changed_files": changed_files,
        "changed_nodes": [
            _node_dict(row) for row in changed_nodes
        ] + [
            {
                "kind": row["kind"], "name": row["name"],
                "qualified_name": row["qualified_name"], "file_path": row["file_path"],
                "line_start": row["line_start"], "line_end": row["line_end"],
                "is_test": bool(row["is_test"]), "deleted": True,
            }
            for row in removed_nodes
        ],
        "impacted": impacted,
        "result_count": len(impacted),
    }


def _execute_sync(inp: dict[str, Any]) -> str:
    validated = _validate(inp)
    if isinstance(validated, dict):
        return json.dumps(validated, ensure_ascii=False)
    action, limit = validated
    root = _project_root()
    try:
        conn = _connect(root)
        try:
            index = _refresh(conn, root)
            previous_snapshot = index.pop("_previous_snapshot")
            if action == "search":
                data = _search(conn, str(inp["query"]), limit)
                summary = f"Found {data['result_count']} matching code node(s)."
            elif action == "query":
                data = _query(conn, str(inp["relation"]), str(inp["target"]), limit)
                if data.get("ok") is False:
                    data["index"] = index
                    return json.dumps(data, ensure_ascii=False)
                summary = f"Found {data['result_count']} related code node(s)."
            elif action == "overview":
                data = _overview(conn, limit)
                summary = (
                    f"Indexed {sum(data['languages'].values())} source file(s) "
                    f"across {len(data['languages'])} language(s)."
                )
            elif action == "impact":
                data = _impact(
                    conn, root, inp.get("changed_files"), limit, previous_snapshot
                )
                if data.get("ok") is False:
                    data["index"] = index
                    return json.dumps(data, ensure_ascii=False)
                summary = (
                    f"Found {data['result_count']} impacted code node(s) "
                    f"within two dependency hops."
                )
            return json.dumps(
                {"ok": True, "action": action, "summary": summary,
                 "index": index, "data": data},
                ensure_ascii=False,
            )
        finally:
            conn.close()
    except Exception as exc:
        return json.dumps(
            _error(action, "graph_error", str(exc)), ensure_ascii=False,
        )


async def execute_code_graph(inp: dict[str, Any]) -> str:
    """Execute one graph action without blocking the agent event loop."""
    return await asyncio.to_thread(_execute_sync, inp)
