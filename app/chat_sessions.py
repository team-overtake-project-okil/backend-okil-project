import hashlib
import json
import secrets
import time
from collections.abc import Callable
from typing import TypedDict, TypeVar

from redis.asyncio import Redis
from redis.exceptions import WatchError

MAX_HISTORY_TURNS = 30
T = TypeVar("T")


class ChatTurn(TypedDict):
    requestId: str
    question: str
    answer: str


class PendingReply(TypedDict):
    attempt: str
    deadline: float


class ChatSession(TypedDict):
    history: list[ChatTurn]
    pending: PendingReply | None


class ChatSessionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ChatSessionStore:
    def __init__(self, redis: Redis, ttl: int):
        self.redis = redis
        self.ttl = ttl

    async def close(self) -> None:
        await self.redis.aclose()

    @staticmethod
    def _key(token: str) -> str:
        return "chat:session:" + hashlib.sha256(token.encode("ascii")).hexdigest()

    async def create(self) -> str:
        while True:
            token = secrets.token_urlsafe(32)
            created = await self.redis.set(
                self._key(token),
                json.dumps({"history": [], "pending": None}),
                ex=self.ttl,
                nx=True,
            )
            if created:
                return token

    async def history(self, token: str) -> list[ChatTurn]:
        raw = await self.redis.get(self._key(token))
        if raw is None:
            raise ChatSessionError("session_expired", "This chat session has expired.")
        return json.loads(raw)["history"]

    async def _update(
        self, token: str, change: Callable[[ChatSession], T], *, refresh_ttl: bool = True
    ) -> T:
        key = self._key(token)
        # WATCH prevents concurrent sockets from claiming or overwriting the same session.
        for _ in range(5):
            try:
                async with self.redis.pipeline() as pipe:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if raw is None:
                        raise ChatSessionError("session_expired", "This chat session has expired.")
                    session: ChatSession = json.loads(raw)
                    result = change(session)
                    updated = json.dumps(session)
                    if updated == raw:
                        return result
                    pipe.multi()
                    if refresh_ttl:
                        pipe.set(key, updated, xx=True, ex=self.ttl)
                    else:
                        pipe.set(key, updated, xx=True, keepttl=True)
                    (saved,) = await pipe.execute()
                    if not saved:
                        raise ChatSessionError("session_expired", "This chat session has expired.")
                    return result
            except WatchError:
                continue
        raise ChatSessionError("busy", "The chat session is busy. Try again.")

    async def begin(
        self, token: str, request_id: str, question: str, attempt: str, lease_seconds: int
    ) -> tuple[list[ChatTurn], str | None]:
        def claim(session: ChatSession) -> tuple[list[ChatTurn], str | None]:
            for turn in session["history"]:
                if turn["requestId"] == request_id:
                    if turn["question"] != question:
                        raise ChatSessionError("request_id_reused", "Use a new requestId for a new question.")
                    return session["history"], turn["answer"]

            now = time.time()
            pending = session["pending"]
            if pending and pending["deadline"] > now:
                raise ChatSessionError("busy", "A reply is already being prepared.")
            session["pending"] = {"attempt": attempt, "deadline": now + lease_seconds}
            return session["history"], None

        return await self._update(token, claim)

    async def complete(self, token: str, attempt: str, turn: ChatTurn) -> None:
        def save(session: ChatSession) -> None:
            pending = session["pending"]
            if not pending or pending["attempt"] != attempt or pending["deadline"] <= time.time():
                raise ChatSessionError("request_interrupted", "This reply can no longer be saved.")
            session["history"] = [*session["history"], turn][-MAX_HISTORY_TURNS:]
            session["pending"] = None

        await self._update(token, save)

    async def abort(self, token: str, attempt: str) -> None:
        def release(session: ChatSession) -> None:
            pending = session["pending"]
            if pending and pending["attempt"] == attempt:
                session["pending"] = None

        await self._update(token, release, refresh_ttl=False)
