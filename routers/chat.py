import asyncio
import logging
import secrets
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError

from app.chat_protocol import ChatProtocolError, parse_message, parse_question, parse_request_id, parse_session_token
from app.chat_service import ChatReplyError, REPLY_TIMEOUT_SECONDS, generate_next_prompt, generate_reply
from app.chat_sessions import ChatSessionError, ChatSessionStore, ChatTurn
from app.config import settings
from app.chat_dialogue import NEED_CELL
from app.schemas import HexagonWeights
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from typing import Annotated


class MapMobility(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    transportation: str = "bus"
    anchor_lat: list[Annotated[float, Field(ge=-90, le=90)]] | None = None
    anchor_lon: list[Annotated[float, Field(ge=-180, le=180)]] | None = None
    weight: list[Annotated[float, Field(ge=0)]] | None = None

    @model_validator(mode="after")
    def matching_destinations(self):
        if self.anchor_lat is None and self.anchor_lon is None and self.weight is None:
            return self
        if (not self.anchor_lat or not self.anchor_lon or len(self.anchor_lat) != len(self.anchor_lon)
                or self.weight is not None and len(self.weight) != len(self.anchor_lat)):
            raise ValueError("Provide matching destination coordinates and weights.")
        return self
router = APIRouter()
logger = logging.getLogger(__name__)

SendEvent = Callable[[dict[str, object]], Awaitable[None]]


async def send_error(send: SendEvent, code: str, message: str, request_id: str | None = None) -> None:
    await send({"type": "error", "requestId": request_id, "code": code, "message": message})


async def run_reply(
    store: ChatSessionStore, token: str, request_id: str, question: str, send: SendEvent,
    stream: bool = False,
    weights: dict[str, float] | None = None,
    mobility: dict | None = None,
) -> None:
    attempt = secrets.token_hex(16)
    # A cancelled Redis write may already have applied. Release only our own attempt.
    needs_cleanup = True
    error: tuple[str, str] | None = None
    try:
        history, answer = await store.begin(
            token, request_id, question, attempt, REPLY_TIMEOUT_SECONDS + 30
        )
        needs_cleanup = answer is None
        next_prompt = None
        await send({"type": "message.accepted", "requestId": request_id})
        if answer is None:
            await send({"type": "answer.status", "requestId": request_id, "stage": "generating"})
            deadline = asyncio.get_running_loop().time() + REPLY_TIMEOUT_SECONDS
            state = {}

            async def send_part(part: str) -> None:
                for chunk in re.findall(r".+?(?:[.!?](?=\s)|\Z)", part, re.DOTALL):
                    await send({"type": "answer.delta", "requestId": request_id, "text": chunk})

            try:
                answer = await asyncio.wait_for(
                    generate_reply(question, history, state=state, on_part=send_part if stream else None,
                                   **({"user_weights": weights} if weights is not None else {}),
                                   **({"mobility": mobility} if mobility is not None else {})),
                    REPLY_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                raise ChatSessionError("reply_timeout", "The reply took too long.") from None
            except ChatReplyError as exc:
                raise ChatSessionError(exc.code, str(exc)) from None
            except Exception as exc:
                logger.error("Chat reply generation failed (%s)", type(exc).__name__)
                raise ChatSessionError("reply_failed", "The reply could not be prepared.") from None

            if not state.get("social") and answer != NEED_CELL:
                prompt_options = {}
                prompt_history = history
                if state.get("activeQuestion"):
                    prompt_options["state"] = state
                    prompt_history = [*history, {"question": question, "answer": answer, **state}]
                next_prompt = await generate_next_prompt(
                    state.get("activeQuestion", question), answer, prompt_history, **prompt_options,
                    timeout_seconds=max(0.0, deadline - asyncio.get_running_loop().time()),
                )
            turn: ChatTurn = {"requestId": request_id, "question": question, "answer": answer}
            turn.update(state)
            if next_prompt is not None:
                turn["nextPrompt"] = next_prompt
            await store.complete(token, attempt, turn)
            needs_cleanup = False
        else:
            next_prompt = next(turn.get("nextPrompt") for turn in history if turn["requestId"] == request_id)
        event: dict[str, object] = {"type": "answer.completed", "requestId": request_id, "text": answer}
        if next_prompt is not None:
            event["nextPrompt"] = next_prompt
        await send(event)
    except WebSocketDisconnect:
        return
    except ChatSessionError as exc:
        error = (exc.code, str(exc))
    except RedisError:
        error = ("storage_unavailable", "Chat storage is temporarily unavailable.")
    finally:
        if needs_cleanup:
            with suppress(ChatSessionError, RedisError):
                await store.abort(token, attempt)

    if error:
        with suppress(WebSocketDisconnect):
            await send_error(send, *error, request_id=request_id)


@router.websocket("/ws/chat")
async def chat_websocket(websocket: WebSocket) -> None:
    if websocket.headers.get("origin") not in settings.CORS_ORIGINS:
        await websocket.close()
        return

    await websocket.accept()
    store: ChatSessionStore = websocket.app.state.chat_sessions
    token: str | None = None
    reply_task: asyncio.Task[None] | None = None
    send_lock = asyncio.Lock()

    async def send(event: dict[str, object]) -> None:
        async with send_lock:
            await websocket.send_json(event)

    try:
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                return

            request_id = None
            try:
                payload = parse_message(frame.get("text"))
                if payload.get("type") == "session.clear":
                    request_id = parse_request_id(payload)
                    try:
                        supplied_token = parse_session_token(payload.get("sessionToken"))
                    except ChatProtocolError as exc:
                        raise ChatSessionError(exc.code, str(exc)) from None
                    if supplied_token is None:
                        raise ChatSessionError("invalid_message", "Provide sessionToken.")
                    if token is None or not secrets.compare_digest(token, supplied_token):
                        raise ChatSessionError("session_required", "Open this session before clearing it.")
                    deleted = await store.clear(supplied_token)
                    token = None
                    if reply_task:
                        reply_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await reply_task
                        reply_task = None
                    await send({"type": "session.cleared", "requestId": request_id, "deleted": deleted})
                    continue

                if payload.get("type") == "session.open":
                    if reply_task and not reply_task.done():
                        raise ChatSessionError("busy", "A reply is already being prepared.")

                    token = None
                    supplied_token = parse_session_token(payload.get("sessionToken"))
                    unfinished = {}
                    if supplied_token is None:
                        token = await store.create()
                        history = []
                    else:
                        snapshot = await store.snapshot(supplied_token)
                        history = snapshot["history"]
                        pending = snapshot.get("pending")
                        if pending and "requestId" in pending:
                            unfinished["pending"] = {
                                "requestId": pending["requestId"], "question": pending["question"],
                                "status": "generating" if pending["deadline"] > time.time() else "interrupted",
                            }
                        token = supplied_token
                    await send({"type": "session.ready", "sessionToken": token, "history": history, **unfinished})
                    continue

                request_id, question = parse_question(payload)
                mobility = None
                if "mobility" in payload:
                    try:
                        mobility = MapMobility.model_validate(payload["mobility"]).model_dump(exclude_none=True)
                    except ValidationError:
                        raise ChatSessionError("invalid_mobility", "Перевірте налаштування транспорту й адрес.") from None
                weights = None
                if "weights" in payload:
                    raw = payload["weights"]
                    if not isinstance(raw, dict) or any(
                        key.removeprefix("weight_") not in HexagonWeights.model_fields for key in raw
                    ):
                        raise ChatSessionError("invalid_weights", "Передайте ваги категорій OKIL.")
                    try:
                        weights = HexagonWeights.model_validate({
                            "weight_" + key.removeprefix("weight_"): value for key, value in raw.items()
                        }).model_dump()
                    except ValidationError:
                        raise ChatSessionError("invalid_weights", "Ваги мають бути числами від 0 до 5.") from None
                if token is None:
                    raise ChatSessionError("session_required", "Send session.open before a question.")
                if reply_task and not reply_task.done():
                    raise ChatSessionError("busy", "A reply is already being prepared.")

                reply_task = asyncio.create_task(run_reply(
                    store, token, request_id, question, send, stream=payload.get("stream") is True,
                    weights=weights, mobility=mobility,
                ))
            except ChatProtocolError as exc:
                await send_error(send, exc.code, str(exc), exc.request_id)
            except ChatSessionError as exc:
                await send_error(send, exc.code, str(exc), request_id)
            except RedisError:
                await send_error(send, "storage_unavailable", "Chat storage is temporarily unavailable.", request_id)
    except WebSocketDisconnect:
        pass
    finally:
        if reply_task:
            reply_task.cancel()
            with suppress(asyncio.CancelledError):
                await reply_task
