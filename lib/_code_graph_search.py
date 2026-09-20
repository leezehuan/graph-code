"""Private FTS5, semantic, and hybrid ranking for ``code_graph``."""

from __future__ import annotations

import heapq
import math
import sqlite3
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from ._code_graph_embeddings import EmbeddingConfig


def drop_search_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS nodes_fts_ai;
        DROP TRIGGER IF EXISTS nodes_fts_ad;
        DROP TRIGGER IF EXISTS nodes_fts_au;
        DROP TABLE IF EXISTS nodes_fts;
        """
    )


def ensure_search_schema(conn: sqlite3.Connection) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
    ).fetchone() is not None
    try:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
                name, qualified_name, file_path, kind, parent_qualified,
                content='nodes', content_rowid='id',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS nodes_fts_ai AFTER INSERT ON nodes BEGIN
                INSERT INTO nodes_fts(
                    rowid, name, qualified_name, file_path, kind, parent_qualified
                ) VALUES (
                    new.id, new.name, new.qualified_name, new.file_path,
                    new.kind, new.parent_qualified
                );
            END;
            CREATE TRIGGER IF NOT EXISTS nodes_fts_ad AFTER DELETE ON nodes BEGIN
                INSERT INTO nodes_fts(
                    nodes_fts, rowid, name, qualified_name, file_path,
                    kind, parent_qualified
                ) VALUES (
                    'delete', old.id, old.name, old.qualified_name, old.file_path,
                    old.kind, old.parent_qualified
                );
            END;
            CREATE TRIGGER IF NOT EXISTS nodes_fts_au AFTER UPDATE ON nodes BEGIN
                INSERT INTO nodes_fts(
                    nodes_fts, rowid, name, qualified_name, file_path,
                    kind, parent_qualified
                ) VALUES (
                    'delete', old.id, old.name, old.qualified_name, old.file_path,
                    old.kind, old.parent_qualified
                );
                INSERT INTO nodes_fts(
                    rowid, name, qualified_name, file_path, kind, parent_qualified
                ) VALUES (
                    new.id, new.name, new.qualified_name, new.file_path,
                    new.kind, new.parent_qualified
                );
            END;
            """
        )
        if not exists:
            conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
        return True
    except sqlite3.OperationalError:
        drop_search_schema(conn)
        return False


def _fts_query(query: str) -> str:
    terms = [term for term in query.split() if term]
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _like_pattern(value: str, *, prefix: bool = False) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%" if prefix else f"%{escaped}%"


def _result(row: sqlite3.Row, score: float) -> dict[str, Any]:
    return {
        "kind": row["kind"],
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "file_path": row["file_path"],
        "line_start": row["line_start"],
        "line_end": row["line_end"],
        "is_test": bool(row["is_test"]),
        "score": float(score),
    }


def _ranking_boost(item: dict[str, Any], query: str, context: set[str]) -> float:
    name = item["name"].lower()
    needle = query.lower()
    if name == needle:
        boost = 3.0
    elif name.startswith(needle):
        boost = 2.0
    else:
        boost = 0.0
    if item["file_path"] in context:
        boost += 1.0
    return boost


