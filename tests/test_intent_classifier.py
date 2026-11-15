import asyncio

import pytest

from app.intent_classifier import (
    DEFAULT_MIN_SIMILARITY,
    EMBEDDING_DIMENSIONS,
    INTENT_DESCRIPTIONS,
    Intent,
    IntentClassifier,
    SentenceTransformerEmbedder,
)


def make_classifier(query_embedding, *, min_similarity=DEFAULT_MIN_SIMILARITY):
    vectors = {
        Intent.POI: [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
        Intent.HEX_SCORE: [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2),
        Intent.PROJECT_INFO: [0.0, 0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 3),
    }

    async def embed_text(text):
        if text == "питання користувача":
            return query_embedding
        intent = next(
            intent
            for intent, description in INTENT_DESCRIPTIONS.items()
            if description == text
        )
        return vectors[intent]

    return IntentClassifier(embed_text, vectors, min_similarity)


def test_classifies_using_highest_cosine_similarity():
    classifier = make_classifier(
        [0.8, 0.6] + [0.0] * (EMBEDDING_DIMENSIONS - 2)
    )

    result = asyncio.run(classifier.classify("питання користувача"))

    assert result.intent is Intent.POI
    assert result.scores[Intent.POI] == pytest.approx(0.8)
    assert result.scores[Intent.HEX_SCORE] == pytest.approx(0.6)


def test_returns_no_intent_below_optional_threshold():
    classifier = make_classifier(
        [-1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
        min_similarity=0.1,
    )

    result = asyncio.run(classifier.classify("питання користувача"))

    assert result.intent is None


def test_default_threshold_returns_no_intent_for_weak_match():
    classifier = make_classifier(
        [-1.0, -1.0, -1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 3)
    )

    result = asyncio.run(classifier.classify("питання користувача"))

    assert result.intent is None
    assert result.similarity < DEFAULT_MIN_SIMILARITY


def test_default_threshold_keeps_a_clear_intent_match():
    classifier = make_classifier(
        [0.8, 0.6] + [0.0] * (EMBEDDING_DIMENSIONS - 2)
    )

    result = asyncio.run(classifier.classify("питання користувача"))

    assert result.intent is Intent.POI
    assert result.similarity >= DEFAULT_MIN_SIMILARITY


def test_rejects_empty_query():
    classifier = make_classifier([1.0, 0.0, 0.0])

    with pytest.raises(ValueError, match="Query must not be empty"):
        asyncio.run(classifier.classify(" "))


def test_rejects_query_embedding_with_wrong_dimensions():
    classifier = make_classifier([1.0, 0.0])

    with pytest.raises(ValueError, match="same dimensions"):
        asyncio.run(classifier.classify("питання користувача"))


def test_create_embeds_each_scenario_once_and_reuses_centroids():
    calls = []

    async def embed_text(text):
        calls.append(text)
        if text == "нове запитання":
            return [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
        intent = next(
            intent
            for intent, description in INTENT_DESCRIPTIONS.items()
            if text in description.split("\n\n")
        )
        vector = [0.0] * EMBEDDING_DIMENSIONS
        vector[list(Intent).index(intent)] = 1.0
        return vector

    classifier = asyncio.run(IntentClassifier.create(embed_text))
    result = asyncio.run(classifier.classify("нове запитання"))

    assert result.intent is Intent.POI
    scenarios = [
        scenario
        for description in INTENT_DESCRIPTIONS.values()
        for scenario in description.split("\n\n")
    ]
    assert all(calls.count(scenario) == 1 for scenario in scenarios)
    assert len(calls) == len(scenarios) + 1


def test_create_preserves_later_scenarios_and_normalizes_each(monkeypatch):
    # A short-input encoder sees each scenario independently. A later case
    # must influence the centroid even when the first case has a larger norm.
    monkeypatch.setattr("app.intent_classifier.INTENT_DESCRIPTIONS", {
        Intent.POI: "first scenario\n\nlast scenario",
        Intent.HEX_SCORE: "score scenario",
        Intent.PROJECT_INFO: "project scenario",
    })
    vectors = {
        "first scenario": [100.0, 0.0, 0.0],
        "last scenario": [0.0, 1.0, 0.0],
        "score scenario": [-1.0, 0.0, 0.0],
        "project scenario": [0.0, 0.0, 1.0],
        "query about last scenario": [0.0, 1.0, 0.0],
    }

    async def embed_text(text):
        return vectors[text]

    classifier = asyncio.run(IntentClassifier.create(embed_text))
    match = asyncio.run(classifier.classify("query about last scenario"))
    assert match.intent is Intent.POI
    assert match.similarity == pytest.approx(2 ** -0.5)


def test_create_rejects_inconsistent_scenario_dimensions(monkeypatch):
    monkeypatch.setattr("app.intent_classifier.INTENT_DESCRIPTIONS", {
        Intent.POI: "short\n\nlong",
        Intent.HEX_SCORE: "score",
        Intent.PROJECT_INFO: "project",
    })

    async def embed_text(text):
        return [1.0, 0.0] if text == "short" else [1.0, 0.0, 0.0]

    with pytest.raises(ValueError, match="matching embedding dimensions"):
        asyncio.run(IntentClassifier.create(embed_text))


def test_parse_query_separates_supported_and_unrelated_parts():
    relevant_text = "поясни чого в цього гекса така оцінка"
    unrelated_text = "дай рецепт сиринів"
    query_vectors = {
        relevant_text: [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2),
        unrelated_text: [-1.0, -1.0, -1.0]
        + [0.0] * (EMBEDDING_DIMENSIONS - 3),
    }
    intent_vectors = {
        Intent.POI: [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
        Intent.HEX_SCORE: [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2),
        Intent.PROJECT_INFO: [0.0, 0.0, 1.0]
        + [0.0] * (EMBEDDING_DIMENSIONS - 3),
    }

    async def embed_text(text):
        if text in query_vectors:
            return query_vectors[text]
        intent = next(
            intent
            for intent, description in INTENT_DESCRIPTIONS.items()
            if description == text
        )
        return intent_vectors[intent]

    classifier = IntentClassifier(embed_text, intent_vectors)

    result = asyncio.run(
        classifier.parse_query(
            f"{relevant_text} і {unrelated_text}"
        )
    )

    assert len(result.relevant_parts) == 1
    assert result.relevant_parts[0].text == relevant_text
    assert result.relevant_parts[0].match.intent is Intent.HEX_SCORE
    assert result.unrelated_parts == (unrelated_text,)


def test_parse_query_keeps_one_intent_even_with_conjunction():
    query = "де знайти кафе і магазини біля гекса"
    vector = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
    intent_vectors = {
        Intent.POI: vector,
        Intent.HEX_SCORE: [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2),
        Intent.PROJECT_INFO: [0.0, 0.0, 1.0]
        + [0.0] * (EMBEDDING_DIMENSIONS - 3),
    }

    async def embed_text(text):
        if text == query:
            return vector
        intent = next(
            intent
            for intent, description in INTENT_DESCRIPTIONS.items()
            if description == text
        )
        return intent_vectors[intent]

    result = asyncio.run(
        IntentClassifier(embed_text, intent_vectors).parse_query(query)
    )

    assert [part.text for part in result.relevant_parts] == [query]
    assert result.relevant_parts[0].match.intent is Intent.POI
    assert result.unrelated_parts == ()


def test_sentence_transformer_embedder_uses_multilingual_model(monkeypatch):
    class FakeSentenceTransformer:
        def __init__(self, model_name):
            assert model_name == "paraphrase-multilingual-MiniLM-L12-v2"

        def get_embedding_dimension(self):
            return EMBEDDING_DIMENSIONS

        def encode(self, text, *, convert_to_numpy, normalize_embeddings):
            assert text == "питання"
            assert convert_to_numpy is True
            assert normalize_embeddings is True
            return [0.0] * EMBEDDING_DIMENSIONS

    monkeypatch.setattr(
        "app.intent_classifier._load_sentence_transformer",
        FakeSentenceTransformer,
    )
    embedder = SentenceTransformerEmbedder()

    embedding = asyncio.run(embedder("питання"))

    assert len(embedding) == EMBEDDING_DIMENSIONS


def test_sentence_transformer_embedder_rejects_unexpected_dimensions(monkeypatch):
    class FakeSentenceTransformer:
        def __init__(self, model_name):
            pass

        def get_embedding_dimension(self):
            return 3

    monkeypatch.setattr(
        "app.intent_classifier._load_sentence_transformer",
        FakeSentenceTransformer,
    )

    with pytest.raises(ValueError, match="Expected 384-dimensional"):
        SentenceTransformerEmbedder()
