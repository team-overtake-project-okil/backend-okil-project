from fastapi import FastAPI
from sqlalchemy import text

from app.database import engine, Base
import app.models
from routers.points import router as points_router

app = FastAPI()
app.include_router(points_router)

@app.on_event("startup")
async def on_startup():
    print("Створення таблиць у базі даних (асинхронно)...")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS h3;"))

        await conn.run_sync(Base.metadata.create_all)
    print("Готово!")