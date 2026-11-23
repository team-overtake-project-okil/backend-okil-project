"""Question -> intent -> answering service, wired with the real components.

The real ``IntentClassifier``, ``ChatAIService`` and ``answer_intent`` are used.
Only the sentence-embedding model, the database lookups and Groq are replaced,
so these tests check routing and payloads, not language-model quality.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import h3
import pytest

from app import ai_assistant
from app.ai_assistant import answer_intent
from app.chat_service import ChatAIService, ChatReplyError
from app.intent_classifier import (
    EMBEDDING_DIMENSIONS,
    INTENT_DESCRIPTIONS,
    Intent,
    IntentClassifier,
)

CELL = h3.latlng_to_cell(49.84, 24.03, 9)

# One-hot centroid per intent; queries are embedded by simple keyword rules.
INTENT_AXIS = {intent: index for index, intent in enumerate(Intent)}
UNRELATED_AXIS = len(INTENT_AXIS)
KEYWORDS = (("оцінк", Intent.HEX_SCORE), ("проєкт", Intent.PROJECT_INFO), ("місц", Intent.POI))


def one_hot(axis, value=1.0):
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[axis] = value
    return vector


async def keyword_embedder(text):
    lowered = text.lower()
    for keyword, intent in KEYWORDS:
        if keyword in lowered:
            return one_hot(INTENT_AXIS[intent])
    return one_hot(UNRELATED_AXIS)


def make_classifier():
    centroids = {intent: one_hot(axis) for intent, axis in INTENT_AXIS.items()}
    return IntentClassifier(keyword_embedder, centroids)


CONTEXTS = {
    Intent.POI: {"h3_id": CELL, "objects": [{"name": "Аптека", "type": "pharmacy"}], "total_returned": 1,
                  "knowledge_fragments": []},
    Intent.HEX_SCORE: {"h3_id": CELL, "overall_rating": 72, "categories": [], "knowledge_fragments": []},
    Intent.PROJECT_INFO: {"project": "OKIL", "knowledge_fragments": ["Довідка про Окіл."]},
}
CONTEXTS.update({intent: {"knowledge_fragments": []} for intent in Intent if intent not in CONTEXTS})


class Pipeline:
    def __init__(self, monkeypatch):
        self.db = Mock(execute=AsyncMock())
        self.session_opened = False
        self.groq = Mock(answer=AsyncMock(side_effect=lambda q, intent, ctx: f"AI:{intent.value}"))
        self.services = {
            intent: AsyncMock(return_value=CONTEXTS[intent])
            for intent in Intent
        }
        self.services["knowledge"] = AsyncMock(return_value=[])
        monkeypatch.setattr(ai_assistant, "get_poi_information", self.services[Intent.POI])
        monkeypatch.setattr(ai_assistant, "get_hex_information", self.services[Intent.HEX_SCORE])
        monkeypatch.setattr(ai_assistant, "get_project_information", self.services[Intent.PROJECT_INFO])
        monkeypatch.setattr(ai_assistant, "get_knowledge_fragments", self.services["knowledge"])

        @asynccontextmanager
        async def session():
            self.session_opened = True
            yield self.db

        self.service = ChatAIService(
            make_classifier(), self.groq, answer_intent, session, Intent.PROJECT_INFO, Intent.HEX_SCORE
        )

    def reply(self, text, history=()):
        return asyncio.run(self.service.reply(text, list(history)))

    def groq_calls(self):
        return [(call.args[1], call.args[2]) for call in self.groq.answer.await_args_list]


@pytest.fixture
def pipeline(monkeypatch):
    return Pipeline(monkeypatch)


# --- contract of the supported intents ------------------------------------------


def test_supported_intent_values_are_unchanged():
    assert {intent.value for intent in Intent} == {
        "poi", "hex_score", "project_info", "hex_overview", "mobility_routes",
        "top_hexagons", "weights_simulation", "transit_lines",
        "neighborhood_context", "infrastructure_gaps",
    }


def test_every_intent_has_a_description_and_a_routing_case():
    """Fails when an intent is added without descriptions or a test here."""
    assert set(INTENT_DESCRIPTIONS) == set(Intent)
    assert all(text.strip() for text in INTENT_DESCRIPTIONS.values())
    assert set(CONTEXTS) == set(Intent), "Add a routing case for the new intent to CONTEXTS and the tests below."


def test_intent_scenarios_are_non_empty_and_unique_across_intents():
    """Each scenario is embedded separately; a duplicate would blur two intents' centroids."""
    scenarios = [
        scenario.strip()
        for description in INTENT_DESCRIPTIONS.values()
        for scenario in description.split("\n\n")
    ]
    assert all(scenarios)
    assert len(scenarios) == len(set(scenarios))


