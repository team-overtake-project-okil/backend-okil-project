import asyncio
import importlib

import pytest


@pytest.fixture
def app(monkeypatch):
    # Import the real application without connecting to a database or running startup.
    monkeypatch.setenv("DB_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:5173"]')
    return importlib.import_module("main").app


def request(app, method, origin, requested_method=None):
    async def run():
        headers = [(b"origin", origin.encode())]
        if requested_method:
            headers.append((b"access-control-request-method", requested_method.encode()))
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": "/openapi.json",
                "raw_path": b"/openapi.json",
                "query_string": b"",
                "root_path": "",
                "headers": headers,
                "server": ("localhost", 8000),
                "client": ("127.0.0.1", 12345),
            },
            receive,
            send,
        )
        response = next(m for m in messages if m["type"] == "http.response.start")
        return response["status"], dict(response["headers"])

    return asyncio.run(run())


def test_frontend_can_read_get_response(app):
    status, headers = request(app, "GET", "http://localhost:5173")
    assert status == 200
    assert headers[b"access-control-allow-origin"] == b"http://localhost:5173"
    assert b"access-control-allow-credentials" not in headers


def test_other_origin_has_no_browser_access(app):
    status, headers = request(app, "GET", "https://other.example")
    assert status == 200  # CORS controls browser access, not server authentication.
    assert b"access-control-allow-origin" not in headers


@pytest.mark.parametrize(
    "origin, method, expected",
    [
        ("http://localhost:5173", "GET", 200),
        ("https://other.example", "GET", 400),
        ("http://localhost:5173", "POST", 400),
    ],
)
def test_preflight(app, origin, method, expected):
    status, _ = request(app, "OPTIONS", origin, method)
    assert status == expected


def test_origins_can_be_configured_in_environment(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", '["https://frontend.example"]')
    from app.config import Settings

    settings = Settings(DB_URL="postgresql://test:test@localhost/test", _env_file=None)
    assert settings.CORS_ORIGINS == ["https://frontend.example"]
