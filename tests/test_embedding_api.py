import asyncio
import json

import httpx
import pytest

from app.embedding_api import (
    BATCH_SIZE, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, CloudflareEmbedder, EmbeddingAPIError,
)
from app.intent_classifier import INTENT_DESCRIPTIONS, Intent, create_default_intent_classifier

ACCOUNT = "a" * 32


def vector(axis=0):
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[axis] = 2.0
    return values


@pytest.fixture
def fake_api(monkeypatch):
    client = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            "app.embedding_api.httpx.AsyncClient",
            lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
        )

    return install


def test_batches_and_preserves_input_order(fake_api):
    inputs = [str(index) for index in range(BATCH_SIZE + 1)]
    batches = []

    def handler(request):
        assert str(request.url) == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert json.loads(request.content)["model"] == EMBEDDING_MODEL
        texts = json.loads(request.content)["input"]
        batches.append(texts)
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": vector(int(text))}
            for index, text in reversed(list(enumerate(texts)))
        ]})

    fake_api(handler)
    rows = asyncio.run(CloudflareEmbedder(ACCOUNT, "test-token").embed_many(inputs))
    assert batches == [inputs[:BATCH_SIZE], inputs[BATCH_SIZE:]]
    assert [row.index(1.0) for row in rows] == list(range(len(inputs)))


@pytest.mark.parametrize("data", [
    [],
    [{"index": 0, "embedding": [1] * 384}],
    [{"index": 0, "embedding": [0] * EMBEDDING_DIMENSIONS}],
    [{"index": 2, "embedding": vector()}],
    [{"index": True, "embedding": vector()}],
    [{"index": 0, "embedding": ["1"] * EMBEDDING_DIMENSIONS}],
])
def test_invalid_provider_vectors_fail(fake_api, data):
    fake_api(lambda request: httpx.Response(200, json={"data": data}))
    with pytest.raises(EmbeddingAPIError, match="invalid embeddings"):
        asyncio.run(CloudflareEmbedder(ACCOUNT, "token")("text"))


def test_provider_error_does_not_echo_response_body(fake_api):
    fake_api(lambda request: httpx.Response(429, text="sensitive text"))
    with pytest.raises(EmbeddingAPIError, match=r"^Cloudflare embedding API returned HTTP 429\.$"):
        asyncio.run(CloudflareEmbedder(ACCOUNT, "token")("text"))


def test_factory_uses_same_api_for_scenarios_and_queries(fake_api, monkeypatch):
    from app.config import settings
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", SecretStr("token"))
    axes = {
        text: index
        for index, description in enumerate(INTENT_DESCRIPTIONS.values())
        for text in description.split("\n\n")
    }

    def handler(request):
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": vector(axes.get(text, 1))}
            for index, text in enumerate(json.loads(request.content)["input"])
        ]})

    fake_api(handler)

    async def classify():
        classifier = await create_default_intent_classifier()
        return await classifier.classify("Question about ratings")

    result = asyncio.run(classify())
    assert result.intent is Intent.HEX_SCORE
    assert result.similarity == pytest.approx(1.0)
