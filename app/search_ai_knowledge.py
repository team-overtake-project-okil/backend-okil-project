"""Read-only semantic search of ai_knowledge with source metadata in the result."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.embedding_api import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, create_embedder
from app.models import AIKnowledge

PROJECT_KNOWLEDGE_LIMIT = 3
PROJECT_KNOWLEDGE_MIN_SIMILARITY = 0.52
PROJECT_METADATA_FIELDS = (
    "document_id", "chunk_id", "title", "source_document", "source_kind",
    "source_provider", "source_url", "source_dataset_id", "source_resource_id",
    "source_record_id", "source_row_id", "entity_id", "source_license",
    "source_license_url", "source_updated_at", "source_scope", "entity_type",
    "address", "settlement", "district", "latitude", "longitude",
)


async def search(query: str, *, limit: int, min_similarity: float) -> dict:
    from sqlalchemy import bindparam, select, text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.config import settings

    embedding = await create_embedder()(query)
    engine = create_async_engine(
        url=settings.DATABASE_URL_asyncpg,
        echo=False,
        connect_args={"ssl": "require"},
    )
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                query_vector = bindparam(
                    "knowledge_query", list(embedding), type_=AIKnowledge.embedding.type,
                )
                distance = AIKnowledge.embedding.cosine_distance(query_vector)
                metadata = AIKnowledge.metadata_json
                result = await session.execute(
                    select(
                        AIKnowledge.content.label("text"),
                        AIKnowledge.category.label("category"),
                        metadata.label("metadata"),
                        (1 - distance).label("similarity"),
                    )
                    .where(
                        metadata["embedding_model"].astext == EMBEDDING_MODEL,
                        metadata["embedding_dimensions"].astext == str(EMBEDDING_DIMENSIONS),
                        distance <= 1 - min_similarity,
                    )
                    .order_by(distance)
                    .limit(limit)
                )
                rows = result.mappings().all()
        return {
            "query": query,
            "results": [
                {
                    "text": row["text"],
                    "category": row["category"],
                    "similarity": round(float(row["similarity"]), 6),
                    "metadata": {
                        key: row["metadata"][key]
                        for key in PROJECT_METADATA_FIELDS
                        if key in row["metadata"] and row["metadata"][key] is not None
                    },
                }
                for row in rows
            ],
        }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True, help="Natural-language query to embed and search.")
    parser.add_argument("--limit", type=int, default=PROJECT_KNOWLEDGE_LIMIT)
    parser.add_argument("--min-similarity", type=float, default=PROJECT_KNOWLEDGE_MIN_SIMILARITY)
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        parser.error("--limit must be from 1 to 20.")
    if not 0 <= args.min_similarity <= 1:
        parser.error("--min-similarity must be from 0 to 1.")
    try:
        result = asyncio.run(search(
            args.query,
            limit=args.limit,
            min_similarity=args.min_similarity,
        ))
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, f"Search failed: {message}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
