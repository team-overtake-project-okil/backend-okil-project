from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sqlalchemy import text
from app.chat_sessions import ChatSessionStore
from app.config import settings
from app.database import engine, Base
import app.models
from routers.chat import router as chat_router
from routers.points import router as points_router
from routers.routes import router as routes_router
from routers.stops import router as stops_router
from routers.point_types import router as point_types_router
from routers.hexagons import router as hexagons_router

app = FastAPI()
app.state.chat_sessions = ChatSessionStore(
    Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry=Retry(NoBackoff(), 0),
    ),
    settings.CHAT_SESSION_TTL_SECONDS,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=[],
)
app.include_router(points_router)
app.include_router(routes_router)
app.include_router(stops_router)
app.include_router(point_types_router)
app.include_router(hexagons_router)
app.include_router(chat_router)

@app.on_event("startup")
async def on_startup():
    if not settings.DB_INIT_ON_STARTUP:
        return
    print("Створення таблиць у базі даних (асинхронно)...")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS h3;"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))

        await conn.run_sync(Base.metadata.create_all)
        # create_all не додає колонки в уже існуючу таблицю.
        await conn.execute(text(
            "ALTER TABLE stops ADD COLUMN IF NOT EXISTS name VARCHAR(255);"
        ))
    print("Готово!")


@app.on_event("startup")
async def warm_chat_ai():
    if settings.CHAT_PRELOAD_AI:
        from app.chat_service import preload_ai
        await preload_ai()


@app.on_event("shutdown")
async def close_chat_store():
    await app.state.chat_sessions.close()
