"""Embed curated Markdown with MiniLM and import it into existing ai_knowledge."""

import argparse
import asyncio
import hashlib
import json
import math
import re
from pathlib import Path

from app.export_ai_knowledge import DEFAULT_SOURCE, parse_knowledge_markdown
from app.intent_classifier import (
    DEFAULT_EMBEDDING_MODEL,
    EMBEDDING_DIMENSIONS,
    SentenceTransformerEmbedder,
)


def split_embedding_text(text, token_count, max_tokens):
    """Preserve all text in bounded windows instead of silently truncating it."""
    if token_count(text) <= max_tokens:
        return [text]
    if len(text) < 2:
        raise ValueError("The model token limit cannot hold the input")
    midpoint = len(text) // 2
    boundaries = [match.end() for match in re.finditer(r"\s+", text)]
    cut = min(boundaries, key=lambda position: abs(position - midpoint)) if boundaries else midpoint
    if cut >= len(text):
        cut = midpoint
    return [
        window
        for part in (text[:cut].strip(), text[cut:].strip())
        if part
        for window in split_embedding_text(part, token_count, max_tokens)
    ]


async def embed_records(records, embed_text, token_count, max_tokens):
    prepared = []
    for record in records:
        windows = split_embedding_text(record["content"], token_count, max_tokens)
        vectors = []
        for window in windows:
            vector = tuple(float(value) for value in await embed_text(window))
            if len(vector) != EMBEDDING_DIMENSIONS or not all(map(math.isfinite, vector)):
                raise ValueError("Expected a finite 384-dimensional embedding")
            norm = math.sqrt(math.fsum(value * value for value in vector))
            if not norm:
                raise ValueError("Embedding must not be a zero vector")
            vectors.append(tuple(value / norm for value in vector))
        mean = tuple(
            math.fsum(vector[index] for vector in vectors) / len(vectors)
            for index in range(EMBEDDING_DIMENSIONS)
        )
        norm = math.sqrt(math.fsum(value * value for value in mean))
        if not norm:
            raise ValueError("Mean embedding must not be a zero vector")
        prepared.append({
            **record,
            "embedding": [value / norm for value in mean],
            "metadata": {
                **record["metadata"],
                "embedding_model": DEFAULT_EMBEDDING_MODEL,
                "embedding_dimensions": EMBEDDING_DIMENSIONS,
                "embedding_strategy": "normalized_mean_windows_v1",
                "embedding_max_tokens": max_tokens,
                "embedding_windows": len(windows),
                "content_sha256": hashlib.sha256(record["content"].encode("utf-8")).hexdigest(),
            },
        })
    return prepared


def write_records(connection, records):
    """Atomic insert/update under a per-document lock; never remove other data."""
    document_ids = {record["metadata"]["document_id"] for record in records}
    if len(document_ids) != 1:
        raise ValueError("Import exactly one nonempty document at a time")
    document_id = document_ids.pop()
    ids = [record["metadata"]["chunk_id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate input chunk IDs")
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    with connection.transaction():
        with connection.cursor() as cursor:
            # Neon pooled connections reject these as startup options.
            # Transaction-local settings work with transaction pooling.
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute("SET LOCAL lock_timeout = '10s'")
            cursor.execute(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = to_regclass('public.ai_knowledge') "
                "AND attname = 'embedding' AND NOT attisdropped"
            )
            row = cursor.fetchone()
            if row is None or row[0] != f"vector({EMBEDDING_DIMENSIONS})":
                raise ValueError("ai_knowledge.embedding must be vector(384); no rows written")
            # The table has no unique document/chunk constraint. Cooperating
            # importers serialize writes for this document in the transaction.
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (document_id,))
            cursor.execute(
                "SELECT id, category, content, metadata FROM public.ai_knowledge "
                "WHERE metadata->>'document_id' = %s FOR UPDATE", (document_id,)
            )
            existing = {}
            for row_id, category, content, metadata in cursor.fetchall():
                chunk_id = metadata.get("chunk_id")
                if chunk_id in existing:
                    raise ValueError("Existing duplicate chunk IDs; import cancelled")
                existing[chunk_id] = (row_id, category, content, metadata)
            for record in records:
                metadata = record["metadata"]
                old = existing.get(metadata["chunk_id"])
                if old and old[1:] == (record["category"], record["content"], metadata):
                    counts["unchanged"] += 1
                    continue
                vector = "[" + ",".join(str(value) for value in record["embedding"]) + "]"
                params = (
                    record["category"], record["content"], vector,
                    json.dumps(metadata, ensure_ascii=False),
                )
                if old:
                    cursor.execute(
                        "UPDATE public.ai_knowledge SET category=%s, content=%s, "
                        "embedding=%s::vector, metadata=%s::jsonb WHERE id=%s",
                        (*params, old[0]),
                    )
                    counts["updated"] += 1
                else:
                    cursor.execute(
                        "INSERT INTO public.ai_knowledge (category, content, embedding, metadata) "
                        "VALUES (%s, %s, %s::vector, %s::jsonb)", params,
                    )
                    counts["inserted"] += 1
    return counts


async def run(args):
    records = parse_knowledge_markdown(
        args.source.read_text(encoding="utf-8"), source=args.source.name,
    )
    print(f"Validated {len(records)} Markdown sections.")
    if args.validate_only:
        return
    print(f"Loading {DEFAULT_EMBEDDING_MODEL}; the first run may download model files.")
    embedder = await asyncio.to_thread(SentenceTransformerEmbedder)
    model = embedder._model
    max_tokens = model.max_seq_length
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("The model must specify a positive max_seq_length")
    prepared = await embed_records(
        records, embedder,
        lambda text: len(model.tokenizer.encode(
            text, add_special_tokens=True, truncation=False, verbose=False,
        )),
        max_tokens,
    )
    print(f"Created {len(prepared)} normalized {EMBEDDING_DIMENSIONS}-dimensional vectors.")
    if not args.apply:
        print("Dry run: no database connection or writes. Add --apply to import.")
        return
    import psycopg
    from app.config import settings

    url = settings.DB_URL
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            url = "postgresql://" + url[len(prefix):]
    with psycopg.connect(
        url, sslmode="require", connect_timeout=10,
    ) as connection:
        counts = write_records(connection, prepared)
    print("Import committed: " + ", ".join(f"{key}={value}" for key, value in counts.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_true", help="Parse Markdown without model or DB")
    mode.add_argument("--apply", action="store_true", help="Write embeddings to the configured DB_URL")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except ModuleNotFoundError as error:
        parser.exit(
            1,
            f"Import failed: missing Python module '{error.name}'.\n"
            "Install dependencies with the same Python interpreter:\n"
            "  python -m pip install -r requirements.txt\n",
        )
    except Exception as error:
        # Driver exceptions may contain connection details: keep credentials
        # and database response bodies out of CLI output.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, f"Import failed: {message}\n")


if __name__ == "__main__":
    main()