def _keyword_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    kind: str | None,
    context: set[str],
) -> list[dict[str, Any]]:
    words = query.lower().split()
    conditions: list[str] = []
    params: list[Any] = []
    for word in words:
        conditions.append(
            "(lower(name) LIKE ? ESCAPE '\\' "
            "OR lower(qualified_name) LIKE ? ESCAPE '\\' "
            "OR lower(file_path) LIKE ? ESCAPE '\\')"
        )
        pattern = _like_pattern(word)
        params.extend([pattern, pattern, pattern])
    if kind is not None:
        conditions.append("kind=?")
        params.append(kind)
    params.extend([query.lower(), _like_pattern(query.lower(), prefix=True), limit * 3])
    rows = conn.execute(
        f"""SELECT * FROM nodes WHERE {' AND '.join(conditions)}
        ORDER BY CASE
          WHEN lower(name)=? THEN 0
          WHEN lower(name) LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END,
          qualified_name LIMIT ?""",
        params,
    ).fetchall()
    results = [_result(row, 1.0) for row in rows]
    for item in results:
        item["score"] += _ranking_boost(item, query, context)
    results.sort(key=lambda item: (-item["score"], item["qualified_name"]))
    return results[:limit]


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    *,
    kind: str | None = None,
    context_files: Iterable[str] = (),
    requested_mode: str = "fts",
) -> dict[str, Any]:
    context = {PurePosixPath(path).as_posix() for path in context_files}
    rows: list[sqlite3.Row] = []
    try:
        expression = _fts_query(query)
        if expression:
            params: list[Any] = [expression]
            kind_sql = ""
            if kind is not None:
                kind_sql = "AND n.kind=?"
                params.append(kind)
            params.extend([
                query.lower(), _like_pattern(query.lower(), prefix=True), limit * 3,
            ])
            rows = conn.execute(
                f"""SELECT n.*, bm25(nodes_fts) AS bm25_score
                FROM nodes_fts JOIN nodes n ON n.id=nodes_fts.rowid
                WHERE nodes_fts MATCH ? {kind_sql}
                ORDER BY CASE
                  WHEN lower(n.name)=? THEN 0
                  WHEN lower(n.name) LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END,
                  bm25_score, n.qualified_name LIMIT ?""",
                params,
            ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    if not rows:
        results = _keyword_search(conn, query, limit, kind, context)
        return {
            "requested_mode": requested_mode,
            "search_mode": "keyword",
            "results": results,
            "result_count": len(results),
        }

    results = [_result(row, max(0.0, -float(row["bm25_score"]))) for row in rows]
    for item in results:
        item["score"] += _ranking_boost(item, query, context)
    results.sort(key=lambda item: (-item["score"], item["qualified_name"]))
    results = results[:limit]
    return {
        "requested_mode": requested_mode,
        "search_mode": "fts",
        "results": results,
        "result_count": len(results),
    }


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return math.fsum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def semantic_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    *,
    config: EmbeddingConfig,
    kind: str | None = None,
    context_files: Iterable[str] = (),
) -> dict[str, Any]:
    from ._code_graph_embeddings import (
        embed_query,
        iter_cached_embeddings,
        refresh_embeddings,
    )

    context = {PurePosixPath(path).as_posix() for path in context_files}
    cache = refresh_embeddings(conn, config)
    query_vector = embed_query(config, query, cache.dimension)
    heap: list[tuple[float, int, sqlite3.Row]] = []
    for ordinal, (row, vector) in enumerate(
        iter_cached_embeddings(conn, cache.provider_identity, kind=kind)
    ):
        score = _cosine(query_vector, vector)
        item = _result(row, score)
        score += _ranking_boost(item, query, context)
        entry = (score, -ordinal, row)
        if len(heap) < limit:
            heapq.heappush(heap, entry)
        elif entry[:2] > heap[0][:2]:
            heapq.heapreplace(heap, entry)
    results = [_result(row, score) for score, _ordinal, row in heap]
    results.sort(key=lambda item: (-item["score"], item["qualified_name"]))
    return {
        "requested_mode": "semantic",
        "search_mode": "semantic",
        "results": results,
        "result_count": len(results),
        "embedding": cache.metadata(config.model),
    }


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    *,
    config: EmbeddingConfig,
    kind: str | None = None,
    context_files: Iterable[str] = (),
) -> dict[str, Any]:
    candidate_limit = min(300, max(limit * 3, limit))
    lexical = fts_search(
        conn, query, candidate_limit, kind=kind, context_files=(),
        requested_mode="hybrid",
    )
    semantic = semantic_search(
        conn, query, candidate_limit, config=config, kind=kind, context_files=(),
    )
    merged: dict[str, dict[str, Any]] = {}
    for candidates in (lexical["results"], semantic["results"]):
        for rank, item in enumerate(candidates, start=1):
            qualified_name = item["qualified_name"]
            if qualified_name not in merged:
                merged[qualified_name] = {**item, "score": 0.0}
            merged[qualified_name]["score"] += 1.0 / (60 + rank)
    context = {PurePosixPath(path).as_posix() for path in context_files}
    results = list(merged.values())
    for item in results:
        item["score"] += _ranking_boost(item, query, context)
    results.sort(key=lambda item: (-item["score"], item["qualified_name"]))
    results = results[:limit]
    return {
        "requested_mode": "hybrid",
        "search_mode": "hybrid",
        "results": results,
        "result_count": len(results),
        "embedding": semantic["embedding"],
    }


def search_nodes(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    *,
    mode: str = "fts",
    config: EmbeddingConfig | None = None,
    kind: str | None = None,
    context_files: Iterable[str] = (),
) -> dict[str, Any]:
    if mode == "semantic" and config is not None:
        return semantic_search(
            conn, query, limit, config=config, kind=kind,
            context_files=context_files,
        )
    if mode == "hybrid" and config is not None:
        return hybrid_search(
            conn, query, limit, config=config, kind=kind,
            context_files=context_files,
        )
    return fts_search(
        conn, query, limit, kind=kind, context_files=context_files,
        requested_mode=mode,
    )


def search_index_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        fts_nodes = int(conn.execute("SELECT count(*) FROM nodes_fts").fetchone()[0])
        available = True
    except sqlite3.OperationalError:
        fts_nodes = 0
        available = False
    embedding_sets: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            """SELECT provider, model, COUNT(*) AS nodes
            FROM node_embeddings GROUP BY provider_identity, provider, model
            ORDER BY provider, model"""
        ).fetchall()
        embedding_sets = [
            {"provider": row["provider"], "model": row["model"], "nodes": row["nodes"]}
            for row in rows
        ]
    except sqlite3.OperationalError:
        pass
    return {
        "fts_available": available,
        "fts_nodes": fts_nodes,
        "embedding_sets": embedding_sets,
    }
