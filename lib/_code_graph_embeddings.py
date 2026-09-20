"""Private OpenAI-compatible embedding implementation for ``code_graph``."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings
from openai import APIStatusError, Omit
from pydantic import SecretStr

PROVIDER = "openai-compatible"
MAX_BATCH_SIZE = 20
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
    embeddings: Embeddings | None = None
    cache_identity: str | None = None

    @property
    def identity(self) -> str:
        if self.cache_identity:
            return self.cache_identity
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


def load_embedding_config(embeddings: Embeddings | None = None,
                          identity: str | None = None) -> EmbeddingConfig:
    # The consent gate also applies to injected providers, which may use a network.
    if os.getenv("LANGCODE_ACCEPT_CLOUD_EMBEDDINGS") != "1":
        raise EmbeddingError("cloud_egress_not_accepted",
                             "Set LANGCODE_ACCEPT_CLOUD_EMBEDDINGS=1 to allow embeddings")
    if embeddings is not None:
        return EmbeddingConfig("injected", identity or "injected", "", embeddings, identity)
    base_url = os.getenv("LANGCODE_EMBEDDING_BASE_URL", "").strip()
    model = os.getenv("LANGCODE_EMBEDDING_MODEL", "").strip()
    if not base_url or not model:
        raise EmbeddingError("provider_unavailable", "Missing LANGCODE_EMBEDDING_BASE_URL or LANGCODE_EMBEDDING_MODEL")
    key = os.getenv("LANGCODE_EMBEDDING_API_KEY", "")
    return EmbeddingConfig(base_url.rstrip("/"), model, key)


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


class _ValidatedClient:
    """Validate raw response indexes before LangChain discards that metadata."""

    def __init__(self, client):
        self.client = client

    def create(self, *, input, **kwargs):
        try:
            response = self.client.create(input=input, **kwargs)
            payload = response if isinstance(response, dict) else response.model_dump(warnings=False)
        except (ValueError, AttributeError, TypeError) as exc:
            raise EmbeddingError("embedding_response_invalid", "Invalid embedding response") from exc
        _validated_vectors(payload, len(input))
        return payload


def _request_embeddings(config: EmbeddingConfig, texts: list[str], *, is_query: bool = False) -> list[tuple[float, ...]]:
    try:
        if config.embeddings is not None:
            vectors = ([config.embeddings.embed_query(texts[0])] if is_query
                       else config.embeddings.embed_documents(texts))
        else:
            provider = OpenAIEmbeddings(
                model=config.model, base_url=config.base_url,
                api_key=SecretStr(config.api_key or "not-required"), chunk_size=MAX_BATCH_SIZE,
                timeout=REQUEST_TIMEOUT_SECONDS, max_retries=2,
                check_embedding_ctx_length=False,
            )
            provider.client = _ValidatedClient(provider.client)
            headers = {"Authorization": Omit()} if not config.api_key else {}
            vectors = ([provider.embed_query(texts[0], extra_headers=headers)] if is_query
                       else provider.embed_documents(texts, extra_headers=headers))
        return _validated_vectors({"data": [
            {"index": i, "embedding": vector} for i, vector in enumerate(vectors)
        ]}, len(texts))
    except EmbeddingError:
        raise
    except APIStatusError as exc:
        raise EmbeddingError("embedding_error", f"Embedding provider returned HTTP {exc.status_code}") from exc
    except Exception as exc:
        # Do not include provider payloads or credentials in tool errors.
        raise EmbeddingError("embedding_error", f"Embedding request failed ({type(exc).__name__})") from exc


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
    vector = _request_embeddings(config, [query], is_query=True)[0]
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
