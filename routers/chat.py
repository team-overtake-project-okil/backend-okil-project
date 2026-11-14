import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError

from app.chat_protocol import ChatProtocolError, parse_message, parse_question, parse_request_id, parse_session_token
from app.chat_service import ChatReplyError, REPLY_TIMEOUT_SECONDS, generate_reply
from app.chat_sessions import ChatSessionError, ChatSessionStore
from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

SendEvent = Callable[[dict[str, object]], Awaitable[None]]


async def send_error(send: SendEvent, code: str, message: str, request_id: str | None = None) -> None:
    await send({"type": "error", "requestId": request_id, "code": code, "message": message})


async def run_reply(
    store: ChatSessionStore, token: str, request_id: str, question: str, send: SendEvent
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
        await send({"type": "message.accepted", "requestId": request_id})
        if answer is None:
            await send({"type": "answer.status", "requestId": request_id, "stage": "generating"})
            try:
                answer = await asyncio.wait_for(generate_reply(question, history), REPLY_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise ChatSessionError("reply_timeout", "The reply took too long.") from None
            except ChatReplyError as exc:
                raise ChatSessionError(exc.code, str(exc)) from None
            except Exception as exc:
                logger.error("Chat reply generation failed (%s)", type(exc).__name__)
                raise ChatSessionError("reply_failed", "The reply could not be prepared.") from None

            await store.complete(
                token, attempt, {"requestId": request_id, "question": question, "answer": answer}
            )
            needs_cleanup = False
        await send({"type": "answer.completed", "requestId": request_id, "text": answer})
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
                    if supplied_token is None:
                        token = await store.create()
                        history = []
                    else:
                        history = await store.history(supplied_token)
                        token = supplied_token
                    await send({"type": "session.ready", "sessionToken": token, "history": history})
                    continue

                request_id, question = parse_question(payload)
                if token is None:
                    raise ChatSessionError("session_required", "Send session.open before a question.")
                if reply_task and not reply_task.done():
                    raise ChatSessionError("busy", "A reply is already being prepared.")

                reply_task = asyncio.create_task(run_reply(store, token, request_id, question, send))
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
