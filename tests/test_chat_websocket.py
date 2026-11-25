import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.websockets import WebSocketDisconnect

fakeredis = pytest.importorskip("fakeredis", reason="Install tests/requirements-chat.txt")

from app import chat_service
from app.chat_service import ChatReplyError
from app.chat_sessions import ChatSessionError, ChatSessionStore
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
    monkeypatch.setattr(chat, "generate_next_prompt", AsyncMock(return_value=None))
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


def test_streamed_sentences_reconcile_with_saved_answer(client):
    http, reply, _ = client
    answer = "Перше речення. Друге речення!"

    async def generate(question, history, *, state, on_part):
        await on_part(answer)
        return answer

    reply.side_effect = generate
    with connect(http) as ws:
        ready = open_session(ws)
        ws.send_json({"type": "message.send", "text": "Покажи рейтинг", "requestId": "r", "stream": True})
        accepted(ws, "r")
        chunks = [ws.receive_json(), ws.receive_json()]
        assert all(event["type"] == "answer.delta" and event["requestId"] == "r" for event in chunks)
        assert "".join(event["text"] for event in chunks) == answer
        assert ws.receive_json()["text"] == answer
        assert open_session(ws, ready["sessionToken"])["history"][0]["answer"] == answer


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
        reply.assert_awaited_once_with("Що таке OKIL?", [], state={}, on_part=None)
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
        chat.generate_next_prompt.assert_not_awaited()
        assert open_session(ws, ready["sessionToken"])["history"] == []
        send(ws)
        accepted(ws)
        assert ws.receive_json()["text"] == "Відновлена відповідь"


def test_timeout_releases_session_and_can_retry(client, monkeypatch):
    http, reply, _ = client

    async def pending(*args, **kwargs):
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

    async def pending(*args, **kwargs):
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

    async def pending(*args, **kwargs):
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


def test_next_prompt_is_plain_text_saved_restored_and_replayed_without_ai(client, monkeypatch):
    http, reply, _ = client
    suggestion = "Які категорії оцінює OKIL?"
    generate = AsyncMock(return_value=suggestion)
    monkeypatch.setattr(chat, "generate_next_prompt", generate)
    expected = {
        "type": "answer.completed", "requestId": "req_1",
        "text": reply.return_value, "nextPrompt": suggestion,
    }
    with connect(http) as ws:
        ready = open_session(ws)
        send(ws)
        accepted(ws)
        assert ws.receive_json() == expected
    with connect(http) as ws:
        restored = open_session(ws, ready["sessionToken"])
        assert restored["history"] == [{
            "requestId": "req_1", "question": "Що таке OKIL?",
            "answer": reply.return_value, "nextPrompt": suggestion,
        }]
        send(ws)
        assert ws.receive_json() == {"type": "message.accepted", "requestId": "req_1"}
        assert ws.receive_json() == expected
    reply.assert_awaited_once()
    generate.assert_awaited_once()
    assert generate.await_args.args == ("Що таке OKIL?", reply.return_value, [])


@pytest.mark.parametrize("failure", ["error", "timeout", "invalid", "none"])
def test_optional_generation_failure_delivers_and_saves_main_answer(client, monkeypatch, failure):
    http, reply, _ = client
    cancelled = threading.Event()

    async def suggest(*args):
        if failure == "error":
            raise RuntimeError("private provider error")
        if failure == "timeout":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return {"unexpected": "object"} if failure == "invalid" else None

    provider = AsyncMock(side_effect=suggest)
    monkeypatch.setattr(chat_service, "_get_service", AsyncMock(return_value=SimpleNamespace(
        client=SimpleNamespace(next_prompt=provider),
    )))
    monkeypatch.setattr(chat, "generate_next_prompt", chat_service.generate_next_prompt)
    # The remaining reply budget is smaller than the optional generation timeout.
    monkeypatch.setattr(chat, "REPLY_TIMEOUT_SECONDS", 0.05)
    with connect(http) as ws:
        ready = open_session(ws)
        send(ws)
        accepted(ws)
        assert ws.receive_json() == {"type": "answer.completed", "requestId": "req_1", "text": reply.return_value}
        assert open_session(ws, ready["sessionToken"])["history"] == [{
            "requestId": "req_1", "question": "Що таке OKIL?", "answer": reply.return_value,
        }]
        send(ws)
        assert ws.receive_json()["type"] == "message.accepted"
        assert "nextPrompt" not in ws.receive_json()
        provider.assert_awaited_once()
    if failure == "timeout":
        assert cancelled.is_set()


