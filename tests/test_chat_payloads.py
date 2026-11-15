"""Exact WebSocket event payloads and failure isolation for ``/ws/chat``.
Each test gets a private in-memory Redis (see ``conftest.redis_server``) and a
mocked ``generate_reply``, so no AI provider or shared state is involved.
Complements ``test_chat_websocket.py``; it checks the payload fields themselves.
"""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

fakeredis = pytest.importorskip("fakeredis", reason="Install tests/requirements-chat.txt")

from app.chat_protocol import MAX_MESSAGE_BYTES, MAX_TEXT_LENGTH
from app.chat_service import ChatReplyError
from app.chat_sessions import ChatSessionStore
from routers import chat

ORIGIN = "http://localhost:5173"
ANSWER = "OKIL — відповідь AI на основі даних."
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
UNKNOWN_TOKEN = "B" * 43


@pytest.fixture
def client(monkeypatch, redis_server):
    store = ChatSessionStore(fakeredis.FakeAsyncRedis(server=redis_server, decode_responses=True), 1800)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.chat_sessions = store
    app.include_router(chat.router)
    monkeypatch.setattr(chat.settings, "CORS_ORIGINS", [ORIGIN])
    reply = AsyncMock(return_value=ANSWER)
    monkeypatch.setattr(chat, "generate_reply", reply)
    with TestClient(app) as http:
        yield http, reply, store


def connect(http):
    return http.websocket_connect("/ws/chat", headers={"origin": ORIGIN})


def open_session(ws, token=None):
    ws.send_json({"type": "session.open", **({"sessionToken": token} if token else {})})
    return ws.receive_json()


def ask(ws, request_id="req_1", text="Що таке OKIL?"):
    ws.send_json({"type": "message.send", "requestId": request_id, "text": text})


def receive_answer(ws, request_id="req_1"):
    """Consume the accepted/status events and return the final event."""
    assert ws.receive_json() == {"type": "message.accepted", "requestId": request_id}
    assert ws.receive_json() == {"type": "answer.status", "requestId": request_id, "stage": "generating"}
    return ws.receive_json()


def error(code, message, request_id=None):
    return {"type": "error", "requestId": request_id, "code": code, "message": message}


def expire(redis_server, token):
    """Remove a session out-of-band, like a Redis TTL expiry would."""
    key = ChatSessionStore._key(token)
    assert fakeredis.FakeRedis(server=redis_server).delete(key) == 1


# --- success payloads ---------------------------------------------------------


def test_session_open_returns_exact_ready_payload(client):
    http, _, _ = client
    with connect(http) as ws:
        event = open_session(ws)
    assert set(event) == {"type", "sessionToken", "history"}
    assert event["type"] == "session.ready"
    assert TOKEN_PATTERN.fullmatch(event["sessionToken"])
    assert event["history"] == []


def test_question_produces_the_documented_event_sequence_and_calls_ai_once(client):
    http, reply, _ = client
    with connect(http) as ws:
        open_session(ws)
        ask(ws, "req_7", "Поясни оцінку")
        completed = receive_answer(ws, "req_7")
    assert completed == {"type": "answer.completed", "requestId": "req_7", "text": ANSWER}
    reply.assert_awaited_once_with("Поясни оцінку", [])


def test_restored_session_returns_history_items_in_order_with_exact_fields(client):
    http, reply, _ = client
    reply.side_effect = ["Перша відповідь", "Друга відповідь"]
    with connect(http) as ws:
        token = open_session(ws)["sessionToken"]
        ask(ws, "req_1", "Перше питання")
        receive_answer(ws, "req_1")
        ask(ws, "req_2", "Друге питання")
        receive_answer(ws, "req_2")
    with connect(http) as ws:
        restored = open_session(ws, token)
    assert restored == {
        "type": "session.ready",
        "sessionToken": token,
        "history": [
            {"requestId": "req_1", "question": "Перше питання", "answer": "Перша відповідь"},
            {"requestId": "req_2", "question": "Друге питання", "answer": "Друга відповідь"},
        ],
    }


