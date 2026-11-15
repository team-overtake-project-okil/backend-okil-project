import asyncio
import sys
from contextlib import asynccontextmanager
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import h3
import pytest
from sqlalchemy.exc import InterfaceError

from app import chat_service
from app.chat_service import ChatAIService, ChatReplyError, hex_context

CELL = h3.latlng_to_cell(49.84, 24.03, 9)
OTHER_CELL = h3.latlng_to_cell(49.82, 23.99, 9)


class Intent(str, Enum):
    PROJECT = "project_info"
    POI = "poi"
    SCORE = "hex_score"


def part(text, intent):
    return SimpleNamespace(text=text, match=SimpleNamespace(intent=intent))


def turn(question, answer="AI answer"):
    return {"requestId": "previous", "question": question, "answer": answer}


def backend(parts, answer="Відповідь AI"):
    classifier = SimpleNamespace(parse_query=AsyncMock(return_value=SimpleNamespace(relevant_parts=parts, unrelated_parts=[])))
    answer_intent = AsyncMock(return_value=answer)
    db = Mock(execute=AsyncMock())

    @asynccontextmanager
    async def session():
        yield db

    service = ChatAIService(classifier, object(), answer_intent, session)
    return service, answer_intent, db


def test_current_context_overrides_history_and_case_is_normalized():
    assert hex_context(f"Оцінка {CELL.upper()}", [turn(OTHER_CELL)]) == CELL


def test_last_user_question_provides_context_not_assistant_answer():
    assert hex_context("А які місця?", [turn(CELL), turn("Дякую", OTHER_CELL)]) == CELL
    assert hex_context("Які місця?", [turn("Що таке OKIL?", CELL)]) is None


@pytest.mark.parametrize("text", ["000000000000000", h3.latlng_to_cell(49.84, 24.03, 8), f"{CELL} і {OTHER_CELL}"])
def test_invalid_or_ambiguous_current_context_never_reuses_old_cell(text):
    with pytest.raises(ChatReplyError) as error:
        hex_context(text, [turn(CELL)])
    assert error.value.code == "invalid_context"


def test_project_question_uses_the_upstream_api_and_db_session():
    service, answer, db = backend([part("Що таке OKIL?", Intent.PROJECT)])
    assert asyncio.run(service.reply("Що таке OKIL?", [])) == "Відповідь AI"
    answer.assert_awaited_once_with(db, service.client, "Що таке OKIL?", Intent.PROJECT, None)


def test_map_header_is_context_not_extra_questions_and_scores_come_from_db():
    question = "Чому така оцінка?"
    service, answer, db = backend([part(question, Intent.SCORE)])
    message = f"Ділянка Львова (H3: {CELL}). Оцінка комфорту: 999/100. Категорії: Здоров'я: 999/100; Освіта: 1/100.\n\n{question}"
    assert asyncio.run(service.reply(message, [])) == "Відповідь AI"
    service.classifier.parse_query.assert_awaited_once_with(question)
    answer.assert_awaited_once_with(db, service.client, question, Intent.SCORE, CELL)


def test_plain_multiline_question_is_not_stripped_as_ui_context():
    service, _, _ = backend([part("OKIL", Intent.PROJECT)])
    question = "Що таке OKIL?\n\nПоясни коротко."
    asyncio.run(service.reply(question, []))
    service.classifier.parse_query.assert_awaited_once_with(question)


@pytest.mark.parametrize("question", ["Що таке проєкт OKIL?", "Що таке OKIL?", "Для чого потрібен Окіл?", "Як працює Окіл?"])
def test_explicit_project_faq_does_not_require_hex_context(question):
    service, answer, db = backend([part(question.rstrip("?"), Intent.POI)])
    service.project_intent = Intent.PROJECT
    asyncio.run(service.reply(question, []))
    answer.assert_awaited_once_with(db, service.client, question.rstrip("?"), Intent.PROJECT, None)


