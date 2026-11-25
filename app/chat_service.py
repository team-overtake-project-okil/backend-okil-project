import asyncio
import logging
import re
from collections.abc import Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import h3
from sqlalchemy import text as sql_text
from sqlalchemy.exc import DBAPIError

from app.chat_sessions import ChatTurn
from app.chat_dialogue import NEED_CELL, resolve

# Allow up to a minute for TravelTime and leave time to generate the reply.
REPLY_TIMEOUT_SECONDS = 90
NEXT_PROMPT_TIMEOUT_SECONDS = 3.0
MAX_NEXT_PROMPT_LENGTH = 240
UNRELATED_REPLY = "Я допомагаю з питаннями про OKIL, оцінки ділянок і місця в них. Уточніть запитання."
H3_ID_PATTERN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{15}(?![0-9a-fA-F])")
HEX_CONTEXT_INTENTS = frozenset({
    "poi", "hex_score", "hex_overview", "mobility_routes",
    "weights_simulation", "transit_lines", "neighborhood_context", "infrastructure_gaps",
})
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
PROJECT_CATEGORIES_QUESTION = re.compile(
    r"\A(?:які(?:\s+саме)?|що за|назви|перелічи|поясни|розкажи про)\s+"
    r"(?:категор\w*|критері\w*)\b[^?!.]*\b(?:okil|окіл)[.?!]*\Z",
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
        question = question.strip()
        if self.project_intent is not None and (
            PROJECT_QUESTION.fullmatch(question)
            or (
                PROJECT_CATEGORIES_QUESTION.fullmatch(question)
                and not HEX_SCOPE.search(question)
                and not H3_ID_PATTERN.search(question)
            )
        ):
            return self.project_intent
        if self.score_intent is not None and SCORE_QUESTION.search(question) and HEX_SCOPE.search(question):
            return self.score_intent
        return None

    def _resolved_parts(self, parsed):
        # Product FAQs can be misclassified as POI/score by the embedding model.
        # Apply identical corrections to answers and candidate follow-up questions.
        parts = [
            (
                part.text,
                (self._explicit_intent(part.text) or part.match.intent)
                if part.match.intent.value in {"poi", "hex_score", "project_info"}
                else part.match.intent,
            )
            for part in parsed.relevant_parts
        ]
        parts.extend((part, intent) for part in parsed.unrelated_parts
                     if (intent := self._explicit_intent(part)) is not None)
        return parts

    async def supports_next_prompt(self, question: str, cell: str | None) -> bool:
        suggested_cells = {match.lower() for match in H3_ID_PATTERN.findall(question)}
        if suggested_cells and suggested_cells != {cell}:
            return False
        parsed = await self.classifier.parse_query(question)
        if any(self._explicit_intent(part) is None for part in parsed.unrelated_parts):
            return False
        parts = self._resolved_parts(parsed)
        return bool(parts) and all(
            intent is not None
            and (cell is not None or intent.value not in HEX_CONTEXT_INTENTS)
            for _, intent in parts
        )

    async def reply(self, text: str, history: Sequence[ChatTurn], *, state=None, on_part=None, user_weights=None, mobility=None) -> str:
        # The map adds a context header, not extra questions. Never trust its
        # supplied scores: answer_intent fetches the actual facts from the DB.
        resolved = resolve(_question_text(text), history)
        if resolved.answer:
            if state is not None:
                state["social"] = True
            return resolved.answer
        question = resolved.question
        if (not resolved.intent and hasattr(type(self.client), "dialogue_choice")
                and len(question.split()) <= 8 and not H3_ID_PATTERN.search(question)):
            previous = next((t for t in reversed(history) if not t.get("social")), {})
            choices = {"greet": "тільки привітання", "thanks": "тільки подяка",
                       "unrelated": "суто сторонній запит поза OKIL, картою, міським середовищем, "
                                    "місцями, транспортом, ділянками та їх оцінками"}
            if previous.get("offerIntent"):
                choices["confirm"] = "згода виконати щойно запропоновану дію"
            if previous.get("activeIntent") == "top_hexagons":
                choices.update(worst="ті самі гекси з найнижчими оцінками",
                               best="ті самі гекси з найвищими оцінками")
            try:
                choice = await self.client.dialogue_choice(question, choices)
            except Exception as exc:
                logger.info("Dialogue classification skipped (%s)", type(exc).__name__)
                choice = "other"
            equivalents = {"greet": "привіт", "thanks": "дякую", "confirm": "так",
                           "worst": "і найгірших", "best": "і найкращих"}
            if choice == "unrelated":
                return UNRELATED_REPLY
            if choice in choices:
                resolved = resolve(equivalents[choice], history)
                if resolved.answer:
                    if state is not None:
                        state["social"] = True
                    return resolved.answer
                question = resolved.question
        query_embeddings = {}
        if resolved.intent:
            from app.intent_classifier import Intent
            parts = [(question, Intent(resolved.intent))]
        else:
            parsed = await self.classifier.parse_query(question)
            parts = self._resolved_parts(parsed)
            query_embeddings = {
                part.text: getattr(part.match, "embedding", None)
                for part in parsed.relevant_parts
            }
        if not parts:
            return UNRELATED_REPLY

        # Об'єднуємо фрагменти з однаковим інтентом в один запит до LLM
        merged_parts: list[tuple[str, object]] = []
        for part_text, intent in parts:
            if merged_parts and merged_parts[-1][1] == intent:
                prev_text, _ = merged_parts[-1]
                merged_parts[-1] = (f"{prev_text} {part_text}", intent)
            else:
                merged_parts.append((part_text, intent))
        parts = merged_parts

        if state is not None and len(parts) == 1:
            state.update(activeQuestion=parts[0][0], activeIntent=parts[0][1].value)

        needs_hex = any(intent.value in HEX_CONTEXT_INTENTS for _, intent in parts)
        cell = hex_context(text, history) if needs_hex else None
        if needs_hex and cell is None:
            if state is not None and "activeQuestion" in state:
                return NEED_CELL
            raise ChatReplyError(
                "context_required",
                "Додайте H3 ID ділянки до запитання про її оцінку або місця.",
            )

        answers = []
        async with _connected_session(self.session_factory) as db:
            for part_text, intent in parts:
                query_embedding = query_embeddings.get(part_text)
                kwargs = (
                    {"query_embedding": query_embedding}
                    if intent.value == "project_info" and query_embedding is not None
                    else {}
                )
                if intent.value == "top_hexagons":
                    if user_weights is not None:
                        kwargs["user_weights"] = user_weights
                    if mobility is not None:
                        kwargs["mobility"] = mobility
                answer = await self.answer_intent(
                    db, self.client, part_text, intent, cell, **kwargs
                )
                if not isinstance(answer, str) or not answer.strip():
                    raise ChatReplyError("ai_failed", "AI-сервіс не повернув текст відповіді. Спробуйте ще раз.")
                answers.append(answer.strip())
                if on_part is not None:
                    await on_part(("\n\n" if len(answers) > 1 else "") + answers[-1])
        return "\n\n".join(answers)


_service: ChatAIService | None = None
_service_lock = asyncio.Lock()


async def _create_service() -> ChatAIService:
    # Lazy imports keep non-chat endpoints available when chat setup is missing.
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
    """Optionally prepare intent vectors before accepting chat requests."""
    try:
        await _get_service()
    except ChatReplyError:
        # Initialization already logged a safe warning. Other endpoints can
        # still start, and the next chat request may retry initialization.
        pass


async def generate_reply(text: str, history: Sequence[ChatTurn], **options) -> str:
    """Return #69's answer using its supported question, intent and H3 context."""
    try:
        resolved = resolve(_question_text(text), history)
        if resolved.answer:
            if options.get("state") is not None:
                options["state"]["social"] = True
            return resolved.answer
        service = await _get_service()
        return await service.reply(text, history, **options)
    except ChatReplyError:
        raise
    except Exception as exc:
        logger.warning("Chat AI reply failed (%s)", type(exc).__name__)
        # Never expose provider response bodies, credentials, SQL or prompts.
        raise ChatReplyError(
            "ai_failed", "Не вдалося отримати відповідь AI-сервісу. Спробуйте ще раз."
        ) from None


def _question_text(text: str) -> str:
    match = UI_CONTEXT_PREFIX.fullmatch(text)
    return match.group("question") if match else text


def _normalized_prompt(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def _validated_next_prompt(suggestion: str | None, previous_texts: Sequence[str]) -> str | None:
    if not isinstance(suggestion, str):
        return None
    suggestion = suggestion.strip()
    # Keep this an optional plain-text field, never provider JSON, markup or links.
    if (
        not suggestion
        or len(suggestion) > MAX_NEXT_PROMPT_LENGTH
        or any(not char.isprintable() for char in suggestion)
        or re.search(r"[<>{}\[\]`*_#]|https?://|www\.", suggestion, re.IGNORECASE)
    ):
        return None
    normalized = _normalized_prompt(suggestion)
    if not normalized or normalized in {_normalized_prompt(text) for text in previous_texts}:
        return None
    return suggestion


async def generate_next_prompt(
    text: str,
    answer: str,
    history: Sequence[ChatTurn],
    *,
    timeout_seconds: float = NEXT_PROMPT_TIMEOUT_SECONDS,
    state=None,
) -> str | None:
    """Best-effort enrichment; a provider failure must never discard a ready answer."""
    if timeout_seconds <= 0 or answer == UNRELATED_REPLY:
        return None
    try:
        async with asyncio.timeout(min(timeout_seconds, NEXT_PROMPT_TIMEOUT_SECONDS)):
            service = await _get_service()
            question = _question_text(text)
            previous_questions = [_question_text(turn["question"]) for turn in history]
            previous_suggestions = [turn["nextPrompt"] for turn in history if "nextPrompt" in turn]
            cell = hex_context(text, history)
            suggestion = await service.client.next_prompt(
                question, answer, cell, previous_questions, previous_suggestions
            )
            suggestion = _validated_next_prompt(
                suggestion, [question, *previous_questions, *previous_suggestions]
            )
            if suggestion is None:
                return None
            if state is not None:
                candidate = resolve(suggestion, history)
                if candidate.answer:
                    return None
                if candidate.intent:
                    if candidate.intent in HEX_CONTEXT_INTENTS and not cell:
                        return None
                    state.update(offerQuestion=candidate.question, offerIntent=candidate.intent)
                else:
                    parsed = await service.classifier.parse_query(suggestion)
                    parts = service._resolved_parts(parsed)
                    if parsed.unrelated_parts or len(parts) != 1:
                        return None
                    part, intent = parts[0]
                    if intent.value in HEX_CONTEXT_INTENTS and not cell:
                        return None
                    if any(h.lower() != cell for h in H3_ID_PATTERN.findall(part)):
                        return None
                    state.update(offerQuestion=part, offerIntent=intent.value)
            elif not await service.supports_next_prompt(suggestion, cell):
                return None
            return suggestion
    except Exception as exc:
        # Cancellation still propagates, including when the socket disconnects.
        logger.info("Chat next-prompt generation skipped (%s)", type(exc).__name__)
        return None
