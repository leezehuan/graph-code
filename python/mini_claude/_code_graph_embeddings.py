"""Private OpenAI-compatible embedding implementation for ``code_graph``."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from ._dotenv import dotenv_values

PROVIDER = "openai-compatible"
MAX_BATCH_SIZE = 64
MAX_EMBEDDING_TEXT = 4096
REQUEST_TIMEOUT_SECONDS = 60


class EmbeddingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    api_key: str

    @property
    def identity(self) -> str:
        endpoint_hash = hashlib.sha256(self.base_url.encode("utf-8")).hexdigest()[:16]
        return f"{PROVIDER}:{endpoint_hash}:{self.model}"


@dataclass(frozen=True)
class EmbeddingCache:
    provider_identity: str
    dimension: int
    cache_hits: int
    updated_nodes: int

    def metadata(self, model: str) -> dict[str, Any]:
        return {
            "provider": PROVIDER,
            "model": model,
            "cache_hits": self.cache_hits,
            "updated_nodes": self.updated_nodes,
        }


def load_embedding_config(project_root: Path | None = None) -> EmbeddingConfig:
    local_environment = dotenv_values(project_root)

    def configured_value(name: str) -> str:
        return os.environ.get(name, local_environment.get(name, ""))

    if configured_value("MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS") != "1":
        raise EmbeddingError(
            "cloud_egress_not_accepted",
            "Set MINI_CLAUDE_ACCEPT_CLOUD_EMBEDDINGS=1 to allow cloud embeddings",
        )
    base_url = configured_value("MINI_CLAUDE_EMBEDDING_BASE_URL").strip()
    model = configured_value("MINI_CLAUDE_EMBEDDING_MODEL").strip()
    missing = []
    if not base_url:
        missing.append("MINI_CLAUDE_EMBEDDING_BASE_URL")
    if not model:
        missing.append("MINI_CLAUDE_EMBEDDING_MODEL")
    if missing:
        raise EmbeddingError(
            "provider_unavailable",
            f"Missing embedding configuration: {', '.join(missing)}",
        )
    return EmbeddingConfig(
        base_url=base_url.rstrip("/"),
        model=model,
        api_key=configured_value("MINI_CLAUDE_EMBEDDING_API_KEY"),
    )


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS node_embeddings (
            qualified_name TEXT NOT NULL,
            provider_identity TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            vector BLOB NOT NULL,
            dimension INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            PRIMARY KEY (qualified_name, provider_identity),
            FOREIGN KEY (qualified_name) REFERENCES nodes(qualified_name)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_node_embeddings_provider
            ON node_embeddings(provider_identity, qualified_name);
        CREATE TRIGGER IF NOT EXISTS node_embeddings_ad AFTER DELETE ON nodes BEGIN
            DELETE FROM node_embeddings WHERE qualified_name=old.qualified_name;
        END;
        """
    )
    conn.execute(
        """DELETE FROM node_embeddings
        WHERE NOT EXISTS (
            SELECT 1 FROM nodes
            WHERE nodes.qualified_name=node_embeddings.qualified_name
        )"""
    )
    conn.commit()


def _split_identifier(value: str) -> str:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return " ".join(part for part in re.split(r"[^A-Za-z0-9]+", separated) if part)


def _embedding_text(row: sqlite3.Row) -> str:
    values = [
        f"qualified name: {row['qualified_name']}",
        f"name: {row['name']}",
        f"name terms: {_split_identifier(row['name'])}",
        f"kind: {row['kind']}",
        f"parent scope: {row['parent_qualified']}",
        f"relative path: {row['file_path']}",
        f"language: {row['language']}",
    ]
    return "\n".join(values)[:MAX_EMBEDDING_TEXT]


def _cache_hash(text: str, file_content_hash: str) -> str:
    payload = f"{file_content_hash}\0{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _encode_vector(vector: Iterable[float]) -> bytes:
    values = tuple(vector)
    try:
        return struct.pack(f"={len(values)}f", *values)
    except (OverflowError, struct.error) as exc:
        raise EmbeddingError(
            "embedding_response_invalid",
            "Embedding values cannot be represented as float32",
        ) from exc


def decode_vector(blob: bytes, dimension: int) -> tuple[float, ...]:
    expected = dimension * 4
    if dimension <= 0 or len(blob) != expected:
        raise EmbeddingError(
            "embedding_response_invalid", "Cached embedding has an invalid dimension"
        )
    return struct.unpack(f"={dimension}f", blob)


def _validated_vectors(payload: Any, expected_count: int) -> list[tuple[float, ...]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise EmbeddingError(
            "embedding_response_invalid", "Embedding response must contain a data array"
        )
    data = payload["data"]
    if len(data) != expected_count:
        raise EmbeddingError(
            "embedding_response_invalid", "Embedding response count does not match input"
        )
    ordered: list[tuple[float, ...] | None] = [None] * expected_count
    dimension: int | None = None
    for fallback_index, item in enumerate(data):
        if not isinstance(item, dict):
            raise EmbeddingError(
                "embedding_response_invalid", "Embedding response item must be an object"
            )
        index = item.get("index")
        vector = item.get("embedding")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index != fallback_index
            or ordered[index] is not None
            or not isinstance(vector, list)
            or not vector
        ):
            raise EmbeddingError(
                "embedding_response_invalid", "Embedding response item is malformed"
            )
        values: list[float] = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EmbeddingError(
                    "embedding_response_invalid", "Embedding values must be numeric"
                )
            number = float(value)
            if not math.isfinite(number):
                raise EmbeddingError(
                    "embedding_response_invalid", "Embedding values must be finite"
                )
            values.append(number)
        if dimension is None:
            dimension = len(values)
        elif len(values) != dimension:
            raise EmbeddingError(
                "embedding_response_invalid", "Embedding dimensions are inconsistent"
            )
        ordered[index] = tuple(values)
    if any(vector is None for vector in ordered):
        raise EmbeddingError(
            "embedding_response_invalid", "Embedding response indexes are incomplete"
        )
    return [vector for vector in ordered if vector is not None]