def test_project_faq_rule_does_not_override_other_questions_or_forward_unrelated_text():
    service, answer, db = backend([part("Які місця в OKIL?", Intent.POI)])
    service.project_intent = Intent.PROJECT
    with pytest.raises(ChatReplyError) as error:
        asyncio.run(service.reply("Які місця в OKIL?", []))
    assert error.value.code == "context_required"
    answer.assert_not_awaited()


def test_low_similarity_explicit_project_faq_is_still_supported():
    service, answer, db = backend([])
    service.project_intent = Intent.PROJECT
    service.classifier.parse_query.return_value.unrelated_parts = ["Що таке OKIL", "дай рецепт"]
    asyncio.run(service.reply("Що таке OKIL? Дай рецепт", []))
    answer.assert_awaited_once_with(db, service.client, "Що таке OKIL", Intent.PROJECT, None)


@pytest.mark.parametrize("question", ["Поясни коротко оцінку цієї ділянки", "Чому така оцінка", "Яка оцінка цього гекса"])
def test_explicit_hex_score_question_is_not_sent_to_poi_service(question):
    service, answer, db = backend([part(question, Intent.POI)])
    service.score_intent = Intent.SCORE
    asyncio.run(service.reply(question, [turn(CELL)]))
    answer.assert_awaited_once_with(db, service.client, question, Intent.SCORE, CELL)


def test_explicit_score_rule_does_not_override_a_place_search():
    service, answer, db = backend([part("Назви клініку з оцінкою", Intent.POI)])
    service.score_intent = Intent.SCORE
    asyncio.run(service.reply("Назви клініку з оцінкою", [turn(CELL)]))
    answer.assert_awaited_once_with(db, service.client, "Назви клініку з оцінкою", Intent.POI, CELL)


def test_only_relevant_parts_are_forwarded_with_supported_h3_context():
    parts = [part("Поясни оцінку", Intent.SCORE), part("Покажи місця", Intent.POI)]
    service, answer, db = backend(parts)
    answer.side_effect = ["Оцінка AI", "Місця AI"]
    result = asyncio.run(service.reply(f"Поясни оцінку {CELL} та покажи місця, а ще рецепт", []))
    assert result == "Оцінка AI\n\nМісця AI"
    assert [call.args for call in answer.await_args_list] == [
        (db, service.client, "Поясни оцінку", Intent.SCORE, CELL),
        (db, service.client, "Покажи місця", Intent.POI, CELL),
    ]


def test_missing_hex_returns_actionable_error_before_database_or_model():
    service, answer, _ = backend([part("Які тут місця?", Intent.POI)])
    service.session_factory = Mock(side_effect=AssertionError("No DB without context"))
    with pytest.raises(ChatReplyError) as error:
        asyncio.run(service.reply("Які тут місця?", []))
    assert error.value.code == "context_required"
    answer.assert_not_awaited()


def test_unrelated_question_does_not_query_database_or_generate_content():
    service, answer, _ = backend([])
    service.session_factory = Mock(side_effect=AssertionError("No DB for unrelated requests"))
    assert "OKIL" in asyncio.run(service.reply("Рецепт пирога", []))
    answer.assert_not_awaited()


@pytest.mark.parametrize("text", [None, "", "  ", {"text": "unexpected"}])
def test_empty_or_malformed_answer_is_not_returned_as_success(text):
    service, _, _ = backend([part("OKIL", Intent.PROJECT)], text)
    with pytest.raises(ChatReplyError) as error:
        asyncio.run(service.reply("OKIL", []))
    assert error.value.code == "ai_failed"


def test_provider_errors_are_sanitized(monkeypatch):
    service, answer, _ = backend([part("OKIL", Intent.PROJECT)])
    answer.side_effect = RuntimeError("Bearer SECRET; SQL connection password; raw provider body")
    monkeypatch.setattr(chat_service, "_get_service", AsyncMock(return_value=service))
    with pytest.raises(ChatReplyError) as error:
        asyncio.run(chat_service.generate_reply("OKIL", []))
    assert error.value.code == "ai_failed"
    assert "SECRET" not in str(error.value)
    assert "password" not in str(error.value)


