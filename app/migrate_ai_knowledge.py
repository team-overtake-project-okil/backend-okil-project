"""Regenerate existing knowledge vectors and migrate their column atomically."""

import argparse
import asyncio
import json

import psycopg
from psycopg.conninfo import conninfo_to_dict

from app.embedding_api import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, EmbeddingAPIError, create_embedder
from app.import_ai_knowledge import MAX_WINDOW_BYTES, embed_records, embedding_input_size

TARGET_TYPE = f"vector({EMBEDDING_DIMENSIONS})"
INDEX_NAME = "ix_ai_knowledge_embedding_hnsw"


def column_type(cursor):
    cursor.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = to_regclass('public.ai_knowledge') "
        "AND attname = 'embedding' AND NOT attisdropped"
    )
    row = cursor.fetchone()
    return row[0] if row else None


def content_snapshot(cursor):
    cursor.execute("SELECT id, content FROM public.ai_knowledge ORDER BY id")
    return cursor.fetchall()


def verify(cursor):
    if column_type(cursor) != TARGET_TYPE:
        raise ValueError(f"Expected public.ai_knowledge.embedding to be {TARGET_TYPE}.")
    cursor.execute(
        "SELECT count(*), count(*) FILTER (WHERE embedding IS NULL "
        "OR vector_dims(embedding) IS DISTINCT FROM %s "
        "OR metadata->>'embedding_model' IS DISTINCT FROM %s "
        "OR metadata->>'embedding_dimensions' IS DISTINCT FROM %s) "
        "FROM public.ai_knowledge",
        (EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, str(EMBEDDING_DIMENSIONS)),
    )
    count, invalid = cursor.fetchone()
    cursor.execute(
        "SELECT i.indisvalid AND am.amname = 'hnsw' AND opc.opcname = 'vector_cosine_ops' "
        "AND i.indkey[0] = a.attnum "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_am am ON am.oid = c.relam JOIN pg_opclass opc ON opc.oid = i.indclass[0] "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attname = 'embedding' "
        "WHERE i.indrelid = 'public.ai_knowledge'::regclass AND c.relname = %s",
        (INDEX_NAME,),
    )
    index = cursor.fetchone()
    if invalid or not index or not index[0]:
        raise ValueError(f"Verification failed: {invalid} invalid rows or missing/invalid cosine HNSW index.")
    return count


def replace_vectors(connection, source_type, snapshot, prepared):
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '10s'")
        cursor.execute("SET LOCAL statement_timeout = '5min'")
        cursor.execute("LOCK TABLE public.ai_knowledge IN ACCESS EXCLUSIVE MODE")
        if column_type(cursor) != source_type or content_snapshot(cursor) != snapshot:
            raise ValueError("Knowledge changed during embedding generation. Retry the migration.")
        cursor.execute(f"DROP INDEX IF EXISTS public.{INDEX_NAME}")
        cursor.execute("ALTER TABLE public.ai_knowledge ALTER COLUMN embedding TYPE vector")
        if prepared:
            cursor.executemany(
                "UPDATE public.ai_knowledge SET embedding = %s::vector, "
                "metadata = (metadata - 'embedding_max_tokens') || %s::jsonb WHERE id = %s",
                [(json.dumps(record["embedding"]), json.dumps(record["metadata"]), record["id"])
                 for record in prepared],
            )
        cursor.execute(f"ALTER TABLE public.ai_knowledge ALTER COLUMN embedding TYPE {TARGET_TYPE}")
        cursor.execute(
            f"CREATE INDEX {INDEX_NAME} ON public.ai_knowledge USING hnsw (embedding vector_cosine_ops)"
        )
        return verify(cursor)


async def run(connection, apply):
    with connection.cursor() as cursor:
        source_type = column_type(cursor)
        if source_type == TARGET_TYPE:
            return verify(cursor)
        if not apply or source_type not in {"vector(384)", "vector(1536)"}:
            raise ValueError(f"Found {source_type or 'no embedding column'}. Expected {TARGET_TYPE}.")
        snapshot = content_snapshot(cursor)

    print(f"Generating BGE-M3 embeddings for {len(snapshot)} existing rows before taking a table lock.")
    prepared = []
    if snapshot:
        prepared = await embed_records(
            [{"id": row_id, "content": content, "metadata": {}} for row_id, content in snapshot],
            create_embedder(), embedding_input_size, MAX_WINDOW_BYTES,
        )
    return replace_vectors(connection, source_type, snapshot, prepared)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify", action="store_true", help="Read-only check of the new table and index")
    mode.add_argument("--apply", action="store_true", help="Regenerate all rows and change the column type")
    args = parser.parse_args()
    try:
        from app.config import settings

        url = settings.DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        host = conninfo_to_dict(url).get("host", "")
        if args.apply and any("-pooler" in name for name in host.split(",")):
            raise ValueError("Use a direct Neon DB_URL without -pooler for the migration.")
        with psycopg.connect(url, sslmode="require", connect_timeout=10, autocommit=True) as connection:
            rows = asyncio.run(run(connection, args.apply))
        print(f"Verified: {rows} rows, {TARGET_TYPE}, {EMBEDDING_MODEL}, cosine HNSW index.")
    except (ValueError, EmbeddingAPIError) as error:
        parser.exit(1, f"Migration failed: {error}\n")
    except psycopg.Error as error:
        parser.exit(1, f"Migration failed: {type(error).__name__} (SQLSTATE {error.sqlstate or 'unavailable'}).\n")


if __name__ == "__main__":
    main()