def _request_embeddings(config: EmbeddingConfig, texts: list[str]) -> list[tuple[float, ...]]:
    body = json.dumps({"model": config.model, "input": texts}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    try:
        request = urllib.request.Request(
            f"{config.base_url}/embeddings", data=body, headers=headers, method="POST"
        )
    except ValueError as exc:
        raise EmbeddingError(
            "embedding_error", f"Embedding endpoint is invalid: {exc}"
        ) from exc
    response_body: bytes | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                response_body = response.read()
            break
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if retryable and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise EmbeddingError(
                "embedding_error", f"Embedding provider returned HTTP {exc.code}"
            ) from exc
        except (
            http.client.HTTPException,
            OSError,
            TimeoutError,
            urllib.error.URLError,
            ValueError,
        ) as exc:
            raise EmbeddingError(
                "embedding_error", f"Embedding request failed: {exc}"
            ) from exc
    if response_body is None:
        raise EmbeddingError("embedding_error", "Embedding request failed")
    try:
        payload = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EmbeddingError(
            "embedding_response_invalid", "Embedding response is not valid JSON"
        ) from exc
    return _validated_vectors(payload, len(texts))


def refresh_embeddings(
    conn: sqlite3.Connection, config: EmbeddingConfig
) -> EmbeddingCache:
    _ensure_schema(conn)
    rows = conn.execute(
        """SELECT n.*, f.language, f.content_hash
        FROM nodes n JOIN files f ON f.path=n.file_path
        ORDER BY n.qualified_name"""
    ).fetchall()
    cached_rows = conn.execute(
        """SELECT qualified_name, text_hash, dimension
        FROM node_embeddings WHERE provider_identity=?""",
        (config.identity,),
    ).fetchall()
    cached = {row["qualified_name"]: row for row in cached_rows}
    missing: list[tuple[sqlite3.Row, str, str]] = []
    cache_hits = 0
    current_names = {row["qualified_name"] for row in rows}
    dimensions = {
        int(row["dimension"])
        for row in cached_rows
        if row["qualified_name"] in current_names
    }
    if len(dimensions) > 1:
        raise EmbeddingError(
            "embedding_response_invalid",
            "Cached embedding dimensions are inconsistent",
        )
    for row in rows:
        text = _embedding_text(row)
        text_hash = _cache_hash(text, row["content_hash"])
        stored = cached.get(row["qualified_name"])
        if stored is not None and stored["text_hash"] == text_hash:
            cache_hits += 1
        else:
            missing.append((row, text, text_hash))

    updated = 0
    for offset in range(0, len(missing), MAX_BATCH_SIZE):
        batch = missing[offset : offset + MAX_BATCH_SIZE]
        vectors = _request_embeddings(config, [item[1] for item in batch])
        if dimensions and any(len(vector) not in dimensions for vector in vectors):
            raise EmbeddingError(
                "embedding_response_invalid",
                "Embedding dimension differs from the existing provider cache",
            )
        dimensions.update(len(vector) for vector in vectors)
        if len(dimensions) != 1:
            raise EmbeddingError(
                "embedding_response_invalid", "Embedding dimensions are inconsistent"
            )
        with conn:
            conn.executemany(
                """INSERT OR REPLACE INTO node_embeddings(
                    qualified_name, provider_identity, text_hash, vector,
                    dimension, provider, model
                ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        row["qualified_name"], config.identity, text_hash,
                        _encode_vector(vector), len(vector), PROVIDER, config.model,
                    )
                    for (row, _text, text_hash), vector in zip(batch, vectors)
                ],
            )
        updated += len(batch)

    if not dimensions:
        raise EmbeddingError(
            "embedding_response_invalid", "No code nodes are available to embed"
        )
    return EmbeddingCache(
        provider_identity=config.identity,
        dimension=next(iter(dimensions)),
        cache_hits=cache_hits,
        updated_nodes=updated,
    )


def embed_query(
    config: EmbeddingConfig, query: str, expected_dimension: int
) -> tuple[float, ...]:
    vector = _request_embeddings(config, [query])[0]
    if len(vector) != expected_dimension:
        raise EmbeddingError(
            "embedding_response_invalid",
            "Query embedding dimension differs from the indexed embeddings",
        )
    return vector


def iter_cached_embeddings(
    conn: sqlite3.Connection,
    provider_identity: str,
    *,
    kind: str | None = None,
) -> Iterator[tuple[sqlite3.Row, tuple[float, ...]]]:
    params: list[Any] = [provider_identity]
    kind_sql = ""
    if kind is not None:
        kind_sql = "AND n.kind=?"
        params.append(kind)
    cursor = conn.execute(
        f"""SELECT n.*, e.vector, e.dimension
        FROM node_embeddings e JOIN nodes n ON n.qualified_name=e.qualified_name
        WHERE e.provider_identity=? {kind_sql}
        ORDER BY n.qualified_name""",
        params,
    )
    for row in cursor:
        yield row, decode_vector(row["vector"], int(row["dimension"]))