def test_cancellation_propagates_to_provider(monkeypatch):
    cancelled = False

    class PendingService:
        async def reply(self, text, history):
            nonlocal cancelled
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True

    monkeypatch.setattr(chat_service, "_get_service", AsyncMock(return_value=PendingService()))

    async def scenario():
        task = asyncio.create_task(chat_service.generate_reply("OKIL", []))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert cancelled


def test_service_initializes_once_for_concurrent_requests(monkeypatch):
    service = object()

    async def scenario():
        monkeypatch.setattr(chat_service, "_service", None)
        monkeypatch.setattr(chat_service, "_service_lock", asyncio.Lock())
        create = AsyncMock(return_value=service)
        monkeypatch.setattr(chat_service, "_create_service", create)
        assert await asyncio.gather(chat_service._get_service(), chat_service._get_service()) == [service, service]
        create.assert_awaited_once()

    asyncio.run(scenario())


def test_failed_initialization_can_be_retried(monkeypatch):
    service = object()

    async def scenario():
        monkeypatch.setattr(chat_service, "_service", None)
        monkeypatch.setattr(chat_service, "_service_lock", asyncio.Lock())
        monkeypatch.setattr(chat_service, "_create_service", AsyncMock(side_effect=[
            ChatReplyError("ai_unavailable", "AI unavailable"), service,
        ]))
        with pytest.raises(ChatReplyError):
            await chat_service._get_service()
        assert await chat_service._get_service() is service

    asyncio.run(scenario())


def test_missing_ai_dependency_returns_readable_unavailable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.ai_assistant", None)
    with pytest.raises(ChatReplyError) as error:
        asyncio.run(chat_service._create_service())
    assert error.value.code == "ai_unavailable"
    assert "Traceback" not in str(error.value)


def test_preload_initializes_the_same_cached_service(monkeypatch):
    get_service = AsyncMock(return_value=object())
    monkeypatch.setattr(chat_service, "_get_service", get_service)
    asyncio.run(chat_service.preload_ai())
    get_service.assert_awaited_once()


def test_preload_failure_keeps_non_chat_endpoints_available(monkeypatch):
    monkeypatch.setattr(chat_service, "_get_service", AsyncMock(side_effect=
        ChatReplyError("ai_unavailable", "AI unavailable")))
    asyncio.run(chat_service.preload_ai())


@pytest.mark.parametrize("failure_stage", ["enter", "probe"])
def test_dead_connection_reconnects_before_ai_call(failure_stage):
    service, answer, good_db = backend([part("OKIL", Intent.PROJECT)])
    attempts = []
    closed = InterfaceError("SELECT 1", {}, Exception("connection is closed"), connection_invalidated=True)
    bad_db = Mock(execute=AsyncMock(side_effect=closed))

    @asynccontextmanager
    async def sessions():
        attempts.append(len(attempts))
        if len(attempts) == 1:
            if failure_stage == "enter":
                raise closed
            yield bad_db
        else:
            yield good_db

    service.session_factory = sessions
    assert asyncio.run(service.reply("OKIL", [])) == "Відповідь AI"
    assert len(attempts) == 2
    answer.assert_awaited_once_with(good_db, service.client, "OKIL", Intent.PROJECT, None)


def test_database_probe_failure_is_bounded_and_does_not_call_ai():
    service, answer, db = backend([part("OKIL", Intent.PROJECT)])
    db.execute.side_effect = InterfaceError("SELECT 1", {}, Exception("closed"), connection_invalidated=True)
    with pytest.raises(InterfaceError):
        asyncio.run(service.reply("OKIL", []))
    assert db.execute.await_count == 2
    answer.assert_not_awaited()


def test_database_statement_errors_are_not_retried():
    service, answer, db = backend([part("OKIL", Intent.PROJECT)])
    db.execute.side_effect = InterfaceError("SELECT 1", {}, Exception("other error"), connection_invalidated=False)
    with pytest.raises(InterfaceError):
        asyncio.run(service.reply("OKIL", []))
    db.execute.assert_awaited_once()
    answer.assert_not_awaited()


