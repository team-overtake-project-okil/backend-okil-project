import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.request_timing import RequestTimingMiddleware, request_id, timed_stage


def test_request_header_correlates_stages_without_logging_secrets(caplog):
    app = FastAPI()
    app.add_middleware(RequestTimingMiddleware)

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        with timed_stage("traveltime.http") as timing:
            timing["http_status"] = 200
        return {"ok": True}

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        response = TestClient(app).get(
            "/items/private-value?apiKey=secret-query",
            headers={"Authorization": "Bearer secret-header", "X-Request-ID": "untrusted"},
        )
    assert response.json() == {"ok": True}
    correlation_id = response.headers["x-request-id"]
    assert len(correlation_id) == 32
    assert correlation_id != "untrusted"
    assert all(f"request_id={correlation_id}" in record.message for record in caplog.records)
    assert "stage=traveltime.http" in caplog.text
    assert "route=/items/{item_id}" in caplog.text
    assert "status=200" in caplog.text
    assert "duration_ms=" in caplog.text
    for secret in ("private-value", "secret-query", "secret-header", "untrusted"):
        assert secret not in caplog.text
    assert request_id.get() == "-"


def test_concurrent_requests_keep_separate_ids_and_restore_context():
    observed = []

    async def app(scope, receive, send):
        before = request_id.get()
        await asyncio.sleep(0)
        observed.append((before, request_id.get()))
        scope["route"] = SimpleNamespace(path="/test")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def run():
        async def send(message):
            pass

        middleware = RequestTimingMiddleware(app)
        await asyncio.gather(*(
            middleware({"type": "http", "method": "GET"}, None, send)
            for _ in range(2)
        ))
        assert request_id.get() == "-"

    asyncio.run(run())
    assert len({before for before, _ in observed}) == 2
    assert all(before == after for before, after in observed)


def test_failures_are_logged_and_propagated_without_exception_message(caplog):
    async def app(scope, receive, send):
        with timed_stage("here.http"):
            raise TimeoutError("secret-url?apiKey=secret")

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with pytest.raises(TimeoutError):
            asyncio.run(RequestTimingMiddleware(app)(
                {"type": "http", "method": "GET"}, None, None
            ))
    assert "stage=here.http" in caplog.text
    assert "status=TimeoutError" in caplog.text
    assert "outcome=TimeoutError" in caplog.text
    assert "secret" not in caplog.text
    assert request_id.get() == "-"


def test_database_timing_preserves_result_and_exception(monkeypatch, caplog):
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.database import TimedAsyncSession

    expected = object()

    async def execute(self, *args, **kwargs):
        assert args == ("test-statement",)
        assert kwargs == {"params": {"value": "private"}}
        return expected

    monkeypatch.setattr(AsyncSession, "execute", execute)

    async def run():
        async with TimedAsyncSession() as session:
            return await session.execute("test-statement", params={"value": "private"})

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        assert asyncio.run(run()) is expected
    assert "stage=db.execute" in caplog.text
    assert "private" not in caplog.text

    async def fail(self, *args, **kwargs):
        raise RuntimeError("private database error")

    monkeypatch.setattr(AsyncSession, "execute", fail)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with pytest.raises(RuntimeError):
            asyncio.run(run())
    assert "status=RuntimeError" in caplog.text
    assert "private" not in caplog.text
