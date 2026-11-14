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
        "Пошук реальних місць, закладів і об'єктів поруч із гексом або адресою. "
        "Що є поблизу? Покажи місця в цьому районі. Де найближчий потрібний заклад?\n\n"
        "Повсякденні покупки й послуги біля дому. Де купити продукти? "
        "Знайди супермаркет, продуктовий магазин, аптеку, пошту, банк або банкомат поруч.\n\n"
        "Пошук медичних та освітніх закладів. Де найближча лікарня, поліклініка "
        "чи стоматологія? Які школи й дитячі садочки є біля цієї адреси?\n\n"
        "Пошук їжі й напоїв. Де випити кави або пообідати? "
        "Покажи кав'ярні, кафе, ресторани, пекарні, фастфуд, паби та бари в гексі.\n\n"
        "Спорт, прогулянки та дозвілля. Куди піти з дитиною? Де тренуватися? "
        "Знайди спортзал, стадіон, майданчик, музей, кінотеатр, торговий центр або парк.\n\n"
        "Пошук укриттів і транспортних об'єктів. Де найближче укриття? "
        "Які зупинки поруч? Які автобуси їдуть із цієї зупинки? Як дістатися до конкретного місця?\n\n"
        "Кількість і розташування об'єктів. Скільки аптек у цьому гексі? "
        "Чи є садочок до 500 метрів? Як далеко до школи? Де ближчий супермаркет: біля А чи Б?\n\n"
        "Відомості про конкретний заклад. Яка адреса цієї аптеки? "
        "Коли працює магазин? Які контакти, ціни й послуги? Чи є доступний вхід?\n\n"
        "Підбір місць за потребами та уточнення пошуку. Порадь місце поруч. "
        "Покажи ще варіанти. А найближча аптека? Знайди ветклініку або інший потрібний об'єкт."
    ),
    Intent.HEX_SCORE: (
        "Оцінка, рейтинг та індекс комфорту конкретного гекса або ділянки. "
        "Який тут бал? Чому цей гекс має 42 зі 100? Поясни оцінку цього району.\n\n"
        "Інтерпретація сильних і слабких сторін місця за балами категорій. "
        "Що знижує рейтинг цієї ділянки? Чому тут низька освіта, здоров'я або безпека?\n\n"
        "Розбіжність між об'єктами та оцінкою. Чому школа поруч, а освіта низька? "
        "Чому аптека не підняла бал? Чому біля зупинки мобільність не 100?\n\n"
        "Порівняння ділянок за оцінками Околу. Чому сусідній гекс кращий? "
        "Порівняй рейтинги А і Б. Де вищий індекс комфорту за однакових пріоритетів?\n\n"
        "Персоналізоване оцінювання місцевості. Який гекс краще за рейтингом для "
        "сім'ї з дітьми або студента? Оціни цю ділянку з пріоритетом освіти й транспорту.\n\n"
        "Формула рейтингу та категорійних балів. Як розраховується індекс комфорту? "
        "Чому геометричне середнє? Як відстань, радіуси й типи об'єктів впливають на бали?\n\n"
        "Користувацькі та системні ваги в розрахунку оцінки. Як нормалізуються ваги? "
        "Як зміниться рейтинг, якщо збільшити важливість транспорту або вимкнути їжу?\n\n"
        "Нульові оцінки та округлення. Чому нуль однієї категорії обнуляє весь рейтинг? "
        "Що буде, якщо вимкнути всі категорії? Чому 88.5 округлилося до 89?\n\n"
        "Транспортний бал і час поїздки. Як TravelTime оцінює мобільність? "
        "Як впливають автобус, автомобіль, час виїзду й опорні точки на рейтинг гекса?\n\n"
        "Поля оцінки й відмінності результатів. Що означають score, weight та adjusted_score? "
        "Чому бал у чаті відрізняється від мапи? Чому тут червоний гекс із низьким балом?"
    ),
    Intent.PROJECT_INFO: (
        "Загальна інформація про Окіл, OKIL та його призначення. "
        "Що таке Окіл? Для кого цей проєкт? Як він допомагає вибрати місце для життя? Хто його створює?\n\n"
        "Будова мапи й гексагональна сітка H3. Що таке гекс і h3_id? "
        "Чому карта поділена на шестикутники? Чим комірка відрізняється від адміністративного району?\n\n"
        "Загальний зміст категорій проєкту. Які категорії враховує Окіл? "
        "Що входить у здоров'я, освіту, щоденні потреби, мобільність, спорт, дозвілля, їжу та безпеку?\n\n"
        "Поняття POI та розподіл типів об'єктів. Що означає POI? "
        "Чому аптеки належать до щоденних потреб? Чим дитячий майданчик відрізняється від спортивного в каталозі?\n\n"
        "Користування інтерфейсом. Як знайти адресу й вибрати комірку? "
        "Де відкрити деталі, налаштування пріоритетів або порівняння? Як читати легенду й кольори мапи?\n\n"
        "Покриття, джерела й актуальність даних. Які міста підтримуються? "
        "Звідки дані про школи, укриття й транспорт? Як часто оновлюються записи? Чи повна база?\n\n"
        "Межі методики та надійність висновків. Чи враховані шум, повітря й злочинність? "
        "Чи означає близька лікарня якісне лікування? Чи гарантує мапа відкрите укриття?\n\n"
        "Загальна механіка відстаней і маршрутів у сервісі. Відстань до об'єкта "
        "рахується по прямій чи пішки? Навіщо Околу TravelTime та HERE? Чи є актуальний розклад транспорту?\n\n"
        "Можливості ШІ та проблеми сервісу. Що вміє помічник? Чому він просить H3 ID? "
        "Мапа не відкривається. Пошук не працює. Як повідомити про неправильні або відсутні дані?"
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
        # Encode short scenarios separately: one long description can be
        # truncated by the sentence model, silently losing later use cases.
        # Positive examples avoid embedding other intents through exclusions.
        embeddings = {}
        for intent, description in INTENT_DESCRIPTIONS.items():
            vectors = [
                _validated_vector(await embed_text(scenario), label=intent.value)
                for scenario in description.split("\n\n")
                if scenario.strip()
            ]
            if not vectors or len({len(vector) for vector in vectors}) != 1:
                raise ValueError("Intent scenarios must have matching embedding dimensions")
            # Normalize each scenario so its length/norm cannot dominate the
            # centroid. classify() still compares one vector per intent.
            normalized = []
            for vector in vectors:
                norm = math.sqrt(math.fsum(value * value for value in vector))
                normalized.append(tuple(value / norm for value in vector))
            embeddings[intent] = tuple(
                math.fsum(vector[index] for vector in normalized) / len(normalized)
                for index in range(len(normalized[0]))
            )
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