def test_failure_after_generation_started_does_not_retry_provider():
    service, answer, db = backend([part("OKIL", Intent.PROJECT)])
    answer.side_effect = InterfaceError("provider-side work", {}, Exception("closed"), connection_invalidated=True)
    with pytest.raises(InterfaceError):
        asyncio.run(service.reply("OKIL", []))
    db.execute.assert_awaited_once()
    answer.assert_awaited_once()


@pytest.fixture
def next_prompt_client(monkeypatch):
    client = SimpleNamespace(next_prompt=AsyncMock(return_value="Які місця є в цій ділянці?"))
    service, _, _ = backend([part("Які місця є в цій ділянці?", Intent.POI)])
    service.client = client
    monkeypatch.setattr(chat_service, "_get_service", AsyncMock(return_value=service))
    return client


def test_next_prompt_uses_current_answer_and_explicit_context(next_prompt_client):
    history = [turn(f"Поясни оцінку {CELL}")]
    history[0]["nextPrompt"] = "Як працює OKIL?"
    question = "Чому така оцінка?"
    text = f"Ділянка Львова (H3: {OTHER_CELL}). Оцінка комфорту: 999/100. Категорії: Освіта: 999/100.\n\n{question}"
    assert asyncio.run(chat_service.generate_next_prompt(text, "Оцінка 79", history)) == "Які місця є в цій ділянці?"
    next_prompt_client.next_prompt.assert_awaited_once_with(
        question, "Оцінка 79", OTHER_CELL, [history[0]["question"]], ["Як працює OKIL?"],
    )


def test_next_prompt_can_use_previous_user_hex_but_never_model_hex(next_prompt_client):
    asyncio.run(chat_service.generate_next_prompt("Чому?", "Оцінка 79", [turn(CELL)]))
    assert next_prompt_client.next_prompt.await_args.args[2] == CELL
    asyncio.run(chat_service.generate_next_prompt("Що таке OKIL?", CELL, []))
    assert next_prompt_client.next_prompt.await_args.args[2] is None


@pytest.mark.parametrize("suggestion", [
    None, "", "  ", {"text": "Питання?"}, "а" * 241,
    "Перше питання?\nДруге питання?", "<b>Питання?</b>", "**Питання?**",
    "[Питання](https://example.com)", "Дивись https://example.com", "```json```",
    '{"next_prompt": "Питання?"}', "ЩО таке OKIL?!", "Як працює карта?",
    "Які категорії оцінює OKIL?",
])
def test_invalid_or_repeated_next_prompts_are_omitted(next_prompt_client, suggestion):
    next_prompt_client.next_prompt.return_value = suggestion
    previous = turn("Як працює карта?")
    previous["nextPrompt"] = "Які категорії оцінює OKIL?"
    assert asyncio.run(chat_service.generate_next_prompt("Що таке OKIL?", "Опис", [previous])) is None


def test_unrelated_answer_or_exhausted_deadline_does_not_call_ai(next_prompt_client):
    assert asyncio.run(chat_service.generate_next_prompt("Рецепт", chat_service.UNRELATED_REPLY, [])) is None
    assert asyncio.run(chat_service.generate_next_prompt("OKIL", "Опис", [], timeout_seconds=0)) is None
    next_prompt_client.next_prompt.assert_not_awaited()


def test_next_prompt_failure_is_optional_and_logs_no_provider_details(next_prompt_client, caplog):
    next_prompt_client.next_prompt.side_effect = RuntimeError("Bearer SECRET; raw provider body")
    with caplog.at_level("INFO", logger="app.chat_service"):
        assert asyncio.run(chat_service.generate_next_prompt("OKIL", "Опис", [])) is None
    assert "RuntimeError" in caplog.text
    assert "SECRET" not in caplog.text


def test_next_prompt_timeout_cancels_provider(next_prompt_client):
    cancelled = False

    async def pending(*args):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    next_prompt_client.next_prompt.side_effect = pending
    assert asyncio.run(chat_service.generate_next_prompt("OKIL", "Опис", [], timeout_seconds=0.01)) is None
    assert cancelled


