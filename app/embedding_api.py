"""Cloudflare Workers AI client for 1024-dimensional BGE-M3 embeddings."""

import math
import re
from collections.abc import Sequence

import httpx

EMBEDDING_MODEL = "@cf/baai/bge-m3"
EMBEDDING_DIMENSIONS = 1024
BATCH_SIZE = 16


class EmbeddingAPIError(RuntimeError):
    """Provider failure safe to report without token, input or response body."""


class CloudflareEmbedder:
    def __init__(self, account_id: str, api_token: str, timeout: float = 10.0):
        account_id = account_id.strip()
        if not re.fullmatch("[a-fA-F0-9]{32}", account_id):
            raise ValueError("Set CLOUDFLARE_ACCOUNT_ID to the Cloudflare account ID.")
        if not api_token.strip():
            raise ValueError("Set CLOUDFLARE_API_TOKEN to a Workers AI token.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("EMBEDDING_API_TIMEOUT_SECONDS must be positive and finite.")
        self._token = api_token.strip()
        self._url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/embeddings"
        self._timeout = timeout

    async def __call__(self, text: str) -> tuple[float, ...]:
        return (await self.embed_many((text,)))[0]

    async def embed_many(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding inputs must be non-empty strings.")
        vectors = []
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
            for start in range(0, len(texts), BATCH_SIZE):
                batch = list(texts[start:start + BATCH_SIZE])
                try:
                    response = await client.post(
                        self._url,
                        headers={"Authorization": f"Bearer {self._token}"},
                        json={"model": EMBEDDING_MODEL, "input": batch, "encoding_format": "float"},
                    )
                except httpx.HTTPError:
                    raise EmbeddingAPIError("Cloudflare embedding API request failed.") from None
                if not 200 <= response.status_code < 300:
                    raise EmbeddingAPIError(f"Cloudflare embedding API returned HTTP {response.status_code}.")
                vectors.extend(_parse_embeddings(response, len(batch)))
        return tuple(vectors)


def _parse_embeddings(response: httpx.Response, expected: int) -> list[tuple[float, ...]]:
    try:
        rows = response.json()["data"]
        if not isinstance(rows, list) or len(rows) != expected:
            raise ValueError
        indexed = {}
        for row in rows:
            index, vector = row["index"], row["embedding"]
            if type(index) is not int or index not in range(expected) or index in indexed:
                raise ValueError
            if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
                raise ValueError
            if any(type(value) not in (float, int) or not math.isfinite(value) for value in vector):
                raise ValueError
            norm = math.hypot(*vector)
            if not math.isfinite(norm) or not norm:
                raise ValueError
            indexed[index] = tuple(value / norm for value in vector)
        return [indexed[index] for index in range(expected)]
    except (ValueError, TypeError, KeyError, OverflowError):
        raise EmbeddingAPIError(
            f"Cloudflare returned invalid embeddings. Expected one finite, nonzero "
            f"{EMBEDDING_DIMENSIONS}-dimensional vector per input."
        ) from None


def create_embedder() -> CloudflareEmbedder:
    from app.config import settings

    return CloudflareEmbedder(
        settings.CLOUDFLARE_ACCOUNT_ID, settings.CLOUDFLARE_API_TOKEN.get_secret_value(),
        settings.EMBEDDING_API_TIMEOUT_SECONDS,
    )
