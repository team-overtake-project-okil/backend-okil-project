"""Batch embeddings for spike records using the project's existing provider."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from app.embedding_api import BATCH_SIZE, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from app.import_ai_knowledge import split_embedding_text

EmbeddingBatch = Callable[[Sequence[str]], Awaitable[Sequence[Sequence[float]]]]


async def embed_spike_records(
    records: list[dict[str, Any]],
    embed_many: EmbeddingBatch,
    token_count: Callable[[str], int],
    max_tokens: int,
) -> list[dict[str, Any]]:
    """Embed all source text in bounded provider batches before any DB writes."""
    record_windows = [
        split_embedding_text(record["content"], token_count, max_tokens)
        for record in records
    ]
    windows = [window for chunks in record_windows for window in chunks]
    raw_vectors = []
    for start in range(0, len(windows), BATCH_SIZE):
        batch = windows[start:start + BATCH_SIZE]
        vectors = await embed_many(batch)
        if len(vectors) != len(batch):
            raise ValueError("Embedding provider returned an unexpected batch size")
        raw_vectors.extend(vectors)

    def normalize(vector: Sequence[float]) -> tuple[float, ...]:
        values = tuple(float(value) for value in vector)
        if len(values) != EMBEDDING_DIMENSIONS or not all(map(math.isfinite, values)):
            raise ValueError("Expected a finite 1024-dimensional embedding")
        norm = math.sqrt(math.fsum(value * value for value in values))
        if not norm:
            raise ValueError("Embedding must not be a zero vector")
        return tuple(value / norm for value in values)

    prepared = []
    offset = 0
    for record, record_text_windows in zip(records, record_windows):
        vectors = [
            normalize(vector)
            for vector in raw_vectors[offset:offset + len(record_text_windows)]
        ]
        offset += len(record_text_windows)
        mean = tuple(
            math.fsum(vector[index] for vector in vectors) / len(vectors)
            for index in range(EMBEDDING_DIMENSIONS)
        )
        mean_norm = math.sqrt(math.fsum(value * value for value in mean))
        if not mean_norm:
            raise ValueError("Mean embedding must not be a zero vector")
        prepared.append({
            **record,
            "embedding": [value / mean_norm for value in mean],
            "metadata": {
                **record["metadata"],
                "embedding_model": EMBEDDING_MODEL,
                "embedding_dimensions": EMBEDDING_DIMENSIONS,
                "embedding_strategy": "normalized_mean_utf8_windows_v1",
                "embedding_max_utf8_bytes": max_tokens,
                "embedding_windows": len(record_text_windows),
                "content_sha256": hashlib.sha256(record["content"].encode("utf-8")).hexdigest(),
            },
        })
    return prepared
