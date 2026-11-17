"""Unit tests for ``ChatSessionStore`` against an isolated in-memory Redis."""

import asyncio
import hashlib
import json
import re
from types import SimpleNamespace

import pytest

fakeredis = pytest.importorskip("fakeredis", reason="Install tests/requirements-chat.txt")

from app import chat_sessions
from app.chat_sessions import MAX_HISTORY_TURNS, ChatSessionError, ChatSessionStore

TTL = 1800
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
MISSING_TOKEN = "A" * 43


def key_for(token):
    return "chat:session:" + hashlib.sha256(token.encode("ascii")).hexdigest()


def turn(request_id="req_1", question="Що таке OKIL?", answer="Відповідь"):
    return {"requestId": request_id, "question": question, "answer": answer}


def run_with_store(server, scenario, ttl=TTL):
    """Run ``scenario(store)`` in one event loop on a fresh client for ``server``."""

    async def main():
        store = ChatSessionStore(fakeredis.FakeAsyncRedis(server=server, decode_responses=True), ttl)
        try:
            return await scenario(store)
        finally:
            await store.close()

    return asyncio.run(main())


async def finish(store, token, request_id="req_1", question="Що таке OKIL?", answer="Відповідь"):
    """Drive one full begin -> complete cycle and return the stored turn."""
    await store.begin(token, request_id, question, f"attempt-{request_id}", 60)
    saved = turn(request_id, question, answer)
    await store.complete(token, f"attempt-{request_id}", saved)
    return saved


async def error_code(awaitable):
    with pytest.raises(ChatSessionError) as error:
        await awaitable
    return error.value.code


# --- creating sessions -------------------------------------------------------


def test_create_stores_an_empty_session_under_a_hashed_key_with_ttl(redis_server):
    async def scenario(store):
        token = await store.create()
        assert TOKEN_PATTERN.fullmatch(token)
        # Only the hash is stored: a Redis dump must not reveal usable tokens.
        assert await store.redis.keys("*") == [key_for(token)]
        assert token not in key_for(token)
        assert await store.redis.get(key_for(token)) == '{"history": [], "pending": null}'
        assert 0 < await store.redis.ttl(key_for(token)) <= TTL

    run_with_store(redis_server, scenario)


def test_every_created_session_gets_a_unique_token_and_key(redis_server):
    async def scenario(store):
        tokens = [await store.create() for _ in range(25)]
        assert len(set(tokens)) == 25
        assert len(await store.redis.keys("*")) == 25

    run_with_store(redis_server, scenario)


def test_new_session_has_empty_history(redis_server):
    async def scenario(store):
        assert await store.history(await store.create()) == []

    run_with_store(redis_server, scenario)


# --- restoring sessions ------------------------------------------------------


def test_session_history_is_restored_by_a_new_store_instance(redis_server):
    """A reconnecting client (or another server process) sees the same history."""

    async def scenario(first):
        token = await first.create()
        saved = await finish(first, token)
        second = ChatSessionStore(fakeredis.FakeAsyncRedis(server=redis_server, decode_responses=True), TTL)
        try:
            assert await second.history(token) == [saved]
        finally:
            await second.close()

    run_with_store(redis_server, scenario)


def test_completed_turn_contains_only_the_public_fields(redis_server):
    async def scenario(store):
        token = await store.create()
        await finish(store, token, answer="Готово")
        (stored,) = await store.history(token)
        assert stored == {"requestId": "req_1", "question": "Що таке OKIL?", "answer": "Готово"}

    run_with_store(redis_server, scenario)


def test_history_keeps_turn_order(redis_server):
    async def scenario(store):
        token = await store.create()
        for index in range(3):
            await finish(store, token, f"req_{index}", f"Питання {index}", f"Відповідь {index}")
        assert [item["requestId"] for item in await store.history(token)] == ["req_0", "req_1", "req_2"]

    run_with_store(redis_server, scenario)


def test_history_is_limited_to_the_most_recent_turns(redis_server):
    async def scenario(store):
        token = await store.create()
        for index in range(MAX_HISTORY_TURNS + 5):
            await finish(store, token, f"req_{index}", f"Питання {index}")
        history = await store.history(token)
        assert len(history) == MAX_HISTORY_TURNS
        assert history[0]["requestId"] == "req_5"
        assert history[-1]["requestId"] == f"req_{MAX_HISTORY_TURNS + 4}"

    run_with_store(redis_server, scenario)


# --- missing, expired and removed sessions -----------------------------------


def test_nonexistent_session_is_reported_as_expired(redis_server):
    async def scenario(store):
        assert await error_code(store.history(MISSING_TOKEN)) == "session_expired"
        assert await error_code(store.begin(MISSING_TOKEN, "req_1", "?", "attempt", 60)) == "session_expired"
        assert await error_code(store.complete(MISSING_TOKEN, "attempt", turn())) == "session_expired"
        assert await error_code(store.abort(MISSING_TOKEN, "attempt")) == "session_expired"
        # Failed lookups must not create the session as a side effect.
        assert await store.redis.keys("*") == []

    run_with_store(redis_server, scenario)


