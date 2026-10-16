from fastapi import FastAPI

from app.database import check_db
from routers.points import router as points_router

app = FastAPI()
app.include_router(points_router)

@app.get("/")
def read_root():
    is_ok = check_db()
    if is_ok:
        return {"status": "Підключено до бази даних"}
    return {"status": "Помилка підключення"}