def test_next_prompt_cancellation_is_not_swallowed(next_prompt_client):
    async def scenario():
        started = asyncio.Event()

        async def pending(*args):
            started.set()
            await asyncio.Event().wait()

        next_prompt_client.next_prompt.side_effect = pending
        task = asyncio.create_task(chat_service.generate_next_prompt("OKIL", "Опис", []))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize("question", [
    "Які категорії оцінює OKIL?",
    "Які саме категорії оцінювання доступності показує OKIL?",
    "Які критерії використовуються для оцінки категорії «Здоров’я» у проєкті OKIL?",
    "Поясни категорії оцінювання в Окіл",
])
def test_generated_project_category_question_can_be_answered_without_hex(question, monkeypatch):
    service, answer, db = backend([part(question.rstrip("?"), Intent.SCORE)])
    service.project_intent = Intent.PROJECT
    service.client = SimpleNamespace(next_prompt=AsyncMock(return_value=question))
    monkeypatch.setattr(chat_service, "_get_service", AsyncMock(return_value=service))

    suggestion = asyncio.run(chat_service.generate_next_prompt("Що таке OKIL?", "Опис проєкту", []))
    assert suggestion == question
    assert asyncio.run(service.reply(suggestion, [turn("Що таке OKIL?")])) == "Відповідь AI"
    answer.assert_awaited_once_with(db, service.client, question.rstrip("?"), Intent.PROJECT, None)


@pytest.mark.parametrize("question", [
    "Які категорії знижують оцінку цієї ділянки в OKIL?",
    f"Які категорії найслабші в {CELL} у OKIL?",
    "Назви місця категорії Здоров’я в OKIL?",
])
def test_category_faq_correction_does_not_override_cell_or_place_questions(question):
    service, _, _ = backend([])
    service.project_intent = Intent.PROJECT
    assert service._explicit_intent(question) is None


@pytest.mark.parametrize("intent, cell, expected", [
    (Intent.POI, None, None),
    (Intent.SCORE, None, None),
    (Intent.POI, CELL, "Які місця є в цій ділянці?"),
])
def test_next_prompt_requiring_missing_context_is_omitted(monkeypatch, intent, cell, expected):
    suggestion = "Які місця є в цій ділянці?"
    service, _, _ = backend([part(suggestion, intent)])
    service.client = SimpleNamespace(next_prompt=AsyncMock(return_value=suggestion))
    monkeypatch.setattr(chat_service, "_get_service", AsyncMock(return_value=service))
    history = [turn(cell)] if cell else []
    assert asyncio.run(chat_service.generate_next_prompt("OKIL", "Опис", history)) == expected


def test_unrelated_or_model_invented_hex_suggestion_is_omitted(monkeypatch):
    service, _, _ = backend([])
    service.classifier.parse_query.return_value.unrelated_parts = ["Дай рецепт"]
    service.client = SimpleNamespace(next_prompt=AsyncMock(return_value="Дай рецепт"))
    monkeypatch.setattr(chat_service, "_get_service", AsyncMock(return_value=service))
    assert asyncio.run(chat_service.generate_next_prompt("OKIL", "Опис", [])) is None
    service.client.next_prompt.return_value = f"Яка оцінка {OTHER_CELL}?"
    service.classifier.parse_query.reset_mock()
    assert asyncio.run(chat_service.generate_next_prompt("OKIL", "Опис", [turn(CELL)])) is None
    service.classifier.parse_query.assert_not_awaited()


def test_suggestion_classifier_failure_does_not_discard_main_answer(monkeypatch):
    service, _, _ = backend([])
    service.classifier.parse_query.side_effect = RuntimeError("classification failed")
    service.client = SimpleNamespace(next_prompt=AsyncMock(return_value="Які категорії оцінює OKIL?"))
    monkeypatch.setattr(chat_service, "_get_service", AsyncMock(return_value=service))
    assert asyncio.run(chat_service.generate_next_prompt("Що таке OKIL?", "Опис", [])) is None