def test_sessions_are_isolated_from_each_other(client):
    http, reply, _ = client
    with connect(http) as first, connect(http) as second:
        token_a = open_session(first)["sessionToken"]
        token_b = open_session(second)["sessionToken"]
        assert token_a != token_b
        ask(first, "req_1", "Питання тільки для A")
        receive_answer(first, "req_1")
        assert open_session(second, token_b)["history"] == []
        ask(second, "req_1", "Питання для B")
        receive_answer(second, "req_1")
    # Same requestId in two sessions is fine, and neither sees the other's turns.
    assert [call.args for call in reply.await_args_list] == [("Питання тільки для A", []), ("Питання для B", [])]
    with connect(http) as ws:
        history = open_session(ws, token_a)["history"]
    assert [item["question"] for item in history] == ["Питання тільки для A"]


# --- unknown, expired and invalid sessions -----------------------------------


def test_unknown_session_token_returns_session_expired_and_socket_stays_usable(client):
    http, reply, _ = client
    with connect(http) as ws:
        assert open_session(ws, UNKNOWN_TOKEN) == error("session_expired", "This chat session has expired.")
        ready = open_session(ws)
        assert ready["type"] == "session.ready" and ready["history"] == []
        ask(ws)
        assert receive_answer(ws)["type"] == "answer.completed"
    reply.assert_awaited_once()


def test_expired_session_is_rejected_then_replaced_by_a_fresh_one(client, redis_server):
    http, reply, _ = client
    with connect(http) as ws:
        old = open_session(ws)["sessionToken"]
        ask(ws)
        receive_answer(ws)
    expire(redis_server, old)
    with connect(http) as ws:
        assert open_session(ws, old)["code"] == "session_expired"
        fresh = open_session(ws)
        assert fresh["sessionToken"] != old
        assert fresh["history"] == []
        ask(ws, "req_1", "Нове питання")
        receive_answer(ws)
    assert reply.await_args_list[-1].args == ("Нове питання", [])


def test_question_after_the_session_expired_mid_conversation_reports_expiry(client, redis_server):
    http, reply, _ = client
    with connect(http) as ws:
        token = open_session(ws)["sessionToken"]
        expire(redis_server, token)
        ask(ws)
        assert ws.receive_json() == error("session_expired", "This chat session has expired.", "req_1")
        # The connection still works: the client can start over.
        assert open_session(ws)["type"] == "session.ready"
    reply.assert_not_awaited()


@pytest.mark.parametrize("token", ["too-short", 123, ["x"], "a" * 44])
def test_malformed_session_token_is_rejected_without_creating_a_session(client, redis_server, token):
    http, _, _ = client
    redis = fakeredis.FakeRedis(server=redis_server)
    with connect(http) as ws:
        ws.send_json({"type": "session.open", "sessionToken": token})
        assert ws.receive_json() == error("invalid_message", "Invalid sessionToken.")
        assert redis.keys("*") == []
        # The socket is still usable and only now is a session created.
        assert open_session(ws)["type"] == "session.ready"
        assert len(redis.keys("*")) == 1


# --- malformed frames ----------------------------------------------------------


@pytest.mark.parametrize("frame", ["not json", "[1, 2]", "null", '"text"'])
def test_non_object_json_returns_invalid_message_and_keeps_the_socket_open(client, frame):
    http, reply, _ = client
    with connect(http) as ws:
        ws.send_text(frame)
        assert ws.receive_json() == error("invalid_message", "Send a valid JSON object.")
        assert open_session(ws)["type"] == "session.ready"
    reply.assert_not_awaited()


def test_binary_frame_is_rejected_without_closing_the_socket(client):
    http, _, _ = client
    with connect(http) as ws:
        ws.send_bytes(b"\x00\x01")
        assert ws.receive_json() == error("invalid_message", "Send a JSON text message.")
        assert open_session(ws)["type"] == "session.ready"


def test_oversized_frame_is_rejected_before_any_work(client):
    http, reply, _ = client
    with connect(http) as ws:
        open_session(ws)
        ws.send_text(json.dumps({"type": "message.send", "requestId": "r", "text": "x" * MAX_MESSAGE_BYTES}))
        assert ws.receive_json() == error("message_too_long", "Message exceeds 32 KiB.")
    reply.assert_not_awaited()


