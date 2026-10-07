from fastapi import FastAPI
from app.database import check_db

app = FastAPI()

@app.get("/")
def read_root():
    is_ok = check_db()
    if is_ok:
        return {"status": "Підключено до бази даних"}
    return {"status": "Помилка підключення"}