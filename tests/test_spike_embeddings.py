import asyncio

import pytest

from app.spike_embeddings import embed_spike_records


def record(content):
    return {
        "category": "health",
        "content": content,
        "metadata": {"document_id": "health", "chunk_id": content},
    }


def test_spike_embeddings_use_provider_batches_and_record_provenance():
    batches = []

    async def embed_many(texts):
        batches.append(list(texts))
        return [[0.0, 3.0] + [0.0] * 1022 for _ in texts]

    prepared = asyncio.run(embed_spike_records(
        [record(f"Facility {index}") for index in range(17)],
        embed_many,
        len,
        100,
    ))

    assert [len(batch) for batch in batches] == [16, 1]
    assert len(prepared) == 17
    assert prepared[0]["embedding"] == [0.0, 1.0] + [0.0] * 1022
    assert prepared[0]["metadata"]["embedding_model"] == "@cf/baai/bge-m3"
    assert len(prepared[0]["metadata"]["content_sha256"]) == 64


@pytest.mark.parametrize("vectors", [[], [[0.0] * 1024]])
def test_spike_embedding_provider_must_return_one_valid_vector_per_input(vectors):
    async def embed_many(_texts):
        return vectors

    with pytest.raises(ValueError):
        asyncio.run(embed_spike_records([record("Clinic")], embed_many, len, 100))