def test_too_long_text_error_carries_the_request_id(client):
    http, reply, _ = client
    with connect(http) as ws:
        open_session(ws)
        ask(ws, "req_9", "x" * (MAX_TEXT_LENGTH + 1))
        assert ws.receive_json() == error("message_too_long", "Text exceeds 4000 characters.", "req_9")
    reply.assert_not_awaited()


def test_unknown_message_type_is_rejected_with_its_request_id(client):
    http, reply, _ = client
    with connect(http) as ws:
        open_session(ws)
        ws.send_json({"type": "message.unknown", "requestId": "req_3", "text": "Привіт"})
        event = ws.receive_json()
        assert (event["type"], event["code"], event["requestId"]) == ("error", "invalid_message", "req_3")
    reply.assert_not_awaited()


# --- AI and storage failures never break the chat flow ---------------------------


def test_unexpected_ai_exception_is_sanitized_and_the_chat_keeps_working(client):
    http, reply, _ = client
    reply.side_effect = [RuntimeError("Bearer sk-SECRET provider body"), "Відновлена відповідь"]
    with connect(http) as ws:
        token = open_session(ws)["sessionToken"]
        ask(ws)
        failure = receive_answer(ws)
        assert failure == error("reply_failed", "The reply could not be prepared.", "req_1")
        assert "SECRET" not in json.dumps(failure)

        # The failed turn was not stored, so the same requestId can be retried.
        assert open_session(ws, token)["history"] == []
        ask(ws)
        assert receive_answer(ws)["text"] == "Відновлена відповідь"


def test_failed_turn_is_not_part_of_the_context_for_the_next_question(client):
    http, reply, _ = client
    reply.side_effect = [ChatReplyError("ai_failed", "AI не відповів."), "Друга відповідь"]
    with connect(http) as ws:
        open_session(ws)
        ask(ws, "req_1", "Перше питання")
        assert receive_answer(ws, "req_1") == error("ai_failed", "AI не відповів.", "req_1")
        ask(ws, "req_2", "Друге питання")
        assert receive_answer(ws, "req_2")["text"] == "Друга відповідь"
    assert [call.args for call in reply.await_args_list] == [("Перше питання", []), ("Друге питання", [])]


def test_redis_failure_while_starting_a_reply_returns_storage_error_without_calling_ai(client, monkeypatch):
    http, reply, store = client
    monkeypatch.setattr(store, "begin", AsyncMock(side_effect=RedisConnectionError("redis://user:pass@host")))
    with connect(http) as ws:
        open_session(ws)
        ask(ws)
        event = ws.receive_json()
        assert event == error("storage_unavailable", "Chat storage is temporarily unavailable.", "req_1")
        assert "pass" not in json.dumps(event)
        assert open_session(ws)["type"] == "session.ready"
    reply.assert_not_awaited()


# --- concurrency on a single connection ------------------------------------------


def test_second_question_on_the_same_socket_is_busy_and_ai_is_called_once(client):
    http, reply, _ = client

    async def pending(*args):
        await asyncio.Event().wait()

    reply.side_effect = pending
    with connect(http) as ws:
        open_session(ws)
        ask(ws, "req_1", "Перше")
        assert ws.receive_json()["type"] == "message.accepted"
        assert ws.receive_json()["type"] == "answer.status"
        ask(ws, "req_2", "Друге")
        assert ws.receive_json() == error("busy", "A reply is already being prepared.", "req_2")
    reply.assert_awaited_once()


def test_reopening_a_session_while_a_reply_is_pending_is_busy(client):
    http, reply, _ = client

    async def pending(*args):
        await asyncio.Event().wait()

    reply.side_effect = pending
    with connect(http) as ws:
        token = open_session(ws)["sessionToken"]
        ask(ws)
        assert ws.receive_json()["type"] == "message.accepted"
        assert ws.receive_json()["type"] == "answer.status"
        assert open_session(ws, token) == error("busy", "A reply is already being prepared.")
