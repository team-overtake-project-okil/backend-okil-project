"""Request correlation and timings without logging credentials or payloads."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from uuid import uuid4

logger = logging.getLogger("uvicorn.error")
request_id: ContextVar[str] = ContextVar("timing_request_id", default="-")


@contextmanager
def timed_stage(stage: str):
    started = perf_counter()
    result = {"status": "ok"}
    try:
        yield result
    except BaseException as exc:
        result["status"] = type(exc).__name__
        raise
    finally:
        logger.info(
            "timing request_id=%s stage=%s duration_ms=%.3f status=%s http_status=%s",
            request_id.get(), stage, (perf_counter() - started) * 1000,
            result["status"], result.get("http_status", "-"),
        )


class RequestTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = uuid4().hex
        token = request_id.set(correlation_id)
        started = perf_counter()
        status = 500
        outcome = "ok"
        # Route templates avoid logging query strings or user-supplied path values.
        logger.info("request_start request_id=%s method=%s", correlation_id, scope["method"])

        async def timed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = dict(message)
                message["headers"] = [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", correlation_id.encode("ascii"))]
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            logger.info(
                "request_end request_id=%s method=%s route=%s status=%s duration_ms=%.3f outcome=%s",
                correlation_id, scope["method"], route, status,
                (perf_counter() - started) * 1000, outcome,
            )
            request_id.reset(token)
