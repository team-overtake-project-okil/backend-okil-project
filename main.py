from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.config import settings
from app.database import engine, Base
import app.models
from routers.points import router as points_router
from routers.routes import router as routes_router
from routers.stops import router as stops_router
from routers.point_types import router as point_types_router
from routers.hexagons import router as hexagons_router

app = FastAPI()
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

@app.on_event("startup")
async def on_startup():
    print("Створення таблиць у базі даних (асинхронно)...")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS h3;"))

        await conn.run_sync(Base.metadata.create_all)
    print("Готово!")
