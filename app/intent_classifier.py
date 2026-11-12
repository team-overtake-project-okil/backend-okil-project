import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer as SentenceTransformerModel

DEFAULT_EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSIONS = 384
DEFAULT_MIN_SIMILARITY = 0.2


class Intent(str, Enum):
    POI = "poi"
    HEX_SCORE = "hex_score"
    PROJECT_INFO = "project_info"


INTENT_DESCRIPTIONS: Mapping[Intent, str] = {
    Intent.POI: (
        "Запитання про місця, заклади, POI та об'єкти поблизу гекса або "
        "в конкретному районі: що знаходиться поруч, що можна відвідати, "
        "де є потрібна локація."
    ),
    Intent.HEX_SCORE: (
        "Запитання про оцінку або рейтинг гекса чи району: чому така оцінка, "
        "як вона розрахована, що означають її складові та категорії оцінювання."
    ),
    Intent.PROJECT_INFO: (
        "Запитання про сам проєкт, його призначення, роботу, дані, мапу, "
        "гекси та загальні пояснення категорій, не про оцінку конкретного гекса."
    ),
}

EmbeddingFunction = Callable[[str], Awaitable[Sequence[float]]]

_QUERY_PART_SEPARATOR = re.compile(
    r"[.!?;\n]+"
    r"|\b(?:а ще|також|плюс)\b"
    r"|\b(?:і|й|та)\s+(?=(?:дай|покажи|поясни|розкажи|знайди|"
    r"підкажи|скажи|напиши|опиши|порахуй|підбери)\b)",
    re.IGNORECASE,
)


def _load_sentence_transformer(model_name: str) -> "SentenceTransformerModel":
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name)


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL) -> None:
        self._model = _load_sentence_transformer(model_name)
        dimensions = self._model.get_embedding_dimension()
        if dimensions != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"Expected {EMBEDDING_DIMENSIONS}-dimensional embeddings from "
                f"{model_name}, got {dimensions}"
            )

    def _encode(self, text: str) -> tuple[float, ...]:
        embedding = self._model.encode(
            text,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return tuple(float(value) for value in embedding)

    async def __call__(self, text: str) -> Sequence[float]:
        return await asyncio.to_thread(self._encode, text)


@dataclass(frozen=True)
class IntentMatch:
    intent: Intent | None
    similarity: float
    scores: Mapping[Intent, float]


@dataclass(frozen=True)
class ClassifiedQueryPart:
    text: str
    match: IntentMatch


@dataclass(frozen=True)
class ParsedQuery:
    relevant_parts: tuple[ClassifiedQueryPart, ...]
    unrelated_parts: tuple[str, ...]


def _validated_vector(values: Sequence[float], *, label: str) -> tuple[float, ...]:
    vector = tuple(float(value) for value in values)
    if not vector:
        raise ValueError(f"{label} embedding must not be empty")
    if not all(math.isfinite(value) for value in vector):
        raise ValueError(f"{label} embedding must contain only finite values")
    if not any(vector):
        raise ValueError(f"{label} embedding must not be a zero vector")
    return vector


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Query and intent embeddings must have the same dimensions")

    dot_product = math.fsum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    return dot_product / (left_norm * right_norm)


class IntentClassifier:
    def __init__(
        self,
        embed_text: EmbeddingFunction,
        intent_embeddings: Mapping[Intent, Sequence[float]],
        min_similarity: float | None = DEFAULT_MIN_SIMILARITY,
    ) -> None:
        if min_similarity is not None and not -1.0 <= min_similarity <= 1.0:
            raise ValueError("min_similarity must be between -1 and 1")
        if set(intent_embeddings) != set(Intent):
            raise ValueError("An embedding is required for every supported intent")

        self._embed_text = embed_text
        self._intent_embeddings = {
            intent: _validated_vector(vector, label=f"{intent.value} intent")
            for intent, vector in intent_embeddings.items()
        }
        dimensions = {len(vector) for vector in self._intent_embeddings.values()}
        if len(dimensions) != 1:
            raise ValueError("All intent embeddings must have the same dimensions")
        self._dimensions = dimensions.pop()
        self._min_similarity = min_similarity

    @classmethod
    async def create(
        cls,
        embed_text: EmbeddingFunction,
        min_similarity: float | None = DEFAULT_MIN_SIMILARITY,
    ) -> "IntentClassifier":
        embeddings = {
            intent: await embed_text(description)
            for intent, description in INTENT_DESCRIPTIONS.items()
        }
        return cls(embed_text, embeddings, min_similarity)

    async def classify(self, query: str) -> IntentMatch:
        if not query.strip():
            raise ValueError("Query must not be empty")

        query_embedding = _validated_vector(
            await self._embed_text(query),
            label="Query",
        )
        if len(query_embedding) != self._dimensions:
            raise ValueError(
                "Query and intent embeddings must have the same dimensions"
            )

        scores = {
            intent: _cosine_similarity(query_embedding, example_embedding)
            for intent, example_embedding in self._intent_embeddings.items()
        }
        best_intent = max(scores, key=scores.__getitem__)
        best_similarity = scores[best_intent]
        if (
            self._min_similarity is not None
            and best_similarity < self._min_similarity
        ):
            best_intent = None

        return IntentMatch(best_intent, best_similarity, scores)

    async def parse_query(self, query: str) -> ParsedQuery:
        if not query.strip():
            raise ValueError("Query must not be empty")

        parts = tuple(
            part.strip(" \t\r\n,")
            for part in _QUERY_PART_SEPARATOR.split(query)
            if part.strip(" \t\r\n,")
        )
        relevant_parts = []
        unrelated_parts = []
        for part in parts:
            match = await self.classify(part)
            if match.intent is None:
                unrelated_parts.append(part)
            else:
                relevant_parts.append(ClassifiedQueryPart(part, match))

        return ParsedQuery(tuple(relevant_parts), tuple(unrelated_parts))


async def create_default_intent_classifier(
    min_similarity: float | None = DEFAULT_MIN_SIMILARITY,
) -> IntentClassifier:
    embedder = await asyncio.to_thread(SentenceTransformerEmbedder)
    return await IntentClassifier.create(embedder, min_similarity)