# --- each intent reaches its own service and the model --------------------------


@pytest.mark.parametrize(
    "question, expected",
    [
        (f"Які місця є в цій ділянці {CELL}?", Intent.POI),
        (f"Поясни оцінку ділянки {CELL}", Intent.HEX_SCORE),
        ("Розкажи про проєкт", Intent.PROJECT_INFO),
    ],
)
def test_question_is_routed_to_the_matching_service_and_model_context(pipeline, question, expected):
    answer = pipeline.reply(question)

    assert answer == f"AI:{expected.value}"
    for intent, service in pipeline.services.items():
        if intent == "knowledge":
            assert service.await_count == (0 if expected is Intent.PROJECT_INFO else 1)
            continue
        assert service.await_count == (1 if intent is expected else 0), intent
    # The model gets the facts from that service, never anything the user typed.
    assert pipeline.groq_calls() == [(expected, CONTEXTS[expected])]


def test_h3_scoped_services_receive_the_database_session_and_cell(pipeline):
    pipeline.reply(f"Які місця поруч {CELL}?")
    pipeline.reply(f"Поясни оцінку {CELL}")
    pipeline.services[Intent.POI].assert_awaited_once_with(pipeline.db, CELL)
    pipeline.services[Intent.HEX_SCORE].assert_awaited_once_with(pipeline.db, CELL)
    assert pipeline.services["knowledge"].await_count == 2


def test_project_question_needs_no_h3_context(pipeline):
    assert pipeline.reply("Розкажи про проєкт") == "AI:project_info"
    pipeline.services[Intent.PROJECT_INFO].assert_awaited_once_with(
        pipeline.db, tuple(one_hot(INTENT_AXIS[Intent.PROJECT_INFO])),
    )


def test_h3_context_can_come_from_an_earlier_question(pipeline):
    history = [{"requestId": "r1", "question": f"Оцінка {CELL}", "answer": "..."}]
    assert pipeline.reply("Які місця поруч?", history) == "AI:poi"
    pipeline.services[Intent.POI].assert_awaited_once_with(pipeline.db, CELL)


# --- several questions in one message --------------------------------------------


def test_combined_question_returns_one_answer_per_part_in_order(pipeline):
    answer = pipeline.reply(f"Поясни оцінку {CELL}. Які місця поруч?")

    assert answer == "AI:hex_score\n\nAI:poi"
    assert [intent for intent, _ in pipeline.groq_calls()] == [Intent.HEX_SCORE, Intent.POI]


def test_unrelated_part_of_a_combined_question_is_ignored(pipeline):
    answer = pipeline.reply("Розкажи про проєкт. Дай рецепт борщу")

    assert answer == "AI:project_info"
    assert len(pipeline.groq_calls()) == 1


# --- unsupported and incomplete questions ------------------------------------------


@pytest.mark.parametrize("question", ["Дай рецепт борщу", "Яка завтра погода?", "2 + 2"])
def test_unsupported_question_gets_a_polite_answer_without_database_or_model(pipeline, question):
    answer = pipeline.reply(question)

    assert "OKIL" in answer
    assert not pipeline.session_opened
    pipeline.groq.answer.assert_not_awaited()
    assert all(service.await_count == 0 for service in pipeline.services.values())


@pytest.mark.parametrize("question", ["Які місця поруч?", "Поясни оцінку цієї ділянки"])
def test_h3_scoped_question_without_a_cell_asks_for_one_before_any_lookup(pipeline, question):
    with pytest.raises(ChatReplyError) as error:
        pipeline.reply(question)

    assert error.value.code == "context_required"
    assert not pipeline.session_opened
    pipeline.groq.answer.assert_not_awaited()


# --- answer_intent contract ---------------------------------------------------------


@pytest.mark.parametrize("intent", [Intent.POI, Intent.HEX_SCORE])
def test_answer_intent_requires_an_h3_id_for_h3_scoped_intents(pipeline, intent):
    with pytest.raises(ValueError, match="H3 ID"):
        asyncio.run(answer_intent(pipeline.db, pipeline.groq, "Питання", intent, None))
    pipeline.groq.answer.assert_not_awaited()


def test_answer_intent_rejects_an_unsupported_intent(pipeline):
    with pytest.raises(ValueError, match="Unsupported intent"):
        asyncio.run(answer_intent(pipeline.db, pipeline.groq, "Питання", "not-an-intent", CELL))
    pipeline.groq.answer.assert_not_awaited()


def test_failure_of_the_model_is_not_hidden_as_an_answer(pipeline):
    pipeline.groq.answer.side_effect = RuntimeError("Groq returned HTTP 503")
    with pytest.raises(RuntimeError):
        pipeline.reply("Розкажи про проєкт")
