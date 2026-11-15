import asyncio
from unittest.mock import AsyncMock, Mock


def test_existing_database_can_start_without_schema_writes(monkeypatch):
    monkeypatch.setenv("DB_URL", "postgresql+asyncpg://test:test@127.0.0.1/test")
    import main
    monkeypatch.setattr(main.settings, "DB_INIT_ON_STARTUP", False)
    engine = Mock()
    monkeypatch.setattr(main, "engine", engine)
    asyncio.run(main.on_startup())
    engine.begin.assert_not_called()


def test_chat_preload_is_opt_in(monkeypatch):
    import main
    from app import chat_service
    monkeypatch.setattr(main.settings, "CHAT_PRELOAD_AI", False)
    preload = AsyncMock()
    monkeypatch.setattr(chat_service, "preload_ai", preload)
    asyncio.run(main.warm_chat_ai())
    preload.assert_not_awaited()


def test_configured_chat_preload_finishes_before_startup(monkeypatch):
    import main
    from app import chat_service
    monkeypatch.setattr(main.settings, "CHAT_PRELOAD_AI", True)
    preload = AsyncMock()
    monkeypatch.setattr(chat_service, "preload_ai", preload)
    asyncio.run(main.warm_chat_ai())
    preload.assert_awaited_once()