def test_disconnect_during_next_prompt_cancels_provider_and_releases_session(client, monkeypatch):
    http, reply, _ = client
    started = threading.Event()
    cancelled = threading.Event()

    async def pending(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider = AsyncMock(side_effect=pending)
    monkeypatch.setattr(chat_service, "_get_service", AsyncMock(return_value=SimpleNamespace(
        client=SimpleNamespace(next_prompt=provider),
    )))
    monkeypatch.setattr(chat, "generate_next_prompt", chat_service.generate_next_prompt)
    with connect(http) as ws:
        ready = open_session(ws)
        send(ws)
        accepted(ws)
        assert started.wait(2)
    assert cancelled.wait(2)
    provider.side_effect = None
    provider.return_value = None
    with connect(http) as ws:
        assert open_session(ws, ready["sessionToken"])["history"] == []
        send(ws)
        accepted(ws)
        assert ws.receive_json()["text"] == reply.return_value

def clear_session(ws, token, request_id="clear_1"):
    ws.send_json({"type": "session.clear", "requestId": request_id, "sessionToken": token})
    return ws.receive_json()


def test_clear_deletes_only_current_session_and_allows_new_conversation(client):
    http, reply, store = client
    with connect(http) as ws, connect(http) as other:
        token = open_session(ws)["sessionToken"]
        other_token = open_session(other)["sessionToken"]
        send(ws)
        accepted(ws)
        ws.receive_json()
        assert clear_session(ws, token) == {"type": "session.cleared", "requestId": "clear_1", "deleted": True}
        assert http.portal.call(store.redis.exists, store._key(token)) == 0
        assert open_session(other, other_token)["history"] == []
        send(ws, "after_clear")
        assert ws.receive_json()["code"] == "session_required"
        assert open_session(ws)["sessionToken"] != token
        ws.send_json({"type": "session.open", "sessionToken": token})
        assert ws.receive_json()["code"] == "session_expired"


@pytest.mark.parametrize("bad_token", [None, "invalid", "і" * 43, "a" * 43])
def test_clear_rejects_missing_invalid_or_different_token(client, bad_token):
    http, _, _ = client
    with connect(http) as ws:
        token = open_session(ws)["sessionToken"]
        event = clear_session(ws, bad_token)
        assert event["type"] == "error"
        assert event["requestId"] == "clear_1"
        assert event["code"] in {"invalid_message", "session_required"}
        assert open_session(ws, token)["history"] == []


def test_shared_sockets_can_clear_already_deleted_session_idempotently(client):
    http, _, _ = client
    with connect(http) as first, connect(http) as second:
        token = open_session(first)["sessionToken"]
        open_session(second, token)
        assert clear_session(first, token)["deleted"] is True
        assert clear_session(second, token)["deleted"] is False
        assert open_session(second)["history"] == []


def test_clear_cancels_own_reply(client):
    http, reply, _ = client
    cancelled = threading.Event()

    async def pending(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    reply.side_effect = pending
    with connect(http) as ws:
        token = open_session(ws)["sessionToken"]
        send(ws)
        accepted(ws)
        assert clear_session(ws, token)["type"] == "session.cleared"
        assert cancelled.wait(2)
        assert open_session(ws)["history"] == []


def test_reply_on_another_socket_cannot_recreate_deleted_history(client):
    http, reply, store = client
    release = asyncio.Event()

    async def pending(*args, **kwargs):
        await release.wait()
        return "Late answer"

    reply.side_effect = pending
    with connect(http) as first, connect(http) as second:
        token = open_session(first)["sessionToken"]
        open_session(second, token)
        send(first)
        accepted(first)
        assert clear_session(second, token)["deleted"] is True
        http.portal.call(release.set)
        assert first.receive_json()["code"] == "session_expired"
        assert http.portal.call(store.redis.exists, store._key(token)) == 0


def test_clear_storage_failure_preserves_session_for_retry(client, monkeypatch):
    http, _, store = client
    original = store.clear
    monkeypatch.setattr(store, "clear", AsyncMock(side_effect=RedisConnectionError("private URL")))
    with connect(http) as ws:
        token = open_session(ws)["sessionToken"]
        event = clear_session(ws, token)
        assert event["code"] == "storage_unavailable"
        assert event["requestId"] == "clear_1"
        assert "private" not in event["message"]
        monkeypatch.setattr(store, "clear", original)
        assert clear_session(ws, token)["deleted"] is True


def test_clear_store_validates_token_and_expired_session(client):
    http, _, store = client
    with pytest.raises(ChatSessionError, match="Invalid sessionToken"):
        http.portal.call(store.clear, "bad")
    with connect(http) as ws:
        token = open_session(ws)["sessionToken"]
        http.portal.call(store.redis.delete, store._key(token))
        assert clear_session(ws, token)["deleted"] is False


def test_clear_requires_valid_request_id(client):
    http, _, _ = client
    with connect(http) as ws:
        token = open_session(ws)["sessionToken"]
        assert clear_session(ws, token, " ")["code"] == "invalid_message"
        assert clear_session(ws, token)["deleted"] is True


def test_clear_removes_saved_next_prompt_and_new_session_does_not_replay_it(client, monkeypatch):
    http, reply, store = client
    suggestion = "Які категорії оцінює OKIL?"
    generate = AsyncMock(return_value=suggestion)
    monkeypatch.setattr(chat, "generate_next_prompt", generate)
    with connect(http) as ws:
        token = open_session(ws)["sessionToken"]
        send(ws)
        accepted(ws)
        assert ws.receive_json()["nextPrompt"] == suggestion
        assert open_session(ws, token)["history"][0]["nextPrompt"] == suggestion

        assert clear_session(ws, token)["deleted"] is True
        assert http.portal.call(store.redis.exists, store._key(token)) == 0
        fresh = open_session(ws)
        assert fresh["sessionToken"] != token
        assert fresh["history"] == []

        # Reusing an ID in the new session must generate a fresh answer.
        send(ws)
        accepted(ws)
        assert ws.receive_json()["nextPrompt"] == suggestion
        assert reply.await_count == 2
        assert generate.await_count == 2
        assert reply.await_args.args[1] == []
        assert generate.await_args.args[2] == []


def test_clear_during_next_prompt_cancels_provider_and_discards_turn(client, monkeypatch):
    http, _, store = client
    started = threading.Event()
    cancelled = threading.Event()

    async def pending(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider = AsyncMock(side_effect=pending)
    monkeypatch.setattr(chat_service, "_get_service", AsyncMock(return_value=SimpleNamespace(
        client=SimpleNamespace(next_prompt=provider),
    )))
    monkeypatch.setattr(chat, "generate_next_prompt", chat_service.generate_next_prompt)
    with connect(http) as ws:
        token = open_session(ws)["sessionToken"]
        send(ws)
        accepted(ws)
        assert started.wait(2)
        assert clear_session(ws, token)["type"] == "session.cleared"
        assert cancelled.wait(2)
        assert http.portal.call(store.redis.exists, store._key(token)) == 0
        assert open_session(ws)["history"] == []
        ws.send_json({"type": "session.open", "sessionToken": token})
        assert ws.receive_json()["code"] == "session_expired"
    provider.assert_awaited_once()


def test_other_socket_clear_during_next_prompt_cannot_restore_deleted_turn(client, monkeypatch):
    http, _, store = client
    started = threading.Event()
    release = asyncio.Event()

    async def pending(*args, **kwargs):
        started.set()
        await release.wait()
        return "Які категорії оцінює OKIL?"

    monkeypatch.setattr(chat, "generate_next_prompt", AsyncMock(side_effect=pending))
    with connect(http) as first, connect(http) as second:
        token = open_session(first)["sessionToken"]
        open_session(second, token)
        send(first)
        accepted(first)
        assert started.wait(2)
        assert clear_session(second, token)["deleted"] is True
        http.portal.call(release.set)
        assert first.receive_json()["code"] == "session_expired"
        assert http.portal.call(store.redis.exists, store._key(token)) == 0
