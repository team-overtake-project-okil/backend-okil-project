import asyncio
import threading
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.websockets import WebSocketDisconnect

fakeredis = pytest.importorskip("fakeredis", reason="Install tests/requirements-chat.txt")

from app.chat_service import ChatReplyError
from app.chat_sessions import ChatSessionStore
from routers import chat

ORIGIN = "http://localhost:5173"


@pytest.fixture
def client(monkeypatch):
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    store = ChatSessionStore(redis, 1800)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.chat_sessions = store
    app.include_router(chat.router)
    monkeypatch.setattr(chat.settings, "CORS_ORIGINS", [ORIGIN])
    reply = AsyncMock(return_value="OKIL — відповідь AI на основі даних.")
    monkeypatch.setattr(chat, "generate_reply", reply)
    with TestClient(app) as http:
        yield http, reply, store


def connect(http):
    return http.websocket_connect("/ws/chat", headers={"origin": ORIGIN})


def open_session(ws, token=None):
    ws.send_json({"type": "session.open", **({"sessionToken": token} if token else {})})
    event = ws.receive_json()
    assert event["type"] == "session.ready"
    return event


def send(ws, request_id="req_1", text="Що таке OKIL?"):
    # Exact payload emitted by Chat.tsx in frontend PR #95.
    ws.send_json({"type": "message.send", "requestId": request_id, "text": text})


def accepted(ws, request_id="req_1"):
    assert ws.receive_json() == {"type": "message.accepted", "requestId": request_id}
    assert ws.receive_json() == {"type": "answer.status", "requestId": request_id, "stage": "generating"}


def test_frontend_payload_gets_answer_and_session_restores_without_duplicate_ai_call(client):
    http, reply, _ = client
    with connect(http) as ws:
        ready = open_session(ws)
        assert ready["history"] == []
        send(ws)
        accepted(ws)
        assert ws.receive_json() == {"type": "answer.completed", "requestId": "req_1", "text": reply.return_value}
    with connect(http) as ws:
        restored = open_session(ws, ready["sessionToken"])
        assert restored["history"] == [{"requestId": "req_1", "question": "Що таке OKIL?", "answer": reply.return_value}]
        send(ws)
        assert ws.receive_json()["type"] == "message.accepted"
        assert ws.receive_json()["type"] == "answer.completed"
        reply.assert_awaited_once_with("Що таке OKIL?", [])
        send(ws, "req_2", "Як працює карта?")
        accepted(ws, "req_2")
        assert ws.receive_json()["type"] == "answer.completed"
        assert reply.await_args_list[-1].args[1] == restored["history"]


@pytest.mark.parametrize("code", ["ai_failed", "ai_unavailable", "context_required", "invalid_context"])
def test_ai_errors_keep_socket_open_do_not_save_failed_turn_and_allow_retry(client, code):
    http, reply, _ = client
    reply.side_effect = [ChatReplyError(code, "Зрозуміла помилка AI."), "Відновлена відповідь"]
    with connect(http) as ws:
        ready = open_session(ws)
        send(ws)
        accepted(ws)
        assert ws.receive_json() == {"type": "error", "requestId": "req_1", "code": code, "message": "Зрозуміла помилка AI."}
        assert open_session(ws, ready["sessionToken"])["history"] == []
        send(ws)
        accepted(ws)
        assert ws.receive_json()["text"] == "Відновлена відповідь"


def test_timeout_releases_session_and_can_retry(client, monkeypatch):
    http, reply, _ = client

    async def pending(*args):
        await asyncio.Event().wait()

    reply.side_effect = pending
    monkeypatch.setattr(chat, "REPLY_TIMEOUT_SECONDS", 0.02)
    with connect(http) as ws:
        ready = open_session(ws)
        send(ws)
        accepted(ws)
        assert ws.receive_json()["code"] == "reply_timeout"
        assert open_session(ws, ready["sessionToken"])["history"] == []
        reply.side_effect = None
        send(ws)
        accepted(ws)
        assert ws.receive_json()["type"] == "answer.completed"


def test_disconnect_cancels_reply_and_releases_redis_claim(client):
    http, reply, _ = client
    cancelled = threading.Event()

    async def pending(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    reply.side_effect = pending
    with connect(http) as ws:
        ready = open_session(ws)
        send(ws)
        accepted(ws)
    assert cancelled.wait(2)
    reply.side_effect = None
    with connect(http) as ws:
        assert open_session(ws, ready["sessionToken"])["history"] == []
        send(ws)
        accepted(ws)
        assert ws.receive_json()["type"] == "answer.completed"


def test_request_id_conflict_is_preserved(client):
    http, reply, _ = client
    with connect(http) as ws:
        open_session(ws)
        send(ws)
        accepted(ws)
        ws.receive_json()
        send(ws, text="Інше питання")
        assert ws.receive_json()["code"] == "request_id_reused"
        reply.assert_awaited_once()


def test_concurrent_sockets_cannot_generate_two_replies_in_same_session(client):
    http, reply, _ = client

    async def pending(*args):
        await asyncio.Event().wait()

    reply.side_effect = pending
    with connect(http) as first, connect(http) as second:
        ready = open_session(first)
        open_session(second, ready["sessionToken"])
        send(first)
        accepted(first)
        send(second, "req_2", "Інше питання")
        assert second.receive_json()["code"] == "busy"
        reply.assert_awaited_once()


def test_validation_errors_do_not_call_ai(client):
    http, reply, _ = client
    with connect(http) as ws:
        send(ws)
        assert ws.receive_json()["code"] == "session_required"
        open_session(ws)
        send(ws, text=" ")
        assert ws.receive_json()["code"] == "invalid_message"
        reply.assert_not_awaited()


def test_redis_failure_returns_existing_error_without_ai(client, monkeypatch):
    http, reply, store = client
    monkeypatch.setattr(store, "create", AsyncMock(side_effect=RedisConnectionError("private URL")))
    with connect(http) as ws:
        ws.send_json({"type": "session.open"})
        event = ws.receive_json()
        assert event["code"] == "storage_unavailable"
        assert "private" not in event["message"]
        reply.assert_not_awaited()


def test_origin_check_remains_in_place(client):
    http, reply, _ = client
    with pytest.raises(WebSocketDisconnect):
        with http.websocket_connect("/ws/chat", headers={"origin": "https://untrusted.test"}):
            pytest.fail("Untrusted Origin must be rejected")
    reply.assert_not_awaited()
