"""Check configured BGE-M3 API access without Groq, Redis or database calls."""

import asyncio
import math
import sys

from app.embedding_api import create_embedder


async def check() -> None:
    vectors = await create_embedder().embed_many(("Що таке Окіл?", "Які кав'ярні є поруч?"))
    for index, vector in enumerate(vectors, start=1):
        print(f"BGE-M3 API OK: input={index}, dimensions={len(vector)}, norm={math.hypot(*vector):.6f}")


def main() -> int:
    try:
        asyncio.run(check())
    except (ValueError, RuntimeError) as error:
        print(f"BGE-M3 API check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
