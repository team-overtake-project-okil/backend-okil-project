import asyncio
import logging
import re
from collections.abc import Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import h3
from sqlalchemy import text as sql_text
from sqlalchemy.exc import DBAPIError

from app.chat_sessions import ChatTurn

REPLY_TIMEOUT_SECONDS = 30
H3_ID_PATTERN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{15}(?![0-9a-fA-F])")
logger = logging.getLogger(__name__)
UI_CONTEXT_PREFIX = re.compile(
    r"\AДілянка Львова \(H3: [0-9a-fA-F]{15}\)\. Оцінка комфорту: [^\n]*"
    r"\. Категорії: [^\n]*\.\n\n(?P<question>.+)\Z", re.DOTALL
)
PROJECT_QUESTION = re.compile(
    r"(?:що таке|що це за|як працює|розкажи про|"
    r"для чого (?:потрібен|створений))\s+(?:про[єе]кт\s+)?(?:okil|окіл)[.?!]*",
    re.IGNORECASE,
)
SCORE_QUESTION = re.compile(
    r"\A(?:поясни|поясніть|чому|яка|який|що означає|розкажи)\b.*"
    r"\b(?:оцінк\w*|рейтинг\w*|бал\w*)\b", re.IGNORECASE
)
HEX_SCOPE = re.compile(
    r"\b(?:гекс\w*|ділян\w*|район\w*|комфорт\w*|така|цей|цієї|цього|тут)\b",
    re.IGNORECASE,
)


class ChatReplyError(Exception):
    """An error safe to return over the existing chat error envelope."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def hex_context(text: str, history: Sequence[ChatTurn]) -> str | None:
    """Use explicit user-supplied H3 context; never infer it from model output."""
    for question in [text, *(turn["question"] for turn in reversed(history))]:
        matches = {match.lower() for match in H3_ID_PATTERN.findall(question)}
        if not matches:
            continue
        if len(matches) != 1:
            raise ChatReplyError("invalid_context", "Вкажіть один H3 ID ділянки в запитанні.")
        cell = matches.pop()
        if not h3.is_valid_cell(cell) or h3.get_resolution(cell) != 9:
            raise ChatReplyError("invalid_context", "Вкажіть коректний H3 ID ділянки рівня 9.")
        return cell
    return None


@asynccontextmanager
async def _connected_session(session_factory):
    """Reconnect once before generation if the pool returned a dead connection."""
    for attempt in range(2):
        async with AsyncExitStack() as stack:
            try:
                db = await stack.enter_async_context(session_factory())
                await db.execute(sql_text("SELECT 1"))
            except DBAPIError as exc:
                if attempt == 0 and exc.connection_invalidated:
                    logger.info("Reconnecting chat database before AI generation")
                    continue
                raise
            # Provider calls are outside the retry block: never generate twice.
            yield db
            return


class ChatAIService:
    """Adapt #69's classifier and answer_intent without changing their APIs."""

    def __init__(self, classifier, client, answer_intent, session_factory, project_intent=None, score_intent=None):
        self.classifier = classifier
        self.client = client
        self.answer_intent = answer_intent
        self.session_factory = session_factory
        self.project_intent = project_intent
        self.score_intent = score_intent

    def _explicit_intent(self, question: str):
        if self.project_intent is not None and PROJECT_QUESTION.fullmatch(question.strip()):
            return self.project_intent
        if self.score_intent is not None and SCORE_QUESTION.search(question) and HEX_SCOPE.search(question):
            return self.score_intent
        return None

    async def reply(self, text: str, history: Sequence[ChatTurn]) -> str:
        # The map adds a context header, not extra questions. Never trust its
        # supplied scores: answer_intent fetches the actual facts from the DB.
        match = UI_CONTEXT_PREFIX.fullmatch(text)
        question = match.group("question") if match else text
        parsed = await self.classifier.parse_query(question)
        # Short, explicit product FAQs can be misclassified as POI by #69.
        # Correct only full FAQ fragments, never arbitrary appended instructions.
        parts = [
            (part.text, self._explicit_intent(part.text) or part.match.intent)
            for part in parsed.relevant_parts
        ]
        parts.extend((part, intent) for part in parsed.unrelated_parts
                     if (intent := self._explicit_intent(part)) is not None)
        if not parts:
            return "Я допомагаю з питаннями про OKIL, оцінки ділянок і місця в них. Уточніть запитання."

        needs_hex = any(
            intent.value in {"poi", "hex_score"}
            for _, intent in parts
        )
        cell = hex_context(text, history) if needs_hex else None
        if needs_hex and cell is None:
            raise ChatReplyError(
                "context_required",
                "Додайте H3 ID ділянки до запитання про її оцінку або місця.",
            )

        answers = []
        async with _connected_session(self.session_factory) as db:
            for part_text, intent in parts:
                answer = await self.answer_intent(
                    db, self.client, part_text, intent, cell
                )
                if not isinstance(answer, str) or not answer.strip():
                    raise ChatReplyError("ai_failed", "AI-сервіс не повернув текст відповіді. Спробуйте ще раз.")
                answers.append(answer.strip())
        return "\n\n".join(answers)


_service: ChatAIService | None = None
_service_lock = asyncio.Lock()


async def _create_service() -> ChatAIService:
    # #69 is a separate PR. Lazy imports keep non-chat endpoints available while
    # that dependency/model/key is absent; no test answer is used as a fallback.
    try:
        from app.ai_assistant import GroqClient, answer_intent
        from app.database import async_session_maker
        from app.intent_classifier import Intent, create_default_intent_classifier

        client = GroqClient()
        classifier = await create_default_intent_classifier()
    except Exception as exc:
        logger.warning("Chat AI initialization failed (%s)", type(exc).__name__)
        raise ChatReplyError(
            "ai_unavailable", "AI-сервіс недоступний. Перевірте його налаштування або спробуйте пізніше."
        ) from None
    return ChatAIService(classifier, client, answer_intent, async_session_maker, Intent.PROJECT_INFO, Intent.HEX_SCORE)


async def _get_service() -> ChatAIService:
    global _service
    if _service is None:
        async with _service_lock:
            if _service is None:
                _service = await _create_service()
    return _service


async def preload_ai() -> None:
    """Optionally load the local classifier before accepting chat requests."""
    try:
        await _get_service()
    except ChatReplyError:
        # Initialization already logged a safe warning. Other endpoints can
        # still start, and the next chat request may retry initialization.
        pass


async def generate_reply(text: str, history: Sequence[ChatTurn]) -> str:
    """Return #69's answer using its supported question, intent and H3 context."""
    try:
        service = await _get_service()
        return await service.reply(text, history)
    except ChatReplyError:
        raise
    except Exception as exc:
        logger.warning("Chat AI reply failed (%s)", type(exc).__name__)
        # Never expose provider response bodies, credentials, SQL or prompts.
        raise ChatReplyError(
            "ai_failed", "Не вдалося отримати відповідь AI-сервісу. Спробуйте ще раз."
        ) from None