def test_expired_session_is_not_resurrected_by_later_writes(redis_server):
    async def scenario(store):
        token = await store.create()
        await store.redis.pexpire(key_for(token), 1)
        await asyncio.sleep(0.05)
        assert await error_code(store.history(token)) == "session_expired"
        assert await error_code(store.begin(token, "req_1", "?", "attempt", 60)) == "session_expired"
        assert await store.redis.exists(key_for(token)) == 0

    run_with_store(redis_server, scenario)


def test_reply_cannot_be_saved_after_its_session_disappears_mid_flight(redis_server):
    async def scenario(store):
        token = await store.create()
        await store.begin(token, "req_1", "?", "attempt", 60)
        await store.redis.delete(key_for(token))
        assert await error_code(store.complete(token, "attempt", turn())) == "session_expired"
        assert await store.redis.exists(key_for(token)) == 0

    run_with_store(redis_server, scenario)


def test_removing_one_session_leaves_others_untouched_and_allows_a_new_one(redis_server):
    async def scenario(store):
        first = await store.create()
        second = await store.create()
        kept = await finish(store, second)
        await finish(store, first, "req_a", "Питання A")

        # Deleting a single key is what clearing a chat does; it must not flush.
        assert await store.redis.delete(key_for(first)) == 1

        assert await error_code(store.history(first)) == "session_expired"
        assert await store.history(second) == [kept]
        assert await store.redis.keys("*") == [key_for(second)]

        replacement = await store.create()
        assert replacement not in (first, second)
        assert await store.history(replacement) == []
        # The old token stays dead even after a new session exists.
        assert await error_code(store.history(first)) == "session_expired"

    run_with_store(redis_server, scenario)


def test_sessions_do_not_share_history(redis_server):
    async def scenario(store):
        first, second = await store.create(), await store.create()
        saved = await finish(store, first)
        assert await store.history(first) == [saved]
        assert await store.history(second) == []

    run_with_store(redis_server, scenario)


# --- clearing a session ---------------------------------------------------------


def test_clear_deletes_only_the_given_session_and_reports_it(redis_server):
    async def scenario(store):
        target, neighbour = await store.create(), await store.create()
        kept = await finish(store, neighbour, "req_n", "Питання сусіда", "Відповідь сусіда")
        await finish(store, target)

        assert await store.clear(target) is True

        assert await store.redis.exists(key_for(target)) == 0
        assert await store.history(neighbour) == [kept]
        assert await store.redis.keys("*") == [key_for(neighbour)]

    run_with_store(redis_server, scenario)


def test_clear_never_flushes_data_it_does_not_own(redis_server):
    async def scenario(store):
        token = await store.create()
        await store.redis.set("unrelated:key", "keep me")
        await store.redis.hset("unrelated:hash", "field", "value")

        await store.clear(token)

        assert await store.redis.get("unrelated:key") == "keep me"
        assert await store.redis.hget("unrelated:hash", "field") == "value"

    run_with_store(redis_server, scenario)


def test_clearing_twice_or_clearing_an_expired_session_reports_nothing_deleted(redis_server):
    async def scenario(store):
        token = await store.create()
        assert await store.clear(token) is True
        assert await store.clear(token) is False
        assert await store.clear(MISSING_TOKEN) is False

        expired = await store.create()
        await store.redis.pexpire(key_for(expired), 1)
        await asyncio.sleep(0.05)
        assert await store.clear(expired) is False

    run_with_store(redis_server, scenario)


@pytest.mark.parametrize("token", ["", "short", "a" * 42, "a" * 44, "a" * 42 + "!", "і" * 43, 123, None])
def test_clear_validates_the_token_before_touching_redis(redis_server, token):
    async def scenario(store):
        survivor = await store.create()
        assert await error_code(store.clear(token)) == "invalid_message"
        assert await store.redis.keys("*") == [key_for(survivor)]

    run_with_store(redis_server, scenario)


def test_cleared_session_stays_dead_and_a_pending_reply_cannot_recreate_it(redis_server):
    async def scenario(store):
        token = await store.create()
        await store.begin(token, "req_1", "Питання", "attempt", 60)

        await store.clear(token)

        assert await error_code(store.complete(token, "attempt", turn())) == "session_expired"
        assert await error_code(store.abort(token, "attempt")) == "session_expired"
        assert await error_code(store.history(token)) == "session_expired"
        assert await store.redis.keys("*") == []

        fresh = await store.create()
        assert fresh != token
        assert await store.history(fresh) == []

    run_with_store(redis_server, scenario)


# --- reply claims (begin / complete / abort) ----------------------------------


def test_begin_claims_the_session_and_returns_history_without_an_answer(redis_server):
    async def scenario(store):
        token = await store.create()
        earlier = await finish(store, token, "req_0")
        history, answer = await store.begin(token, "req_1", "Нове питання", "attempt-1", 45)
        assert (history, answer) == ([earlier], None)
        pending = json.loads(await store.redis.get(key_for(token)))["pending"]
        assert pending["attempt"] == "attempt-1"
        assert pending["deadline"] > 0

    run_with_store(redis_server, scenario)


def test_repeating_a_finished_request_returns_the_saved_answer_without_a_new_claim(redis_server):
    async def scenario(store):
        token = await store.create()
        saved = await finish(store, token, answer="Збережена відповідь")
        history, answer = await store.begin(token, "req_1", saved["question"], "attempt-again", 60)
        assert answer == "Збережена відповідь"
        assert history == [saved]
        # Nothing is pending, so the next new question is accepted right away.
        await store.begin(token, "req_2", "Інше питання", "attempt-2", 60)

    run_with_store(redis_server, scenario)


def test_reusing_a_request_id_for_a_different_question_is_rejected(redis_server):
    async def scenario(store):
        token = await store.create()
        await finish(store, token)
        assert await error_code(store.begin(token, "req_1", "Інше питання", "attempt-2", 60)) == "request_id_reused"
        assert len(await store.history(token)) == 1

    run_with_store(redis_server, scenario)


def test_second_question_is_busy_while_a_reply_is_pending(redis_server):
    async def scenario(store):
        token = await store.create()
        await store.begin(token, "req_1", "Перше", "attempt-1", 60)
        assert await error_code(store.begin(token, "req_2", "Друге", "attempt-2", 60)) == "busy"

    run_with_store(redis_server, scenario)


def test_expired_claim_can_be_taken_over_and_the_old_attempt_cannot_save(redis_server, monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(chat_sessions, "time", SimpleNamespace(time=lambda: clock["now"]))

    async def scenario(store):
        token = await store.create()
        await store.begin(token, "req_1", "Перше", "stale", 30)
        clock["now"] += 31
        await store.begin(token, "req_2", "Друге", "fresh", 30)
        assert await error_code(store.complete(token, "stale", turn("req_1"))) == "request_interrupted"
        await store.complete(token, "fresh", turn("req_2", "Друге"))
        assert [item["requestId"] for item in await store.history(token)] == ["req_2"]

    run_with_store(redis_server, scenario)


def test_complete_requires_the_matching_unexpired_claim(redis_server, monkeypatch):
    clock = {"now": 5_000.0}
    monkeypatch.setattr(chat_sessions, "time", SimpleNamespace(time=lambda: clock["now"]))

    async def scenario(store):
        token = await store.create()
        assert await error_code(store.complete(token, "nobody", turn())) == "request_interrupted"
        await store.begin(token, "req_1", "?", "mine", 10)
        assert await error_code(store.complete(token, "someone-else", turn())) == "request_interrupted"
        clock["now"] += 11
        assert await error_code(store.complete(token, "mine", turn())) == "request_interrupted"
        assert await store.history(token) == []

    run_with_store(redis_server, scenario)


def test_abort_releases_only_the_callers_own_claim(redis_server):
    async def scenario(store):
        token = await store.create()
        await store.begin(token, "req_1", "Перше", "mine", 60)

        await store.abort(token, "someone-else")
        assert await error_code(store.begin(token, "req_2", "Друге", "other", 60)) == "busy"

        await store.abort(token, "mine")
        await store.begin(token, "req_2", "Друге", "other", 60)
        assert await store.history(token) == []

    run_with_store(redis_server, scenario)


def test_only_one_of_two_simultaneous_questions_is_accepted(redis_server):
    async def scenario(store):
        token = await store.create()
        results = await asyncio.gather(
            store.begin(token, "req_1", "Перше", "attempt-1", 60),
            store.begin(token, "req_2", "Друге", "attempt-2", 60),
            return_exceptions=True,
        )
        failures = [item for item in results if isinstance(item, ChatSessionError)]
        assert len(failures) == 1
        assert failures[0].code == "busy"
        assert sum(not isinstance(item, Exception) for item in results) == 1

    run_with_store(redis_server, scenario)


# --- TTL handling -------------------------------------------------------------


def test_activity_refreshes_ttl_but_aborting_does_not(redis_server):
    async def scenario(store):
        token = await store.create()
        key = key_for(token)

        await store.redis.expire(key, 100)
        await store.begin(token, "req_1", "?", "attempt", 60)
        assert await store.redis.ttl(key) > 1000

        await store.redis.expire(key, 100)
        await store.abort(token, "attempt")
        assert 0 < await store.redis.ttl(key) <= 100

        await store.begin(token, "req_1", "?", "attempt", 60)
        await store.redis.expire(key, 100)
        await store.complete(token, "attempt", turn())
        assert await store.redis.ttl(key) > 1000

    run_with_store(redis_server, scenario)
